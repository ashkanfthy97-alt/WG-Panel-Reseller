#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
SITE="$(cd -- "$ROOT/../.." && pwd)"
VENV="$ROOT/venv"
UPSTREAM_URL="https://github.com/Azumi67/WG_Panel.git"
UPSTREAM_COMMIT="f0f41068e1fd453d17acad23b9b90bc2a284bf59"
MODE=""
PANEL_PORT=8766
PORTAL_PORT=8765

usage() {
  cat <<'EOF'
Usage: bash install.sh --managed [--panel-port 8766] [--portal-port 8765]
       bash install.sh --existing-port PORT [--portal-port 8765]
Installs OS packages, Python venv and systemd services. No Docker or port 443.
EOF
}
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1024 && 10#$1 <= 65535 )); }

while (($#)); do
  case "$1" in
    --managed) MODE=managed; shift ;;
    --existing-port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PANEL_PORT="$2"; MODE=existing; shift 2 ;;
    --panel-port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PANEL_PORT="$2"; shift 2 ;;
    --portal-port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PORTAL_PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == Linux ]] || { echo "Linux is required" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo "Run as root" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "Ubuntu/Debian with apt-get is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
[[ -n "$MODE" ]] || { usage >&2; exit 2; }
valid_port "$PANEL_PORT" && valid_port "$PORTAL_PORT" || { echo "Invalid port" >&2; exit 2; }
[[ "$PANEL_PORT" != "$PORTAL_PORT" ]] || { echo "Ports must differ" >&2; exit 2; }
[[ -f "$SITE/dist/index.html" && -f "$SITE/dist/app.html" ]] || { echo "Built site files are missing" >&2; exit 1; }

packages="ca-certificates curl git openssl python3 python3-venv python3-pip"
if [[ "$MODE" == managed ]]; then
  packages="$packages python3-dev build-essential pkg-config libffi-dev libssl-dev wireguard wireguard-tools iproute2 iptables procps"
fi
missing=""
for package in $packages; do
  if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
    missing="$missing $package"
  fi
done
if [[ -n "$missing" ]]; then
  echo "Installing OS packages:$missing"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -o Acquire::Retries=3
  apt-get install -y --no-install-recommends $missing
fi

if [[ "$MODE" == managed ]]; then
  VENDOR="$ROOT/vendor/WG_Panel"
  if [[ ! -d "$VENDOR/.git" ]]; then
    mkdir -p "$ROOT/vendor"
    git clone --filter=blob:none --no-checkout "$UPSTREAM_URL" "$VENDOR"
    git -C "$VENDOR" checkout --detach "$UPSTREAM_COMMIT"
  fi
  [[ "$(git -C "$VENDOR" rev-parse HEAD)" == "$UPSTREAM_COMMIT" ]] || {
    echo "WG_Panel is not at the pinned revision; refusing to overwrite it" >&2
    exit 1
  }
fi

[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
if [[ "$MODE" == managed ]]; then
  "$VENV/bin/python" -m pip install -r "$VENDOR/requirements.txt"
else
  "$VENV/bin/python" -m pip install Flask requests gunicorn
fi

mkdir -p "$ROOT/runtime/site/assets"
cp -a "$SITE/dist/." "$ROOT/runtime/site/"
printf '%s\n' 'window.WG_PORTAL_RUNTIME = Object.freeze({ mode: "wgpanel" });' > "$ROOT/runtime/site/assets/runtime-config.js"

if [[ "$MODE" == managed ]]; then
  if [[ ! -f "$ROOT/.env" ]]; then
    umask 077
    {
      printf 'BIND=127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'DATABASE_URL=sqlite:///instance/wg_panel.db\n'
      printf 'WIREGUARD_CONF_PATH=/etc/wireguard\n'
      printf 'FLASK_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'FERNET_KEY=%s\n' "$("$VENV/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
      printf 'API_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'SETUP_TOKEN=%s\n' "$(openssl rand -hex 24)"
      printf 'PANEL_BASE_URL=http://127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'PANEL=http://127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'SECURE_COOKIES=0\nWORKERS=2\nTHREADS=4\nTG_BOT_TOKEN=\n'
    } > "$ROOT/.env"
  fi
  chmod 600 "$ROOT/.env"
  configured_port="$(sed -n 's/^BIND=127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p' "$ROOT/.env" | head -n 1)"
  [[ "$configured_port" == "$PANEL_PORT" ]] || {
    echo "Existing .env uses another panel port; rerun with --panel-port $configured_port" >&2
    exit 1
  }
  cat > /etc/systemd/system/wg-portal-panel.service <<EOF
[Unit]
Description=WG Portal managed WG_Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$VENDOR
EnvironmentFile=$ROOT/.env
ExecStart=$VENV/bin/python $VENDOR/app.py
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  cat > /etc/systemd/system/wg-portal-bot.service <<EOF
[Unit]
Description=WG Portal optional Telegram bot
After=wg-portal-panel.service
Requires=wg-portal-panel.service

[Service]
Type=simple
User=root
WorkingDirectory=$VENDOR
EnvironmentFile=$ROOT/.env
ExecStart=$VENV/bin/python $VENDOR/telegram_bot.py
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
fi

cat > /etc/systemd/system/wg-portal.service <<EOF
[Unit]
Description=WG Portal frontend and WG_Panel gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$ROOT
Environment=WG_PORTAL_SITE_ROOT=$ROOT/runtime/site
Environment=WG_PORTAL_PANEL_URL=http://127.0.0.1:$PANEL_PORT
ExecStart=$VENV/bin/gunicorn --bind 127.0.0.1:$PORTAL_PORT --workers 2 --threads 4 --timeout 120 portal_proxy:app
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
if [[ "$MODE" == managed ]]; then
  systemctl enable wg-portal-panel.service
  systemctl restart wg-portal-panel.service
fi
systemctl enable wg-portal.service
systemctl restart wg-portal.service

wait_url() {
  local url="$1"
  for _ in $(seq 1 90); do
    if curl --fail --silent --output /dev/null "$url"; then return 0; fi
    sleep 1
  done
  return 1
}
if [[ "$MODE" == managed ]]; then
  wait_url "http://127.0.0.1:$PANEL_PORT/login" || {
    journalctl -u wg-portal-panel.service -n 30 --no-pager >&2
    exit 1
  }
fi
wait_url "http://127.0.0.1:$PORTAL_PORT/" || {
  journalctl -u wg-portal.service -n 30 --no-pager >&2
  exit 1
}
printf '\nPortal: http://127.0.0.1:%s/\n' "$PORTAL_PORT"
printf 'WireGuard controls: http://127.0.0.1:%s/users\n' "$PORTAL_PORT"
printf 'Both listeners are local only; use your existing HTTPS reverse proxy for public access.\n'
