import argparse
import base64
import binascii
import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from backup import MAX_BACKUP_BYTES
from browser_auth import BrowserSession
from fabric import FabricClient, FabricError, guid

logger = logging.getLogger(__name__)

WARNINGS = [
    "Definitions retain original workspace, item, connection, and data-source references.",
    "References are NOT remapped. Review and update dependencies manually before running restored workloads.",
    "No data, credentials, permissions, sensitivity labels, schedules, or folder hierarchy are restored.",
    "Only available definitions are submitted. Support depends on the item type and destination permissions/capacity.",
    "Never rerun an interrupted or unknown creation until destination inventory has been checked.",
]


def load_backup(path, source_workspace):
    source_workspace = guid(source_workspace, "Source workspace ID")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 100000 or len(set(names)) != len(names):
            raise ValueError(
                "Backup ZIP has too many entries or duplicate entry names."
            )
        if sum(entry.file_size for entry in entries) > MAX_BACKUP_BYTES:
            raise ValueError("Backup ZIP exceeds the 100 MiB uncompressed limit.")
        for name in names:
            if (
                "\\" in name
                or PurePosixPath(name).is_absolute()
                or ".." in PurePosixPath(name).parts
            ):
                raise ValueError("Backup ZIP contains unsafe entry paths.")
        if any(
            entry.flag_bits & 1 or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
            for entry in entries
        ):
            raise ValueError(
                "Encrypted or unsupported ZIP compression is not accepted."
            )

        def read_json(name):
            if name not in names:
                raise ValueError(f"Backup is missing {name}.")
            value = json.loads(archive.read(name))
            if not isinstance(value, dict):
                raise TypeError(f"Backup entry {name} must be a JSON object.")
            return value

        manifest = read_json("manifest.json")
        if manifest.get("schemaVersion") != 1 or not isinstance(
            manifest.get("workspaces"), list
        ):
            raise ValueError("Unsupported backup manifest schema.")
        matches = [
            row
            for row in manifest["workspaces"]
            if isinstance(row, dict) and row.get("workspaceId") == source_workspace
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("items"), list):
            raise ValueError(
                "Select exactly one source workspace listed in manifest.json."
            )
        source = matches[0]
        candidates = []
        seen = set()
        for item in source["items"]:
            if not isinstance(item, dict):
                raise TypeError("Invalid item entry in backup manifest.")
            item_id = guid(item.get("id"), "Source item ID")
            if item_id in seen:
                raise ValueError("Backup manifest contains duplicate source item IDs.")
            seen.add(item_id)
            entry = {
                "sourceId": item_id,
                "name": item.get("name", ""),
                "type": item.get("type", ""),
                "status": "unavailable",
                "message": "No exported definition is available; nothing will be created.",
            }
            candidates.append(entry)
            if item.get("definitionStatus") != "exported":
                continue
            prefix = f"workspaces/{source_workspace}/items/{item_id}"
            if (
                item.get("metadataPath") != f"{prefix}/metadata.json"
                or item.get("definitionPath") != f"{prefix}/definition.json"
            ):
                raise ValueError(
                    "Backup item paths do not match their source workspace/item IDs."
                )
            metadata = read_json(item["metadataPath"])
            exported = read_json(item["definitionPath"])
            if guid(metadata.get("id"), "Metadata item ID") != item_id:
                raise ValueError("Backup metadata item ID does not match the manifest.")
            name = metadata.get("displayName", metadata.get("name"))
            item_type = metadata.get("type")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(item_type, str)
                or not item_type.strip()
            ):
                raise ValueError(
                    "An exported item is missing its display name or type."
                )
            definition = exported.get("definition")
            parts = definition.get("parts") if isinstance(definition, dict) else None
            if not isinstance(parts, list) or not parts:
                raise ValueError("An exported definition has no parts.")
            for part in parts:
                if (
                    not isinstance(part, dict)
                    or not isinstance(part.get("path"), str)
                    or not part["path"]
                    or part.get("payloadType") != "InlineBase64"
                    or not isinstance(part.get("payload"), str)
                ):
                    raise ValueError(
                        "Only definitions with valid InlineBase64 parts can be restored."
                    )
                try:
                    base64.b64decode(part["payload"], validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ValueError(
                        "Backup contains an invalid base64 definition payload."
                    ) from exc
            payload = {"displayName": name, "type": item_type, "definition": definition}
            description = metadata.get("description")
            if description is not None:
                if not isinstance(description, str) or len(description) > 256:
                    raise ValueError(
                        "Item description must be a string of at most 256 characters."
                    )
                payload["description"] = description
            entry.update(
                name=name,
                type=item_type,
                status="ready",
                message="Create from preserved definition.",
                payload=payload,
            )
        return source, candidates


def item_key(item):
    return (item["name"].strip().casefold(), item["type"].strip().casefold())


def restore_items(api, source, candidates, target_workspace, apply, persist):
    target_workspace = guid(target_workspace, "Destination workspace ID")
    api.workspace(target_workspace)
    existing = {item_key(item) for item in api.items(target_workspace, admin=False)}
    report = {
        "mode": "apply" if apply else "preview",
        "status": "running" if apply else "preview",
        "sourceWorkspaceId": source["workspaceId"],
        "targetWorkspaceId": target_workspace,
        "sourceBackupStatus": source.get("status", "unknown"),
        "startedAtUtc": datetime.now(UTC).isoformat(),
        "warnings": WARNINGS,
        "items": [
            {key: value for key, value in item.items() if key != "payload"}
            for item in candidates
        ],
    }
    for item in report["items"]:
        if item["status"] == "ready" and item_key(item) in existing:
            item.update(
                status="skipped_existing",
                message="Destination already has this name/type; no overwrite.",
            )
    persist(report)
    if not apply:
        return report
    stopped = False
    for item, candidate in zip(report["items"], candidates, strict=True):
        if item["status"] != "ready":
            continue
        if stopped:
            item.update(
                status="not_attempted",
                message="Stopped after an uncertain or blocked request. Check the destination before retrying.",
            )
            continue
        try:
            existing.update(
                item_key(row) for row in api.items(target_workspace, admin=False)
            )
            if item_key(item) in existing:
                item.update(
                    status="skipped_existing",
                    message="Destination now has this name/type; no overwrite.",
                )
                persist(report)
                continue
        except FabricError as exc:
            item.update(
                status="failed",
                message=f"Could not recheck destination inventory: {exc}",
            )
            stopped = True
            persist(report)
            continue
        item.update(
            status="creating",
            message="Creation about to be submitted. If interrupted, check the destination before retrying.",
        )
        persist(report)
        try:
            created = api.create_item(target_workspace, candidate["payload"])
        except FabricError as exc:
            item.update(
                status="unknown" if exc.uncertain else "failed", message=str(exc)
            )
            stopped = exc.uncertain or exc.status in {401, 403, 429}
        else:
            item.update(
                status="created",
                destinationId=created["id"],
                message="Created; preserved references still require manual review.",
            )
            existing.add(item_key(item))
        persist(report)
    report["status"] = (
        "partial"
        if any(item["status"] != "created" for item in report["items"])
        or source.get("status") != "complete"
        else "completed"
    )
    report["finishedAtUtc"] = datetime.now(UTC).isoformat()
    persist(report)
    return report


def write_report(path, report):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".restore-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, indent=2, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview or create Fabric artifacts from a metadata ZIP. No overwrite or reference remapping."
    )
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--source-workspace", required=True)
    parser.add_argument("--target-workspace", required=True)
    parser.add_argument(
        "--tenant-id", required=True, help="Destination Entra tenant UUID"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="Preview only (the default)"
    )
    mode.add_argument(
        "--apply", action="store_true", help="Explicitly allow item creation"
    )
    parser.add_argument(
        "--confirm", help="Must exactly match destination workspace UUID when applying"
    )
    parser.add_argument("--acknowledge-reference-risk", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        help="New report file; an existing file is never overwritten",
    )
    args = parser.parse_args(argv)
    if args.apply and (
        args.confirm != args.target_workspace or not args.acknowledge_reference_risk
    ):
        parser.error(
            "--apply requires --confirm <target-workspace> and --acknowledge-reference-risk."
        )
    report_path = (
        args.report or Path("local-data") / f"restore-report-{uuid4().hex}.json"
    )
    auth = None
    transport = None
    report = {"status": "starting", "warnings": WARNINGS}
    reserved = False
    try:
        source_id = guid(args.source_workspace, "Source workspace ID")
        target_id = guid(args.target_workspace, "Destination workspace ID")
        tenant_id = guid(args.tenant_id, "Destination tenant ID")
        source, candidates = load_backup(args.backup, source_id)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle)
        reserved = True
        for warning in WARNINGS:
            print(f"WARNING: {warning}")
        print(
            "Opening Microsoft Entra sign-in for the destination tenant. Preview performs reads only."
        )
        auth = BrowserSession()
        auth.start_login(tenant_id)
        deadline = time.monotonic() + 330
        while auth.poll_login() is None:
            if time.monotonic() >= deadline:
                raise FabricError("Destination sign-in timed out.", 401)
            time.sleep(0.2)
        api = FabricClient(auth.get_token)
        transport = api.transport
        report = restore_items(
            api,
            source,
            candidates,
            target_id,
            args.apply,
            lambda value: write_report(report_path, value),
        )
        print(json.dumps(report, indent=2, ensure_ascii=True))
        print(f"Report saved to: {report_path.resolve()}")
        return 0 if report["status"] in {"preview", "completed"} else 1
    except (FabricError, ValueError, TypeError, BadZipFile, OSError) as exc:
        logger.error("Restore stopped: %s", exc)
        if reserved:
            # Keep any durable 'creating' entry if a write or report save was interrupted.
            logger.error(
                "Inspect the report and destination before retrying: %s", report_path
            )
        return 1
    except KeyboardInterrupt:
        logger.error(
            "Restore interrupted. Inspect the report and destination before retrying: %s",
            report_path,
        )
        return 130
    finally:
        if auth is not None:
            auth.close()
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    raise SystemExit(main())
