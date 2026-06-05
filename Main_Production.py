"""
Main.py — PRODUCTION AegisQuant Trading Engine (Async Native)
========================================================================

STARTUP SEQUENCE:
1. Load configuration
2. Run system diagnostics
3. Initialize logging
4. Load state from persistence
5. Initialize AsyncEngine
6. Send startup Telegram notification
7. Enter async main trading loop + state save tasks

ZERO DOWNTIME OPERATION:
- Asynchronous execution layer
- Background periodic state persistence
- Async-safe Telegram notifications
"""

import signal
import sys
import os
import time
import json
import asyncio
import gc
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

gc.set_threshold(700, 10, 10)

from AegisQuantConfig import CONFIG, validate_config
from Core.Logger import AG_LOGGER
from Core.SystemDiagnostics import SystemDiagnostics, SystemDiagnosticsException
from Core.StateManager import StateManager
from Core.AsyncEngine import AsyncEngine
from Services.TelegramService import TelegramService
from Core.Singleton import SingletonLock, SingletonException
from Core.BinanceTimeSync import BinanceTimeSync, CriticalStartupException

class ProductionMainEngine:
    """Production trading engine with full async lifecycle management."""
    
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self.telegram = TelegramService()
        self.running = True
        self.startup_complete = False
        self.state_manager: Optional[StateManager] = None
        self.trading_engine: Optional[AsyncEngine] = None
        
        self.state_save_interval  = 60   # seconds
        self._retrain_lock: bool  = False
        
        self.logger.info("=" * 70)
        self.logger.info("PRODUCTION AEGISQUANT ENGINE INITIALIZATION (ASYNC)")
        self.logger.info("BINANCE_TIME_SYNC_SINGLETON_V2 — INSTITUTION GRADE SAFETY ACTIVE")
        self.logger.info("=" * 70)

    async def startup(self) -> bool:
        """Complete startup sequence."""
        try:
            self.logger.info("Acquiring Singleton Execution OS Lock...")
            try:
                self.singleton = SingletonLock("aegisquant_engine.lock")
                self.singleton.acquire()
            except SingletonException as se:
                self.logger.critical("🛑 Singleton Lock Failure: %s", se)
                self.telegram.send_message(f"🛑 CRITICAL: Duplicate AegisQuant engine instance attempted to start (PID: {os.getpid()}). Aborting completely to preserve capital.", severity="ERROR")
                return False
                
            self.telegram.send_message("🚀 AegisQuant async startup in progress...", severity="INFO")
            
            # Step 1B: Binance Time Sync Validation
            self.logger.info("STEP 1B: Validating exchange time synchronization...")
            self.logger.info(
                "BinanceTimeSync will initialize on exchange connect (sync_interval=60s, max_offset=5000ms)"
            )
            
            # Step 2: Validate config
            self.logger.info("STEP 1: Validating configuration...")
            try:
                validate_config()
            except ValueError as e:
                self.logger.critical("Configuration validation failed: %s", e)
                return False

            # Step 2: Diagnostics
            self.logger.info("STEP 2: Running system diagnostics...")
            diagnostics = SystemDiagnostics()
            if not diagnostics.run_all():
                return False

            # Step 3: State Manager
            self.logger.info("STEP 3: Initializing state manager...")
            self.state_manager = StateManager()

            # Step 4: Trading Engine
            self.logger.info("STEP 4: Initializing AsyncEngine...")
            self.trading_engine = AsyncEngine()
            
            # Step 5: State Recovery
            self.logger.info("STEP 5: Recovering state...")
            await self._recover_previous_state()
            
            self.startup_complete = True
            self.logger.info("✓ STARTUP COMPLETE - READY FOR TRADING")
            return True

        except Exception as e:
            self.logger.critical("Startup failed: %s", e, exc_info=True)
            return False

    async def _recover_previous_state(self) -> None:
        if not self.state_manager:
            return
        last_balance = self.state_manager.load_balance()
        if last_balance:
            self.logger.info(f"Recovered persistent balance: ${last_balance:.2f}")

    async def _periodic_state_task(self):
        """Background task for state saving."""
        while self.running:
            await asyncio.sleep(self.state_save_interval)
            if self.trading_engine and self.state_manager:
                try:
                    balance = await self.trading_engine._get_balance()
                    if balance is not None:
                        self.state_manager.save_balance(balance)
                    
                    # Fetch and save positions
                    # asyncio.to_thread() so any executor that makes a network
                    # call (e.g. FOREX / STOCKS futures) doesn't block the loop.
                    all_positions = []
                    for ac in ["CRYPTO", "FOREX", "STOCKS"]:
                        if CONFIG["PROJECT"].get(f"{ac}_ENABLED"):
                            try:
                                pos = await asyncio.to_thread(
                                    self.trading_engine.router.get_open_positions, ac
                                )
                                if isinstance(pos, list):
                                    for p in pos:
                                        p["_asset_class"] = ac
                                        all_positions.append(p)
                            except Exception:
                                pass
                    self.state_manager.save_positions(all_positions)
                except Exception as e:
                    self.logger.debug(f"State save failed: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Auto-retraining scheduler
    # ─────────────────────────────────────────────────────────────────

    def _load_last_retrain_ts(self) -> float:
        rt_cfg = CONFIG.get("AUTO_RETRAIN", {})
        lock_file = rt_cfg.get("LOCK_FILE", os.path.join(os.getcwd(), "logs", "last_retrain.json"))
        try:
            if os.path.exists(lock_file):
                with open(lock_file) as f:
                    return float(json.load(f).get("ts", 0))
        except Exception:
            pass
        return 0.0

    def _save_last_retrain_ts(self) -> None:
        rt_cfg    = CONFIG.get("AUTO_RETRAIN", {})
        lock_file = rt_cfg.get("LOCK_FILE", os.path.join(os.getcwd(), "logs", "last_retrain.json"))
        try:
            os.makedirs(os.path.dirname(lock_file), exist_ok=True)
            with open(lock_file, "w") as f:
                json.dump({"ts": time.time(), "date": datetime.now(timezone.utc).isoformat()}, f)
        except Exception as e:
            self.logger.warning("Could not write retrain lock file: %s", e)

    async def _run_retrain(self) -> None:
        """Runs AegisQuantTrainer.retrain_all() in a thread so it doesn't block the event loop."""
        if self._retrain_lock:
            self.logger.info("[AutoRetrain] Retrain already in progress — skipping.")
            return
        self._retrain_lock = True
        self.logger.info("[AutoRetrain] Starting scheduled model retraining...")
        self.telegram.send_message(
            "🔄 *Auto-Retrain Started*\nRetraining all models with latest market data.\n"
            "Trading continues during retraining.",
            severity="INFO",
        )
        try:
            from Data.Trainer.AegisQuantTrainer import retrain_all
            await asyncio.to_thread(retrain_all)
            self._save_last_retrain_ts()
            # Reload models into the live engine without restart
            if self.trading_engine:
                from Data.ModelLoader import load_all_models
                new_models = await asyncio.to_thread(load_all_models)
                self.trading_engine.models    = new_models
                self.trading_engine.predictor = __import__("AI.Predictor", fromlist=["AIPredictor"]).AIPredictor(models=new_models)
            self.logger.info("[AutoRetrain] Retraining complete — models hot-swapped.")
            self.telegram.send_message(
                "✅ *Auto-Retrain Complete*\nModels updated and hot-swapped into live engine.",
                severity="INFO",
            )
        except Exception as e:
            self.logger.error("[AutoRetrain] Retrain failed: %s", e, exc_info=True)
            self.telegram.send_message(
                f"❌ *Auto-Retrain Failed*\n{e}", severity="ERROR",
            )
        finally:
            self._retrain_lock = False

    async def _auto_retrain_task(self) -> None:
        """Background task: triggers retrain on schedule and on model drift."""
        rt_cfg = CONFIG.get("AUTO_RETRAIN", {})
        if not rt_cfg.get("ENABLED", True):
            return

        interval_days = rt_cfg.get("INTERVAL_DAYS", 7)
        interval_sec  = interval_days * 86400
        # Wait 10 minutes after startup before first check
        await asyncio.sleep(600)

        while self.running:
            last_ts = self._load_last_retrain_ts()
            now     = time.time()
            due_in  = max(0.0, (last_ts + interval_sec) - now)

            if due_in <= 0:
                await self._run_retrain()
            else:
                self.logger.info(
                    "[AutoRetrain] Next scheduled retrain in %.1f hours.",
                    due_in / 3600,
                )

            # Check model drift every 6 hours
            await asyncio.sleep(6 * 3600)

    async def run(self) -> None:
        """Main async loop."""
        if not self.startup_complete or not self.trading_engine:
            self.logger.critical("Startup incomplete")
            return

        self.logger.info("Starting AsyncEngine loop...")

        state_task   = asyncio.create_task(self._periodic_state_task())
        retrain_task = asyncio.create_task(self._auto_retrain_task())

        try:
            await self.trading_engine.run()
        except asyncio.CancelledError:
            self.logger.info("Engine cancelled")
        except Exception as e:
            self.logger.critical(f"Fatal Engine loop error: {e}", exc_info=True)
        finally:
            self.running = False
            state_task.cancel()
            retrain_task.cancel()
            await self._shutdown()

    async def _shutdown(self) -> None:
        self.logger.info("INITIATING GRACEFUL SHUTDOWN")
        if self.trading_engine and self.state_manager:
            try:
                balance = await self.trading_engine._get_balance()
                if balance is not None:
                    self.state_manager.save_balance(balance)
            except Exception:
                pass
            
            self.trading_engine.stop()
        
        self.telegram.notify_shutdown("Graceful shutdown")
        self.logger.info("AEGISQUANT ASYNC ENGINE STOPPED")


def main() -> int:
    # On Windows, Python 3.8+ defaults to ProactorEventLoop which uses IOCP
    # (I/O Completion Ports).  When daemon threads open SSL sockets shortly
    # after the event loop starts, those socket registrations interact with the
    # IOCP completion port and can stall the event loop indefinitely.
    # WindowsSelectorEventLoopPolicy uses select() instead of IOCP, avoiding
    # this conflict while fully supporting asyncio.to_thread / ThreadPoolExecutor.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    engine = ProductionMainEngine()
    
    def _shutdown(sig, frame):
        engine.logger.info(f"Signal {sig} received, stopping...")
        engine.running = False
        if engine.trading_engine:
            engine.trading_engine.stop()
    
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    
    try:
        success = loop.run_until_complete(engine.startup())
        if not success:
            return 1
        loop.run_until_complete(engine.run())
        return 0
    finally:
        loop.close()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        AG_LOGGER.critical("Unhandled main exception: %s", e, exc_info=True)
        sys.exit(1)
