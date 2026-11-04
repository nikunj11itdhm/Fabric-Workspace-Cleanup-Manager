import logging
from collections import Counter
from datetime import UTC, datetime

from fabric import FabricError, guid

logger = logging.getLogger(__name__)


def utc_now():
    return datetime.now(UTC).isoformat()


def scan_workspace(api, workspace, include_item_descriptions=True):
    workspace_id = workspace["id"]
    result = {
        "id": workspace_id,
        "name": workspace.get("name", workspace_id),
        "state": workspace.get("state", "Unknown"),
        "status": workspace.get("status", "Unknown"),
        "capacityId": workspace.get("capacityId"),
        "sku": workspace.get("sku"),
        "scanStatus": "scanned",
        "items": [],
        "checks": [],
        "findings": [],
        "errors": [],
    }

    def check(code, label, passed, item_id=None):
        result["checks"].append(
            {"code": code, "label": label, "passed": passed, "itemId": item_id}
        )

    def finding(code, message, item_id=None):
        result["findings"].append({"code": code, "message": message, "itemId": item_id})

    def failure(stage, exc):
        result["errors"].append({"stage": stage, "message": str(exc)})
        if exc.status in {401, 429}:
            result["stopReason"] = str(exc)
        logger.warning("Dashboard scan %s for %s failed: %s", stage, workspace_id, exc)

    if workspace.get("error"):
        failure(
            "tenantInventory",
            FabricError(workspace.get("status", "Workspace unavailable"), 403),
        )
    metadata = None
    try:
        metadata = api.workspace(workspace_id)
    except FabricError as exc:
        failure("workspaceMetadata", exc)
    check("metadataAccess", "Workspace metadata accessible", metadata is not None)
    if metadata is not None:
        result["name"] = metadata.get("displayName", result["name"])
        description = metadata.get("description")
        known = "description" in metadata
        described = bool(str(description or "").strip()) if known else None
        check("workspaceDescription", "Workspace description present", described)
        if described is False:
            finding("missingWorkspaceDescription", "Add a workspace description.")

    capacity_known = (
        None
        if workspace.get("error")
        or (workspace.get("capacityId") and not workspace.get("sku"))
        else True
    )
    check(
        "capacityMetadata",
        "Assigned capacity SKU known (unassigned is not unknown)",
        capacity_known,
    )
    if capacity_known is not True:
        finding(
            "unknownCapacity",
            "Verify capacity metadata and access; no capacity change is recommended automatically.",
        )
    sources = {}
    for source, path, key, params in [
        ("admin", "admin/items", "itemEntities", {"workspaceId": workspace_id}),
        ("core", f"workspaces/{workspace_id}/items", "value", {"recursive": "true"}),
    ]:
        if result.get("stopReason"):
            check(f"{source}Inventory", f"{source.title()} inventory accessible", None)
            continue
        try:
            rows = api.pages(path, key, **params)
            for item in rows:
                if not isinstance(item, dict):
                    raise FabricError("Invalid item metadata in inventory.")
                guid(item.get("id"), "Item ID")
        except (FabricError, ValueError) as exc:
            failure(
                f"{source}Inventory",
                exc if isinstance(exc, FabricError) else FabricError(str(exc)),
            )
            check(f"{source}Inventory", f"{source.title()} inventory accessible", False)
        else:
            sources[source] = rows
            check(f"{source}Inventory", f"{source.title()} inventory accessible", True)
    merged = {}
    for rows in sources.values():
        for item in rows:
            item_id = guid(item["id"])
            merged[item_id] = {**merged.get(item_id, {}), **item, "id": item_id}
    complete_inventory = len(sources) == 2
    result["inventoryComplete"] = complete_inventory
    check(
        "nonemptyWorkspace",
        "Workspace contains artifacts",
        bool(merged) if complete_inventory else None,
    )
    if complete_inventory and not merged:
        finding(
            "emptyWorkspace",
            "Review this empty workspace; emptiness alone is not permission to delete it.",
        )
    for item in merged.values():
        described = (
            bool(str(item.get("description") or "").strip())
            if "description" in item
            else None
        )
        result["items"].append(
            {
                "id": item["id"],
                "name": item.get("displayName", item.get("name", "")),
                "type": item.get("type", "Unknown"),
                "descriptionStatus": "unknown"
                if described is None
                else "present"
                if described
                else "missing",
            }
        )
        if include_item_descriptions:
            check(
                "itemDescription", "Artifact description present", described, item["id"]
            )
            if described is False:
                finding(
                    "missingItemDescription",
                    "Add an artifact description; no usage assessment was performed.",
                    item["id"],
                )
    result["health"] = (
        "errors"
        if result["errors"]
        else "unknown"
        if any(check["passed"] is None for check in result["checks"])
        else "review"
        if any(check["passed"] is False for check in result["checks"])
        else "healthy"
    )
    return result


def summarize(scan):
    rows = scan["workspaces"]
    assessed = [row for row in rows if row["scanStatus"] != "pending"]
    health = Counter(row.get("health", "unknown") for row in rows)
    checks = [check for row in assessed for check in row["checks"]]
    passed = sum(check["passed"] is True for check in checks)
    known = sum(check["passed"] is not None for check in checks)
    findings = Counter(
        finding["code"] for row in assessed for finding in row["findings"]
    )
    types = Counter(item["type"] for row in assessed for item in row["items"])
    complete = len(assessed) == len(rows) and not any(row["errors"] for row in assessed)
    return {
        "workspaceCount": len(rows),
        "activeWorkspaces": sum(row.get("state") == "Active" for row in rows),
        "assessedWorkspaces": len(assessed),
        "itemCount": sum(types.values()),
        "itemCountComplete": complete
        and all(row.get("inventoryComplete") for row in assessed),
        "cleanupReviewFindings": findings["emptyWorkspace"]
        + findings["missingWorkspaceDescription"]
        + findings["missingItemDescription"],
        "healthScore": round(100 * passed / len(checks)) if checks else None,
        "passedChecks": passed,
        "totalChecks": len(checks),
        "knownChecks": known,
        "health": {
            key: health[key] for key in ("healthy", "review", "errors", "unknown")
        },
        "artifactTypes": [
            {"type": kind, "count": count} for kind, count in sorted(types.items())
        ],
        "findings": dict(findings),
        "status": "complete"
        if complete
        else "partial"
        if scan.get("stopReason") or len(assessed) == len(rows)
        else "running",
    }
