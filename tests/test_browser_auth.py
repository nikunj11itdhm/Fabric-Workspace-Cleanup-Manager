import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.identity import AuthenticationRequiredError, InteractiveBrowserCredential

from browser_auth import FABRIC_SCOPES, BrowserSession
from fabric import FabricError

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_TENANT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
def auth(monkeypatch):
    credential = Mock()
    credential.authenticate.return_value = SimpleNamespace(
        tenant_id=TENANT,
        username="Test administrator",
    )
    credential.get_token.return_value = SimpleNamespace(token="test-token")
    constructor = Mock(return_value=credential)
    monkeypatch.setattr("browser_auth.InteractiveBrowserCredential", constructor)
    session = BrowserSession()
    yield session, credential, constructor
    session.close()


def sign_in(session):
    session.start_login(TENANT)
    assert session._done.wait(2), "Authentication worker did not finish."
    return session.poll_login()


def test_browser_login_uses_memory_cache_and_no_automatic_reauthentication(auth):
    session, credential, constructor = auth
    assert sign_in(session) == {"name": "Test administrator", "tenantId": TENANT}
    constructor.assert_called_once_with(
        tenant_id=TENANT,
        disable_automatic_authentication=True,
        timeout=300,
    )
    credential.authenticate.assert_called_once_with(scopes=FABRIC_SCOPES)


def test_multiple_api_requests_reuse_the_same_credential_without_browser_prompts(auth):
    session, credential, constructor = auth
    sign_in(session)
    for _ in range(5):
        assert session.get_token() == "test-token"
    assert credential.get_token.call_count == 5
    credential.authenticate.assert_called_once()
    constructor.assert_called_once()


def test_silent_token_refresh_does_not_restart_signin(auth):
    session, credential, _ = auth
    sign_in(session)
    credential.get_token.side_effect = [
        SimpleNamespace(token="cached-token"),
        SimpleNamespace(token="refreshed-token"),
    ]
    assert session.get_token() == "cached-token"
    assert session.get_token() == "refreshed-token"
    credential.authenticate.assert_called_once()


def test_required_interaction_is_an_error_not_another_login_window(auth):
    session, credential, _ = auth
    sign_in(session)
    credential.get_token.side_effect = AuthenticationRequiredError(
        FABRIC_SCOPES,
        message="Interaction required",
    )
    with pytest.raises(FabricError, match="will not open sign-in windows") as error:
        session.get_token()
    assert error.value.status == 401
    credential.authenticate.assert_called_once()


def test_duplicate_login_only_opens_browser_once(auth):
    session, credential, constructor = auth
    sign_in(session)
    session.start_login(TENANT)
    credential.authenticate.assert_called_once()
    constructor.assert_called_once()


def test_incomplete_authentication_cannot_access_api_tokens(auth):
    session, credential, _ = auth
    with pytest.raises(FabricError, match="Sign in"):
        session.get_token()
    credential.get_token.assert_not_called()


def test_wrong_tenant_is_not_authorized(auth):
    session, credential, _ = auth
    credential.authenticate.return_value = SimpleNamespace(
        tenant_id=OTHER_TENANT,
        username="Test administrator",
    )
    with pytest.raises(FabricError, match="different tenant") as error:
        sign_in(session)
    assert error.value.status == 403
    with pytest.raises(FabricError):
        session.get_token()
    credential.get_token.assert_not_called()


def test_consent_error_is_explicit_and_does_not_retry_browser_auth(auth):
    session, credential, _ = auth
    credential.authenticate.side_effect = ClientAuthenticationError(
        message="AADSTS65001: Private authentication diagnostic",
    )
    with pytest.raises(FabricError) as error:
        sign_in(session)
    assert "AADSTS65001" in str(error.value)
    assert "Private authentication diagnostic" not in str(error.value)
    credential.authenticate.assert_called_once()


def test_network_failure_is_not_reported_as_signed_in(auth):
    session, credential, _ = auth
    credential.authenticate.side_effect = ServiceRequestError("Network unavailable")
    with pytest.raises(FabricError, match="Could not reach") as error:
        sign_in(session)
    assert error.value.status == 502


def test_signout_releases_credential_and_blocks_further_requests(auth):
    session, credential, _ = auth
    sign_in(session)
    session.close()
    session.close()
    credential.close.assert_called_once()
    assert session._credential is None
    with pytest.raises(FabricError):
        session.get_token()


def test_close_during_login_discards_result_and_closes_after_worker_finishes(auth):
    session, credential, _ = auth
    started = threading.Event()
    finish = threading.Event()

    def authenticate(**_kwargs):
        started.set()
        assert finish.wait(2)
        return SimpleNamespace(tenant_id=TENANT, username="Test administrator")

    credential.authenticate.side_effect = authenticate
    try:
        session.start_login(TENANT)
        assert started.wait(2)
        assert session.poll_login() is None
        session.close()
        credential.close.assert_not_called()
    finally:
        finish.set()
        session._worker.join(2)
    credential.close.assert_called_once()
    with pytest.raises(FabricError):
        session.poll_login()


def test_real_sdk_reports_required_interaction_without_launching_a_browser(monkeypatch):
    browser = Mock(side_effect=AssertionError("API requests must not open browsers"))
    monkeypatch.setattr("webbrowser.open", browser)
    with (
        InteractiveBrowserCredential(
            tenant_id=TENANT,
            disable_automatic_authentication=True,
        ) as credential,
        pytest.raises(AuthenticationRequiredError),
    ):
        credential.get_token(*FABRIC_SCOPES)
    browser.assert_not_called()
