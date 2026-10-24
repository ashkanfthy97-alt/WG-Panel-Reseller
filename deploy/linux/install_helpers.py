"""Conservative, testable helpers used by the Linux installer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile


_BIND_RE = re.compile(r"^(?:127\.0\.0\.1|localhost):([0-9]{1,5})$")


def update_env_text(source: str, updates: dict[str, str]) -> str:
    """Update named keys without altering unrelated secrets or ambiguous input."""
    existing = _env_values(source)
    for key, value in updates.items():
        if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key) or not isinstance(value, str) or any(c in value for c in '\r\n\0'):
            raise ValueError('Environment updates require named single-line values')
        if len(set(existing.get(key, []))) > 1:
            raise ValueError('Conflicting duplicate environment values require explicit recovery')
    def encoded(key):
        value = updates[key]
        return key + '=' + (value if re.fullmatch(r'[A-Za-z0-9_./:@,+\[\]-]*', value) else json.dumps(value, ensure_ascii=False))
    lines, written = [], set()
    for line in source.splitlines():
        entry = line.strip().removeprefix('export ')
        key = entry.split('=', 1)[0].strip() if '=' in entry and not entry.startswith('#') else ''
        if key in updates:
            if key not in written:
                lines.append(encoded(key))
                written.add(key)
        else:
            lines.append(line)
    lines.extend(encoded(key) for key in updates if key not in written)
    return '\n'.join(lines).rstrip() + '\n'


def _env_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.setdefault(key, []).append(value)
    return values


def _parse_loopback_bind(value: str) -> tuple[str, int]:
    value = value.strip()
    match = _BIND_RE.fullmatch(value)
    if not match:
        raise ValueError("Panel BIND must use localhost or 127.0.0.1 and a port; public binds are refused.")
    port = int(match.group(1))
    if not 1024 <= port <= 65535:
        raise ValueError("Panel port must be between 1024 and 65535.")
    host = value.rsplit(":", 1)[0]
    return host, port


def migrate_env_text(text: str, requested_port: int, port_explicit: bool = False) -> tuple[str, int]:
    """Preserve all existing dotenv lines while mapping the old bind key safely."""
    if not 1024 <= int(requested_port) <= 65535:
        raise ValueError("Requested panel port must be between 1024 and 65535.")
    values = _env_values(text)
    modern = values.get("BIND", [])
    legacy = values.get("WG_PANEL_BIND", [])
    if len(set(modern)) > 1 or len(set(legacy)) > 1:
        raise ValueError("Conflicting duplicate panel bind keys; resolve them manually before installing.")
    if modern and legacy and modern[-1] != legacy[-1]:
        raise ValueError("BIND and WG_PANEL_BIND disagree; refusing an ambiguous migration.")
    existing = modern[-1] if modern else (legacy[-1] if legacy else "")
    if existing:
        _, selected_port = _parse_loopback_bind(existing)
        if port_explicit and selected_port != int(requested_port):
            raise ValueError(f"Existing panel port is {selected_port}; explicitly requested port is {requested_port}.")
        if modern:
            return text, selected_port
        separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
        return text + separator + f"BIND=127.0.0.1:{selected_port}\n", selected_port
    separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
    return text + separator + f"BIND=127.0.0.1:{int(requested_port)}\n", int(requested_port)


def runtime_error(version: tuple[int, ...], system: str, machine: str) -> str | None:
    if system != "Linux":
        return "Supported installer platform: Linux x86_64."
    if machine.lower() not in {"x86_64", "amd64"}:
        return "The dependency lock currently supports x86_64 Linux only."
    if tuple(version[:2]) != (3, 12):
        current = ".".join(str(part) for part in version[:2])
        return f"CPython {current} is unsupported; the dependency lock is pinned for CPython 3.12."
    return None


def migrate_env_file(destination: Path, fallback: Path | None, requested_port: int, port_explicit: bool) -> int:
    destination = Path(destination)
    fallback = Path(fallback) if fallback else None
    source = destination if destination.is_file() else fallback if fallback and fallback.is_file() else None
    if source is None:
        raise FileNotFoundError("No existing panel environment file was found; create the initial file first.")
    original = source.read_text(encoding="utf-8")
    migrated, port = migrate_env_text(original, requested_port, port_explicit)
    if destination.is_file() and migrated == original:
        os.chmod(destination, 0o600)
        return port

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        backup = destination.with_name(destination.name + ".pre-wg-portal")
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = destination.with_name(destination.name + f".pre-wg-portal.{suffix}")
        shutil.copy2(destination, backup)
        os.chmod(backup, 0o600)

    fd, temporary = tempfile.mkstemp(prefix=".env-portal-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(migrated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return port


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-runtime")
    migrate = sub.add_parser("migrate-env")
    migrate.add_argument("destination", type=Path)
    migrate.add_argument("--fallback", type=Path)
    migrate.add_argument("--port", type=int, required=True)
    migrate.add_argument("--port-explicit", action="store_true")
    args = parser.parse_args()
    if args.command == "check-runtime":
        error = runtime_error(tuple(os.sys.version_info), platform.system(), platform.machine())
        if error:
            parser.exit(1, error + "\n")
        print(f"Supported runtime: CPython {os.sys.version_info.major}.{os.sys.version_info.minor} on {platform.machine()}.")
        return 0
    if args.command == "migrate-env":
        port = migrate_env_file(args.destination, args.fallback, args.port, args.port_explicit)
        print(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
