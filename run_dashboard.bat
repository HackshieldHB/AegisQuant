@echo off
REM ============================================================
REM run_dashboard.bat - Streamlit Dashboard Launcher
REM ============================================================
REM Starts the real-time monitoring dashboard.
REM Access at: http://localhost:8501
REM ============================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

title AegisQuant Dashboard

echo.
echo ============================================================
echo   AegisQuant Dashboard
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
    echo [WARN] .env not found — dashboard may show limited data.
    echo.
)

set PYTHONUNBUFFERED=1

echo Starting dashboard...
echo Access at: http://localhost:8501
echo Press Ctrl+C to stop.
echo.

"%SCRIPT_DIR%.venv\Scripts\python.exe" -m streamlit run "%SCRIPT_DIR%Dashboard\app.py" ^
  --server.headless=true ^
  --server.port=8501 ^
  --server.address=localhost ^
  --browser.gatherUsageStats=false ^
  --logger.level=warning

set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% neq 0 (
    echo.
    echo [ERROR] Dashboard exited with code %EXIT_CODE%
)

echo.
echo Dashboard stopped.  Press any key to close this window.
pause
exit /b %EXIT_CODE%
