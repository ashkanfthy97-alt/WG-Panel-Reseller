"""Public TLS edge and first-run API for WG Portal."""

from __future__ import annotations

import hmac
import base64
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session

from acme_manager import Certificate, CertificateManager, DnsTargetMismatch, InvalidDomain, validate_certificate, validate_domain_target
from setup_state import (
    InvalidSetupToken,
    InvalidTransition,
    OwnerClaimInProgress,
    SetupAlreadyClaimed,
    SetupAlreadyComplete,
    SetupState,
    SetupStateError,
)


ROOT = Path(os.environ.get("WG_PORTAL_SETUP_ROOT", Path(__file__).resolve().parent)).resolve()
RUNTIME = Path(os.environ.get("WG_PORTAL_RUNTIME", ROOT / "runtime")).resolve()
UI_ROOT = Path(os.environ.get("WG_PORTAL_SETUP_UI_ROOT", Path(__file__).parent / "setup_ui")).resolve()
PANEL_URL = os.environ.get("WG_PORTAL_PANEL_URL", "http://127.0.0.1:8766").rstrip("/")
GATEWAY_URL = os.environ.get("WG_PORTAL_GATEWAY_URL", "http://127.0.0.1:8765").rstrip("/")
SETUP_DB = Path(os.environ.get("WG_PORTAL_SETUP_DB", RUNTIME / "setup.sqlite3"))
PORTAL_ENV = Path(os.environ.get("WG_PORTAL_ENV_FILE", RUNTIME / "portal.env"))
TLS_CURRENT = Path(os.environ.get("WG_PORTAL_TLS_CURRENT", RUNTIME / "tls" / "current"))
WEBROOT = Path(os.environ.get("WG_PORTAL_ACME_WEBROOT", RUNTIME / "acme-webroot"))
PENDING_HOST = RUNTIME / "acme-pending-host"
RELOAD_HELPER = Path("/usr/local/lib/wgportal/schedule-setup-reload")
TELEGRAM_HELPER = Path("/usr/local/lib/wgportal/start-telegram")
PUBLIC_IP = os.environ.get("WG_PORTAL_PUBLIC_IP", "").strip()
STATE = SetupState(SETUP_DB)
app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", ""),
    MAX_CONTENT_LENGTH=256 * 1024 * 1024,
    SESSION_COOKIE_NAME="wgportal_setup_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=1800,
)

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade",
}
STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'"
    ),
    "Cache-Control": "no-store",
}


def _allowed_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    if PUBLIC_IP:
        hosts.add(PUBLIC_IP.strip("[]").lower())
    try:
        current = STATE.read()
        if current.get("domain") and current.get("tls_ready") and current.get("cert_name") == "wg-portal-domain":
            hosts.add(current["domain"].lower().rstrip("."))
        journal = _read_tls_journal()
        if journal.get("phase") == "pending":
            previous = journal.get("previous", {}).get("tls_state", {})
            if previous.get("domain") and previous.get("cert_name") == "wg-portal-domain":
                hosts.add(previous["domain"].lower().rstrip("."))
    except SetupStateError:
        pass
    hosts.update(
        entry.strip().lower().rstrip(".")
        for entry in os.environ.get("WG_PORTAL_ALLOWED_HOSTS", "").split(",")
        if entry.strip()
    )
    return hosts


def _panel_session_headers() -> dict[str, str]:
    cookies = request.headers.get("Cookie", "")
    headers = {"Accept": "application/json"}
    if cookies:
        headers["Cookie"] = cookies
    return headers


def _panel_request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("X-Forwarded-Proto", "https")
    headers.setdefault("X-Forwarded-Host", request.host)
    cookies = request.headers.get("Cookie", "")
    if cookies:
        headers.setdefault("Cookie", cookies)
    transport = requests.Session()
    transport.trust_env = False
    try:
        return transport.request(
            method, f"{PANEL_URL}{path}", headers=headers,
            allow_redirects=False, timeout=(3, 12), **kwargs,
        )
    finally:
        transport.close()


def _owner_exists() -> bool:
    client = requests.Session()
    client.trust_env = False
    try:
        response = client.get(PANEL_URL + "/register", allow_redirects=False, timeout=(2, 5))
    except requests.RequestException:
        return False
    finally:
        client.close()
    return response.status_code in {301, 302, 303, 307, 308} and urlsplit(response.headers.get("Location", "")).path == "/login"


