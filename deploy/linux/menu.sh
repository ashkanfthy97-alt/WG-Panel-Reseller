#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
VENDOR="$ROOT/vendor/WG_Panel"

status() {
  printf '\nWG Portal: %s\n' "$(systemctl is-active wg-portal.service 2>/dev/null || true)"
  printf 'WG_Panel:  %s\n' "$(systemctl is-active wg-panel.service 2>/dev/null || true)"
  printf 'Telegram:  %s\n' "$(systemctl is-active wg-panel-bot.service 2>/dev/null || true)"
  if command -v curl >/dev/null 2>&1; then
    printf 'Portal:    '
    curl --silent --output /dev/null --write-out '%{http_code}\n' --max-time 3 http://127.0.0.1:8765/ || true
    printf 'Panel:     '
    curl --silent --output /dev/null --write-out '%{http_code}\n' --max-time 3 http://127.0.0.1:8766/login || true
  fi
}

pause() {
  printf '\nPress Enter to continue... '
  read -r _ || true
}

while true; do
  printf '\n================ WG Portal ================\n'
  status
  cat <<'EOF'
1) Install or repair managed WG_Panel + portal
2) Connect portal to an existing local WG_Panel
3) Open the complete upstream WG_Panel menu
4) Show service logs
5) Restart services
6) Show private access instructions
0) Exit
EOF
  printf 'Choose: '
  read -r choice || exit 0
  case "$choice" in
    1)
      bash "$ROOT/install.sh" --managed
      pause
      ;;
    2)
      printf 'Existing local WG_Panel port: '
      read -r panel_port || continue
      bash "$ROOT/install.sh" --existing-port "$panel_port"
      pause
      ;;
    3)
      if [[ -f "$VENDOR/wg.py" && -x "$VENDOR/venv/bin/python" ]]; then
        "$VENDOR/venv/bin/python" "$ROOT/native_menu.py" "$VENDOR"
      elif command -v wgpanel >/dev/null 2>&1; then
        wgpanel
      else
        echo "Install the managed panel first."
      fi
      pause
      ;;
    4)
      journalctl -u wg-panel.service -u wg-portal.service -u wg-panel-bot.service -n 80 --no-pager
      pause
      ;;
    5)
      if systemctl list-unit-files wg-panel.service --no-legend 2>/dev/null | grep -q wg-panel; then
        systemctl restart wg-panel.service
      fi
      systemctl restart wg-portal.service
      status
      pause
      ;;
    6)
      cat <<'EOF'
The site listens on 127.0.0.1:8765, so the public VPS IP cannot open it directly.
For a private browser preview, run this on your OWN computer:
  ssh -L 8765:127.0.0.1:8765 root@YOUR_VPS_IP
Then open http://127.0.0.1:8765/ or http://127.0.0.1:8765/register
For public access, route an HTTPS hostname through your existing reverse proxy
to http://127.0.0.1:8765. The installer does not bind port 443.
EOF
      pause
      ;;
    0) exit 0 ;;
    *) echo "Invalid option." ;;
  esac
done
