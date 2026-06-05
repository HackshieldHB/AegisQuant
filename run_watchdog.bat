@echo off
REM ============================================================
REM run_watchdog.bat - Watchdog Supervisor Launcher
REM ============================================================
REM Starts WatchdogSupervisor.py which manages the trading
REM engine with auto-restart and Telegram alerts.
REM ============================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

title AegisQuant Watchdog Supervisor

echo.
echo ============================================================
echo   AegisQuant Watchdog Supervisor
echo ============================================================
echo.

REM ── Virtual-environment check ────────────────────────────────
if not exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo  Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM ── .env check (non-blocking) ────────────────────────────────
if not exist "%SCRIPT_DIR%.env" (
    echo [WARN] .env not found — API keys may be missing.
    echo.
)

REM ── Disable Python output buffering so logs appear in real time
set PYTHONUNBUFFERED=1
set PYTHONDONTWRITEBYTECODE=1

echo Starting watchdog...  (Ctrl+C to stop)
echo.

"%SCRIPT_DIR%.venv\Scripts\python.exe" "%SCRIPT_DIR%WatchdogSupervisor.py"

set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% neq 0 (
    echo.
    echo [ERROR] Watchdog exited with code %EXIT_CODE%
    echo  Check %SCRIPT_DIR%logs\ for details.
)

echo.
echo Watchdog stopped.  Press any key to close this window.
pause
exit /b %EXIT_CODE%
