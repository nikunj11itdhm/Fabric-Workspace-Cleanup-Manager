import hmac
import logging
import secrets
import threading
import time
from datetime import timedelta
from functools import wraps

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException

from browser_auth import BrowserSession
from fabric import FabricClient, FabricError, guid, parse_workspace_csv


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_NAME="fabric_workspace_manager_5000",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
        TRUSTED_HOSTS=["localhost", "127.0.0.1"],
    )
    if test_config:
        app.config.update(test_config)
    states = {}
    states_lock = threading.Lock()
    app.extensions["local_states"] = states

    @app.before_request
    def protect_local_app():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403, "This app is available on localhost only.")
        now = time.monotonic()
        with states_lock:
            for sid in [key for key, value in states.items() if value["expires"] < now]:
                expired = states[sid]
                if expired["lock"].acquire(blocking=False):
                    try:
                        if expired.get("auth"):
                            expired["auth"].close()
                        del states[sid]
                    finally:
                        expired["lock"].release()
            sid = session.get("sid")
            if sid not in states:
                session.clear()
                sid = secrets.token_urlsafe(32)
                session["sid"] = sid
                session.permanent = True
                states[sid] = {
                    "expires": now + 3600,
                    "csrf": secrets.token_urlsafe(32),
                    "context": secrets.token_urlsafe(16),
                    "workspaces": [],
                    "lock": threading.Lock(),
                }
            g.state = states[sid]
            g.state["expires"] = now + 3600
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("X-CSRF-Token") or request.form.get(
                "csrf_token", ""
            )
            if not hmac.compare_digest(str(csrf), g.state["csrf"]):
                app.logger.warning(
                    "Rejected a request with a stale or missing CSRF token."
                )
                return jsonify(
                    error="Your page's session token is out of date. Reload the page "
                    "and retry; no action was performed.",
                    code="csrf_mismatch",
                ), 403
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                abort(403, "Cross-origin writes are not permitted.")

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(FabricError)
    def fabric_error(exc):
        app.logger.warning("Fabric operation failed: %s", exc)
        status = exc.status if exc.status in {400, 401, 403, 404, 409, 429} else 502
        return jsonify(error=str(exc)), status

    @app.errorhandler(ValueError)
    def invalid_input(exc):
        app.logger.warning("Invalid request: %s", exc)
        return jsonify(error=str(exc)), 400

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    def signed_in(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if not g.state.get("account"):
                abort(401, "Sign in with your Fabric administrator account first.")
            return fn(*args, **kwargs)

        return decorated

    def client():
        factory = app.config.get(
            "FABRIC_CLIENT_FACTORY",
            lambda auth: FabricClient(token_provider=auth.get_token),
        )
        return factory(g.state.get("auth"))

    def imported(workspace_id):
        workspace_id = guid(workspace_id, "Workspace ID")
        if workspace_id not in g.state["workspaces"]:
            abort(400, "Workspace must be imported from your CSV first.")
        return workspace_id

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            abort(400, "Request body must be an object.")
        return value

    def exclusive(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            lock = g.state["lock"]
            if not lock.acquire(blocking=False):
                abort(409, "Another operation is running in this session.")
            try:
                return fn(*args, **kwargs)
            finally:
                lock.release()

        return decorated

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            csrf=g.state["csrf"],
            session_context=g.state["context"],
            user=g.state.get("user"),
            login_pending=bool(g.state.get("auth") and not g.state.get("account")),
        )

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/auth/session")
    def session_info():
        return jsonify(
            csrfToken=g.state["csrf"],
            contextId=g.state["context"],
            authenticated=bool(g.state.get("account")),
        )

    @app.post("/auth/login")
    @exclusive
    def login():
        tenant = guid(request.form.get("tenant_id"), "Tenant ID")
        if g.state.get("auth"):
            if g.state["auth"].tenant != tenant:
                abort(409, "Sign out before changing tenants.")
            return jsonify(
                status="authenticated" if g.state.get("account") else "pending",
            )
        factory = app.config.get("BROWSER_SESSION_FACTORY", BrowserSession)
        auth = factory()
        g.state["auth"] = auth
        g.state.pop("account", None)
        g.state.pop("user", None)
        g.state.pop("delete_preview", None)
        g.state["workspaces"] = []
        try:
            auth.start_login(tenant)
        except (FabricError, ValueError):
            auth.close()
            g.state.pop("auth")
            raise
        return jsonify(status="pending")

    @app.get("/auth/status")
    @exclusive
    def auth_status():
        if g.state.get("account"):
            return jsonify(status="authenticated")
        auth = g.state.get("auth")
        if not auth:
            return jsonify(status="idle")
        try:
            identity = auth.poll_login()
        except (FabricError, ValueError):
            auth.close()
            g.state.pop("auth")
            raise
        if identity is None:
            return jsonify(status="pending")
        g.state["account"] = identity
        g.state["user"] = identity["name"]
        with states_lock:
            old_sid = session["sid"]
            new_sid = secrets.token_urlsafe(32)
            states[new_sid] = states.pop(old_sid)
            session["sid"] = new_sid
        return jsonify(status="authenticated")

    @app.post("/auth/logout")
    @exclusive
    def logout():
        if g.state.get("auth"):
            g.state["auth"].close()
        with states_lock:
            states.pop(session.get("sid"), None)
        session.clear()
        if request.accept_mimetypes.best == "application/json":
            return jsonify(status="signed_out")
        return redirect("/")

    @app.post("/api/import")
    @signed_in
    @exclusive
    def upload():
        file = request.files.get("file")
        if not file:
            abort(400, "Choose a CSV file.")
        workspace_ids = parse_workspace_csv(file.read())
        g.state["workspaces"] = workspace_ids
        g.state.pop("delete_preview", None)
        return jsonify(workspaceIds=workspace_ids, count=len(workspace_ids))

    @app.get("/api/inventory")
    @signed_in
    @exclusive
    def inventory():
        if not g.state["workspaces"]:
            abort(400, "Import a workspace CSV first.")
        return jsonify(client().inventory(g.state["workspaces"]))

    @app.get("/api/workspaces/<workspace_id>/items")
    @signed_in
    @exclusive
    def items(workspace_id):
        return jsonify(items=client().items(imported(workspace_id)))

    @app.post("/api/assign")
    @signed_in
    @exclusive
    def assign():
        data = body()
        ids = data.get("workspaceIds")
        if not isinstance(ids, list) or not ids:
            abort(400, "Select at least one workspace.")
        workspace_ids = list(dict.fromkeys(imported(value) for value in ids))
        primary = guid(data.get("primary"), "Primary capacity ID")
        secondary = (
            guid(data["secondary"], "Secondary capacity ID")
            if data.get("secondary")
            else None
        )
        if primary == secondary:
            abort(400, "Primary and secondary capacities must be different.")
        api = client()
        results = []
        for workspace_id in workspace_ids:
            try:
                result = api.assign(workspace_id, primary, secondary)
            except FabricError as exc:
                app.logger.warning(
                    "Workspace assignment failed for %s: %s", workspace_id, exc
                )
                result = {"status": "failed", "message": str(exc)}
            results.append({"workspaceId": workspace_id, **result})
        return jsonify(results=results)

    @app.post("/api/workspaces/<workspace_id>/delete-preview")
    @signed_in
    @exclusive
    def delete_preview(workspace_id):
        workspace_id = imported(workspace_id)
        api = client()
        workspace = api.workspace(workspace_id)
        snapshot = api.cleanup_items(workspace_id)
        confirmation = f"DELETE {workspace_id}"
        nonce = secrets.token_urlsafe(32)
        g.state["delete_preview"] = {
            "workspaceId": workspace_id,
            "items": snapshot,
            "nonce": nonce,
            "confirmation": confirmation,
            "expires": time.monotonic() + 300,
        }
        return jsonify(
            workspaceId=workspace_id,
            name=workspace.get("displayName", workspace_id),
            items=snapshot,
            confirmation=confirmation,
            nonce=nonce,
        )

    @app.post("/api/workspaces/<workspace_id>/delete")
    @signed_in
    @exclusive
    def delete_items(workspace_id):
        workspace_id = imported(workspace_id)
        data = body()
        preview = g.state.get("delete_preview")
        if (
            not preview
            or preview["expires"] < time.monotonic()
            or preview["workspaceId"] != workspace_id
        ):
            abort(400, "Deletion preview expired or is missing. Preview again.")
        if (
            not hmac.compare_digest(str(data.get("nonce", "")), preview["nonce"])
            or data.get("confirmation") != preview["confirmation"]
        ):
            abort(400, "Type the exact deletion confirmation phrase.")
        g.state.pop("delete_preview")
        return jsonify(client().delete_snapshot(workspace_id, preview["items"]))

    return app


if __name__ == "__main__":
    from waitress import serve

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    print("Fabric Workspace Manager: http://localhost:5000", flush=True)
    serve(create_app(), host="127.0.0.1", port=5000, threads=4)
