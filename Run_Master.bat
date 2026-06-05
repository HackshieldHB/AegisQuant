@echo off
REM ================================================================
REM  Run_Master.bat  —  AegisQuant Master Launcher
REM  Starts Watchdog (trading engine) + Dashboard in separate windows
REM ================================================================
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
cd /d "%ROOT%"

REM Enable ANSI colors on Windows 10+
reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul 2>&1

REM Color codes
set "GRN=[92m"
set "RED=[91m"
set "YLW=[93m"
set "BLU=[94m"
set "CYN=[96m"
set "WHT=[97m"
set "DIM=[90m"
set "RST=[0m"
set "BLD=[1m"

cls
echo.
echo %CYN%%BLD%  ╔══════════════════════════════════════════════════════════╗%RST%
echo %CYN%%BLD%  ║                                                          ║%RST%
echo %CYN%%BLD%  ║    ██████  ███████  ██████  ██ ███████                   ║%RST%
echo %CYN%%BLD%  ║   ██   ██ ██      ██       ██ ██                         ║%RST%
echo %CYN%%BLD%  ║   ███████ █████   ██   ███ ██ ███████                    ║%RST%
echo %CYN%%BLD%  ║   ██   ██ ██      ██    ██ ██      ██                    ║%RST%
echo %CYN%%BLD%  ║   ██   ██ ███████  ██████  ██ ███████  QUANT             ║%RST%
echo %CYN%%BLD%  ║                                                          ║%RST%
echo %CYN%%BLD%  ║         Institutional-Grade AI Trading Engine            ║%RST%
echo %CYN%%BLD%  ║                    v3.2.0  —  LIVE                       ║%RST%
echo %CYN%%BLD%  ╚══════════════════════════════════════════════════════════╝%RST%
echo.
echo  %DIM%  %DATE%  %TIME%%RST%
echo.

REM ── Pre-flight checks ─────────────────────────────────────────────
echo  %WHT%%BLD%PRE-FLIGHT CHECKS%RST%
echo  %DIM%  ─────────────────────────────────────────────────────%RST%

set CHECKS_PASSED=1

REM Virtual environment
if exist "%ROOT%.venv\Scripts\python.exe" (
    echo  %GRN%  ✓%RST%  Virtual environment
) else (
    echo  %RED%  ✗%RST%  Virtual environment %RED%NOT FOUND%RST%
    echo.
    echo      %YLW%  Fix:%RST%  python -m venv .venv
    echo             .venv\Scripts\pip install -r requirements.txt
    set CHECKS_PASSED=0
)

REM .env file
if exist "%ROOT%.env" (
    echo  %GRN%  ✓%RST%  .env credentials file
) else (
    echo  %YLW%  !%RST%  .env file %YLW%not found%RST% — API keys missing
    echo.
    echo      %YLW%  Fix:%RST%  copy .env.example .env  ^(then fill your keys^)
    set CHECKS_PASSED=0
)

REM logs directory
if not exist "%ROOT%logs\" (
    mkdir "%ROOT%logs"
    echo  %GRN%  ✓%RST%  Created logs\ directory
) else (
    echo  %GRN%  ✓%RST%  logs\ directory
)

REM Models directory
if exist "%ROOT%Data\Models\" (
    for /f %%C in ('dir /b "%ROOT%Data\Models\*.joblib" 2^>nul ^| find /c /v ""') do set MDL_COUNT=%%C
    if !MDL_COUNT! gtr 0 (
        echo  %GRN%  ✓%RST%  ML models ^(!MDL_COUNT! files found^)
    ) else (
        echo  %YLW%  !%RST%  No model files — run: python Data\Trainer\AegisQuantTrainer.py
    )
) else (
    echo  %YLW%  !%RST%  Models directory missing
)

REM WatchdogSupervisor check
if exist "%ROOT%WatchdogSupervisor.py" (
    echo  %GRN%  ✓%RST%  WatchdogSupervisor.py
) else (
    echo  %RED%  ✗%RST%  WatchdogSupervisor.py %RED%NOT FOUND%RST%
    set CHECKS_PASSED=0
)

echo.

if "%CHECKS_PASSED%"=="0" (
    echo  %RED%%BLD%  ✗  Critical checks failed. Fix the above issues first.%RST%
    echo.
    pause
    exit /b 1
)

echo  %GRN%%BLD%  All checks passed.%RST%
echo.

REM ── Launch services ───────────────────────────────────────────────
echo  %WHT%%BLD%LAUNCHING SERVICES%RST%
echo  %DIM%  ─────────────────────────────────────────────────────%RST%
echo.

echo  %CYN%  [1/2]%RST%  Starting Watchdog Supervisor...
start "AegisQuant — Watchdog" cmd /k "%ROOT%run_watchdog.bat"
timeout /t 3 /nobreak >nul

echo  %CYN%  [2/2]%RST%  Starting Dashboard...
start "AegisQuant — Dashboard" cmd /k "%ROOT%run_dashboard.bat"
timeout /t 2 /nobreak >nul

echo.
echo %GRN%%BLD%  ╔══════════════════════════════════════════════════════════╗%RST%
echo %GRN%%BLD%  ║   ✓  AegisQuant is RUNNING                              ║%RST%
echo %GRN%%BLD%  ╠══════════════════════════════════════════════════════════╣%RST%
echo %GRN%  ║%RST%                                                          %GRN%%BLD%║%RST%
echo %GRN%  ║%RST%   %CYN%Dashboard:%RST%   http://localhost:8501                  %GRN%%BLD%║%RST%
echo %GRN%  ║%RST%   %CYN%Watchdog:%RST%    Auto-restarts engine on crash          %GRN%%BLD%║%RST%
echo %GRN%  ║%RST%   %CYN%Logs:%RST%        %ROOT%logs\          %GRN%%BLD%║%RST%
echo %GRN%  ║%RST%                                                          %GRN%%BLD%║%RST%
echo %GRN%  ║%RST%   %DIM%To stop: run stop.bat or close Watchdog window%RST%       %GRN%%BLD%║%RST%
echo %GRN%%BLD%  ╚══════════════════════════════════════════════════════════╝%RST%
echo.
echo  This window can be safely closed.
echo.
pause
exit /b 0
