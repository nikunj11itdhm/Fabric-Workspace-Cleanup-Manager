import re
import threading

from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.identity import AuthenticationRequiredError, InteractiveBrowserCredential

from fabric import FabricError, guid

FABRIC_SCOPES = ["https://api.fabric.microsoft.com/.default"]


def authentication_error(exc):
    match = re.search(r"\bAADSTS\d+\b", str(exc))
    code = f" ({match.group()})" if match else ""
    if isinstance(exc, ServiceRequestError):
        return FabricError(
            "Could not reach Microsoft Entra ID. Check connectivity.", 502
        )
    return FabricError(
        f"Microsoft Entra sign-in failed{code}. Check the tenant ID, administrator "
        "consent for the Microsoft SDK development application, and organization "
        "sign-in policies. No additional sign-in window will be opened automatically.",
        401,
    )


class BrowserSession:
    def __init__(self):
        self.tenant = None
        self._credential = None
        self._worker = None
        self._done = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._credential_closed = False
        self._identity = None
        self._error = None

    def start_login(self, tenant):
        tenant = guid(tenant, "Tenant ID")
        if self._closed:
            raise FabricError("This sign-in session has ended. Sign in again.", 401)
        if self._worker is not None:
            if tenant != self.tenant:
                raise FabricError("Sign out before changing tenants.", 409)
            return
        self.tenant = tenant
        self._credential = InteractiveBrowserCredential(
            tenant_id=tenant,
            disable_automatic_authentication=True,
            timeout=300,
        )
        self._worker = threading.Thread(
            target=self._authenticate,
            name="entra-browser-login",
            daemon=True,
        )
        self._worker.start()

    def _authenticate(self):
        try:
            record = self._credential.authenticate(scopes=FABRIC_SCOPES)
            if guid(record.tenant_id, "Signed-in tenant ID") != self.tenant:
                raise FabricError(
                    "The signed-in account is in a different tenant.", 403
                )
            if not record.username:
                raise FabricError("Microsoft did not return a signed-in user.", 401)
            self._identity = {"name": record.username, "tenantId": self.tenant}
        except (ClientAuthenticationError, ServiceRequestError) as exc:
            self._error = authentication_error(exc)
        except (FabricError, ValueError) as exc:
            self._error = exc
        finally:
            self._done.set()
            if self._closed:
                self._close_credential()

    def poll_login(self):
        if self._closed:
            raise FabricError("This sign-in session has ended. Sign in again.", 401)
        if self._worker is None:
            raise FabricError("No sign-in is in progress.", 400)
        if not self._done.is_set():
            return None
        if self._error:
            raise self._error
        if not self._identity:
            raise FabricError(
                "Sign-in stopped unexpectedly. Check the server terminal."
            )
        return self._identity

    def get_token(self):
        if self._closed or not self._identity or self._error:
            raise FabricError("Sign in with Microsoft before accessing Fabric.", 401)
        try:
            return self._credential.get_token(*FABRIC_SCOPES).token
        except AuthenticationRequiredError as exc:
            raise FabricError(
                "Microsoft requires interaction or additional consent. Sign out and "
                "sign in again explicitly. API calls will not open sign-in windows.",
                401,
            ) from exc
        except (ClientAuthenticationError, ServiceRequestError) as exc:
            raise authentication_error(exc) from exc

    def _close_credential(self):
        with self._lifecycle_lock:
            if self._credential is None or self._credential_closed:
                return
            self._credential_closed = True
            credential = self._credential
            self._credential = None
        credential.close()

    def close(self):
        self._closed = True
        self._identity = None
        if self._worker is None or self._done.is_set():
            self._close_credential()
