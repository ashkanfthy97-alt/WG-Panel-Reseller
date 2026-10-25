#!/usr/bin/env bash
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
SOURCE_PATH="$(/usr/bin/realpath -ms -- "${BASH_SOURCE[0]}")"
ROOT="$(/usr/bin/dirname -- "$SOURCE_PATH")"
SITE="$(/usr/bin/realpath -ms -- "$ROOT/../..")"
[[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || { echo "Run on Linux as root" >&2; exit 1; }
[[ -x /usr/bin/python3 ]] || { echo "Ubuntu's system Python is required for safe path preflight" >&2; exit 1; }
# Isolated system Python cannot load checkout/sitecustomize/PYTHONPATH code here.
/usr/bin/python3 -I -S - "$SITE" "$ROOT" "$SOURCE_PATH" <<'PY'
import importlib.util, os, pathlib, stat, sys
def protected(value):
    path = pathlib.Path(os.path.abspath(value))
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit("Unsafe privileged path; use a root-owned non-writable checkout without symlink ancestry: " + str(item))
site, root, script = map(pathlib.Path, sys.argv[1:])
helper = root / "service_helpers/tls_service.py"
for value in (site, root, script, helper, pathlib.Path('/usr/local/lib/wgportal'),
              pathlib.Path('/etc/systemd/system'), pathlib.Path('/usr/local/bin/wgportal')):
    protected(value)
spec = importlib.util.spec_from_file_location('wgportal_path_guard', helper)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.assert_secure_checkout(root)
PY
VENV="$ROOT/venv"
UPSTREAM_URL="https://github.com/Azumi67/WG_Panel.git"
UPSTREAM_COMMIT="f0f41068e1fd453d17acad23b9b90bc2a284bf59"
MODE="managed"
PANEL_PORT_EXPLICIT=0
PANEL_PORT=8766
PORTAL_PORT=8765
SETUP_PORT=8767
HTTP_PORT=80
if [[ -f "$ROOT/runtime/install.conf" ]]; then
  while IFS='=' read -r key value; do
    case "$key:$value" in
      PORTAL_PORT:*) [[ "$value" =~ ^[0-9]+$ ]] && PORTAL_PORT="$value" ;;
      PANEL_PORT:*) [[ "$value" =~ ^[0-9]+$ ]] && PANEL_PORT="$value" ;;
    esac
  done < "$ROOT/runtime/install.conf"
fi
trap 'echo "Installation stopped at line $LINENO. Review the error above; configuration was not reset." >&2' ERR
step() { printf '\n[%s/6] %s\n' "$1" "$2"; }

usage() {
  cat <<'EOF'
Usage: bash install.sh
       bash install.sh --managed [--panel-port 8766] [--portal-port 8765]
Ubuntu 24.04 x86_64, root, Python 3.12 and systemd. No Docker or port 443.
Fresh installs ask only for a hidden first-run token; remaining setup is in the browser.
Unpatched existing-panel installations are not supported by this browser-setup release.
EOF
}
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1024 && 10#$1 <= 65535 )); }

while (($#)); do
  case "$1" in
    --managed) MODE=managed; shift ;;
    --existing-port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PANEL_PORT="$2"; MODE=existing; shift 2 ;;
    --panel-port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PANEL_PORT="$2"; PANEL_PORT_EXPLICIT=1; shift 2 ;;
    --portal-port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PORTAL_PORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$MODE" == managed ]] || {
  echo "This browser-setup release supports managed installations only. An unpatched existing panel is not compatible with scoped reseller actions or the managed Telegram service; its files and services were not changed." >&2
  exit 2
}

