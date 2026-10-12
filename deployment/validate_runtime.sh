#!/bin/bash
set -euo pipefail

TARGET_ROOT="${SUPPORT_PORTAL_RUNTIME_HOME:-$HOME/Library/Application Support/Support PortalIngestor}"

required_paths=(
  "$TARGET_ROOT/app.py"
  "$TARGET_ROOT/config.py"
  "$TARGET_ROOT/run_support_portal_once.sh"
  "$TARGET_ROOT/support_portal"
  "$TARGET_ROOT/notifications"
  "$TARGET_ROOT/services"
  "$TARGET_ROOT/storage"
  "$TARGET_ROOT/logs"
)

missing=0
for path in "${required_paths[@]}"; do
  if [[ ! -e "$path" ]]; then
    echo "missing: $path"
    missing=1
  fi
done

if [[ $missing -ne 0 ]]; then
  exit 1
fi

echo "runtime_root=$TARGET_ROOT"
echo "validation=ok"
