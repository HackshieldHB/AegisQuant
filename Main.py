"""
Main.py — AegisQuant Trading Engine Entry Point
==============================================

This is the primary entry point for the autonomous trading engine.

USAGE:
  Standard:     python Main.py
  Supervised:   python WatchdogSupervisor.py (handles auto-restart)
  With Launcher: Run_Master.bat (Windows) or run_dashboard.bat + run_watchdog.bat

CONFIGURATION:
  All settings are in AegisQuantConfig.py
  Environment variables override defaults (see .env.example)

STARTUP FLOW:
  1. Configuration validation
  2. System diagnostics
  3. State recovery
  4. Exchange reconciliation
  5. Trading begins

GRACEFUL SHUTDOWN:
  Ctrl+C: Graceful shutdown, saves state
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Main_Production import main

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except Exception as e:
        from Core.Logger import AG_LOGGER
        logger = AG_LOGGER
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