[[ "$(uname -s)" == Linux ]] || { echo "Linux is required" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo "Run as root" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "Ubuntu 24.04 with apt-get is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
[[ -r /etc/os-release ]] || { echo "Cannot identify the operating system" >&2; exit 1; }
# shellcheck disable=SC1091
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || {
  echo "Supported installation target: Ubuntu 24.04 x86_64 with its native Python 3.12. No third-party PPA is added." >&2; exit 1;
}
[[ "$(uname -m)" == x86_64 ]] || { echo "Linux x86_64 is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "Ubuntu's python3 interpreter is required" >&2; exit 1; }
python3 "$ROOT/install_helpers.py" check-runtime
command -v ss >/dev/null || { echo "The ss utility (iproute2) is required for safe listener preflight" >&2; exit 1; }
valid_port "$PANEL_PORT" && valid_port "$PORTAL_PORT" || { echo "Invalid port" >&2; exit 2; }
[[ "$PANEL_PORT" != "$PORTAL_PORT" ]] || { echo "Ports must differ" >&2; exit 2; }
[[ "$PANEL_PORT" != "$SETUP_PORT" && "$PORTAL_PORT" != "$SETUP_PORT" ]] || { echo "Port 8767 is reserved for HTTPS setup" >&2; exit 2; }
[[ -f "$SITE/dist/index.html" && -f "$SITE/dist/app.html" ]] || { echo "Built site files are missing" >&2; exit 1; }
VENDOR="$ROOT/vendor/WG_Panel"
if [[ "$MODE" == managed ]]; then
  prior_env="$VENDOR/.env"
  [[ -f "$prior_env" ]] || prior_env="$ROOT/.env"
  if [[ -f "$prior_env" ]]; then
    PANEL_PORT="$(python3 -c 'import pathlib,sys; sys.path.insert(0,sys.argv[1]); from install_helpers import migrate_env_text; print(migrate_env_text(pathlib.Path(sys.argv[2]).read_text(),int(sys.argv[3]),sys.argv[4]=="1")[1])' "$ROOT" "$prior_env" "$PANEL_PORT" "$PANEL_PORT_EXPLICIT")"
    [[ "$PANEL_PORT" != "$PORTAL_PORT" && "$PANEL_PORT" != "$SETUP_PORT" ]] || { echo "Existing panel bind conflicts with another product port" >&2; exit 1; }
  fi
fi
# Reject unrelated service ownership before moving config or stopping anything.
for unit in wg-portal wg-panel wg-panel-bot wg-portal-panel wg-portal-bot wg-portal-setup wg-portal-http wg-portal-acme-renew wg-portal-tls-recover; do
  [[ "$MODE" == existing && ( "$unit" == wg-panel || "$unit" == wg-panel-bot || "$unit" == wg-portal-panel || "$unit" == wg-portal-bot ) ]] && continue
  expected="$VENDOR"
  [[ "$unit" != wg-panel && "$unit" != wg-panel-bot && "$unit" != wg-portal-panel && "$unit" != wg-portal-bot ]] && expected="$ROOT"
  unit_file="$(systemctl show -p FragmentPath --value "$unit.service" 2>/dev/null || true)"
  [[ ! -L "/etc/systemd/system/$unit.service" ]] || { echo "Refusing symlinked service unit $unit" >&2; exit 1; }
  [[ -f "/etc/systemd/system/$unit.service" ]] && unit_file="/etc/systemd/system/$unit.service"
  if [[ -n "$unit_file" && -f "$unit_file" ]] &&
     ! grep -Fxq "WorkingDirectory=$expected" "$unit_file"; then
    echo "$unit belongs to another installation. No services were changed." >&2
    exit 1
  fi
done
for timer in wg-portal-acme-renew wg-portal-tls-recover; do
  timer_file="$(systemctl show -p FragmentPath --value "$timer.timer" 2>/dev/null || true)"
  [[ ! -L "/etc/systemd/system/$timer.timer" ]] || { echo "Refusing symlinked product timer" >&2; exit 1; }
  [[ -f "/etc/systemd/system/$timer.timer" ]] && timer_file="/etc/systemd/system/$timer.timer"
  description='Description=WG Portal certificate renewal every six hours'
  [[ "$timer" == wg-portal-tls-recover ]] && description='Description=WG Portal interrupted TLS operation recovery timer'
  if [[ -n "$timer_file" && -f "$timer_file" ]] && ! grep -Fxq "$description" "$timer_file"; then
    echo "Timer name belongs to another installation; no services were changed" >&2; exit 1
  fi
done
listener_owned() {
  local port="$1" unit="$2" listeners group pid
  listeners="$(ss -ltnpH "sport = :$port")"
  [[ -z "$listeners" ]] && return 0
  group="$(systemctl show -p ControlGroup --value "$unit.service" 2>/dev/null || true)"
  [[ -n "$group" && "$group" != / ]] || return 1
  while read -r pid; do
    [[ -n "$pid" && -r "/proc/$pid/cgroup" ]] || return 1
    grep -Fq ":$group" "/proc/$pid/cgroup" || return 1
  done < <(printf '%s\n' "$listeners" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
  [[ "$listeners" == *"pid="* ]]
}
listener_owned "$PORTAL_PORT" wg-portal || { echo "Gateway port $PORTAL_PORT belongs to another process; no services were changed" >&2; exit 1; }
listener_owned "$SETUP_PORT" wg-portal-setup || { echo "HTTPS port $SETUP_PORT belongs to another process; no services were changed" >&2; exit 1; }
if [[ "$MODE" == managed ]]; then
  listener_owned "$PANEL_PORT" wg-panel || {
    listener_owned "$PANEL_PORT" wg-portal-panel || { echo "Panel port $PANEL_PORT is owned by another process; use existing mode" >&2; exit 1; }
  }
fi
if ! listener_owned 80 wg-portal-http; then HTTP_PORT=8768; fi
[[ "$PANEL_PORT" != "$HTTP_PORT" && "$PORTAL_PORT" != "$HTTP_PORT" ]] || { echo "HTTP welcome port conflicts with a selected internal port" >&2; exit 1; }
listener_owned "$HTTP_PORT" wg-portal-http || { echo "HTTP welcome port $HTTP_PORT is occupied; no services were changed" >&2; exit 1; }
if [[ -e /usr/local/lib/wgportal/installation-root ]] && [[ "$(cat /usr/local/lib/wgportal/installation-root)" != "$ROOT" ]]; then
  echo "WG Portal service helpers belong to another installation; no services were changed" >&2; exit 1
fi
if [[ -d /usr/local/lib/wgportal && ! -f /usr/local/lib/wgportal/installation-root ]] && find /usr/local/lib/wgportal -mindepth 1 -print -quit | grep -q .; then
  echo "Unowned WG Portal helper directory exists; refusing to overwrite it" >&2; exit 1
fi
if [[ -e /usr/local/bin/wgportal ]] && ! grep -Fq "$ROOT/menu.sh" /usr/local/bin/wgportal; then
  echo "The wgportal command belongs to another installation; no services were changed" >&2; exit 1
fi
BOT_ENABLED=0 BOT_ACTIVE=0
if [[ "$MODE" == managed ]]; then
  for unit in wg-panel-bot wg-portal-bot; do
    if [[ -f "/etc/systemd/system/$unit.service" ]] &&
       grep -Fxq "WorkingDirectory=$VENDOR" "/etc/systemd/system/$unit.service"; then
      if systemctl is-enabled --quiet "$unit.service"; then BOT_ENABLED=1; fi
      if systemctl is-active --quiet "$unit.service"; then BOT_ACTIVE=1; fi
    fi
  done
fi

step 1 "Checking system requirements / بررسی پیش‌نیازها"
packages="sudo ca-certificates curl wget git jq rsync unzip openssl python3 python3-venv python3-pip"
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
python3 "$ROOT/install_helpers.py" check-runtime

step 2 "Preparing upstream WG_Panel / آماده‌سازی هسته اصلی"
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
  VENV="$VENDOR/venv"
fi

step 3 "Installing Python dependencies / نصب وابستگی‌ها"
if [[ "$MODE" == managed ]]; then
  python3 "$ROOT/apply_upstream_patches.py" "$VENDOR" --check
fi
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/python" "$ROOT/install_helpers.py" check-runtime
"$VENV/bin/python" -m pip install --require-hashes -r "$ROOT/requirements.lock"
if [[ "$MODE" == managed ]]; then
  python3 "$ROOT/apply_upstream_patches.py" "$VENDOR"
fi

step 4 "Preparing configuration / آماده‌سازی تنظیمات"
if [[ "$MODE" == managed ]]; then
  ENV_FILE="$VENDOR/.env"
  if [[ ! -f "$ENV_FILE" && ! -f "$ROOT/.env" ]]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    umask 077
    temporary_env="$(mktemp "$(dirname "$ENV_FILE")/.env-new.XXXXXX")"
    {
      printf 'BIND=127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'DATABASE_URL=sqlite:///instance/wg_panel.db\n'
      printf 'WIREGUARD_CONF_PATH=/etc/wireguard\n'
      printf 'FLASK_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'FERNET_KEY=%s\n' "$("$VENV/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
      printf 'API_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'PANEL_BASE_URL=http://127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'PANEL=http://127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'SECURE_COOKIES=1\nWORKERS=2\nTHREADS=4\nTG_BOT_TOKEN=\n'
    } > "$temporary_env"
    chmod 600 "$temporary_env"
    if ! ln "$temporary_env" "$ENV_FILE"; then
      rm -f "$temporary_env"
      echo "Could not safely create $ENV_FILE" >&2
      exit 1
    fi
    rm -f "$temporary_env"
  fi
  migration_args=(migrate-env "$ENV_FILE" --fallback "$ROOT/.env" --port "$PANEL_PORT")
  [[ "$PANEL_PORT_EXPLICIT" == 1 ]] && migration_args+=(--port-explicit)
  PANEL_PORT="$("$VENV/bin/python" "$ROOT/install_helpers.py" "${migration_args[@]}")"
  chmod 600 "$ENV_FILE"
  "$VENV/bin/python" -c 'import sys; from dotenv import dotenv_values; values=dotenv_values(sys.argv[1]); key=(values.get("FLASK_SECRET_KEY") or "").strip(); sys.exit(0 if len(key)>=32 and key.lower() not in ("change-me","changeme") else "Unsafe FLASK_SECRET_KEY; set a random key of at least 32 characters in .env before continuing. Existing keys were not changed.")' "$ENV_FILE"
  mkdir -p "$VENDOR/instance"
  if [[ ! -f "$VENDOR/instance/panel_settings.json" ]]; then
    cat > "$VENDOR/instance/panel_settings.json" <<EOF
{
  "tls_enabled": false,
  "domain": "",
  "force_https_redirect": false,
  "hsts": false,
  "http_port": $PANEL_PORT,
  "https_port": 443,
  "tls_cert_path": "",
  "tls_key_path": ""
}
EOF
  fi
  if [[ ! -f "$VENDOR/instance/runtime.json" ]]; then
    cat > "$VENDOR/instance/runtime.json" <<EOF
{
  "bind": "127.0.0.1:$PANEL_PORT",
  "port": $PANEL_PORT,
  "workers": 2,
  "threads": 4,
  "timeout": 120,
  "graceful_timeout": 30,
  "loglevel": "info"
}
EOF
  fi
  if [[ -e /etc/systemd/system/wg-panel.service ]] &&
     ! grep -Fxq "WorkingDirectory=$VENDOR" /etc/systemd/system/wg-panel.service; then
    echo "An existing wg-panel.service belongs to another installation; use --existing-port instead" >&2
    exit 1
  fi
  if [[ -f /etc/systemd/system/wg-portal-panel.service ]] &&
     grep -q '^Description=WG Portal managed WG_Panel$' /etc/systemd/system/wg-portal-panel.service; then
    systemctl disable --now wg-portal-panel.service || true
  fi
  cat > /etc/systemd/system/wg-panel.service <<EOF
[Unit]
Description=WG Portal managed WG_Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$VENDOR
EnvironmentFile=$ENV_FILE
ExecStart="$VENV/bin/python" "$VENDOR/app.py"
ExecStartPre=/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  if [[ -e /etc/systemd/system/wg-panel-bot.service ]] &&
     ! grep -Fxq "WorkingDirectory=$VENDOR" /etc/systemd/system/wg-panel-bot.service; then
    echo "An existing wg-panel-bot.service belongs to another installation; preserving it" >&2
  else
    if [[ -f /etc/systemd/system/wg-portal-bot.service ]] &&
       grep -q '^Description=WG Portal optional Telegram bot$' /etc/systemd/system/wg-portal-bot.service; then
      systemctl disable --now wg-portal-bot.service || true
    fi
    cat > /etc/systemd/system/wg-panel-bot.service <<EOF
[Unit]
Description=WG Portal optional Telegram bot
After=wg-panel.service
Requires=wg-panel.service

[Service]
Type=simple
User=root
WorkingDirectory=$VENDOR
EnvironmentFile=$ENV_FILE
ExecStart="$VENV/bin/python" "$VENDOR/telegram_bot.py"
ExecStartPre=/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  fi
fi

if [[ "$MODE" == existing ]]; then
  ENV_FILE="${WG_PORTAL_PANEL_ENV:-$ROOT/.env}"
  [[ -f "$ENV_FILE" ]] || { echo "Existing mode requires WG_PORTAL_PANEL_ENV pointing to the existing panel .env" >&2; exit 1; }
fi
# All secrets are read from root-private files or /dev/tty, never command arguments.
"$VENV/bin/python" "$ROOT/bootstrap.py" --root "$ROOT" --panel-env "$ENV_FILE" --panel-port "$PANEL_PORT" --portal-port "$PORTAL_PORT" --http-port "$HTTP_PORT"
[[ -x "$ROOT/acme-venv/bin/certbot" ]] || {
  python3 -m venv "$ROOT/acme-venv"
  "$ROOT/acme-venv/bin/python" -m pip install 'certbot==5.8.0'
}

mkdir -p "$ROOT/runtime/site/assets"
cp -a "$SITE/dist/." "$ROOT/runtime/site/"
printf '%s\n' 'window.WG_PORTAL_RUNTIME = Object.freeze({ mode: "wgpanel" });' > "$ROOT/runtime/site/assets/runtime-config.js"
if [[ ! -f "$ROOT/runtime/portal.env" ]]; then
  umask 077
  printf '%s\n' 'WG_PORTAL_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]' > "$ROOT/runtime/portal.env"
  chmod 600 "$ROOT/runtime/portal.env"
fi

step 5 "Installing systemd services / راه‌اندازی سرویس‌ها"
cat > /etc/systemd/system/wg-portal.service <<EOF
[Unit]
Description=WG Portal frontend and WG_Panel gateway
After=network-online.target wg-panel.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$ROOT
Environment=WG_PORTAL_SITE_ROOT=$ROOT/runtime/site
Environment=WG_PORTAL_PANEL_URL=http://127.0.0.1:$PANEL_PORT
EnvironmentFile=$ROOT/runtime/setup.env
EnvironmentFile=-$ROOT/runtime/portal.env
ExecStart="$VENV/bin/gunicorn" --no-control-socket --bind 127.0.0.1:$PORTAL_PORT --workers 2 --threads 4 --timeout 120 portal_proxy:app
ExecStartPre=/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$ROOT/runtime
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/wg-portal-setup.service <<EOF
[Unit]
Description=WG Portal browser setup and HTTPS edge
After=network-online.target wg-panel.service wg-portal.service
Wants=network-online.target wg-portal.service

[Service]
Type=simple
User=root
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/runtime/setup.env
EnvironmentFile=-$ROOT/runtime/portal.env
ExecStart="$VENV/bin/gunicorn" --no-control-socket --bind 0.0.0.0:$SETUP_PORT --workers 2 --threads 4 --timeout 240 --certfile "$ROOT/runtime/tls/current/fullchain.pem" --keyfile "$ROOT/runtime/tls/current/privkey.pem" portal_setup:app
ExecStartPre=/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/wg-portal-http.service <<EOF
[Unit]
Description=WG Portal HTTP welcome and ACME challenge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/runtime/setup.env
EnvironmentFile=-$ROOT/runtime/portal.env
ExecStart="$VENV/bin/gunicorn" --no-control-socket --bind 0.0.0.0:$HTTP_PORT --workers 1 --threads 2 --timeout 30 portal_http:app
ExecStartPre=/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
install -d -m 700 /usr/local/lib/wgportal
install -m 700 "$ROOT/service_helpers/tls_service.py" /usr/local/lib/wgportal/tls_service.py
printf '%s\n' "$ROOT" > /usr/local/lib/wgportal/installation-root
chmod 600 /usr/local/lib/wgportal/installation-root
for action in schedule-setup-reload reload-setup-tls start-telegram apply-pending-tls; do
  case "$action" in schedule-setup-reload) subcommand=schedule ;; reload-setup-tls) subcommand=renew-reload ;; start-telegram) subcommand=start-telegram ;; apply-pending-tls) subcommand=recover ;; esac
  cat > "/usr/local/lib/wgportal/$action" <<EOF
