#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
HTTP_PORT=80
if [[ -f "$ROOT/runtime/install.conf" ]]; then
  while IFS='=' read -r key value; do
    [[ "$key" == HTTP_PORT && "$value" =~ ^[0-9]+$ ]] && HTTP_PORT="$value"
  done < "$ROOT/runtime/install.conf"
fi
browser() {
  if [[ ! -f "$ROOT/runtime/bootstrap-summary.json" ]]; then
    echo "ابتدا گزینه نصب را اجرا کنید / Install first."; return 1
  fi
  python3 - "$ROOT/runtime/bootstrap-summary.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
base = f"https://{value['public_ip']}:{value['https_port']}"
print(f"\nصفحه شروع: http://{value['public_ip']}:{value['http_port']}/")
print(f"راه‌اندازی و مدیر اصلی: {base}/setup")
print(f"پنل: {base}/users")
print(f"گواهی اولیه SHA-256: {value['fingerprint']}")
print("اثر انگشت گواهی را پیش از ورود کد نصب بررسی کنید.")
print("تنظیم حساب، دامنه، WireGuard و تلگرام در مرورگر انجام می‌شود.")
PY
}
status() {
  for unit in wg-portal wg-panel wg-portal-setup wg-portal-http wg-panel-bot wg-portal-acme-renew.timer; do
    printf '  %-28s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
  done
}
while true; do
  printf '\n── WG PORTAL · مدیریت وایرگارد ──\n'
  echo "  1  نصب یا ترمیم خودکار / Install or repair"
  echo "  2  آدرس مرورگر و راهنمای ورود / Open setup"
  echo "  3  وضعیت سرویس‌ها / Service status"
  echo "  4  گزارش خطاها / Logs"
  echo "  5  راه‌اندازی مجدد / Restart"
  echo "  0  خروج / Exit"
  printf '\nانتخاب / Choose: '; read -r choice || exit 0
  case "$choice" in
    1) bash "$ROOT/install.sh" || echo "نصب کامل نشد؛ خطای بالا را بررسی کنید." ;;
    2) browser ;;
    3) status ;;
    4) journalctl -u wg-panel -u wg-portal -u wg-portal-setup -u wg-portal-http -u wg-panel-bot -n 100 --no-pager ;;
    5) systemctl try-restart wg-panel.service wg-portal.service wg-portal-setup.service wg-portal-http.service; status ;;
    0) exit 0 ;;
    *) echo "گزینه نامعتبر / Invalid option" ;;
  esac
  printf '\nEnter / ادامه: '; read -r _ || exit 0
done