def _admin_session() -> bool:
    try:
        response = _panel_request("GET", "/api/admin", headers=_panel_session_headers())
        return response.status_code == 200 and bool(response.json().get("username"))
    except (requests.RequestException, ValueError, AttributeError):
        return False


def _panel_health() -> bool:
    key = os.environ.get("API_KEY", "").strip()
    if not key:
        return False
    transport = requests.Session()
    transport.trust_env = False
    try:
        response = transport.get(PANEL_URL + "/api/healthz", headers={"X-API-KEY": key}, timeout=(2, 5))
        return response.status_code == 200 and response.json().get("ok") is True
    except (requests.RequestException, ValueError, AttributeError):
        return False
    finally:
        transport.close()


def _issue_manager() -> CertificateManager:
    config = Path(os.environ.get("WG_PORTAL_ACME_CONFIG_DIR", RUNTIME / "acme-config"))
    executable = os.environ.get("WG_PORTAL_CERTBOT", str(ROOT / "acme-venv" / "bin" / "certbot"))
    return CertificateManager(
        executable, WEBROOT, config / "live", config_dir=config,
        work_dir=Path(os.environ.get("WG_PORTAL_ACME_WORK_DIR", RUNTIME / "acme-work")),
        logs_dir=Path(os.environ.get("WG_PORTAL_ACME_LOGS_DIR", RUNTIME / "acme-logs")),
    )


