import io
import time
from unittest.mock import Mock

import pytest

from app import create_app
from fabric import FabricError

WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ITEM = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def context():
    api = Mock()
    app = create_app({"TESTING": True, "FABRIC_CLIENT_FACTORY": lambda _: api})
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as browser:
        state = app.extensions["local_states"][browser["sid"]]
    state["account"] = {"home_account_id": "test"}
    state["user"] = "Test Administrator"
    state["workspaces"] = [WORKSPACE]
    return app, client, state, api


def headers(state):
    return {"X-CSRF-Token": state["csrf"]}


def test_unauthenticated_api_is_blocked(context):
    _, client, state, api = context
    state.pop("account")
    assert client.get("/api/inventory").status_code == 401
    api.inventory.assert_not_called()


def test_csrf_and_cross_origin_writes_are_blocked(context):
    _, client, state, _ = context
    assert client.post("/api/assign", json={}).status_code == 403
    result = client.post(
        "/api/assign",
        json={},
        headers={
            **headers(state),
            "Origin": "https://untrusted.invalid",
        },
    )
    assert result.status_code == 403


def test_unknown_host_and_nonlocal_clients_are_blocked(context):
    _, client, _, _ = context
    assert client.get("/", headers={"Host": "untrusted.invalid"}).status_code == 400
    assert (
        client.get("/", environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code
        == 403
    )


def test_ui_contains_user_capacity_fields_and_escapes_identity(context):
    _, client, state, _ = context
    state["user"] = '<script>alert("x")</script>'
    response = client.get("/")
    assert b'id="primary"' in response.data
    assert b'id="secondary"' in response.data
    assert b'<script>alert("x")</script>' not in response.data
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_csv_import_is_session_scoped_and_invalid_import_keeps_previous(context):
    _, client, state, _ = context
    valid = client.post(
        "/api/import",
        headers=headers(state),
        data={
            "file": (io.BytesIO(f"workspace_id\n{OTHER}\n".encode()), "workspaces.csv"),
        },
    )
    assert valid.status_code == 200
    assert state["workspaces"] == [OTHER]
    invalid = client.post(
        "/api/import",
        headers=headers(state),
        data={
            "file": (io.BytesIO(b"workspace_id\nbad\n"), "bad.csv"),
        },
    )
    assert invalid.status_code == 400
    assert state["workspaces"] == [OTHER]


def test_unrelated_localhost_session_cookie_does_not_replace_app_login(context):
    _, client, state, _ = context
    client.set_cookie("session", "another-localhost-app-cookie")
    response = client.post(
        "/api/import",
        headers=headers(state),
        data={
            "file": (io.BytesIO(f"workspace_id\n{OTHER}\n".encode()), "workspaces.csv")
        },
    )
    assert response.status_code == 200
    assert state["workspaces"] == [OTHER]


def test_session_token_refresh_allows_csv_import_without_reauth(context):
    _, client, state, _ = context
    old_token = state["csrf"]
    old_context = state["context"]
    state["csrf"] = "fresh-server-token"
    rejected = client.post(
        "/api/import",
        headers={"X-CSRF-Token": old_token},
        data={
            "file": (io.BytesIO(f"workspace_id\n{OTHER}\n".encode()), "workspaces.csv")
        },
    )
    assert rejected.status_code == 403
    assert rejected.json["code"] == "csrf_mismatch"
    assert state["workspaces"] == [WORKSPACE]
    current = client.get("/auth/session")
    assert current.json["authenticated"]
    assert current.json["contextId"] == old_context
    assert "no-store" in current.headers["Cache-Control"]
    assert "Access-Control-Allow-Origin" not in current.headers
    imported = client.post(
        "/api/import",
        headers={"X-CSRF-Token": current.json["csrfToken"]},
        data={
            "file": (io.BytesIO(f"workspace_id\n{OTHER}\n".encode()), "workspaces.csv")
        },
    )
    assert imported.status_code == 200
    assert imported.json["workspaceIds"] == [OTHER]
    assert "account" in state


def test_lost_server_session_changes_context_and_does_not_grant_upload_access(context):
    app, client, state, _ = context
    original_context = state["context"]
    app.extensions["local_states"].clear()
    current = client.get("/auth/session").json
    assert current["contextId"] != original_context
    assert not current["authenticated"]
    response = client.post(
        "/api/import",
        headers={"X-CSRF-Token": current["csrfToken"]},
        data={
            "file": (io.BytesIO(f"workspace_id\n{OTHER}\n".encode()), "workspaces.csv")
        },
    )
    assert response.status_code == 401


def test_logout_json_response_supports_fresh_token_ajax_submission(context):
    _, client, state, _ = context
    response = client.post(
        "/auth/logout",
        headers={**headers(state), "Accept": "application/json"},
    )
    assert response.json == {"status": "signed_out"}
    assert client.get("/auth/session").json["authenticated"] is False


def test_unimported_workspace_mutations_are_blocked(context):
    _, client, state, api = context
    response = client.post(
        f"/api/workspaces/{OTHER}/delete-preview", headers=headers(state)
    )
    assert response.status_code == 400
    api.workspace.assert_not_called()


def preview(context):
    _, client, state, api = context
    api.workspace.return_value = {"displayName": "Test workspace"}
    api.cleanup_items.return_value = [
        {"id": ITEM, "name": "Notebook", "type": "Notebook"}
    ]
    result = client.post(
        f"/api/workspaces/{WORKSPACE}/delete-preview", headers=headers(state)
    )
    assert result.status_code == 200
    return result.json


def test_delete_requires_exact_confirmation_and_single_use_nonce(context):
    _, client, state, api = context
    data = preview(context)
    api.delete_snapshot.return_value = {
        "complete": True,
        "remaining": [],
        "results": [],
    }
    bad = client.post(
        f"/api/workspaces/{WORKSPACE}/delete",
        headers=headers(state),
        json={"nonce": data["nonce"], "confirmation": "DELETE"},
    )
    assert bad.status_code == 400
    api.delete_snapshot.assert_not_called()
    valid = {"nonce": data["nonce"], "confirmation": data["confirmation"]}
    assert (
        client.post(
            f"/api/workspaces/{WORKSPACE}/delete", headers=headers(state), json=valid
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/workspaces/{WORKSPACE}/delete", headers=headers(state), json=valid
        ).status_code
        == 400
    )
    assert api.delete_snapshot.call_count == 1


def test_delete_preview_expiry_blocks_mutation(context):
    _, client, state, api = context
    data = preview(context)
    state["delete_preview"]["expires"] = time.monotonic() - 1
    response = client.post(
        f"/api/workspaces/{WORKSPACE}/delete",
        headers=headers(state),
        json={"nonce": data["nonce"], "confirmation": data["confirmation"]},
    )
    assert response.status_code == 400
    api.delete_snapshot.assert_not_called()


def test_fabric_inventory_permission_error_is_visible(context):
    _, client, _, api = context
    api.inventory.side_effect = FabricError("Fabric administrator role required", 403)
    response = client.get("/api/inventory")
    assert response.status_code == 403
    assert "administrator" in response.json["error"]


def test_overlapping_operations_are_rejected(context):
    _, client, state, api = context
    with state["lock"]:
        assert client.get("/api/inventory").status_code == 409
    api.inventory.assert_not_called()


def test_logout_clears_server_tokens_and_state(context):
    app, client, state, _ = context
    auth = Mock()
    state["auth"] = auth
    assert (
        client.post("/auth/logout", data={"csrf_token": state["csrf"]}).status_code
        == 302
    )
    assert not app.extensions["local_states"]
    auth.close.assert_called_once()
    assert client.get("/api/inventory").status_code == 401


def test_login_page_no_longer_requires_app_registration(context):
    _, client, state, _ = context
    state.pop("user")
    response = client.get("/")
    assert b'name="tenant_id"' in response.data
    assert b'name="client_id"' not in response.data
    assert b"Sign in with Microsoft in browser" in response.data


def test_sign_in_starts_browser_without_client_id_and_keeps_auth_out_of_cookie(context):
    app, client, state, _ = context
    auth = Mock()
    app.config["BROWSER_SESSION_FACTORY"] = Mock(return_value=auth)
    result = client.post(
        "/auth/login",
        data={
            "tenant_id": TENANT,
            "csrf_token": state["csrf"],
        },
    )
    assert result.status_code == 200
    assert result.json["status"] == "pending"
    auth.start_login.assert_called_once_with(TENANT)
    assert state["workspaces"] == []
    assert "account" not in state
    with client.session_transaction() as cookie:
        assert "auth" not in cookie
        assert "access_token" not in cookie
    assert state["auth"] is auth


def test_completed_browser_login_rotates_session_and_sets_identity(context):
    app, client, state, _ = context
    with client.session_transaction() as cookie:
        old_sid = cookie["sid"]
    auth = Mock()
    auth.poll_login.return_value = {"name": "Admin", "tenantId": TENANT}
    state["auth"] = auth
    state.pop("account")
    result = client.get("/auth/status")
    assert result.status_code == 200
    assert result.json["status"] == "authenticated"
    assert old_sid not in app.extensions["local_states"]
    with client.session_transaction() as cookie:
        assert cookie["sid"] != old_sid
    assert state["user"] == "Admin"


def test_pending_login_is_resumable_but_does_not_authorize_apis(context):
    _, client, state, _ = context
    auth = Mock()
    auth.poll_login.return_value = None
    state["auth"] = auth
    state.pop("user")
    state.pop("account")
    assert client.get("/auth/status").json["status"] == "pending"
    assert client.get("/api/inventory").status_code == 401
    assert b'data-pending="true"' in client.get("/").data


def test_failed_login_cleans_up_and_does_not_authorize(context):
    _, client, state, _ = context
    auth = Mock()
    auth.poll_login.side_effect = FabricError("Different tenant", 403)
    state["auth"] = auth
    state.pop("account")
    assert client.get("/auth/status").status_code == 403
    assert "account" not in state
    assert "auth" not in state
    auth.close.assert_called_once()


def test_failed_login_launch_closes_credential(context):
    app, client, state, _ = context
    auth = Mock()
    auth.start_login.side_effect = FabricError("Launch failed")
    app.config["BROWSER_SESSION_FACTORY"] = Mock(return_value=auth)
    response = client.post(
        "/auth/login",
        data={"tenant_id": TENANT, "csrf_token": state["csrf"]},
    )
    assert response.status_code == 502
    auth.close.assert_called_once()
    assert "auth" not in state


def test_session_expiration_closes_credential(context):
    _, client, state, _ = context
    auth = Mock()
    state["auth"] = auth
    state["expires"] = time.monotonic() - 1
    assert client.get("/api/inventory").status_code == 401
    auth.close.assert_called_once()


@pytest.mark.parametrize("signed_in", [False, True])
def test_duplicate_login_reuses_existing_session_without_reopening_browser(
    context, signed_in
):
    app, client, state, _ = context
    auth = Mock(tenant=TENANT)
    state["auth"] = auth
    if not signed_in:
        state.pop("account")
    factory = Mock()
    app.config["BROWSER_SESSION_FACTORY"] = factory
    for _ in range(2):
        response = client.post(
            "/auth/login",
            data={"tenant_id": TENANT, "csrf_token": state["csrf"]},
        )
        assert response.status_code == 200
        assert response.json["status"] == ("authenticated" if signed_in else "pending")
    factory.assert_not_called()
    auth.start_login.assert_not_called()
    auth.close.assert_not_called()
    assert state["workspaces"] == [WORKSPACE]


def test_changing_tenant_requires_explicit_signout(context):
    _, client, state, _ = context
    auth = Mock(tenant=TENANT)
    state["auth"] = auth
    response = client.post(
        "/auth/login",
        data={"tenant_id": OTHER, "csrf_token": state["csrf"]},
    )
    assert response.status_code == 409
    auth.close.assert_not_called()
