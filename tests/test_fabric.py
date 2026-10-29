from unittest.mock import Mock, call

import pytest
import requests

from fabric import API_ROOT, FabricClient, FabricError, parse_workspace_csv

WORKSPACE = "11111111-1111-4111-8111-111111111111"
PRIMARY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECONDARY = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SOURCE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ITEM = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OTHER_ITEM = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def response(status=200, data=None, headers=None):
    value = Mock(status_code=status, headers=headers or {})
    value.json.return_value = data
    return value


def assignment_client(workspaces, post_error=None):
    api = FabricClient(lambda: "token")
    api.workspace = Mock(side_effect=workspaces)
    api.capacity = Mock(return_value={"sku": "FT1", "state": "Active"})
    api.request = Mock()
    if post_error:
        api.request.side_effect = post_error
    return api


def test_csv_normalizes_bom_headers_and_duplicates():
    content = f"\ufeff Workspace ID ,other\n{PRIMARY.upper()},a\n {PRIMARY} ,b\n{WORKSPACE},c\n"
    assert parse_workspace_csv(content.encode()) == [PRIMARY, WORKSPACE]


@pytest.mark.parametrize(
    "content",
    [
        b"wrong\nvalue\n",
        b"workspace_id\nnot-a-guid\n",
        b"workspace_id\n",
        b"workspace_id\n00000000-0000-0000-0000-000000000000\n",
        b"workspace_id,Workspace ID\n",
        b"\xff",
    ],
)
def test_invalid_csv_is_rejected(content):
    with pytest.raises(ValueError):
        parse_workspace_csv(content)


def test_invalid_csv_row_does_not_partially_import():
    with pytest.raises(ValueError, match="row 3"):
        parse_workspace_csv(f"workspace_id\n{WORKSPACE}\nbad\n".encode())


def test_pagination_preserves_filters_and_ignores_returned_url():
    transport = Mock()
    transport.request.side_effect = [
        response(
            data={
                "itemEntities": [{"id": ITEM}],
                "continuationToken": "opaque+token",
                "continuationUri": "https://untrusted.invalid",
            }
        ),
        response(data={"itemEntities": [{"id": OTHER_ITEM}]}),
    ]
    api = FabricClient(lambda: "token", transport=transport)
    result = api.pages("admin/items", "itemEntities", workspaceId=WORKSPACE)
    assert len(result) == 2
    last = transport.request.call_args
    assert last.args[1] == API_ROOT + "admin/items"
    assert last.kwargs["params"] == {
        "workspaceId": WORKSPACE,
        "continuationToken": "opaque+token",
    }
    assert last.kwargs["allow_redirects"] is False


def test_pagination_fails_instead_of_silently_truncating():
    api = FabricClient(lambda: "token")
    api.json = Mock(return_value={"value": [], "continuationToken": "same"})
    with pytest.raises(FabricError, match="repeated"):
        api.pages("capacities")


def test_throttle_obeys_retry_after():
    transport = Mock()
    transport.request.side_effect = [
        response(429, {"errorCode": "Throttled"}, {"Retry-After": "2"}),
        response(data={"value": []}),
    ]
    sleep = Mock()
    api = FabricClient(lambda: "token", transport=transport, sleep=sleep)
    assert api.pages("capacities") == []
    sleep.assert_called_once_with(2)


def test_existing_trial_is_skipped_without_a_write():
    api = assignment_client([{"capacityId": SOURCE}])
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "skipped"
    api.request.assert_not_called()
    api.capacity.assert_called_once_with(SOURCE)


def test_paid_workspace_is_assigned_and_completion_is_verified():
    api = assignment_client(
        [
            {"capacityId": SOURCE, "capacityAssignmentProgress": "Completed"},
            {"capacityId": PRIMARY, "capacityAssignmentProgress": "Completed"},
        ]
    )
    api.capacity.side_effect = [{"sku": "F64"}, {"sku": "FT1", "state": "Active"}]
    assert api.assign(WORKSPACE, PRIMARY)["status"] == "assigned"
    api.request.assert_called_once_with(
        "POST",
        f"workspaces/{WORKSPACE}/assignToCapacity",
        json={"capacityId": PRIMARY},
    )