#!/usr/bin/env bash
set -euo pipefail
/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
exec "$VENV/bin/python" /usr/local/lib/wgportal/tls_service.py "$subcommand" --root "$ROOT"
EOF
  chmod 700 "/usr/local/lib/wgportal/$action"
done
cat > /etc/systemd/system/wg-portal-acme-renew.service <<EOF
[Unit]
Description=WG Portal isolated certificate renewal
After=network-online.target wg-portal-http.service

[Service]
Type=oneshot
TimeoutStartSec=15min
User=root
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/runtime/setup.env
ExecStartPre=/usr/bin/python3 -I -S /usr/local/lib/wgportal/tls_service.py guard --root "$ROOT"
ExecStart="$VENV/bin/python" /usr/local/lib/wgportal/tls_service.py renew --root "$ROOT"
UMask=0077
PrivateTmp=true
EOF
cat > /etc/systemd/system/wg-portal-acme-renew.timer <<EOF
[Unit]
Description=WG Portal certificate renewal every six hours

[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
RandomizedDelaySec=10min
Persistent=true
Unit=wg-portal-acme-renew.service

[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/wg-portal-tls-recover.service <<EOF
[Unit]
Description=WG Portal interrupted TLS operation recovery
After=wg-portal-setup.service wg-portal.service

[Service]
Type=oneshot
TimeoutStartSec=5min
User=root
WorkingDirectory=$ROOT
ExecStart=/usr/local/lib/wgportal/apply-pending-tls
UMask=0077
EOF
cat > /etc/systemd/system/wg-portal-tls-recover.timer <<EOF
[Unit]
Description=WG Portal interrupted TLS operation recovery timer

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
Persistent=true
Unit=wg-portal-tls-recover.service

[Install]
WantedBy=timers.target
EOF

cat > /usr/local/bin/wgportal <<EOF
#!/usr/bin/env bash
exec /bin/bash "$ROOT/menu.sh" "\$@"
EOF
chmod 755 /usr/local/bin/wgportal
if [[ "$MODE" == managed && ! -e /usr/local/bin/wgpanel && ! -L /usr/local/bin/wgpanel ]]; then
  cat > /usr/local/bin/wgpanel <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$ROOT/native_menu.py" "$VENDOR" "\$@"
EOF
  chmod 755 /usr/local/bin/wgpanel
fi

systemctl daemon-reload
if [[ "$MODE" == managed ]]; then
  systemctl enable wg-panel.service
  systemctl restart wg-panel.service
fi
systemctl enable wg-portal.service
systemctl restart wg-portal.service
systemctl enable wg-portal-setup.service wg-portal-http.service wg-portal-acme-renew.timer wg-portal-tls-recover.timer
systemctl restart wg-portal-setup.service wg-portal-http.service
systemctl start wg-portal-acme-renew.timer wg-portal-tls-recover.timer
if [[ "$MODE" == managed ]]; then
  if [[ "$BOT_ENABLED" == 1 ]]; then systemctl enable wg-panel-bot.service; fi
  if [[ "$BOT_ACTIVE" == 1 ]]; then systemctl restart wg-panel-bot.service; fi
fi

step 6 "Checking local HTTP responses / بررسی پاسخ سایت"
wait_url() {
  local url="$1"
  for _ in $(seq 1 90); do
    if curl --fail --silent --connect-timeout 2 --max-time 3 --output /dev/null "$url"; then return 0; fi
    sleep 1
  done
  return 1
}
if [[ "$MODE" == managed ]]; then
  wait_url "http://127.0.0.1:$PANEL_PORT/login" || {
    journalctl -u wg-panel.service -n 30 --no-pager >&2
    exit 1
  }
fi
wait_url "http://127.0.0.1:$PORTAL_PORT/" || {
  journalctl -u wg-portal.service -n 30 --no-pager >&2
  exit 1
}
PUBLIC_IP="$("$VENV/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["public_ip"])' "$ROOT/runtime/bootstrap-summary.json")"
FINGERPRINT="$("$VENV/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "$ROOT/runtime/bootstrap-summary.json")"
tls_ready=0
for _ in $(seq 1 30); do
  if curl --fail --silent --insecure --connect-timeout 3 --max-time 5 --resolve "$PUBLIC_IP:$SETUP_PORT:127.0.0.1" "https://$PUBLIC_IP:$SETUP_PORT/setup" --output /dev/null; then tls_ready=1; break; fi
  sleep 1
