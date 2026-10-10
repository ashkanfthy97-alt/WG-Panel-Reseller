#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SITE="$(cd -- "$ROOT/../.." && pwd)"
UPSTREAM_URL="https://github.com/Azumi67/WG_Panel.git"
UPSTREAM_COMMIT="f0f41068e1fd453d17acad23b9b90bc2a284bf59"
MODE=""
PANEL_PORT="8766"
PORTAL_PORT="8765"

usage() {
  cat <<'EOF'
Usage: ./install.sh --managed [--panel-port 8766] [--portal-port 8765]
       ./install.sh --existing-port PORT [--portal-port 8765]

The installer leaves 443 untouched. Both listeners bind to 127.0.0.1.
Use your existing HTTPS reverse proxy or SSH port forwarding for access.
EOF
}

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1024 && 10#$1 <= 65535 ))
}

while (($#)); do
  case "$1" in
    --managed) MODE="managed"; shift ;;
    --existing-port) MODE="existing"; PANEL_PORT="${2:-}"; shift 2 ;;
    --panel-port) PANEL_PORT="${2:-}"; shift 2 ;;
    --portal-port) PORTAL_PORT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || { echo "Linux is required" >&2; exit 1; }
[[ -n "$MODE" ]] || { usage >&2; exit 2; }
valid_port "$PANEL_PORT" && valid_port "$PORTAL_PORT" || { echo "Ports must be numeric and in 1024..65535" >&2; exit 2; }
[[ "$PANEL_PORT" != "$PORTAL_PORT" ]] || { echo "Panel and portal ports must differ" >&2; exit 2; }
for tool in docker git openssl sed cp; do command -v "$tool" >/dev/null || { echo "Missing: $tool" >&2; exit 1; }; done
docker compose version >/dev/null || { echo "Docker Compose v2 is required" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Cannot access Docker; run with a Docker-authorized account" >&2; exit 1; }

if [[ "$MODE" == "managed" ]]; then
  VENDOR="$ROOT/vendor/WG_Panel"
  if [[ ! -e "$VENDOR/.git" ]]; then
    mkdir -p "$ROOT/vendor"
    git clone --filter=blob:none --no-checkout "$UPSTREAM_URL" "$VENDOR"
    git -C "$VENDOR" checkout --detach "$UPSTREAM_COMMIT"
  fi
  [[ "$(git -C "$VENDOR" rev-parse HEAD)" == "$UPSTREAM_COMMIT" ]] || {
    echo "Vendor checkout differs from the pinned WG_Panel commit; inspect it before continuing" >&2
    exit 1
  }
  if [[ ! -e "$ROOT/.env" ]]; then
    umask 077
    {
      printf 'WG_PANEL_BIND=127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'PANEL_BASE_URL=http://127.0.0.1:%s\n' "$PANEL_PORT"
      printf 'FLASK_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'FERNET_KEY=%s\n' "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
      printf 'API_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'SETUP_TOKEN=%s\n' "$(openssl rand -hex 24)"
      printf 'SECURE_COOKIES=0\n'
      printf 'TG_BOT_TOKEN=\n'
    } > "$ROOT/.env"
  else
    configured_port="$(sed -n 's/^WG_PANEL_BIND=127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p' "$ROOT/.env" | head -n 1)"
    [[ "$configured_port" == "$PANEL_PORT" ]] || { echo "Existing .env has another panel port; preserve it and pass --panel-port $configured_port" >&2; exit 1; }
  fi
fi

mkdir -p "$ROOT/runtime/site/assets"
cp -a "$SITE/dist/." "$ROOT/runtime/site/"
printf '%s\n' 'window.WG_PORTAL_RUNTIME = Object.freeze({ mode: "wgpanel" });' > "$ROOT/runtime/site/assets/runtime-config.js"
sed -e "s/@PORTAL_PORT@/$PORTAL_PORT/g" -e "s/@PANEL_PORT@/$PANEL_PORT/g" \
  "$ROOT/nginx.conf.template" > "$ROOT/runtime/nginx.conf"

if [[ "$MODE" == "managed" ]]; then
  docker compose --project-directory "$ROOT" -f "$ROOT/compose.yaml" --profile managed up -d --build panel portal
else
  docker compose --project-directory "$ROOT" -f "$ROOT/compose.yaml" up -d portal
fi

printf 'Portal: http://127.0.0.1:%s/\n' "$PORTAL_PORT"
printf 'WireGuard controls: http://127.0.0.1:%s/users\n' "$PORTAL_PORT"
printf 'Keep both ports private; put your existing HTTPS reverse proxy in front of the portal.\n'
