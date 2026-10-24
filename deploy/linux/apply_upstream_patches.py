"""Apply reviewed product changes to a pinned upstream checkout.

Preflights every file before writing; refuses unknown local edits.
Never reads or replaces .env, the database or WireGuard configurations.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

REVISION = "f0f41068e1fd453d17acad23b9b90bc2a284bf59"
HERE = Path(__file__).resolve().parent


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError("Upstream anchor changed: " + before[:70])
    return text.replace(before, after, 1)


def digest(text):
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode("utf-8")).hexdigest()


def atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".portal-", dir=path.parent)
    try:
        options = {} if isinstance(content, bytes) else dict(encoding='utf-8', newline='\n')
        with os.fdopen(fd, 'wb' if isinstance(content, bytes) else 'w', **options) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(root):
    def original(path):
        return subprocess.check_output(["git", "-C", str(root), "show", f"{REVISION}:{path}"]).decode("utf-8").replace("\r\n", "\n")

    config = replace_once(original("config.py"), 'import os\n',
                          'import os\nfrom portal_security import require_secret\n')
    config = replace_once(config, 'os.environ.get("FLASK_SECRET_KEY", "change-me")',
                          'require_secret("FLASK_SECRET_KEY")')
    app = original("app.py")
    app = replace_once(app,
        "    r = requests.get(\n        url,\n        headers={'Authorization': f'Bearer {_read_api_key(node)}'},\n        timeout=timeout,\n    )",
        "    r = _node_client.get_response(node.base_url, '/api/backup/wg', _read_api_key(node), timeout=timeout)")
    app = replace_once(app,
        "            response = requests.post(\n                url,\n                headers={\n"
        "                    'Authorization': (\n                        f'Bearer {_read_api_key(node)}'\n"
        "                    ),\n                    'Content-Type': 'application/json',\n"
        "                },\n                json={",
        "            response = _node_client.post_response(\n"
        "                node.base_url, '/api/backup/wg/restore', _read_api_key(node),\n"
        "                payload={")
    app = replace_once(app, "    require_token = bool(setup_token)",
                       "    require_token = True")
    app = replace_once(app, "        if len(p1) > 1024:",
                       "        if len(p1.strip()) < 12:\n"
                       "            flash('Use at least 12 characters for the admin password.', 'error')\n"
                       "            return render_template('register.html', require_token=True), 400\n"
                       "        if len(p1) > 1024:")
    app = replace_once(app, '    if not new:\n        return jsonify(error="empty_new"), 400',
                       '    if not 12 <= len(new) <= 1024:\n'
                       '        return jsonify(error="password_length", message="Use 12 to 1024 characters."), 400')
    app = replace_once(app, 'x_host=1, x_port=1)', 'x_host=1, x_port=0)')
    app = replace_once(app, 'if __name__ == "__main__":',
                       'from portal_security import install_security\n'
                       'install_security(app, csrf)\n'
                       'from portal_peer_guard import install_peer_guard\n'
                       'install_peer_guard(app, csrf, globals())\n\nif __name__ == "__main__":')
    app = replace_once(app, "                'id': peer.id,\n\n                'shortlink': shortlink_url or '',",
                       "                'id': peer.id,\n                'public_key': peer.public_key,\n"
                       "                'iface_id': peer.iface_id,\n\n                'shortlink': shortlink_url or '',")
    # Keep the upstream sync concurrency model. Do not claim THREADS is effective.
    app = replace_once(app, '    threads          = _env_int("THREADS", 4)',
                       '    # WG Portal: sync workers deliberately do not consume THREADS.')
    wg = replace_once(original("wg.py"), "def update_menu():\n",
                      'def update_menu():\n'
                      '    warn("Managed by WG Portal: update its repository and run deploy/linux/install.sh.")\n'
                      '    pause()\n    return\n')
    updater = replace_once(original("scripts/panel_update.py"),
                           'from __future__ import annotations\n',
                           'from __future__ import annotations\n'
                           'raise SystemExit("Managed WG Portal installation: use the product installer to update.")\n')
    changes = {
        "config.py": config,
        "app.py": app,
        "wg.py": wg,
        "scripts/panel_update.py": updater,
        "portal_security.py": (HERE / "overlays/portal_security.py").read_text(encoding="utf-8"),
        "portal_peer_guard.py": (HERE / "overlays/portal_peer_guard.py").read_text(encoding="utf-8"),
        "panel_core/node_client.py": (HERE / "overlays/node_client.py").read_text(encoding="utf-8"),
        "requirements.txt": (HERE / "requirements.lock").read_text(encoding="utf-8"),
        "install-gui.sh": '#!/usr/bin/env bash\nset -euo pipefail\n'
            'echo "WG Portal: install once, then complete setup in your browser."\n'
            f'exec bash {shlex.quote(str(HERE / "install.sh"))}\n',
    }
    added = {'portal_security.py', 'portal_peer_guard.py'}
    for path in ('static/css/portal-theme.css', 'static/js/portal-locale.js'):
        changes[path] = (HERE / 'overlays' / path).read_text(encoding='utf-8')
        added.add(path)
    for filename in ('Vazirmatn-wght.woff2', 'OFL.txt'):
        destination = 'static/fonts/' + ('portal-vazirmatn.woff2' if filename.endswith('.woff2') else 'portal-vazirmatn-OFL.txt')
        changes[destination] = (HERE.parents[1] / 'dist/assets/fonts' / filename).read_bytes()
        added.add(destination)
    # Skin native templates, preserving their forms, IDs and existing scripts.
    for name in ('base.html', 'login.html', 'register.html', 'user_peer.html',
                 'user_peer_compact.html', 'user_peer_minimal.html', 'user_peer_pro.html', 'subscription_public.html'):
        path = 'templates/' + name
        template = original(path)
        template = replace_once(template, '</head>',
            '<link rel="stylesheet" href="/static/css/portal-theme.css"></head>')
        template = replace_once(template, '</body>',
            '<script defer src="/static/js/portal-locale.js"></script></body>')
        changes[path] = template
    for name, directory, entry in [
        ("bot.service", root, "telegram_bot.py"),
        ("wg-node-agent.service", root / "agent", "node_agent.py"),
    ]:
        changes["systemd/" + name] = (
            "[Unit]\nDescription=WG Portal " + name + "\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\nUser=root\n"
            f"WorkingDirectory={directory}\nEnvironmentFile={directory}/.env\n"
            f"ExecStart={directory}/venv/bin/python {directory}/{entry}\n"
            "Restart=on-failure\nRestartSec=5\nUMask=0077\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
    originals = {}
    for path, content in changes.items():
        if path.endswith(".py"):
            ast.parse(content, filename=path)
        originals[path] = None if path in added else original(path)
    return changes, originals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vendor", type=Path)
    parser.add_argument("--check", action="store_true", help="Read-only patch preflight")
    args = parser.parse_args()
    root = args.vendor.resolve()
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if head != REVISION:
        raise SystemExit("Unsupported upstream revision; refusing to overwrite it.")
    changes, originals = prepare(root)
    marker = root / ".wg-portal-patches.json"
    previous = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    saved = {}
    for rel, target in changes.items():
        path = root / rel
        if path.is_symlink():
            raise SystemExit("Refusing symlink: " + rel)
        current = (path.read_bytes() if isinstance(target, bytes) else path.read_text(encoding="utf-8")) if path.exists() else None
        expected_hash = previous.get("files", {}).get(rel)
        if current not in (originals[rel], target) and (current is None or digest(current) != expected_hash):
            raise SystemExit("Unrecognised local edit; preserve and review before updating: " + rel)
        saved[rel] = current
    if args.check:
        print(f"Patch preflight: {len(changes)} files, no writes.")
        return
    written = []
    try:
        for rel, content in changes.items():
            atomic_write(root / rel, content, 0o755 if rel.endswith(".sh") else 0o644)
            written.append(rel)
        atomic_write(marker, json.dumps({"revision": REVISION,
                     "files": {p: digest(c) for p, c in changes.items()}}, indent=2) + "\n")
    except BaseException:
        for rel in reversed(written):
            if saved[rel] is None:
                (root / rel).unlink(missing_ok=True)
            else:
                atomic_write(root / rel, saved[rel], 0o755 if rel.endswith(".sh") else 0o644)
        raise
    print(f"Applied WG Portal policy to {len(changes)} upstream files.")


if __name__ == "__main__":
    main()
