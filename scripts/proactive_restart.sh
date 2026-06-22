#!/bin/bash
# proactive_restart.sh — scheduled graceful restart of the trading ENGINE only.
#
# Why: on memory-capped shared hosting the long-running engine accumulates RSS
# (glibc arena fragmentation in the threaded numpy/pandas/ccxt stack) and is
# eventually OOM-SIGKILL'd (-9) at an arbitrary, uncontrolled moment. A scheduled
# graceful restart resets RSS far below the ceiling so the kill never happens.
#
# Safe across a restart: balance/positions are persisted and reconciled from the
# exchange on boot, and protective stop/OCO orders live server-side on Binance —
# nothing is left unmanaged during the brief respawn window.
#
# Restarts only the engine child; WatchdogSupervisor respawns it automatically
# (graceful SIGTERM -> engine saves state -> exit 0 -> watchdog restarts).
set -u

LOG_DIR="${HOME}/aegisquant_app/logs"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/proactive_restart.log"
ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

# Only act when a watchdog is alive to respawn the engine; otherwise leave the
# system alone — ensure_watchdog.sh is responsible for cold starts.
if ! pgrep -u "$(id -u)" -f "WatchdogSupervisor.py" >/dev/null; then
    echo "$(ts) | skip: no watchdog running" >> "${LOG}"
    exit 0
fi

if pgrep -u "$(id -u)" -f "aegisquant_app/Main.py" >/dev/null; then
    rss=$(ps -o rss= -p "$(pgrep -u "$(id -u)" -f 'aegisquant_app/Main.py' | head -1)" 2>/dev/null | tr -d ' ')
    echo "$(ts) | SIGTERM engine for scheduled memory reset (rss=${rss:-?}KB)" >> "${LOG}"
    pkill -TERM -u "$(id -u)" -f "aegisquant_app/Main.py"
else
    echo "$(ts) | skip: engine not running, watchdog will handle" >> "${LOG}"
fi