def _ensure_csrf() -> str:
    token = session.get("setup_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["setup_csrf"] = token
    return token


@app.before_request
def protect_public_edge():
    host = (urlsplit("//" + request.host).hostname or "").lower().rstrip(".")
    if host not in _allowed_hosts():
        abort(400, description="Unrecognized Host header")
    if not request.is_secure:
        abort(400, description="TLS is required for the setup service")
    try:
        if STATE.read()["phase"] == "complete" and request.path.startswith("/__setup/api/"):
            abort(404)
    except SetupStateError:
        pass
    control_api = request.path.startswith(("/__setup/api/", "/__manage/api/"))
    if control_api:
        request.max_content_length = 32 * 1024
    if control_api and (request.content_length or 0) > 32 * 1024:
        abort(413)
    if control_api and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin", "")
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.netloc.lower() != request.host.lower():
            abort(403, description="Same-origin HTTPS request required")
        expected = session.get("setup_csrf", "")
        supplied = request.headers.get("X-Setup-CSRF", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(403, description="Invalid setup CSRF token")


@app.after_request
def security_headers(response):
    control = request.path in {"/setup", "/setup/", "/manage", "/manage/"} or request.path.startswith(
        ("/__setup/", "/__manage/", "/assets/setup.")
    )
    if control:
        response.headers.update(STATIC_HEADERS)
    try:
        journal = _read_tls_journal()
        pending = journal.get("phase") == "pending"
        recovery = journal.get("phase") == "error" and journal.get("rollback_ok") is not True
        trusted = STATE.read().get("cert_name") in {"wg-portal-ip", "wg-portal-domain"} and not pending and not recovery
    except SetupStateError:
        trusted = False
    if trusted:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    else:
        response.headers.pop("Strict-Transport-Security", None)
    response.vary.add("Cookie")
    return response


@app.get("/setup")
@app.get("/setup/")
def setup_page():
    try:
        if STATE.read()["phase"] == "complete":
            return redirect("/", code=303)
    except SetupStateError:
        return "Setup state is not initialized. Run the installer recovery command.", 503
    _ensure_csrf()
    return send_from_directory(UI_ROOT, "setup.html")


@app.get("/manage")
@app.get("/manage/")
def management_page():
    if not _admin_session():
        return redirect("/login?next=/manage", code=303)
    try:
        STATE.read()
    except SetupStateError:
        return "Setup storage is unavailable. Use the server recovery command.", 503
    _ensure_csrf()
    return send_from_directory(UI_ROOT, "setup.html")


@app.get("/assets/setup.css")
@app.get("/assets/setup.js")
def setup_assets():
    return send_from_directory(UI_ROOT, request.path.rsplit("/", 1)[-1])


@app.get("/__setup/api/status")
def setup_status():
    try:
        state = STATE.read()
    except SetupStateError:
        return jsonify(ok=False, message="Setup storage is not initialized"), 503
    if not state["owner_claimed"] and _owner_exists():
        STATE.mark_owner_exists()
        state = STATE.read()
    if state["phase"] == "claiming" and _owner_exists():
        STATE.recover_owner_claim(state["claim_id"], admin_exists=True)
        state = STATE.read()
    elif state["phase"] == "claiming" and STATE.recover_stale_owner_claim():
        state = STATE.read()
    return _status_response(state)


def _status_response(state: dict):
    csrf = _ensure_csrf()
    try:
        journal = _read_tls_journal()
    except SetupStateError:
        journal = {"phase": "error", "rollback_ok": False}
    pending = journal.get("phase") == "pending"
    active = journal.get("previous", {}).get("tls_state", {}) if pending else state
    cert_name = active.get("cert_name", "")
    kind = {"wg-portal-bootstrap": "bootstrap", "wg-portal-ip": "ip", "wg-portal-domain": "domain"}.get(cert_name, "none")
    recovery_required = journal.get("phase") == "error" and journal.get("rollback_ok") is not True
    tls_apply = {"phase": journal.get("phase", "idle"), "operation_id": journal.get("operation_id", ""),
                 "candidate_domain": journal.get("candidate", {}).get("domain", ""),
                 "recovery_required": recovery_required}
    return jsonify(
        phase=state["phase"], public_ip=state["public_ip"], domain=active.get("domain"),
        active_domain=active.get("domain", "") if cert_name == "wg-portal-domain" and active.get("tls_ready") else "",
        owner_claimed=state["owner_claimed"], tls_ready=bool(active.get("tls_ready")),
        panel_ready=_panel_health(), cert_name=cert_name, csrf=csrf, csrf_token=csrf,
        certificate_kind=kind, tls_trusted=kind in {"ip", "domain"} and not pending and not recovery_required,
        tls_apply=tls_apply,
        admin_authenticated=_admin_session(), setup_complete=state["phase"] == "complete",
        native_config_links={"wireguard": "/users", "telegram": "/settings?tab=telegram", "dashboard": "/"},
    )


@app.get("/__manage/api/status")
def management_status():
    if not _admin_session():
        return jsonify(message="ورود مدیر لازم است / Administrator sign-in required"), 401
    try:
        return _status_response(STATE.read())
    except SetupStateError:
        return jsonify(message="Setup storage is unavailable"), 503


def _registration_csrf(html: str) -> str:
    match = re.search(
        r"<input(?=[^>]*name=['\"]csrf_token['\"])(?=[^>]*value=['\"]([^'\"]+)['\"])[^>]*>",
        html,
        re.I,
    )
    if not match:
        # HTML attributes are not guaranteed to have a stable order.
        for element in re.findall(r"<input\b[^>]*>", html, re.I):
            if re.search(r"\bname=['\"]csrf_token['\"]", element, re.I):
                value = re.search(r"\bvalue=['\"]([^'\"]+)['\"]", element, re.I)
                if value:
                    return value.group(1)
        raise RuntimeError("WG_Panel registration form did not expose its CSRF token")
    return match.group(1)


def _register_panel_owner(username: str, password: str, token: str) -> bool:
    panel_token = os.environ.get("SETUP_TOKEN", "").strip()
    if len(panel_token) < 32:
        raise RuntimeError("WG_Panel setup token is not configured")
    client = requests.Session()
    client.trust_env = False
    try:
        form = client.get(PANEL_URL + "/register", timeout=(3, 12), allow_redirects=False)
        if form.status_code in {301, 302, 303, 307, 308} and urlsplit(form.headers.get("Location", "")).path == "/login":
            return False
        if form.status_code != 200:
            raise RuntimeError("WG_Panel registration page is unavailable")
        csrf_token = _registration_csrf(form.text)
        response = client.post(
            PANEL_URL + "/register",
            data={
                "csrf_token": csrf_token,
                "setup_token": panel_token,
                "username": username,
                "password": password,
                "password2": password,
            },
            timeout=(3, 20),
            allow_redirects=False,
        )
        return (
            response.status_code in {301, 302, 303, 307, 308}
            and urlsplit(response.headers.get("Location", "")).path == "/login"
        )
    finally:
        client.close()


@app.post("/__setup/api/owner")
def setup_owner():
    try:
        STATE.read()
    except SetupStateError:
        return jsonify(message="Setup storage is unavailable; run the server recovery command."), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(message="درخواست نامعتبر است."), 400
    token = str(data.get("token") or "")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    password2 = str(data.get("password2") or "")
    if not token or len(token) > 256 or not username or len(username) > 64:
        return jsonify(message="کد نصب و نام کاربری را بررسی کنید."), 400
    if len(password) < 12 or len(password) > 1024 or password != password2:
        return jsonify(message="رمز باید حداقل ۱۲ نویسه باشد و دو بار یکسان وارد شود."), 400
    if _owner_exists():
        STATE.mark_owner_exists()
        return jsonify(message="حساب مدیر از قبل ساخته شده؛ وارد پنل شوید."), 409
    try:
        claim = STATE.begin_owner_claim(token, username)
    except InvalidSetupToken:
        return jsonify(message="کد نصب درست نیست یا قبلاً مصرف شده است."), 403
    except (SetupAlreadyClaimed, SetupAlreadyComplete):
        return jsonify(message="حساب مدیر قبلاً ساخته شده است."), 409
    except OwnerClaimInProgress:
        return jsonify(message="ساخت حساب دیگری در جریان است؛ چند لحظه بعد دوباره بررسی کنید."), 409
    except SetupStateError:
        return jsonify(message="وضعیت نصب در دسترس نیست؛ بازیابی نصب را روی سرور اجرا کنید."), 503
    try:
        created = _register_panel_owner(username, password, token)
        if not created and _owner_exists():
            STATE.recover_owner_claim(claim["claim_id"], admin_exists=True)
            return jsonify(message="مدیر پنل ساخته شده است؛ برای ادامه وارد شوید."), 409
        if not created:
            STATE.recover_owner_claim(claim["claim_id"], admin_exists=False)
            return jsonify(message="ثبت حساب در WG_Panel کامل نشد؛ کد نصب قابل تلاش دوباره است."), 502
        STATE.complete_owner_claim(claim["claim_id"])
        return jsonify(ok=True, message="حساب مدیر اصلی با موفقیت ساخته شد؛ حالا وارد پنل شوید.")
    except Exception:
        app.logger.error("First-owner registration failed; request values are withheld")
        try:
            STATE.recover_owner_claim(claim["claim_id"], admin_exists=_owner_exists())
        except Exception:
            app.logger.error("First-owner recovery state could not be updated")
        return jsonify(message="ساخت حساب کامل نشد. کد نصب را نگه دارید و وضعیت پنل را دوباره بررسی کنید."), 502


def _write_allowed_hosts(domain: str) -> None:
    hosts = {"localhost", "127.0.0.1", "[::1]", PUBLIC_IP}
    if domain:
        hosts.add(domain)
    hosts.discard("")
    existing = PORTAL_ENV.read_text(encoding="utf-8") if PORTAL_ENV.is_file() else ""
    lines = [line for line in existing.splitlines() if not line.strip().startswith("WG_PORTAL_ALLOWED_HOSTS=")]
    lines.append("WG_PORTAL_ALLOWED_HOSTS=" + ",".join(sorted(hosts)))
    PORTAL_ENV.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".portal-env-", dir=PORTAL_ENV.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, PORTAL_ENV)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _stage_certificate(certificate: Certificate) -> Path:
    TLS_CURRENT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    versions = TLS_CURRENT.parent / "versions"
    versions.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="version-", dir=versions))
    # Certbot rotates its live symlinks in place; the served keypair must remain
    # immutable until validation and the application-level transaction succeed.
    _atomic_bytes(directory / "fullchain.pem", certificate.fullchain.read_bytes())
    _atomic_bytes(directory / "privkey.pem", certificate.private_key.read_bytes())
    selected = Certificate(certificate.name, certificate.domain, directory)
    validate_certificate(selected, certificate.domain)
    if certificate.name == "wg-portal-domain":
        validate_certificate(selected, PUBLIC_IP)
    return directory


