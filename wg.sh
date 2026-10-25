#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$SOURCE_DIR/venv/bin/python" ]]; then
  exec "$SOURCE_DIR/venv/bin/python" "$SOURCE_DIR/deploy/linux/native_menu.py" "$SOURCE_DIR" "$@"
fi
exec /bin/bash "$SOURCE_DIR/deploy/linux/menu.sh" "$@"
