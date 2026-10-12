#!/bin/bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_ROOT="${SUPPORT_PORTAL_RUNTIME_HOME:-$HOME/Library/Application Support/Support PortalIngestor}"

mkdir -p "$TARGET_ROOT"
mkdir -p "$TARGET_ROOT/logs"

copy_file() {
  local relative_path="$1"
  mkdir -p "$(dirname "$TARGET_ROOT/$relative_path")"
  cp "$SOURCE_ROOT/$relative_path" "$TARGET_ROOT/$relative_path"
}

copy_dir() {
  local relative_path="$1"
  mkdir -p "$(dirname "$TARGET_ROOT/$relative_path")"
  rm -rf "$TARGET_ROOT/$relative_path"
  cp -R "$SOURCE_ROOT/$relative_path" "$TARGET_ROOT/$relative_path"
}

copy_file "app.py"
copy_file "config.py"
copy_file "run_support_portal_once.sh"
copy_file "requirements.txt"
copy_file ".env.example"
copy_dir "support_portal"
copy_dir "notifications"
copy_dir "queries"
copy_dir "scripts"
copy_dir "services"
copy_dir "storage"
copy_dir "utils"
copy_dir "deployment"

if [[ -f "$SOURCE_ROOT/.env" && ! -f "$TARGET_ROOT/.env" ]]; then
  cp "$SOURCE_ROOT/.env" "$TARGET_ROOT/.env"
fi

if [[ -d "$SOURCE_ROOT/.venv" && ! -d "$TARGET_ROOT/.venv" ]]; then
  cp -R "$SOURCE_ROOT/.venv" "$TARGET_ROOT/.venv"
fi

chmod +x "$TARGET_ROOT/run_support_portal_once.sh"
echo "Installed Support Portal runtime to: $TARGET_ROOT"
