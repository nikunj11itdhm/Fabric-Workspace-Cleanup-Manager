import csv
import io
import time
from uuid import UUID

import requests

API_ROOT = "https://api.fabric.microsoft.com/v1/"


class FabricError(Exception):
    def __init__(self, message, status=502, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def guid(value, label="ID"):
    try:
        parsed = UUID(str(value).strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a valid UUID.") from exc
    if parsed.int == 0:
        raise ValueError(f"{label} cannot be the empty UUID.")
    return str(parsed)


def parse_workspace_csv(content):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = reader.fieldnames or []
    candidates = [
        h
        for h in headers
        if h and h.strip().lower().replace("_", "").replace(" ", "") == "workspaceid"
    ]
    if len(candidates) != 1:
        raise ValueError("CSV needs exactly one workspace_id (or Workspace ID) column.")
    ids = []
    seen = set()
    try:
        for line, row in enumerate(reader, 2):
            if not any(row.values()):
                continue
            workspace_id = guid(
                row.get(candidates[0]), f"Workspace ID on CSV row {line}"
            )
            if workspace_id not in seen:
                ids.append(workspace_id)
                seen.add(workspace_id)
            if len(ids) > 500:
                raise ValueError("Import at most 500 unique workspaces at a time.")
    except csv.Error as exc:
        raise ValueError(f"Invalid CSV: {exc}") from exc
    if not ids:
        raise ValueError("CSV contains no workspace IDs.")
    return ids


def is_trial(capacity):
    sku = str(capacity.get("sku") or "").strip().upper()
    if not sku:
        raise FabricError(
            "Capacity SKU is missing; trial status cannot be determined.", 400
        )
    return sku == "FT1" or (sku.startswith("FTL") and sku[3:].isdigit())


class FabricClient:
    def __init__(
        self,
        token_provider,
        transport=None,
        sleep=time.sleep,
        clock=time.monotonic,
        assignment_timeout=90,
    ):
        self.token_provider = token_provider
        self.transport = transport or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.assignment_timeout = assignment_timeout

    def request(self, method, path, **kwargs):
        if not path or path.startswith("/") or "://" in path or ".." in path:
            raise ValueError("Invalid Fabric API path.")
        for attempt in range(3):
            try:
                response = self.transport.request(
                    method,
                    API_ROOT + path,
                    headers={"Authorization": f"Bearer {self.token_provider()}"},
                    timeout=(10, 40),
                    allow_redirects=False,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise FabricError(
                    "Fabric connection failed or timed out. "
                    "For a write request, its outcome may be unknown; refresh before retrying.",
                    uncertain=method != "GET",
                ) from exc
            if response.status_code == 429 and attempt < 2:
                delay = response.headers.get("Retry-After", "5")
                if delay.isdigit() and int(delay) <= 30:
                    self.sleep(max(1, int(delay)))
                    continue
            if not 200 <= response.status_code < 300:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                message = data.get("message", "The Fabric API rejected the request.")
                code = data.get("errorCode", f"HTTP {response.status_code}")
                request_id = data.get(
                    "requestId", response.headers.get("request-id", "")
                )
                suffix = f" (request {request_id})" if request_id else ""
                raise FabricError(
                    f"{code}: {message}{suffix}",
                    response.status_code,
                    uncertain=response.status_code >= 500
                    or response.status_code == 408,
                )
            return response
        raise FabricError("Fabric throttled this request; retry later.", 429)

    def json(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise FabricError("Fabric returned an invalid JSON response.") from exc
        if not isinstance(data, dict):
            raise FabricError("Fabric returned an unexpected response.")
        return data

    def pages(self, path, key="value", **params):
        result = []
        seen = set()
        while True:
            data = self.json("GET", path, params=params)
            if not isinstance(data.get(key), list):
                raise FabricError(f"Fabric response is missing its {key} list.")
            result.extend(data[key])
            token = data.get("continuationToken")
            if not token:
                return result
            if token in seen:
                raise FabricError("Fabric returned a repeated pagination token.")
            seen.add(token)
            params["continuationToken"] = token

    def capacities(self):
        return {guid(c["id"]): c for c in self.pages("capacities")}

    def workspace(self, workspace_id):
        return self.json("GET", f"workspaces/{guid(workspace_id)}")

    def capacity(self, capacity_id):
        return self.json("GET", f"capacities/{guid(capacity_id)}")

    def inventory(self, workspace_ids):
        workspaces = {
            guid(w["id"]): w for w in self.pages("admin/workspaces", "workspaces")
        }
        capacities = self.capacities()
        rows = []
        for workspace_id in workspace_ids:
            workspace = workspaces.get(workspace_id)
            if not workspace:
                rows.append(
                    {
                        "id": workspace_id,
                        "name": workspace_id,
                        "status": "Not found in tenant inventory",
                        "error": True,
                    }
                )
                continue
            row = {
                "id": workspace_id,
                "name": workspace.get("name", workspace_id),
                "capacityId": workspace.get("capacityId"),
                "state": workspace.get("state", "Unknown"),
            }
            capacity_id = row["capacityId"]
            if not capacity_id:
                row["status"] = "Not assigned"
            else:
                capacity = capacities.get(guid(capacity_id))
                if capacity is None or not capacity.get("sku"):
                    row["status"] = "Unknown capacity SKU - access required"
                else:
                    row.update(
                        {
                            "status": "Trial" if is_trial(capacity) else "Non-trial",
                            "capacityName": capacity.get("displayName"),
                            "sku": capacity.get("sku"),
                        }
                    )
            rows.append(row)
        return {"workspaces": rows, "capacities": list(capacities.values())}

    def items(self, workspace_id, admin=True):
        workspace_id = guid(workspace_id)
        if admin:
            rows = self.pages("admin/items", "itemEntities", workspaceId=workspace_id)
        else:
            rows = self.pages(f"workspaces/{workspace_id}/items", recursive="true")
        return [
            {
                "id": guid(item["id"]),
                "name": item.get("displayName", item.get("name", "")),
                "type": item.get("type", "Unknown"),
            }
            for item in rows
        ]

    def cleanup_items(self, workspace_id):
        core = self.items(workspace_id, admin=False)
        admin = self.items(workspace_id)
        # Admin inventory includes legacy artifacts not exposed by the Core listing.
        return list({item["id"]: item for item in [*admin, *core]}.values())

    def assign(self, workspace_id, primary, secondary=None):
        workspace_id = guid(workspace_id)
        targets = [guid(primary, "Primary capacity ID")]
        if secondary:
            second = guid(secondary, "Secondary capacity ID")
            if second == targets[0]:
                raise ValueError("Primary and secondary capacities must be different.")
            targets.append(second)
        workspace = self.workspace(workspace_id)
        if workspace.get("capacityAssignmentProgress") in {"InProgress", "Pending"}:
            return {
                "status": "pending",
                "message": "An assignment is already in progress.",
            }
        current = workspace.get("capacityId")
        # Unknown source SKU must never be treated as proof that it is not a trial.
        if current and is_trial(self.capacity(current)):
            return {
                "status": "skipped",
                "message": "Already assigned to a trial capacity.",
            }
        attempts = []
        previous_progress = workspace.get("capacityAssignmentProgress")
        for target in targets:
            submitted = False
            try:
                capacity = self.capacity(target)
                if not is_trial(capacity):
                    raise FabricError(
                        "Provided capacity is not a recognized trial SKU.", 400
                    )
                if capacity.get("state") != "Active":
                    raise FabricError("Provided trial capacity is not active.", 400)
                submitted = True
                self.request(
                    "POST",
                    f"workspaces/{workspace_id}/assignToCapacity",
                    json={"capacityId": target},
                )
            except FabricError as exc:
                attempts.append({"capacityId": target, "message": str(exc)})
                if submitted and exc.uncertain:
                    return {
                        "status": "unknown",
                        "attempts": attempts,
                        "message": "Assignment outcome is unknown. Refresh before retrying; "
                        "secondary was not submitted to avoid overlapping assignments.",
                    }
                continue
            deadline = self.clock() + self.assignment_timeout
            observed_running = False
            while self.clock() < deadline:
                try:
                    updated = self.workspace(workspace_id)
                except FabricError as exc:
                    return {
                        "status": "pending",
                        "attempts": attempts,
                        "message": f"Assignment accepted; verification failed: {exc}",
                    }
                progress = updated.get("capacityAssignmentProgress")
                if progress in {"InProgress", "Pending"}:
                    observed_running = True
                if progress == "Failed" and (
                    previous_progress != "Failed" or observed_running
                ):
                    attempts.append(
                        {"capacityId": target, "message": "Assignment failed."}
                    )
                    previous_progress = "Failed"
                    break
                if str(
                    updated.get("capacityId") or ""
                ).lower() == target and progress in {"Completed", None}:
                    return {
                        "status": "assigned",
                        "capacityId": target,
                        "attempts": attempts,
                        "message": "Trial capacity assignment verified.",
                    }
                self.sleep(3)
            else:
                return {
                    "status": "pending",
                    "attempts": attempts,
                    "message": "Assignment accepted but not yet verified. Refresh later; "
                    "secondary was not submitted while primary may still be running.",
                }
        return {
            "status": "failed",
            "attempts": attempts,
            "message": "No provided trial capacity could accept this workspace.",
        }

    def delete_snapshot(self, workspace_id, snapshot):
        workspace_id = guid(workspace_id)
        # Delete only the confirmed snapshot, never items created after confirmation.
        current = self.cleanup_items(workspace_id)
        current_ids = {item["id"] for item in current}
        pending = [item for item in snapshot if item["id"] in current_ids]
        results = [
            {**item, "status": "absent", "message": "Already absent from workspace."}
            for item in snapshot
            if item["id"] not in current_ids
        ]
        for pass_number in range(2):
            retry = []
            for item in pending:
                try:
                    self.request(
                        "DELETE",
                        f"workspaces/{workspace_id}/items/{guid(item['id'])}",
                    )
                    results.append(
                        {
                            **item,
                            "status": "deleted",
                            "message": "Delete request completed.",
                        }
                    )
                except FabricError as exc:
                    if exc.status == 404:
                        results.append(
                            {
                                **item,
                                "status": "absent",
                                "message": "Already absent (possibly parent-deleted).",
                            }
                        )
                    else:
                        if (
                            pass_number == 0
                            and not exc.uncertain
                            and exc.status in {400, 409}
                        ):
                            retry.append(item)
                        else:
                            results.append(
                                {**item, "status": "failed", "message": str(exc)}
                            )
            pending = retry
            if not pending:
                break
        try:
            remaining = self.cleanup_items(workspace_id)
        except FabricError as exc:
            return {
                "results": results,
                "remaining": None,
                "complete": False,
                "message": f"Could not verify workspace contents after deletion: {exc}",
            }
        return {
            "results": results,
            "remaining": remaining,
            "complete": not remaining,
            "message": (
                "Workspace is empty; workspace itself was retained."
                if not remaining
                else "Workspace is not verified empty. Remaining or unsupported items "
                "are listed; admin inventory may briefly lag behind deletes."
            ),
        }
