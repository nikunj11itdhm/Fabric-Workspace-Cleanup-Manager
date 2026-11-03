import copy
import io
import json
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

import restore_metadata as restore
from backup import metadata_backup
from fabric import FabricError

SOURCE = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ITEM = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
NEW = "ffffffff-ffff-4fff-8fff-ffffffffffff"
DEFINITION = {
    "parts": [
        {
            "path": "notebook.py",
            "payload": "cHJpbnQoMSk=",
            "payloadType": "InlineBase64",
        }
    ]
}


def exported_backup():
    api = Mock()
    api.workspace.return_value = {"id": SOURCE}
    api.pages.side_effect = [
        [{"id": ITEM, "name": "Demo", "type": "Notebook"}],
        [
            {
                "id": ITEM,
                "displayName": "Demo",
                "type": "Notebook",
                "description": "Original description",
            }
        ],
    ]
    api.item_definition.return_value = {"definition": DEFINITION}
    return metadata_backup(api, [SOURCE])[0]


def candidate(item_id=ITEM, name="Demo"):
    return {
        "sourceId": item_id,
        "name": name,
        "type": "Notebook",
        "status": "ready",
        "message": "",
        "payload": {
            "displayName": name,
            "type": "Notebook",
            "definition": copy.deepcopy(DEFINITION),
        },
    }


def source():
    return {"workspaceId": SOURCE, "status": "complete"}


def test_loader_consumes_actual_backup_format_without_remapping_payloads():
    original, items = restore.load_backup(exported_backup(), SOURCE)
    assert original["workspaceId"] == SOURCE
    assert items[0]["payload"]["definition"] == DEFINITION
    assert items[0]["payload"]["description"] == "Original description"


@pytest.mark.parametrize(
    "change",
    ["unsafe_path", "definition_path", "bad_base64", "wrong_source", "oversize"],
)
def test_invalid_backup_fails_preflight(change, monkeypatch):
    with ZipFile(exported_backup()) as source_zip:
        contents = {name: source_zip.read(name) for name in source_zip.namelist()}
    if change == "unsafe_path":
        contents["../outside.json"] = b"{}"
    elif change == "definition_path":
        manifest = json.loads(contents["manifest.json"])
        manifest["workspaces"][0]["items"][0]["definitionPath"] = "manifest.json"
        contents["manifest.json"] = json.dumps(manifest).encode()
    elif change == "bad_base64":
        path = f"workspaces/{SOURCE}/items/{ITEM}/definition.json"
        data = json.loads(contents[path])
        data["definition"]["parts"][0]["payload"] = "!invalid!"
        contents[path] = json.dumps(data).encode()
    elif change == "oversize":
        monkeypatch.setattr(restore, "MAX_BACKUP_BYTES", 1)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, value in contents.items():
            archive.writestr(name, value)
    buffer.seek(0)
    with pytest.raises(ValueError):
        restore.load_backup(buffer, TARGET if change == "wrong_source" else SOURCE)


def test_preview_only_reads_and_does_not_expose_definition_payload_in_report():
    api = Mock()
    api.items.return_value = []
    persist = Mock()
    report = restore.restore_items(api, source(), [candidate()], TARGET, False, persist)
    assert report["mode"] == report["status"] == "preview"
    assert report["items"][0]["status"] == "ready"
    assert "payload" not in report["items"][0]
    api.create_item.assert_not_called()
    api.items.assert_called_once_with(TARGET, admin=False)


def test_apply_skips_collisions_including_items_created_after_preview():
    api = Mock()
    api.items.side_effect = [[], [{"id": NEW, "name": "dEmO", "type": "Notebook"}]]
    report = restore.restore_items(api, source(), [candidate()], TARGET, True, Mock())
    assert report["items"][0]["status"] == "skipped_existing"
    assert report["status"] == "partial"
    api.create_item.assert_not_called()


def test_apply_creates_only_definitions_and_persists_intent_before_submission():
    api = Mock()
    api.items.return_value = []
    api.create_item.return_value = {"id": NEW}
    snapshots = []
    persist = lambda report: snapshots.append(copy.deepcopy(report))
    api.create_item.side_effect = lambda *args: (
        pytest.fail("Creation intent was not saved")
        if snapshots[-1]["items"][0]["status"] != "creating"
        else {"id": NEW}
    )
    report = restore.restore_items(api, source(), [candidate()], TARGET, True, persist)
    assert report["status"] == "completed"
    assert report["items"][0]["destinationId"] == NEW
    api.create_item.assert_called_once_with(TARGET, candidate()["payload"])
    assert all(
        "payload" not in item for snapshot in snapshots for item in snapshot["items"]
    )


