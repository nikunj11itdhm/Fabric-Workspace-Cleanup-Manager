from unittest.mock import Mock

import pytest

from dashboard import scan_workspace, summarize
from fabric import FabricError

WORKSPACE = "11111111-1111-4111-8111-111111111111"
ITEM = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def workspace():
    return {
        "id": WORKSPACE,
        "name": "Demo",
        "state": "Active",
        "status": "Trial",
        "capacityId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "sku": "FT1",
    }


def api():
    client = Mock()
    client.workspace.return_value = {
        "id": WORKSPACE,
        "displayName": "Demo",
        "description": "Workspace description",
    }
    client.pages.side_effect = [
        [{"id": ITEM, "name": "Legacy name", "type": "Notebook"}],
        [
            {
                "id": ITEM,
                "displayName": "Notebook",
                "type": "Notebook",
                "description": "Useful notebook",
            }
        ],
    ]
    return client


def test_scan_merges_sources_counts_once_and_computes_evidence_based_score():
    client = api()
    row = scan_workspace(client, workspace())
    assert row["health"] == "healthy"
    assert len(row["items"]) == 1
    assert row["items"][0]["descriptionStatus"] == "present"
    summary = summarize({"workspaces": [row]})
    assert summary["healthScore"] == 100
    assert summary["itemCountComplete"]
    assert summary["artifactTypes"] == [{"type": "Notebook", "count": 1}]
    assert summary["cleanupReviewFindings"] == 0
    assert summary["status"] == "complete"
    assert row["status"] == "Trial"


def test_empty_requires_both_inventories_and_missing_descriptions_are_review_not_deletion():
    client = api()
    client.workspace.return_value = {"description": ""}
    client.pages.side_effect = [[], []]
    row = scan_workspace(client, workspace())
    assert row["health"] == "review"
    assert {finding["code"] for finding in row["findings"]} == {
        "emptyWorkspace",
        "missingWorkspaceDescription",
    }
    assert summarize({"workspaces": [row]})["cleanupReviewFindings"] == 2
    assert "permission to delete" in row["findings"][-1]["message"]


def test_partial_inventory_is_not_empty_and_retains_observed_legacy_items():
    client = api()
    client.pages.side_effect = [
        [{"id": ITEM, "name": "Dashboard", "type": "Dashboard"}],
        FabricError("Core access denied", 403),
    ]
    row = scan_workspace(client, workspace())
    assert row["health"] == "errors"
    assert len(row["items"]) == 1
    assert not row["inventoryComplete"]
    assert not any(finding["code"] == "emptyWorkspace" for finding in row["findings"])
    summary = summarize({"workspaces": [row]})
    assert summary["status"] == "partial"
    assert summary["itemCount"] == 1 and not summary["itemCountComplete"]


def test_absent_description_fields_are_unknown_not_missing_and_reduce_score_coverage():
    client = api()
    client.workspace.return_value = {"id": WORKSPACE}
    client.pages.side_effect = [[{"id": ITEM, "type": "Report"}], []]
    row = scan_workspace(client, workspace())
    summary = summarize({"workspaces": [row]})
    assert row["health"] == "unknown"
    assert row["items"][0]["descriptionStatus"] == "unknown"
    assert summary["knownChecks"] == 5
    assert summary["totalChecks"] == 7
    assert summary["healthScore"] == 71
    assert not row["findings"]


def test_description_setting_changes_rules_not_item_inventory():
    client = api()
    client.pages.side_effect = [[{"id": ITEM, "type": "Report", "description": ""}], []]
    row = scan_workspace(client, workspace(), include_item_descriptions=False)
    assert row["items"][0]["descriptionStatus"] == "missing"
    assert row["health"] == "healthy"
    assert not any(check["code"] == "itemDescription" for check in row["checks"])


def test_unknown_capacity_and_unassigned_capacity_are_distinct():
    client = api()
    row = scan_workspace(client, {**workspace(), "sku": None})
    assert row["health"] == "unknown"
    assert [finding["code"] for finding in row["findings"]] == ["unknownCapacity"]
    row = scan_workspace(api(), {**workspace(), "capacityId": None, "sku": None})
    assert row["health"] == "healthy"


@pytest.mark.parametrize("status", [401, 429])
def test_scan_stops_after_auth_or_throttle_failure(status):
    client = api()
    client.workspace.side_effect = FabricError("Access or throttle failure", status)
    row = scan_workspace(client, workspace())
    assert row["stopReason"]
    assert row["health"] == "errors"
    client.pages.assert_not_called()


def test_pending_workspaces_are_excluded_from_score_but_included_in_coverage():
    row = scan_workspace(api(), workspace())
    pending = {
        **workspace(),
        "scanStatus": "pending",
        "items": [],
        "checks": [],
        "findings": [],
        "errors": [],
    }
    summary = summarize({"workspaces": [row, pending]})
    assert summary["healthScore"] == 100
    assert summary["assessedWorkspaces"] == 1
    assert summary["workspaceCount"] == 2
    assert summary["health"] == {"healthy": 1, "review": 0, "errors": 0, "unknown": 1}
    assert summary["status"] == "running"
    assert not summary["itemCountComplete"]


def test_invalid_item_metadata_marks_source_as_error_instead_of_succeeding():
    client = api()
    client.pages.side_effect = [[{"id": "../not-a-uuid"}], []]
    row = scan_workspace(client, workspace())
    assert row["health"] == "errors"
    assert row["errors"][0]["stage"] == "adminInventory"
    assert not row["inventoryComplete"]
