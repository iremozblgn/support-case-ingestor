#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_RUNTIME_HOME="$HOME/Library/Application Support/Support PortalIngestor"
PROJECT_ROOT="${SUPPORT_PORTAL_PROJECT_ROOT:-$SCRIPT_DIR}"
RUNTIME_HOME="${SUPPORT_PORTAL_RUNTIME_HOME:-$PROJECT_ROOT}"

mkdir -p "$RUNTIME_HOME/logs"
cd "$PROJECT_ROOT"

LOCK_DIR="$RUNTIME_HOME/.support_portal_scheduler_run.lock"
PID_FILE="$LOCK_DIR/pid"
START_FILE="$LOCK_DIR/started_at"

process_start_time() {
  local pid="$1"
  ps -p "$pid" -o lstart= 2>/dev/null | awk '{$1=$1; print}'
}

log_scheduler() {
  echo "$1" >> "$RUNTIME_HOME/logs/support_portal_scheduler.log"
}

pid_is_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

acquire_scheduler_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$PID_FILE"
    process_start_time "$$" > "$START_FILE" || date -u +"%Y-%m-%dT%H:%M:%SZ" > "$START_FILE"
    return 0
  fi

  local existing_pid=""
  local existing_start=""
  if [[ -f "$PID_FILE" ]]; then
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  fi
  if [[ -f "$START_FILE" ]]; then
    existing_start="$(cat "$START_FILE" 2>/dev/null || true)"
  fi

  if pid_is_running "$existing_pid"; then
    local current_start=""
    current_start="$(process_start_time "$existing_pid" || true)"
    if [[ -n "$existing_start" && -n "$current_start" && "$existing_start" != "$current_start" ]]; then
      log_scheduler "$(date -u +"%Y-%m-%dT%H:%M:%SZ") stale_scheduler_lock_recovered=true stale_pid=$existing_pid stale_start_time_mismatch=true"
    else
      log_scheduler "$(date -u +"%Y-%m-%dT%H:%M:%SZ") scheduled_run_skipped_already_running=true existing_pid=$existing_pid"
      exit 14
    fi
  fi

  rm -f "$PID_FILE" "$START_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  log_scheduler "$(date -u +"%Y-%m-%dT%H:%M:%SZ") stale_scheduler_lock_recovered=true stale_pid=${existing_pid:-unknown}"
  mkdir "$LOCK_DIR"
  echo "$$" > "$PID_FILE"
  process_start_time "$$" > "$START_FILE" || date -u +"%Y-%m-%dT%H:%M:%SZ" > "$START_FILE"
}

cleanup() {
  rm -f "$PID_FILE" "$START_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

acquire_scheduler_lock

export RUN_ONCE=true
export CHECK_INTERVAL_SECONDS=0
export SUPPORT_PORTAL_RUNTIME_HOME="$RUNTIME_HOME"

source "$PROJECT_ROOT/.venv/bin/activate"

log_scheduler "===== Support Portal scheduled run started at $(date) ====="
if python "$PROJECT_ROOT/app.py" >> "$RUNTIME_HOME/logs/support_portal_scheduler.log" 2>&1; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi
log_scheduler "===== Support Portal scheduled run finished at $(date) exit_code=$EXIT_CODE ====="

exit $EXIT_CODE
