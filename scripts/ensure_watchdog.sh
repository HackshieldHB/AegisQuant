#!/bin/bash
set -u

APP_ROOT="${HOME}/aegisquant_app"
PYTHON="${HOME}/aegisquant_venv/bin/python"
LOG_DIR="${APP_ROOT}/logs"

mkdir -p "${LOG_DIR}"
exec 9>"${LOG_DIR}/watchdog_bootstrap.lock"
flock -n 9 || exit 0

if pgrep -u "$(id -u)" -f "${PYTHON} WatchdogSupervisor.py" >/dev/null; then
    exit 0
fi

rm -f "${LOG_DIR}/watchdog.pid"
cd "${APP_ROOT}" || exit 1
setsid nohup "${PYTHON}" WatchdogSupervisor.py \
    >> "${LOG_DIR}/watchdog_boot.log" 2>&1 < /dev/null &
