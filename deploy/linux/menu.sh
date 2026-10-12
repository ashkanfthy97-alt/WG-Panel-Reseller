#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
VENDOR="$ROOT/vendor/WG_Panel"
PORTAL_PORT=8765 PANEL_PORT=8766 MODE=managed
if [[ -f "$ROOT/runtime/install.conf" ]]; then
  while IFS='=' read -r key value; do
    case "$key:$value" in
      PORTAL_PORT:*) [[ "$value" =~ ^[0-9]+$ ]] && PORTAL_PORT="$value" ;;
      PANEL_PORT:*) [[ "$value" =~ ^[0-9]+$ ]] && PANEL_PORT="$value" ;;
      MODE:managed|MODE:existing) MODE="$value" ;;
    esac
  done < "$ROOT/runtime/install.conf"
fi
C='' G='' Y='' R=''
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C=$'\033[1;36m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[0m'
fi
section() { printf '\n%s── %s ──%s\n' "$C" "$1" "$R"; }
pause() { printf '\nEnter / ادامه: '; read -r _ || true; }
native() {
  if [[ "$MODE" != managed ]]; then
    echo "Use the management command of your existing panel."; return 1
  fi
  if [[ ! -x "$VENDOR/venv/bin/python" ]]; then
    echo "ابتدا نصب را اجرا کنید / Install with option 1 first."; return 1
  fi
  "$VENDOR/venv/bin/python" "$ROOT/native_menu.py" "$VENDOR" "$1"
}
access() {
  section "دسترسی مرورگر و ساخت مدیر / Browser & owner"
  echo "این دستور را روی کامپیوتر خودتان اجرا کنید، نه داخل VPS:"
  echo "Run on YOUR computer and keep the SSH window open:"
  printf '\n  ssh -L %s:127.0.0.1:%s root@YOUR_VPS_IP\n\n' "$PORTAL_PORT" "$PORTAL_PORT"
  for path in / /register /login /users; do
    printf '  http://127.0.0.1:%s%s\n' "$PORTAL_PORT" "$path"
  done
  echo "در اولین ورود، مدیر را با SETUP_TOKEN بسازید و ورود دومرحله‌ای را فعال کنید."
  if [[ "$MODE" == managed ]]; then
    echo "Read the setup token privately on the VPS; do not share its output:"
    printf "  grep '^SETUP_TOKEN=' '%s/.env'\n" "$VENDOR"
  else
    echo "Use the owner account and setup instructions of your existing panel."
  fi
  echo "برای دامنه عمومی، HTTPS proxy موجود را به این آدرس متصل کنید:"
  printf '  http://127.0.0.1:%s\n' "$PORTAL_PORT"
  echo "Port 443 is unchanged. Full domain instructions: README.fa.md"
}
guide() {
  section "راهنمای مرحله‌به‌مرحله / Setup checklist"
  cat <<'EOF'
  1 · نصب / Install
      Option 1: Python, WireGuard, upstream source and systemd services.
      نصب خودکار پیش‌نیازها؛ Docker لازم نیست.
  2 · ورود / Browser
      Option 3: SSH tunnel, website address and first owner registration.
      آدرس عمومی IP به‌تنهایی باز نمی‌شود؛ ابتدا تونل را برقرار کنید.
  3 · اتصال / WireGuard
      Option 4: original interface wizard, subnet, UDP port and NAT.
      پورت UDP انتخاب‌شده را در فایروال VPS مجاز کنید.
  4 · تلگرام / Telegram
      Option 5: original Telegram setup. Obtain a token from BotFather.
      شناسه‌های مجاز مدیر را در تنظیمات ربات تعیین کنید.
  5 · دامنه / Domain
      Reuse your existing HTTPS proxy; keep internal ports private.
      راهنمای کامل فارسی کنار نصب‌کننده در README.fa.md قرار دارد.
  6 · بررسی / Check
      Option 7: service health. Create one peer, download its config,
      connect from a device and confirm traffic in the panel.

Peers, nodes, subscriptions and backups use the real WG_Panel backend.
Reseller billing/payment screens still require a commerce backend.
EOF
}
status() {
  section "وضعیت / Service health"
  for svc in wg-portal wg-panel wg-panel-bot; do
    [[ "$MODE" == existing && "$svc" != wg-portal ]] && continue
    printf '  %-18s %s\n' "$svc" "$(systemctl is-active "$svc.service" 2>/dev/null || true)"
  done
  if command -v curl >/dev/null; then
    for path in / /login /users; do
      printf '  %-18s HTTP ' "$path"
      curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORTAL_PORT$path" || true
    done
  fi
  echo "200: response | 302: login/setup redirect | 000: unreachable"
  echo "ربات تا قبل از تنظیمات می‌تواند غیرفعال باشد / Bot is optional."
}
wizard() {
  section "نصب مرحله‌ای / Guided install"
  echo "Ubuntu/Debian + root + systemd. Existing configuration is preserved."
  printf '1) Managed  2) Existing panel [%s]: ' "$MODE"; read -r selected || return
  case "$selected" in 1) MODE=managed ;; 2) MODE=existing ;; "") ;; *) return ;; esac
  printf 'Portal port [%s]: ' "$PORTAL_PORT"; read -r entered || return
  PORTAL_PORT="${entered:-$PORTAL_PORT}"
  printf 'Panel port [%s]: ' "$PANEL_PORT"; read -r entered || return
  PANEL_PORT="${entered:-$PANEL_PORT}"
  if [[ "$MODE" == managed ]]; then
    bash "$ROOT/install.sh" --managed --panel-port "$PANEL_PORT" --portal-port "$PORTAL_PORT" || return 1
  else
    bash "$ROOT/install.sh" --existing-port "$PANEL_PORT" --portal-port "$PORTAL_PORT" || return 1
  fi
  access
  if [[ "$MODE" == managed ]]; then
    printf '\nساخت Interface و NAT اکنون؟ / Configure WireGuard now? [y/N]: '
    read -r selected || return
    case "$selected" in y|Y) native wireguard || return 1 ;; esac
    printf '\nراه‌اندازی ربات اکنون؟ / Configure Telegram now? [y/N]: '
    read -r selected || return
    case "$selected" in y|Y) native telegram || return 1 ;; esac
  fi
  echo "Next: register the owner in the browser, create one peer, and connect a client."
}
while true; do
  section "WG PORTAL · مرکز مدیریت وایرگارد"
  printf '  Native WG_Panel · Python · systemd\n  Mode: %s  |  Portal: %s  |  Panel: %s\n' "$MODE" "$PORTAL_PORT" "$PANEL_PORT"
  printf '\n%sشروع / START%s\n' "$G" "$R"
  echo "  1  نصب یا ترمیم مرحله‌ای / Guided install or repair"
  echo "  2  راهنمای قدم‌به‌قدم / Setup guide"
  echo "  3  آدرس سایت و ساخت مدیر / Browser & owner"
  printf '\n%sپیکربندی / CONFIGURE%s\n' "$C" "$R"
  echo "  4  ساخت Interface و NAT / WireGuard wizard"
  echo "  5  راه‌اندازی ربات / Telegram wizard"
  echo "  6  منوی کامل WG_Panel / Advanced upstream menu"
  printf '\n%sنگهداری / OPERATE%s\n' "$Y" "$R"
  echo "  7  وضعیت و عیب‌یابی / Health"
  echo "  8  گزارش خطاها / Logs"
  echo "  9  راه‌اندازی مجدد / Restart"
  echo "  0  خروج / Exit"
  printf '\nانتخاب / Choose: '; read -r choice || exit 0
  case "$choice" in
    1) wizard || echo "نصب کامل نشد؛ خطای بالا را بررسی کنید / Installation stopped."; pause ;;
    2) guide; pause ;;
    3) access; pause ;;
    4) native wireguard; pause ;;
    5) native telegram; pause ;;
    6)
      echo "Advanced settings can change ports/TLS. Keep bind 127.0.0.1:$PANEL_PORT."
      echo "Upstream main-branch updates are disabled to preserve WG Portal patches."
      native menu; pause ;;
    7) status; pause ;;
    8) journalctl -u wg-panel.service -u wg-portal.service -u wg-panel-bot.service -n 100 --no-pager; pause ;;
    9)
      [[ "$MODE" == managed ]] && systemctl restart wg-panel.service
      systemctl restart wg-portal.service
      status; pause ;;
    0) exit 0 ;;
    *) echo "گزینه نامعتبر / Invalid option" ;;
  esac
done