def test_unknown_source_capacity_blocks_all_writes():
    api = assignment_client([{"capacityId": SOURCE}])
    api.capacity.side_effect = FabricError("Forbidden", 403)
    with pytest.raises(FabricError, match="Forbidden"):
        api.assign(WORKSPACE, PRIMARY, SECONDARY)
    api.request.assert_not_called()


def test_missing_source_sku_blocks_all_writes():
    api = assignment_client([{"capacityId": SOURCE}])
    api.capacity.return_value = {"state": "Active"}
    with pytest.raises(FabricError, match="SKU is missing"):
        api.assign(WORKSPACE, PRIMARY, SECONDARY)
    api.request.assert_not_called()


def test_definite_primary_failure_uses_secondary():
    api = assignment_client(
        [
            {},
            {"capacityId": SECONDARY, "capacityAssignmentProgress": "Completed"},
        ],
        post_error=[FabricError("Primary full", 400), Mock()],
    )
    result = api.assign(WORKSPACE, PRIMARY, SECONDARY)
    assert result["status"] == "assigned"
    assert result["capacityId"] == SECONDARY
    assert [
        entry.kwargs["json"]["capacityId"] for entry in api.request.call_args_list
    ] == [
        PRIMARY,
        SECONDARY,
    ]
    assert result["attempts"][0]["message"] == "Primary full"


def test_primary_metadata_failure_can_safely_use_secondary():
    api = assignment_client(
        [{}, {"capacityId": SECONDARY, "capacityAssignmentProgress": "Completed"}]
    )
    api.capacity.side_effect = [
        FabricError("Metadata unavailable", 503, uncertain=True),
        {"sku": "FT1", "state": "Active"},
    ]
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "assigned"
    api.request.assert_called_once_with(
        "POST",
        f"workspaces/{WORKSPACE}/assignToCapacity",
        json={"capacityId": SECONDARY},
    )


def test_failed_async_assignment_uses_secondary_after_observed_transition():
    api = assignment_client(
        [
            {"capacityAssignmentProgress": "Completed"},
            {"capacityAssignmentProgress": "Failed"},
            {"capacityAssignmentProgress": "InProgress", "capacityId": SECONDARY},
            {"capacityAssignmentProgress": "Completed", "capacityId": SECONDARY},
        ]
    )
    api.sleep = Mock()
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["capacityId"] == SECONDARY
    assert api.request.call_count == 2


def test_stale_failed_state_does_not_trigger_overlapping_fallback():
    api = assignment_client(
        [
            {"capacityAssignmentProgress": "Failed"},
            {"capacityAssignmentProgress": "Failed"},
        ]
    )
    api.clock = Mock(side_effect=[0, 0, 100])
    api.sleep = Mock()
    result = api.assign(WORKSPACE, PRIMARY, SECONDARY)
    assert result["status"] == "pending"
    assert api.request.call_count == 1


@pytest.mark.parametrize(
    "error",
    [
        FabricError("Connection timed out", uncertain=True),
        FabricError("Server error", 503, uncertain=True),
    ],
)
def test_uncertain_primary_write_never_submits_secondary(error):
    api = assignment_client([{}], post_error=error)
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "unknown"
    assert api.request.call_count == 1


def test_accepted_but_pending_assignment_does_not_submit_secondary():
    api = assignment_client(
        [{}, {"capacityId": None, "capacityAssignmentProgress": "InProgress"}]
    )
    api.clock = Mock(side_effect=[0, 0, 100])
    api.sleep = Mock()
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "pending"
    assert api.request.call_count == 1


def test_inactive_primary_falls_back_without_posting_to_primary():
    api = assignment_client(
        [{}, {"capacityId": SECONDARY, "capacityAssignmentProgress": "Completed"}]
    )
    api.capacity.side_effect = [
        {"sku": "FT1", "state": "Inactive"},
        {"sku": "FTL64", "state": "Active"},
    ]
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "assigned"
    assert api.request.call_args.kwargs["json"]["capacityId"] == SECONDARY
    assert api.request.call_count == 1