done
[[ "$tls_ready" == 1 ]] || {
  echo "Local TLS setup readiness failed. Inspect wg-portal-setup.service; the install is not marked complete." >&2; exit 1;
}
umask 077
printf 'MODE=%s\nPANEL_PORT=%s\nPORTAL_PORT=%s\nHTTP_PORT=%s\n' "$MODE" "$PANEL_PORT" "$PORTAL_PORT" "$HTTP_PORT" > "$ROOT/runtime/install.conf"
printf '\nصفحه شروع: http://%s:%s/\nراه‌اندازی امن: https://%s:%s/setup\n' "$PUBLIC_IP" "$HTTP_PORT" "$PUBLIC_IP" "$SETUP_PORT"
printf 'Bootstrap certificate SHA-256: %s\n' "$FINGERPRINT"
printf 'Compare this fingerprint in the browser before entering the first-run token. The initial certificate is self-signed.\n'
printf 'WireGuard, owner credentials, domain and Telegram are configured in the browser. Port 443 is unchanged.\n'
[[ "$HTTP_PORT" == 8768 ]] && printf 'Port 80 is occupied and was preserved. Automatic ACME requires routing its challenge path to this installation.\n'
printf 'Server menu: wgportal\n'
printf 'Persian installation guide: %s/README.fa.md\n' "$ROOT"
