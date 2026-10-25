"""Root-owned, product-scoped TLS activation, recovery and renewal."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time


def assert_secure_metadata(path: Path, info) -> None:
    if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
        raise ValueError("Privileged paths must be root-owned and not writable by group/others: " + str(path))


def assert_secure_ancestry(path: Path, *, permit_missing=False) -> None:
    absolute = Path(os.path.abspath(path))
    for current in reversed((absolute, *absolute.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if permit_missing:
                continue
            raise ValueError("Required privileged path is missing: " + str(current))
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symlinked privileged ancestry is refused: " + str(current))
        assert_secure_metadata(current, info)


def assert_secure_checkout(root: Path) -> None:
    """Refuse unsafe code before importing checkout modules or invoking its venv."""
    if os.name == "nt":
        return  # Production is Linux; Windows is used only for isolated unit tests.
    root = Path(os.path.abspath(root))
    site = root.parent.parent
    assert_secure_ancestry(site)
    assert_secure_ancestry(root)
    venvs = (root / "venv", root / "acme-venv", root / "vendor/WG_Panel/venv")
    for directory, directories, files in os.walk(site, followlinks=False):
        for name in [*directories, *files]:
            path = Path(directory) / name
            info = path.lstat()
            assert_secure_metadata(path, info)
            if not stat.S_ISLNK(info.st_mode):
                continue
            tls_link = path.parent == root / "runtime/tls" and (path.name == "current" or path.name.startswith(".current-"))
            acme_link = (path.parent.parent == root / "runtime/acme-config/live"
                         and path.parent.name in {"wg-portal-ip", "wg-portal-domain"}
                         and path.name in {"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"})
            venv_link = any(
                (path.parent == venv / "bin" and re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", path.name))
                or path == venv / "lib64" for venv in venvs
            )
            if not tls_link and not acme_link and not venv_link:
                raise ValueError("Unexpected source symlink is refused: " + str(path))
            target = path.resolve(strict=True)
            if tls_link and not any(target.is_relative_to(prefix) for prefix in
                                    (root / "runtime/tls", root / "runtime/acme-config/live")):
                raise ValueError("TLS pointer escapes this installation")
            if acme_link and not target.is_relative_to(root / "runtime/acme-config/archive"):
                raise ValueError("ACME data pointer escapes this installation")
            assert_secure_ancestry(target)
    assert_secure_ancestry(Path("/usr/local/lib/wgportal"), permit_missing=True)
    if Path("/usr/local/lib/wgportal").exists():
        for path in Path("/usr/local/lib/wgportal").iterdir():
            assert_secure_ancestry(path)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tls-operation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_journal(root: Path, journal: dict) -> None:
    _atomic_bytes(root / "runtime/tls/apply-journal.json",
                  (json.dumps(journal, sort_keys=True) + "\n").encode("utf-8"))


def _read_journal(root: Path) -> dict | None:
    path = root / "runtime/tls/apply-journal.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("TLS journal is not a bounded private regular file")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict) or result.get("version") != 1:
        raise ValueError("TLS journal version is unsupported")
    return result


@contextmanager
def _certificate_lock(root: Path):
    path = root / "runtime/certificate-operation.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Certificate operation lock must not be a symlink")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name != "nt":
            assert_secure_metadata(path, os.fstat(fd))
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name != "nt":
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _selected_target(root: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    selected = Path(value).resolve(strict=True)
    permitted = (root / "runtime/tls", root / "runtime/acme-config/live")
    if not any(selected.is_relative_to(path.resolve()) for path in permitted):
        raise ValueError("Selected TLS directory is outside this installation")
    return selected


def _select_target(root: Path, selected: Path | None) -> None:
    current = root / "runtime/tls/current"
    if selected is None:
        if current.is_symlink():
            current.unlink()
        elif current.exists():
            raise ValueError("TLS current is not a managed symlink")
        return
    replacement = current.with_name(".current-" + secrets.token_hex(8))
    replacement.symlink_to(selected, target_is_directory=True)
    os.replace(replacement, current)


def _state(root: Path):
    sys.path.insert(0, str(root))
    from setup_state import SetupState
    return SetupState(root / "runtime/setup.sqlite3")


def _check_served_certificate(root: Path, state: dict, selected: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    leaf = x509.load_pem_x509_certificate((selected / "fullchain.pem").read_bytes())
    expected = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).digest()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    subject = state.get("domain") if state.get("cert_name") == "wg-portal-domain" else state["public_ip"]
    with socket.create_connection(("127.0.0.1", 8767), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname=subject) as secured:
            if hashlib.sha256(secured.getpeercert(binary_form=True)).digest() != expected:
                raise ValueError("The HTTPS service is not serving the selected certificate")
    client = http.client.HTTPSConnection("127.0.0.1", 8767, timeout=5, context=context)
    try:
        client.request("GET", "/__setup/health", headers={"Host": subject + ":8767"})
        response = client.getresponse()
        body = response.read(16385)
        if response.status != 200 or len(body) > 16384 or json.loads(body).get("ok") is not True:
            raise ValueError("HTTPS application readiness was not confirmed")
    finally:
        client.close()


def _restart_selected(root: Path) -> None:
    sys.path.insert(0, str(root))
    from acme_manager import Certificate, validate_certificate
    state = _state(root).read()
    selected = _selected_target(root, str((root / "runtime/tls/current").resolve(strict=True)))
    subject = state.get("domain") if state.get("cert_name") == "wg-portal-domain" else state["public_ip"]
    validate_certificate(Certificate(state["cert_name"], subject, selected), subject, minimum_hours=1)
    subprocess.run(["systemctl", "restart", "wg-portal-setup.service"], check=True, timeout=60)
    subprocess.run(["systemctl", "try-restart", "wg-portal.service"], check=True, timeout=60)
    last_error = None
    for _ in range(20):
        try:
            _check_served_certificate(root, state, selected)
            return
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError("HTTPS certificate/application readiness failed") from last_error


def _restore_previous(root: Path, previous: dict) -> None:
    _select_target(root, _selected_target(root, previous.get("target")))
    env = root / "runtime/portal.env"
    if previous.get("env_b64") is None:
        if env.is_symlink():
            raise ValueError("Environment path must not be a symlink")
        env.unlink(missing_ok=True)
    else:
        value = base64.b64decode(previous["env_b64"], validate=True)
        if len(value) > 1024 * 1024:
            raise ValueError("Previous environment is too large")
        _atomic_bytes(env, value)
    _state(root).restore_tls_state(previous["tls_state"])


def _apply_journal_locked(root: Path, journal: dict) -> None:
    try:
        candidate = journal["candidate"]
        target = _selected_target(root, candidate["target"])
        if target != (root / "runtime/tls/current").resolve(strict=True):
            raise ValueError("TLS selection was interrupted before the candidate became active")
        state = _state(root).read()
        if state.get("cert_name") != candidate["cert_name"] or (state.get("domain") or "") != (candidate.get("domain") or ""):
            raise ValueError("TLS state was interrupted before candidate selection completed")
        _restart_selected(root)
    except Exception as activation_error:
        rollback_ok = False
        try:
            _restore_previous(root, journal["previous"])
            _restart_selected(root)
            rollback_ok = True
        except Exception:
            pass
        journal.update(phase="error", rollback_ok=rollback_ok, finished_at=time.time(),
                       error="Certificate activation failed; previous setup restored" if rollback_ok
                       else "Certificate activation failed; recovery required")
        _write_journal(root, journal)
        raise RuntimeError(journal["error"]) from activation_error
    journal.update(phase="applied", rollback_ok=None, applied_at=time.time())
    journal.pop("error", None)
    _write_journal(root, journal)


def reload_tls(root: Path, *, pending_only=False) -> None:
    with _certificate_lock(root):
        journal = _read_journal(root)
        if journal and journal.get("phase") == "pending":
            _apply_journal_locked(root, journal)
        elif journal and journal.get("phase") == "error" and journal.get("rollback_ok") is not True:
            try:
                _restore_previous(root, journal["previous"])
                _restart_selected(root)
            except Exception as exc:
                journal.update(rollback_ok=False, finished_at=time.time(), error="Certificate recovery required")
                _write_journal(root, journal)
                raise RuntimeError("Certificate recovery required") from exc
            journal.update(rollback_ok=True, finished_at=time.time(), error="Previous certificate recovered")
            _write_journal(root, journal)
        elif not pending_only:
            _restart_selected(root)


def renew(root: Path) -> None:
    with _certificate_lock(root):
        journal = _read_journal(root)
        if journal and (journal.get("phase") == "pending" or (journal.get("phase") == "error" and journal.get("rollback_ok") is not True)):
            return
        runtime = root / "runtime"
        # The parent owns the shared operation lock; a child deploy hook would deadlock.
        command = [str(root / "acme-venv/bin/certbot"), "renew", "--non-interactive", "--no-directory-hooks",
                   "--config-dir", str(runtime / "acme-config"), "--work-dir", str(runtime / "acme-work"),
                   "--logs-dir", str(runtime / "acme-logs")]
        subprocess.run(command, check=True, timeout=600,
                       env={**os.environ, "WG_PORTAL_PARENT_RENEWAL": "1"})
        _renew_reload_locked(root)


def _renew_reload_locked(root: Path) -> None:
    sys.path.insert(0, str(root))
    from acme_manager import Certificate, validate_certificate
    runtime = root / "runtime"
    state = _state(root).read()
    name = state.get("cert_name")
    if name not in {"wg-portal-ip", "wg-portal-domain"}:
        return
    subject = state.get("domain") if name == "wg-portal-domain" else state["public_ip"]
    source = runtime / "acme-config/live" / name
    validate_certificate(Certificate(name, subject, source), subject, minimum_hours=1)
    current = runtime / "tls/current"
    previous_target = current.resolve(strict=True)
    if (source / "fullchain.pem").read_bytes() == (previous_target / "fullchain.pem").read_bytes():
        return
    snapshot = runtime / "tls/versions" / secrets.token_hex(16)
    snapshot.mkdir(mode=0o700, parents=True)
    for filename in ("fullchain.pem", "privkey.pem"):
        _atomic_bytes(snapshot / filename, (source / filename).read_bytes())
    validate_certificate(Certificate(name, subject, snapshot), subject, minimum_hours=1)
    env = runtime / "portal.env"
    journal = {"version": 1, "operation_id": secrets.token_hex(16), "phase": "pending", "created_at": time.time(),
               "previous": {"target": str(previous_target),
                            "env_b64": base64.b64encode(env.read_bytes()).decode("ascii") if env.exists() else None,
                            "tls_state": {key: state.get(key) for key in ("domain", "tls_ready", "cert_name", "cert_path")}},
               "candidate": {"target": str(snapshot), "domain": state.get("domain"), "cert_name": name}}
    _write_journal(root, journal)
    _select_target(root, snapshot)
    _apply_journal_locked(root, journal)


def renew_reload_tls(root: Path) -> None:
    # A stale saved Certbot deploy hook must not reacquire its parent's flock.
    if os.environ.get("WG_PORTAL_PARENT_RENEWAL") == "1":
        return
    with _certificate_lock(root):
        journal = _read_journal(root)
        if journal and (journal.get("phase") == "pending" or (journal.get("phase") == "error" and journal.get("rollback_ok") is not True)):
            return
        _renew_reload_locked(root)


def start_telegram(root: Path, unit_file: Path | None = None) -> None:
    unit = unit_file or Path("/etc/systemd/system/wg-panel-bot.service")
    expected = "WorkingDirectory=" + str(root / "vendor/WG_Panel")
    if not unit.is_file() or expected not in unit.read_text(encoding="utf-8").splitlines():
        raise ValueError("Telegram service does not belong to this WG Portal installation")
    if os.name != "nt":
        assert_secure_ancestry(unit)
    subprocess.run(["systemctl", "enable", "--now", "wg-panel-bot.service"], check=True, timeout=60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("guard", "schedule", "reload", "recover", "renew", "renew-reload", "start-telegram"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = Path(os.path.abspath(args.root))
    if os.name != "nt" and os.geteuid() != 0:
        parser.error("Root privileges are required")
    if os.name != "nt":
        assert_secure_ancestry(Path(__file__))
    assert_secure_checkout(root)
    if args.action == "guard":
        return
    if args.action == "schedule":
        subprocess.run(["systemd-run", "--quiet", "--collect", "--unit=wgportal-tls-reload-" + secrets.token_hex(6),
                        "--on-active=2s", "/usr/local/lib/wgportal/apply-pending-tls"], check=True, timeout=15)
    elif args.action in {"reload", "recover"}:
        reload_tls(root, pending_only=args.action == "recover")
    elif args.action == "renew":
        renew(root)
    elif args.action == "renew-reload":
        renew_reload_tls(root)
    else:
        start_telegram(root)


if __name__ == "__main__":
    main()
