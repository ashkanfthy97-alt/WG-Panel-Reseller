"""WG Portal policy for the pinned native WG_Panel (Linux only)."""
import hmac
import os
from pathlib import Path

from flask import current_app, g, jsonify, request


def require_secret(name, minimum=32):
    value = os.environ.get(name, "")
    if len(value.strip()) < minimum or value.strip().lower() in {"change-me", "changeme"}:
        raise RuntimeError(f"{name} must contain at least {minimum} characters; existing keys are never silently rotated.")
    return value


def install_security(app, csrf):
    @app.before_request
    def portal_policy():
        # Native main-branch updates would remove the product's policy patches.
        if request.path == "/api/panel/update" and request.method == "POST":
            return jsonify(error="managed_update_required",
                           message="Update WG Portal from its repository, then run its installer."), 409

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path.startswith("/api/"):
            expected = str(current_app.config.get("API_KEY") or "").strip()
            authorization = request.headers.get("Authorization", "").split(None, 1)
            bearer = authorization[1].strip() if len(authorization) == 2 and authorization[0].lower() == "bearer" else ""
            supplied = bearer or request.headers.get("X-API-KEY", "").strip()
            if not (expected and supplied and hmac.compare_digest(expected, supplied)):
                csrf.protect()

        if request.path == "/register":
            token = str(current_app.config.get("SETUP_TOKEN") or "").strip()
            # An existing account may still redirect to login without a setup token.
            from models import AdminAccount
            if not AdminAccount.query.first() and len(token) < 32:
                return "Set a random SETUP_TOKEN of at least 32 characters in the server .env before registering.", 503
            if request.method == "POST":
                # Serialise first-owner creation across Gunicorn processes.
                import fcntl
                lock_path = Path(app.instance_path) / "portal-owner.lock"
                lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return "Owner registration is already in progress. Retry shortly.", 409
                except BaseException:
                    os.close(fd)
                    raise
                g.portal_owner_lock = fd
                # Discard any read snapshot taken before obtaining the lock.
                from models import db
                db.session.rollback()

    @app.teardown_request
    def release_owner_lock(_error):
        fd = g.pop("portal_owner_lock", None)
        if fd is not None:
            os.close(fd)