def _select_directory(directory: Path) -> None:
    temporary = TLS_CURRENT.with_name(".current-" + secrets.token_hex(6))
    temporary.symlink_to(directory, target_is_directory=True)
    os.replace(temporary, TLS_CURRENT)
    _sync_directory(TLS_CURRENT.parent)


def _select_certificate(certificate: Certificate) -> None:
    _select_directory(_stage_certificate(certificate))


def _require_helper(helper: Path) -> None:
    if helper.is_symlink() or not helper.is_file() or not os.access(helper, os.X_OK):
        raise RuntimeError("The installation helper is unavailable")
    for component in (helper, *helper.parents):
        if component.is_symlink():
            raise RuntimeError("The installation helper path contains a symbolic link")
        metadata = component.stat()
        if os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            raise RuntimeError("The installation helper path has unsafe ownership or permissions")


def _run_helper(helper: Path) -> None:
    _require_helper(helper)
    subprocess.run([str(helper)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True, timeout=15, check=True)


def _schedule_reload() -> None:
    _run_helper(RELOAD_HELPER)


@contextmanager
def _certificate_operation():
    """Serialize slow issuance and certificate selection across worker processes."""
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = RUNTIME / "certificate-operation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        locked = True
        yield
    finally:
        if locked:
            if os.name == "posix":
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".portal-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_tls_journal() -> dict:
    path = RUNTIME / "tls" / "apply-journal.json"
    if not path.exists():
        return {}
    try:
        if path.is_symlink():
            raise ValueError("Unexpected journal symlink")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("phase") not in {"pending", "applied", "error"}:
            raise ValueError("Invalid TLS journal")
        if not isinstance(value.get("candidate", {}), dict) or not isinstance(value.get("previous", {}), dict):
            raise ValueError("Invalid TLS journal fields")
        if value.get("phase") == "pending" and not isinstance(value.get("previous", {}).get("tls_state"), dict):
            raise ValueError("Invalid previous TLS state")
        return value
    except (OSError, ValueError) as exc:
        raise SetupStateError("The TLS operation journal is unavailable") from exc


def _write_tls_journal(value: dict) -> None:
    _atomic_bytes(RUNTIME / "tls" / "apply-journal.json", json.dumps(value).encode("utf-8"))


def _restore_certificate_selection(previous: str | None) -> None:
    if previous is None:
        TLS_CURRENT.unlink(missing_ok=True)
    else:
        temporary = TLS_CURRENT.with_name(".rollback-" + secrets.token_hex(6))
        temporary.symlink_to(previous, target_is_directory=True)
        os.replace(temporary, TLS_CURRENT)


def _apply_certificate(certificate: Certificate, domain: str) -> None:
    _require_helper(RELOAD_HELPER)
    snapshot = STATE.read()
    if TLS_CURRENT.exists() and not TLS_CURRENT.is_symlink():
        raise RuntimeError("TLS selection must be an installer-managed symlink")
    previous = str(TLS_CURRENT.resolve()) if TLS_CURRENT.is_symlink() else None
    old_env = PORTAL_ENV.read_bytes() if PORTAL_ENV.is_file() else None
    directory = _stage_certificate(certificate)
    tls_state = {name: snapshot.get(name) for name in ("domain", "tls_ready", "cert_name", "cert_path")}
    journal = {"version": 1, "operation_id": secrets.token_hex(16), "phase": "pending", "created_at": time.time(),
               "previous": {"target": previous, "env_b64": base64.b64encode(old_env).decode("ascii") if old_env is not None else None,
                            "tls_state": tls_state},
               "candidate": {"target": str(directory.resolve()), "domain": domain, "cert_name": certificate.name}}
    _write_tls_journal(journal)
    try:
        _select_directory(directory)
        _write_allowed_hosts(domain)
        STATE.mark_tls_ready(certificate.name, str(TLS_CURRENT), domain=domain, allow_complete=True)
        _schedule_reload()
    except Exception:
        rollback_ok = False
        try:
            _restore_certificate_selection(previous)
            if old_env is None:
                PORTAL_ENV.unlink(missing_ok=True)
            else:
                _atomic_bytes(PORTAL_ENV, old_env)
            STATE.restore_tls_state(snapshot)
            rollback_ok = True
        finally:
            _write_tls_journal({**journal, "phase": "error", "rollback_ok": rollback_ok,
                                "error": "certificate_reload_scheduling_failed"})
        raise


def _certificate_request(data: dict):
    kind = data.get("kind", "domain")
    if not isinstance(kind, str) or kind not in {"domain", "ip"}:
        return jsonify(message="Certificate kind must be ip or domain"), 400
    try:
        state = STATE.read()
        if not state["owner_claimed"]:
            return jsonify(message="The initial owner must be registered first"), 409
        domain = normalize_domain_value(data.get("domain", "")) if kind == "domain" else ""
        if domain:
            validate_domain_target(domain, PUBLIC_IP)
        # Refuse incomplete installation before issuing or switching any certificate.
        _require_helper(RELOAD_HELPER)
        with _certificate_operation():
            journal = _read_tls_journal()
            if journal.get("phase") == "pending":
                return jsonify(message="A certificate reload is pending; wait for its result or run server recovery."), 409
            if journal.get("phase") == "error" and journal.get("rollback_ok") is not True:
                return jsonify(message="TLS recovery is required before another certificate operation."), 503
            try:
                if domain:
                    _atomic_bytes(PENDING_HOST, json.dumps({"host": domain, "expires_at": time.time() + 240}).encode())
                manager = _issue_manager()
                certificate = manager.issue_domain(domain, PUBLIC_IP) if domain else manager.issue_ip(PUBLIC_IP)
                _apply_certificate(certificate, domain)
            finally:
                PENDING_HOST.unlink(missing_ok=True)
        return jsonify(ok=True, accepted=True, certificate_kind=kind, tls_trusted=False, tls_apply={"phase": "pending"},
                       message="گواهی صادر شد و درخواست بارگذاری ثبت شد؛ نتیجه را در وضعیت اتصال بررسی کنید. / Certificate issued; reload queued. Check connection status for the applied result."), 202
    except (InvalidDomain, DnsTargetMismatch, ValueError) as exc:
        return jsonify(message=str(exc)), 400
    except SetupStateError:
        return jsonify(message="Setup storage is unavailable"), 503
    except BlockingIOError:
        return jsonify(message="A certificate operation is already in progress"), 409
    except Exception:
        # Certbot stderr, form values and registration tokens never enter public logs.
        app.logger.error("Certificate operation failed; existing TLS configuration was retained")
        return jsonify(message="صدور گواهی کامل نشد؛ تنظیم قبلی حفظ شد. DNS، پورت ۸۰ و نصب Certbot را بررسی کنید. / Certificate issuance failed; the previous configuration was retained."), 502


@app.post("/__setup/api/domain")
def setup_domain():
    if not _admin_session():
        return jsonify(message="برای تغییر دامنه، ابتدا با حساب مدیر در WG_Panel وارد شوید."), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(message="درخواست دامنه نامعتبر است."), 400
    return _certificate_request({**data, "kind": "domain"})


@app.post("/__manage/api/certificate")
@app.post("/__manage/api/domain")
def manage_certificate():
    if not _admin_session():
        return jsonify(message="ورود مدیر لازم است / Administrator sign-in required"), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(message="Invalid certificate request"), 400
    if request.path.endswith("/domain"):
        data = {**data, "kind": "domain"}
    return _certificate_request(data)


@app.post("/__manage/api/telegram/start")
def start_telegram():
    if not _admin_session():
        return jsonify(message="Administrator sign-in required"), 401
    try:
        _run_helper(TELEGRAM_HELPER)
    except Exception:
        app.logger.error("The product Telegram service could not be started")
        return jsonify(message="شروع ربات کامل نشد؛ تنظیم توکن و مدیران و وضعیت سرویس را بررسی کنید. / Bot startup failed; check bot settings and service status."), 502
    return jsonify(ok=True, message="سرویس ربات راه‌اندازی شد. / Telegram service started.")


def normalize_domain_value(value: str) -> str:
    from acme_manager import normalize_domain
    return normalize_domain(str(value or ""))


@app.post("/__setup/api/complete")
def finish_setup():
    if not _admin_session():
        return jsonify(message="برای تکمیل، ابتدا به‌عنوان مدیر وارد شوید."), 401
    if not _panel_health():
        return jsonify(message="سلامت API پنل تأیید نشد؛ وضعیت WG_Panel را بررسی کنید."), 503
    try:
        journal = _read_tls_journal()
        if journal.get("phase") == "pending":
            return jsonify(message="بارگذاری گواهی هنوز تکمیل نشده است. / Certificate reload is still pending."), 409
        if journal.get("phase") == "error" and journal.get("rollback_ok") is not True:
            return jsonify(message="TLS recovery is required before setup can be completed."), 503
        STATE.complete_setup(panel_ready=True)
    except InvalidTransition as exc:
        return jsonify(message=str(exc)), 409
    except SetupStateError:
        return jsonify(message="Setup or certificate operation storage is unavailable"), 503
    return jsonify(ok=True, message="راه‌اندازی امن کامل شد.")


@app.get("/__setup/health")
def application_health():
    panel_ready = _panel_health()
    try:
        state = STATE.read()
    except SetupStateError:
        return jsonify(ok=False, setup_state=False, panel_ready=panel_ready), 503
    try:
        journal = _read_tls_journal()
    except SetupStateError:
        journal = {"phase": "error", "rollback_ok": False}
    pending = journal.get("phase") == "pending"
    recovery = journal.get("phase") == "error" and journal.get("rollback_ok") is not True
    trusted = state.get("cert_name") in {"wg-portal-ip", "wg-portal-domain"} and not pending and not recovery
    ready = bool(state["tls_ready"] and panel_ready and not recovery)
    return jsonify(ok=ready, setup_state=True, tls_ready=state["tls_ready"],
                   tls_trusted=trusted, bootstrap_tls=state.get("cert_name") == "wg-portal-bootstrap",
                   tls_apply={"phase": journal.get("phase", "idle"), "recovery_required": recovery},
                   panel_ready=panel_ready), 200 if ready else 503


@app.route("/", defaults={"path": ""}, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy_to_portal(path: str):
    if path.startswith(("__setup/", "__manage/", "assets/setup.")):
        abort(404)
    if not path and request.method in {"GET", "HEAD"}:
        try:
            if STATE.read()["phase"] != "complete":
                return redirect("/setup", code=303)
        except SetupStateError:
            return "Setup storage is unavailable. Run the server recovery command.", 503
    url = GATEWAY_URL + ("/" + path if path else "/")
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS
        and key.lower() not in {"host", "content-length", "cookie", "origin", "referer"}
        and not key.lower().startswith("x-forwarded-")
    }
    headers.update({
        "Host": request.host,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": request.host,
        "X-Forwarded-For": request.remote_addr or "127.0.0.1",
    })
    if request.headers.get("Cookie"):
        headers["Cookie"] = request.headers["Cookie"]
    if request.headers.get("Origin"):
        headers["Origin"] = request.headers["Origin"]
    if request.method not in {"GET", "HEAD"}:
        headers["Content-Type"] = request.content_type or "application/octet-stream"
    transport = requests.Session()
    transport.trust_env = False
    try:
        upstream = transport.request(
            request.method, url, headers=headers,
            data=request.get_data(cache=False) if request.method not in {"GET", "HEAD"} else None,
            stream=True, allow_redirects=False, timeout=(4, 120),
        )
    except requests.RequestException:
        transport.close()
        app.logger.exception("WG Portal gateway is unavailable")
        return Response("WG Portal is unavailable", status=502, content_type="text/plain")
    response_headers = []
    nominated = {value.strip().lower() for value in upstream.headers.get("Connection", "").split(",")}
    for key in upstream.raw.headers.keys():
        if key.lower() in HOP_HEADERS or key.lower() in nominated:
            continue
        for value in upstream.raw.headers.getlist(key):
            response_headers.append((key, value))
    response = Response(
        upstream.raw.stream(64 * 1024, decode_content=False),
        status=upstream.status_code,
        headers=response_headers,
        direct_passthrough=True,
    )
    response.call_on_close(upstream.close)
    response.call_on_close(transport.close)
    return response


if not app.config["SECRET_KEY"]:
    # The installer is expected to supply the existing strong WG_Panel key.
    raise RuntimeError("FLASK_SECRET_KEY is required for the secure setup service")