def test_uncertain_write_halts_remaining_creates_without_replaying():
    api = Mock()
    api.items.return_value = []
    api.create_item.side_effect = FabricError("Connection interrupted", uncertain=True)
    report = restore.restore_items(
        api, source(), [candidate(), candidate(OTHER, "Second")], TARGET, True, Mock()
    )
    assert report["status"] == "partial"
    assert [item["status"] for item in report["items"]] == ["unknown", "not_attempted"]
    api.create_item.assert_called_once()


def test_apply_reports_unavailable_definitions_without_creating_empty_artifacts():
    api = Mock()
    api.items.return_value = []
    item = {**candidate(), "status": "unavailable"}
    item.pop("payload")
    report = restore.restore_items(api, source(), [item], TARGET, True, Mock())
    assert report["status"] == "partial"
    api.create_item.assert_not_called()


def test_script_requires_apply_confirmation_before_opening_browser(monkeypatch):
    auth = Mock()
    monkeypatch.setattr(restore, "BrowserSession", auth)
    with pytest.raises(SystemExit) as error:
        restore.main(
            [
                "--backup",
                "unused.zip",
                "--source-workspace",
                SOURCE,
                "--target-workspace",
                TARGET,
                "--tenant-id",
                TENANT,
                "--apply",
            ]
        )
    assert error.value.code == 2
    auth.assert_not_called()


def test_cli_default_preview_writes_report_and_never_creates_items(
    tmp_path, monkeypatch
):
    path = tmp_path / "backup.zip"
    path.write_bytes(exported_backup().getvalue())
    report_path = tmp_path / "report.json"
    auth = Mock()
    auth.poll_login.return_value = {"tenantId": TENANT}
    monkeypatch.setattr(restore, "BrowserSession", lambda: auth)
    api = Mock()
    api.items.return_value = []
    monkeypatch.setattr(restore, "FabricClient", lambda _: api)
    arguments = [
        "--backup",
        str(path),
        "--source-workspace",
        SOURCE,
        "--target-workspace",
        TARGET,
        "--tenant-id",
        TENANT,
        "--report",
        str(report_path),
    ]
    assert restore.main(arguments) == 0
    assert json.loads(report_path.read_text())["mode"] == "preview"
    assert not list(tmp_path.glob(".restore-*.tmp"))
    api.create_item.assert_not_called()
    auth.start_login.assert_called_once_with(TENANT)
    auth.close.assert_called_once()
    assert restore.main(arguments) == 1
    assert json.loads(report_path.read_text())["mode"] == "preview"
    auth.start_login.assert_called_once()


def test_cli_apply_requires_acknowledgment_and_saves_created_id(tmp_path, monkeypatch):
    path = tmp_path / "backup.zip"
    path.write_bytes(exported_backup().getvalue())
    report_path = tmp_path / "apply-report.json"
    auth = Mock()
    auth.poll_login.return_value = {"tenantId": TENANT}
    monkeypatch.setattr(restore, "BrowserSession", lambda: auth)
    api = Mock()
    api.items.return_value = []
    api.create_item.return_value = {"id": NEW}
    monkeypatch.setattr(restore, "FabricClient", lambda _: api)
    arguments = [
        "--backup",
        str(path),
        "--source-workspace",
        SOURCE,
        "--target-workspace",
        TARGET,
        "--tenant-id",
        TENANT,
        "--report",
        str(report_path),
        "--apply",
        "--confirm",
        TARGET,
        "--acknowledge-reference-risk",
    ]
    assert restore.main(arguments) == 0
    report = json.loads(report_path.read_text())
    assert report["mode"] == "apply"
    assert report["items"][0]["destinationId"] == NEW
    assert "payload" not in report["items"][0]
    api.create_item.assert_called_once()


def test_report_write_failure_prevents_creation():
    api = Mock()
    api.items.return_value = []
    with pytest.raises(OSError):
        restore.restore_items(
            api,
            source(),
            [candidate()],
            TARGET,
            True,
            Mock(side_effect=OSError("Report disk is unavailable")),
        )
    api.create_item.assert_not_called()
