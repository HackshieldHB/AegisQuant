@echo off
REM ================================================================
REM  stop.bat  —  AegisQuant Stop All Services
REM  Finds and terminates all running AegisQuant Python processes
REM  and the Streamlit dashboard gracefully.
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
echo %YLW%%BLD%  AegisQuant — Stop All Services%RST%
echo  %DIM%  ──────────────────────────────────────%RST%
echo.

set STOPPED=0

REM ── Stop Main trading engine ──────────────────────────────────────
for /f "tokens=2" %%P in ('tasklist /fi "IMAGENAME eq python.exe" /fo csv /nh 2^>nul ^| findstr /i "Main.py\|WatchdogSupervisor\|Main_Production"') do (
    set "PID=%%~P"
    taskkill /pid !PID! /f >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo  %GRN%  ✓%RST%  Stopped trading engine  %DIM%(PID !PID!)%RST%
        set STOPPED=1
    )
)

REM ── Broader Python process search for AegisQuant venv ─────────────
set "VENV_PYTHON=%ROOT%.venv\Scripts\python.exe"
for /f "tokens=2 delims=," %%P in ('wmic process where "ExecutablePath like '%%%ROOT:.=_%%%'" get ProcessId /format:csv 2^>nul') do (
    set "PID=%%P"
    if defined PID (
        taskkill /pid !PID! /f >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            echo  %GRN%  ✓%RST%  Stopped process  %DIM%(PID !PID!)%RST%
            set STOPPED=1
        )
    )
)

REM ── Stop Streamlit / dashboard ────────────────────────────────────
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr ":8501"') do (
    set "PID=%%P"
    if "!PID!" neq "0" (
        taskkill /pid !PID! /f >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            echo  %GRN%  ✓%RST%  Stopped dashboard  %DIM%(port 8501, PID !PID!)%RST%
            set STOPPED=1
        )
    )
)

REM ── Remove singleton lock if leftover ─────────────────────────────
if exist "%ROOT%logs\aegisquant_engine.lock" (
    del /f /q "%ROOT%logs\aegisquant_engine.lock" >nul 2>&1
    echo  %GRN%  ✓%RST%  Cleared singleton lock file
)

echo.
if "%STOPPED%"=="1" (
    echo  %GRN%%BLD%  All AegisQuant services stopped.%RST%
) else (
    echo  %YLW%  No running AegisQuant processes found.%RST%
)
echo.
pause
exit /b 0
