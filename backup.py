import io
import json
import logging
import time
from datetime import UTC, datetime
from zipfile import ZIP_DEFLATED, ZipFile

from fabric import FabricError, guid

logger = logging.getLogger(__name__)
MAX_BACKUP_BYTES = 100 * 1024 * 1024


def metadata_backup(api, workspace_ids):
    output = io.BytesIO()
    manifest = {
        "schemaVersion": 1,
        "startedAtUtc": datetime.now(UTC).isoformat(),
        "status": "complete",
        "scope": "Workspace and artifact metadata plus available Fabric definitions",
        "limitations": [
            "Not a data backup or a guaranteed restorable workspace snapshot.",
            "No data, permissions, credentials, or sensitivity-label management APIs are called.",
            "Definitions may contain sensitive code, connection references, or hardcoded secrets. Protect this ZIP.",
            "Definitions are saved as returned by Fabric, including base64 parts. The separate restore script is create-only and does not remap references.",
            "Inventories are collected sequentially and may change during export.",
        ],
        "workspaces": [],
    }
    size = 0
    deadline = time.monotonic() + 600
    stop_reason = None

    def check_deadline():
        if time.monotonic() >= deadline:
            raise FabricError(
                "Backup export time limit reached. Retry fewer workspaces."
            )

    def problem(entry, stage, exc):
        manifest["status"] = "partial"
        entry["status"] = "partial"
        entry["errors"].append({"stage": stage, "message": str(exc)})
        logger.warning("Metadata backup %s failed: %s", stage, exc)

    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:

        def save(path, data):
            nonlocal size
            content = json.dumps(data, ensure_ascii=True, indent=2).encode("utf-8")
            size += len(content)
            if size > MAX_BACKUP_BYTES:
                raise FabricError(
                    "Metadata backup exceeds the 100 MiB limit. Select fewer workspaces; no ZIP was downloaded."
                )
            archive.writestr(path, content)

        for workspace_id in workspace_ids:
            workspace_id = guid(workspace_id)
            prefix = f"workspaces/{workspace_id}"
            entry = {
                "workspaceId": workspace_id,
                "status": "complete",
                "errors": [],
                "items": [],
            }
            manifest["workspaces"].append(entry)
            try:
                check_deadline()
                workspace = api.workspace(workspace_id)
            except FabricError as exc:
                problem(entry, "workspaceMetadata", exc)
            else:
                save(f"{prefix}/workspace.json", workspace)
                entry["metadataPath"] = f"{prefix}/workspace.json"

            inventory = {}
            for source, path, key, params in [
                ("admin", "admin/items", "itemEntities", {"workspaceId": workspace_id}),
                (
                    "core",
                    f"workspaces/{workspace_id}/items",
                    "value",
                    {"recursive": "true"},
                ),
            ]:
                try:
                    check_deadline()
                    rows = api.pages(path, key, **params)
                    for item in rows:
                        if not isinstance(item, dict):
                            raise FabricError(
                                "Inventory returned invalid item metadata."
                            )
                        guid(item.get("id"), "Item ID")
                except (FabricError, ValueError) as exc:
                    problem(entry, f"{source}Inventory", exc)
                else:
                    inventory[source] = rows
            save(f"{prefix}/inventory.json", inventory)
            entry["inventoryPath"] = f"{prefix}/inventory.json"
            merged = {}
            for rows in inventory.values():
                for item in rows:
                    item_id = guid(item["id"])
                    merged[item_id] = {**merged.get(item_id, {}), **item, "id": item_id}
            for item_id, item in merged.items():
                item_prefix = f"{prefix}/items/{item_id}"
                save(f"{item_prefix}/metadata.json", item)
                exported = {
                    "id": item_id,
                    "name": item.get("displayName", item.get("name", "")),
                    "type": item.get("type", "Unknown"),
                    "metadataPath": f"{item_prefix}/metadata.json",
                }
                entry["items"].append(exported)
                try:
                    if stop_reason:
                        raise FabricError(f"Definition exports stopped: {stop_reason}")
                    check_deadline()
                    definition = api.item_definition(
                        workspace_id,
                        item_id,
                        timeout=min(90, max(1, deadline - time.monotonic())),
                    )
                except FabricError as exc:
                    exported["definitionStatus"] = (
                        "unsupported"
                        if exc.error_code == "OperationNotSupportedForItem"
                        else "failed"
                    )
                    exported["message"] = str(exc)
                    problem(entry, f"definition:{item_id}", exc)
                    if exc.status in {401, 429}:
                        stop_reason = str(exc)
                else:
                    save(f"{item_prefix}/definition.json", definition)
                    exported["definitionStatus"] = "exported"
                    exported["definitionPath"] = f"{item_prefix}/definition.json"
        manifest["finishedAtUtc"] = datetime.now(UTC).isoformat()
        save("manifest.json", manifest)
    output.seek(0)
    return output, manifest
