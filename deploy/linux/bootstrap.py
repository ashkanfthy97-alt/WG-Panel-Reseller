"""Root-only first-run preparation; never prints or accepts the setup token in argv."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import getpass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from urllib.request import ProxyHandler, build_opener


def atomic_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".wgportal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_env(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    return {key: value or "" for key, value in dotenv_values(path, interpolate=False).items()}


def update_env(path: Path, values: dict[str, str]) -> None:
    """Replace only named keys while retaining unknown values and comments."""
    from install_helpers import update_env_text
    source = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_private(path, update_env_text(source, values))


def ensure_token(path: Path, prompt=None) -> str:
    values = load_env(path)
    token = values.get("SETUP_TOKEN", "")
    if token:
        if not 32 <= len(token) <= 256 or token != token.strip() or any(c in token for c in '\r\n\0'):
            raise ValueError("Existing SETUP_TOKEN is shorter than 32 characters; use controlled recovery rather than overwriting it")
        return token
    if prompt is None:
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                token = getpass.getpass("کد نصب اولیه (حداقل ۳۲ نویسه؛ آن را برای مرورگر نگه دارید): ", stream=terminal)
        except OSError as exc:
            raise ValueError("An interactive /dev/tty is required once to enter the first-run token") from exc
    else:
        token = prompt()
    if len(token) < 32 or len(token) > 256 or token != token.strip() or any(character in token for character in "\r\n\0"):
        raise ValueError("The first-run token must contain 32–256 characters")
    update_env(path, {"SETUP_TOKEN": token})
    return token


def public_ipv4(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if address.version != 4 or not address.is_global:
        raise ValueError("A globally routable IPv4 address is required")
    return str(address)


def detect_public_ip(override: str = "", runner=subprocess.run, opener=None) -> str:
    if override:
        return public_ipv4(override)
    try:
        result = runner(["ip", "-j", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5, check=True)
        for route in json.loads(result.stdout):
            try:
                return public_ipv4(route.get("prefsrc") or route.get("src") or "")
            except ValueError:
                continue
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    client = opener or build_opener(ProxyHandler({}))
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with client.open(url, timeout=8) as response:
                return public_ipv4(response.read(128).decode("ascii").strip())
        except (OSError, ValueError, UnicodeError):
            continue
    raise ValueError("Public IPv4 discovery failed; set WG_PORTAL_PUBLIC_IP explicitly and retry")


def bootstrap_certificate(runtime: Path, public_ip: str) -> tuple[Path, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from acme_manager import Certificate, validate_certificate

    current = runtime / "tls" / "current"
    selected = current.resolve(strict=True) if current.is_symlink() else None
    regenerate = selected is None
    if selected is not None:
        try:
            validate_certificate(Certificate("selected", public_ip, selected), public_ip, minimum_hours=1)
        except (OSError, ValueError):
            if not selected.is_relative_to(runtime / "tls") or not selected.name.startswith("bootstrap-ip"):
                raise
            regenerate = True
    if regenerate:
        if current.exists() and not current.is_symlink():
            raise ValueError("TLS current path already exists and is not a managed symlink")
        directory = runtime / "tls" / ("bootstrap-ip-" + secrets.token_hex(8))
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, public_ip)])
        certificate = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(public_ip))]), critical=False)
            .sign(key, hashes.SHA256())
        )
        atomic_private(directory / "privkey.pem", key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ).decode("ascii"))
        atomic_private(directory / "fullchain.pem", certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"))
        temporary = current.with_name(".current-" + secrets.token_hex(6))
        temporary.symlink_to(directory, target_is_directory=True)
        os.replace(temporary, current)
    selected = current.resolve(strict=True)
    validate_certificate(Certificate("selected", public_ip, selected), public_ip, minimum_hours=1)
    cert = x509.load_pem_x509_certificate((selected / "fullchain.pem").read_bytes())
    fingerprint = ":".join(f"{value:02X}" for value in cert.fingerprint(hashes.SHA256()))
    return current, fingerprint


def prepare(root: Path, panel_env: Path, panel_port: int, portal_port: int, http_port: int, public_ip: str = "") -> dict:
    from setup_state import SetupState
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    token = ensure_token(panel_env)
    values = load_env(panel_env)
    if len(values.get("FLASK_SECRET_KEY", "")) < 32 or not values.get("API_KEY"):
        raise ValueError("Existing panel environment must provide a strong FLASK_SECRET_KEY and API_KEY")
    address = detect_public_ip(public_ip)
    state = SetupState(runtime / "setup.sqlite3")
    state.initialize(token, address)
    previous = state.read()
    current = runtime / "tls" / "current"
    if previous.get("cert_name") in {"wg-portal-ip", "wg-portal-domain"}:
        from acme_manager import Certificate, validate_certificate
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        if not current.is_symlink():
            raise ValueError('The configured public certificate is unavailable; use certificate recovery rather than replacing it with bootstrap TLS')
        subject = previous['domain'] if previous['cert_name'] == 'wg-portal-domain' else address
        validate_certificate(Certificate("selected", subject, current.resolve()), subject, minimum_hours=1)
        validate_certificate(Certificate("selected", subject, current.resolve()), address, minimum_hours=1)
        cert = x509.load_pem_x509_certificate((current / "fullchain.pem").read_bytes())
        fingerprint = ":".join(f"{value:02X}" for value in cert.fingerprint(hashes.SHA256()))
    else:
        current, fingerprint = bootstrap_certificate(runtime, address)
        if previous["phase"] != "complete" and not previous.get("tls_ready"):
            state.mark_tls_ready("wg-portal-bootstrap", str(current))
    paths = {
        "WG_PORTAL_SETUP_ROOT": str(root), "WG_PORTAL_RUNTIME": str(runtime),
        "WG_PORTAL_SITE_ROOT": str(runtime / "site"),
        "WG_PORTAL_SETUP_DB": str(runtime / "setup.sqlite3"),
        "WG_PORTAL_RESELLER_DB": str(runtime / "resellers.sqlite3"),
        "WG_PORTAL_ENV_FILE": str(runtime / "portal.env"),
        "WG_PORTAL_TLS_CURRENT": str(current),
        "WG_PORTAL_ACME_WEBROOT": str(runtime / "acme-webroot"),
        "WG_PORTAL_ACME_CONFIG_DIR": str(runtime / "acme-config"),
        "WG_PORTAL_ACME_WORK_DIR": str(runtime / "acme-work"),
        "WG_PORTAL_ACME_LOGS_DIR": str(runtime / "acme-logs"),
        "WG_PORTAL_CERTBOT": str(root / "acme-venv" / "bin" / "certbot"),
        "WG_PORTAL_PANEL_URL": f"http://127.0.0.1:{panel_port}",
        "WG_PORTAL_GATEWAY_URL": f"http://127.0.0.1:{portal_port}",
        "WG_PORTAL_PUBLIC_IP": address, "WG_PORTAL_HTTP_PORT": str(http_port),
        "WG_PORTAL_HTTPS_PORT": "8767", "FLASK_SECRET_KEY": values["FLASK_SECRET_KEY"],
        "API_KEY": values["API_KEY"], "SETUP_TOKEN": token,
    }
    for directory in ("acme-webroot/.well-known/acme-challenge", "acme-config", "acme-work", "acme-logs"):
        (runtime / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    update_env(runtime / "setup.env", paths)
    hosts = load_env(runtime / "portal.env").get("WG_PORTAL_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    hosts.extend([address, "localhost", "127.0.0.1", "[::1]"])
    if previous.get("domain"):
        hosts.append(previous["domain"])
    update_env(runtime / "portal.env", {"WG_PORTAL_ALLOWED_HOSTS": ",".join(sorted(set(hosts)))})
    summary = {"public_ip": address, "http_port": http_port, "https_port": 8767, "fingerprint": fingerprint}
    atomic_private(runtime / "bootstrap-summary.json", json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--panel-env", type=Path, required=True)
    parser.add_argument("--panel-port", type=int, default=8766)
    parser.add_argument("--portal-port", type=int, default=8765)
    parser.add_argument("--http-port", type=int, choices=(80, 8768), default=80)
    args = parser.parse_args()
    if os.name != "nt":
        # Validate the guard module itself and its ancestry before importing any checkout code.
        import importlib.util
        import stat
        root = Path(os.path.abspath(args.root))
        guard = root / "service_helpers/tls_service.py"
        for value in (root, guard, args.panel_env):
            absolute = Path(os.path.abspath(value))
            for path in reversed((absolute, *absolute.parents)):
                if not path.exists() and not path.is_symlink():
                    continue
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    parser.error("Unsafe privileged path: " + str(path))
        spec = importlib.util.spec_from_file_location("wgportal_bootstrap_guard", guard)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.assert_secure_checkout(root)
    try:
        summary = prepare(args.root.resolve(), args.panel_env.resolve(), args.panel_port, args.portal_port,
                          args.http_port, os.environ.get("WG_PORTAL_PUBLIC_IP", ""))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
