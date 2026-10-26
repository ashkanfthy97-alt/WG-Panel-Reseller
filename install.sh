#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$SOURCE_DIR"
exec /bin/bash "$SOURCE_DIR/wg.sh" "$@"
