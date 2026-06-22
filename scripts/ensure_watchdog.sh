#!/bin/bash
set -u

# Curb glibc malloc-arena fragmentation in the threaded numpy/pandas/ccxt engine.
# On memory-capped hosts (CloudLinux LVE / cgroup) the default per-thread arenas
# let RSS balloon and the engine gets OOM-SIGKILL'd (-9). These are inherited by
# the watchdog and its engine child. Takes effect the next time the watchdog
# (re)starts; see scripts/proactive_restart.sh for the periodic RSS reset.
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=131072

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
