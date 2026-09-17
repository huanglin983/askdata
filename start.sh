#!/usr/bin/env bash
# AskData one-click control: ./start.sh [start|stop|restart]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

ACTION="${1:-start}"
HOST="${ASKDATA_HOST:-0.0.0.0}"
PORT="${ASKDATA_PORT:-5050}"
PID_FILE="${ROOT}/.askdata.pid"
LOG_FILE="${ROOT}/.askdata.log"
VENV_DIR="${ROOT}/.venv"

export ASKDATA_HOST="$HOST"
export ASKDATA_PORT="$PORT"
export ASKDATA_DEBUG="${ASKDATA_DEBUG:-1}"
export ASKDATA_RELOAD="${ASKDATA_RELOAD:-0}"

resolve_python() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    echo "${VENV_DIR}/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    echo "python3"
  else
    echo "python"
  fi
}

ensure_venv() {
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "[askdata] creating venv at ${VENV_DIR}"
    python3 -m venv "$VENV_DIR" 2>/dev/null || python -m venv "$VENV_DIR"
    "${VENV_DIR}/bin/pip" install -r "${ROOT}/requirements.txt"
  fi
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  return 1
}

do_stop() {
  local stopped=0
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "[askdata] stopping pid ${pid}"
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.4
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      stopped=1
    fi
    rm -f "$PID_FILE"
  fi

  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      echo "[askdata] freeing port ${PORT}: ${pids}"
      # shellcheck disable=SC2086
      kill ${pids} 2>/dev/null || true
      sleep 0.3
      # shellcheck disable=SC2086
      kill -9 ${pids} 2>/dev/null || true
      stopped=1
    fi
  fi

  if [[ "$stopped" -eq 1 ]]; then
    echo "[askdata] stopped"
  else
    echo "[askdata] not running"
  fi
}

do_start() {
  if is_running; then
    echo "[askdata] already running on ${HOST}:${PORT}"
    exit 0
  fi

  ensure_venv
  local py
  py="$(resolve_python)"

  echo "[askdata] starting on http://${HOST}:${PORT}"
  nohup "$py" "${ROOT}/app.py" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 0.8

  if is_running; then
    echo "[askdata] started (pid $(cat "$PID_FILE"))"
    echo "[askdata] log: ${LOG_FILE}"
  else
    echo "[askdata] failed to start; see ${LOG_FILE}"
    rm -f "$PID_FILE"
    exit 1
  fi
}

case "$ACTION" in
  start)
    do_start
    ;;
  stop)
    do_stop
    ;;
  restart)
    do_stop
    do_start
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac
