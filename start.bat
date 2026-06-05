@echo off
REM ================================================================
REM  start.bat  —  AegisQuant Quick Launcher
REM  Usage:  start.bat           (launches engine + dashboard)
REM          start.bat engine    (engine only)
REM          start.bat dash      (dashboard only)
REM ================================================================
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
cd /d "%ROOT%"
reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul 2>&1

set "GRN=[92m"
set "RED=[91m"
set "YLW=[93m"
set "CYN=[96m"
set "WHT=[97m"
set "DIM=[90m"
set "RST=[0m"
set "BLD=[1m"

echo.
echo %CYN%%BLD%  AegisQuant — Quick Start%RST%
echo  %DIM%  ──────────────────────────────────────%RST%
echo.

REM Sanity check
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo  %RED%  ✗%RST%  .venv not found. Run Run_Master.bat for setup guidance.
    pause & exit /b 1
)

set MODE=all
if /i "%1"=="engine" set MODE=engine
if /i "%1"=="dash"   set MODE=dash
if /i "%1"=="dashboard" set MODE=dash

if "%MODE%"=="engine" goto :start_engine
if "%MODE%"=="dash"   goto :start_dash

:start_both
echo  %CYN%  Starting engine + dashboard...%RST%
start "AegisQuant — Watchdog" cmd /k "%ROOT%run_watchdog.bat"
timeout /t 2 /nobreak >nul
start "AegisQuant — Dashboard" cmd /k "%ROOT%run_dashboard.bat"
echo.
echo  %GRN%  ✓ Engine:     running (Watchdog auto-restart active)%RST%
echo  %GRN%  ✓ Dashboard:  http://localhost:8501%RST%
goto :done

:start_engine
echo  %CYN%  Starting engine only...%RST%
start "AegisQuant — Watchdog" cmd /k "%ROOT%run_watchdog.bat"
echo.
echo  %GRN%  ✓ Engine running (Watchdog auto-restart active)%RST%
echo  %DIM%    Tip: start.bat dash  — to also start the dashboard%RST%
goto :done

:start_dash
echo  %CYN%  Starting dashboard only...%RST%
start "AegisQuant — Dashboard" cmd /k "%ROOT%run_dashboard.bat"
echo.
echo  %GRN%  ✓ Dashboard: http://localhost:8501%RST%
goto :done

:done
echo.
echo  %DIM%  To stop all:  stop.bat%RST%
echo.
timeout /t 3 /nobreak >nul
exit /b 0
