import json
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

import backup
from fabric import FabricError

WORKSPACE = "11111111-1111-4111-8111-111111111111"
ITEM = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def api():
    client = Mock()
    client.workspace.return_value = {"id": WORKSPACE, "displayName": "Demo"}
    client.pages.side_effect = [
        [
            {
                "id": ITEM,
                "name": "../../outside",
                "type": "Notebook",
                "description": "Admin metadata",
            }
        ],
        [
            {
                "id": ITEM,
                "displayName": "Notebook",
                "type": "Notebook",
                "description": "Full description",
            }
        ],
    ]
    client.item_definition.return_value = {
        "definition": {
            "parts": [
                {
                    "path": "../../outside",
                    "payload": "Y29kZQ==",
                    "payloadType": "InlineBase64",
                }
            ]
        }
    }
    return client


def test_backup_preserves_metadata_and_definition_without_extracting_untrusted_paths():
    client = api()
    output, manifest = backup.metadata_backup(client, [WORKSPACE])
    assert manifest["status"] == "complete"
    item = manifest["workspaces"][0]["items"][0]
    assert item["definitionStatus"] == "exported"
    with ZipFile(output) as archive:
        assert len(archive.namelist()) == 5
        assert all(".." not in name for name in archive.namelist())
        assert json.loads(archive.read("manifest.json")) == manifest
        assert (
            json.loads(archive.read(item["metadataPath"]))["description"]
            == "Full description"
        )
        assert (
            json.loads(archive.read(item["definitionPath"]))
            == client.item_definition.return_value
        )
    client.item_definition.assert_called_once()
    assert client.pages.call_args_list[0].args == ("admin/items", "itemEntities")
    assert client.pages.call_args_list[1].kwargs == {"recursive": "true"}


def test_backup_retains_admin_only_items_and_reports_unsupported_and_failed_definitions():
    client = api()
    client.pages.side_effect = [
        [{"id": ITEM, "type": "Dashboard"}, {"id": OTHER, "type": "Notebook"}],
        [],
    ]
    client.item_definition.side_effect = [
        FabricError("Not supported", 400, error_code="OperationNotSupportedForItem"),
        FabricError("Permission denied", 403),
    ]
    output, manifest = backup.metadata_backup(client, [WORKSPACE])
    assert manifest["status"] == "partial"
    entry = manifest["workspaces"][0]
    assert [item["definitionStatus"] for item in entry["items"]] == [
        "unsupported",
        "failed",
    ]
    assert len(entry["errors"]) == 2
    with ZipFile(output) as archive:
        assert all(not name.endswith("definition.json") for name in archive.namelist())
        assert (
            len([name for name in archive.namelist() if name.endswith("metadata.json")])
            == 2
        )


def test_failed_inventory_source_is_not_silently_reported_as_empty():
    client = api()
    client.workspace.side_effect = FabricError("Workspace access denied", 403)
    client.pages.side_effect = [FabricError("Admin inventory denied", 403), []]
    output, manifest = backup.metadata_backup(client, [WORKSPACE])
    entry = manifest["workspaces"][0]
    assert manifest["status"] == entry["status"] == "partial"
    assert {error["stage"] for error in entry["errors"]} == {
        "workspaceMetadata",
        "adminInventory",
    }
    with ZipFile(output) as archive:
        assert json.loads(archive.read(entry["inventoryPath"])) == {"core": []}
    client.item_definition.assert_not_called()


def test_throttling_stops_remaining_definition_calls():
    client = api()
    client.pages.side_effect = [[{"id": ITEM}, {"id": OTHER}], []]
    client.item_definition.side_effect = FabricError("Throttled", 429)
    _, manifest = backup.metadata_backup(client, [WORKSPACE])
    client.item_definition.assert_called_once()
    assert len(manifest["workspaces"][0]["items"]) == 2
    assert "stopped" in manifest["workspaces"][0]["items"][1]["message"]


def test_expired_export_window_does_not_make_more_api_calls(monkeypatch):
    client = api()
    clock = iter([0, 601])
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(clock, 601))
    _, manifest = backup.metadata_backup(client, [WORKSPACE])
    assert manifest["status"] == "partial"
    client.workspace.assert_not_called()
    client.pages.assert_not_called()


def test_oversized_export_returns_error_instead_of_success_shaped_archive(monkeypatch):
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 1)
    with pytest.raises(FabricError, match="100 MiB limit"):
        backup.metadata_backup(api(), [WORKSPACE])