def test_non_trial_targets_are_never_written():
    api = assignment_client([{}])
    api.capacity.return_value = {"sku": "F64", "state": "Active"}
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "failed"
    api.request.assert_not_called()


def test_unknown_progress_is_not_reported_as_completed():
    api = assignment_client(
        [{}, {"capacityId": PRIMARY, "capacityAssignmentProgress": "NewState"}]
    )
    api.clock = Mock(side_effect=[0, 0, 100])
    api.sleep = Mock()
    assert api.assign(WORKSPACE, PRIMARY, SECONDARY)["status"] == "pending"
    assert api.request.call_count == 1


def test_transport_timeout_is_not_blindly_retried():
    transport = Mock()
    transport.request.side_effect = requests.Timeout()
    api = FabricClient(lambda: "token", transport=transport)
    with pytest.raises(FabricError) as error:
        api.request("POST", "workspaces")
    assert error.value.uncertain
    assert transport.request.call_count == 1


def test_cleanup_inventory_includes_admin_only_artifacts():
    api = FabricClient(lambda: "token")
    core = {"id": ITEM, "name": "Core", "type": "Notebook"}
    legacy = {"id": OTHER_ITEM, "name": "Legacy", "type": "Dashboard"}
    api.items = Mock(side_effect=[[core], [legacy, core]])
    assert {item["id"] for item in api.cleanup_items(WORKSPACE)} == {ITEM, OTHER_ITEM}


def test_cleanup_deletes_snapshot_only_and_reports_new_items():
    api = FabricClient(lambda: "token")
    item = {"id": ITEM, "name": "Previewed", "type": "Notebook"}
    other = {"id": OTHER_ITEM, "name": "Created after preview", "type": "Notebook"}
    api.cleanup_items = Mock(side_effect=[[item, other], [other]])
    api.request = Mock()
    result = api.delete_snapshot(WORKSPACE, [item])
    api.request.assert_called_once_with(
        "DELETE", f"workspaces/{WORKSPACE}/items/{ITEM}"
    )
    assert result["remaining"] == [other]
    assert not result["complete"]


def test_cleanup_reports_permission_failure_and_retains_workspace():
    api = FabricClient(lambda: "token")
    item = {"id": ITEM, "name": "Blocked", "type": "Lakehouse"}
    api.cleanup_items = Mock(return_value=[item])
    api.request = Mock(side_effect=FabricError("Not allowed", 403))
    result = api.delete_snapshot(WORKSPACE, [item])
    assert result["results"][0]["status"] == "failed"
    assert not result["complete"]
    assert api.request.call_count == 1
    assert all(
        f"workspaces/{WORKSPACE}/items/" in entry.args[1]
        for entry in api.request.call_args_list
    )


def test_cleanup_retries_dependency_conflicts_after_other_items():
    api = FabricClient(lambda: "token")
    child = {"id": ITEM, "name": "Child", "type": "SQLEndpoint"}
    parent = {"id": OTHER_ITEM, "name": "Parent", "type": "Lakehouse"}
    api.cleanup_items = Mock(side_effect=[[child, parent], []])
    api.request = Mock(
        side_effect=[
            FabricError("Parent dependency", 409),
            Mock(),
            FabricError("Gone", 404),
        ]
    )
    result = api.delete_snapshot(WORKSPACE, [child, parent])
    assert result["complete"]
    assert {row["status"] for row in result["results"]} == {"deleted", "absent"}
    assert api.request.call_args_list == [
        call("DELETE", f"workspaces/{WORKSPACE}/items/{ITEM}"),
        call("DELETE", f"workspaces/{WORKSPACE}/items/{OTHER_ITEM}"),
        call("DELETE", f"workspaces/{WORKSPACE}/items/{ITEM}"),
    ]


def test_failed_final_inventory_does_not_claim_empty():
    api = FabricClient(lambda: "token")
    item = {"id": ITEM, "name": "Notebook", "type": "Notebook"}
    api.cleanup_items = Mock(
        side_effect=[[item], FabricError("Verification failed", 403)]
    )
    api.request = Mock()
    result = api.delete_snapshot(WORKSPACE, [item])
    assert result["remaining"] is None
    assert not result["complete"]
