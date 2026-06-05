"""
WatchdogSupervisor — Production supervision and auto-restart.
============================================================

Monitor trading engine and:
- Detect crashes or unresponsive state
- Auto-restart with exponential backoff
- Prevent infinite restart loops (max restart limit)
- Log all supervisor events
- Send Telegram alerts on restart
- Graceful shutdown on demand

Usage:
  python run_watchdog.py
  
Or from watchdog script:
  python WatchdogSupervisor.py
"""

import os
import sys
import time
import signal
import subprocess
import threading
from datetime import datetime, timezone
from typing import Optional, List

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER
from Services.TelegramService import TelegramService
from Core.Singleton import SingletonLock, SingletonException


class WatchdogSupervisor:
    """Supervise and manage trading engine process."""
    
    MAX_RESTARTS_PER_HOUR = 5
    RESTART_BACKOFF_BASE = 30   # seconds (30 → 60 → 150 → 300)
    RESTART_BACKOFF_MAX = 300   # 5 minutes cap
    ENGINE_HEALTH_CHECK_INTERVAL = 10   # seconds
    # If no heartbeat file update for this many seconds → engine is frozen
    # (process alive but asyncio loop stalled).  15 min cycle + 10 min grace.
    ENGINE_HEARTBEAT_STALE_SEC = 2700   # 45 minutes
    SUBPROCESS_LOG = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs",
        "engine_subprocess.log"
    )
    HEARTBEAT_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs",
        "engine_heartbeat.json"
    )
    
    def __init__(self, engine_script: str = "Main.py") -> None:
        self.logger = AG_LOGGER
        self.telegram = TelegramService()
        self.engine_script = engine_script
        self.engine_process: Optional[subprocess.Popen] = None
        self.running = True
        self.restart_count = 0
        self.last_restart_time = 0
        self.start_times_this_hour: List[float] = []
        
        # Signal handlers
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        
        self.logger.info("WatchdogSupervisor initialized (engine_script=%s)", engine_script)

    def _shutdown(self, signum: int, frame) -> None:
        """Handle shutdown signal."""
        self.logger.info("Shutdown signal received (%s)", signum)
        self.running = False
        self._stop_engine()

    def _get_restart_count_this_hour(self) -> int:
        """Get number of restarts in the last hour."""
        now = time.time()
        one_hour_ago = now - 3600
        self.start_times_this_hour = [t for t in self.start_times_this_hour if t > one_hour_ago]
        return len(self.start_times_this_hour)

    def _can_restart(self) -> bool:
        """Check if we can restart (respect limits)."""
        restart_count = self._get_restart_count_this_hour()
        
        if restart_count >= self.MAX_RESTARTS_PER_HOUR:
            self.logger.critical(
                "Max restarts per hour exceeded (%s >= %s). Watchdog entering TERMINAL HALT.",
                restart_count,
                self.MAX_RESTARTS_PER_HOUR
            )
            self.telegram.send_message(
                f"🔴 *TERMINAL HALT*\n"
                f"Max restarts exceeded: {restart_count}/{self.MAX_RESTARTS_PER_HOUR}\n"
                f"Supervisor entering permanent halt. Manual intervention required.",
                severity="CRITICAL"
            )
            return False
        
        return True

    def _get_backoff_delay(self) -> float:
        """Exponential backoff: 30s → 60s → 150s → 300s (capped)."""
        restart_count = self._get_restart_count_this_hour()
        delay = self.RESTART_BACKOFF_BASE * (2 ** max(0, restart_count - 1))
        delay = min(delay, self.RESTART_BACKOFF_MAX)
        self.logger.info(
            "Restart backoff: %.0fs (attempt %d/%d)",
            delay, restart_count + 1, self.MAX_RESTARTS_PER_HOUR
        )
        return delay

    def _capture_subprocess_output(self, stream, stream_name: str) -> None:
        """Capture subprocess output to separate file AND echo to main logger."""
        try:
            if stream is None:
                return

            os.makedirs(os.path.dirname(self.SUBPROCESS_LOG), exist_ok=True)

            with open(self.SUBPROCESS_LOG, "a", encoding="utf-8", buffering=1) as f:
                for line in iter(stream.readline, ''):
                    if not line:
                        continue
                    line_stripped = line.rstrip("\n")
                    # Write to dedicated subprocess log file
                    f.write(line)
                    # Also echo into the main watchdog logger so everything
                    # appears in aegis_quant.log and the console in one place.
                    if stream_name == "STDERR":
                        self.logger.warning("[ENGINE] %s", line_stripped)
                    else:
                        self.logger.info("[ENGINE] %s", line_stripped)

        except Exception as e:
            self.logger.debug("Error capturing %s: %s", stream_name, e)

    def _start_engine(self) -> bool:
        """Start the trading engine process."""
        try:
            self.logger.info("Starting trading engine: %s", self.engine_script)
            
            engine_path = os.path.join(os.path.dirname(__file__), self.engine_script)
            if not os.path.exists(engine_path):
                self.logger.error("Engine script not found: %s", engine_path)
                return False
            
            # Build subprocess environment: inherit current env and force
            # unbuffered output so log lines appear immediately (not batched).
            sub_env = dict(os.environ)
            sub_env["PYTHONUNBUFFERED"] = "1"
            sub_env["PYTHONDONTWRITEBYTECODE"] = "1"

            # Start subprocess with output capture
            self.engine_process = subprocess.Popen(
                [sys.executable, engine_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,       # Line-buffered (requires text=True)
                env=sub_env,
            )
            
            # Start threads to capture and log subprocess output
            stdout_thread = threading.Thread(
                target=self._capture_subprocess_output,
                args=(self.engine_process.stdout, "STDOUT"),
                daemon=True
            )
            stderr_thread = threading.Thread(
                target=self._capture_subprocess_output,
                args=(self.engine_process.stderr, "STDERR"),
                daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()
            
            self.logger.info("Engine started with PID %s", self.engine_process.pid)
            self.restart_count += 1
            self.last_restart_time = time.time()
            self.start_times_this_hour.append(self.last_restart_time)
            
            # Log restart event
            restart_summary = self._get_restart_count_this_hour()
            self.logger.info("Restart count this hour: %s/%s", restart_summary, self.MAX_RESTARTS_PER_HOUR)
            
            return True
            
        except Exception as e:
            self.logger.error("Failed to start engine: %s", e)
            return False

    def _stop_engine(self) -> None:
        """Stop the trading engine process gracefully."""
        if self.engine_process is None:
            return
        
        try:
            self.logger.info("Stopping engine (PID %s)", self.engine_process.pid)
            
            # Try graceful shutdown first
            self.engine_process.terminate()
            try:
                self.engine_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.logger.warning("Engine did not respond to SIGTERM, forcing kill")
                self.engine_process.kill()
                self.engine_process.wait(timeout=5)
            
            self.logger.info("Engine stopped")
            self.engine_process = None
            
        except Exception as e:
            self.logger.error("Error stopping engine: %s", e)

    def _check_engine_health(self) -> bool:
        """Check if engine process is still running AND not frozen.

        Two-layer check:
          1. Process exit code — engine crashed / exited.
          2. Heartbeat file age — engine process is alive but asyncio loop stalled
             (the scenario that caused the 5-hour freeze).
        """
        if self.engine_process is None:
            return False

        # ── Layer 1: process alive? ───────────────────────────
        poll_result = self.engine_process.poll()
        if poll_result is not None:
            self.logger.warning("Engine process exited with code %s", poll_result)
            return False

        # ── Layer 2: heartbeat file fresh? ───────────────────
        # Only start checking after the engine has had time to complete its
        # first cycle (give it 20 min on startup before we expect a heartbeat).
        uptime = time.time() - self.last_restart_time
        if uptime > 1200 and os.path.exists(self.HEARTBEAT_FILE):
            try:
                import json as _json
                with open(self.HEARTBEAT_FILE) as f:
                    hb = _json.load(f)
                age = time.time() - float(hb.get("ts", 0))
                if age > self.ENGINE_HEARTBEAT_STALE_SEC:
                    self.logger.critical(
                        "ENGINE FROZEN: last heartbeat %.0f min ago (threshold %.0f min). "
                        "Killing and restarting.",
                        age / 60, self.ENGINE_HEARTBEAT_STALE_SEC / 60,
                    )
                    self.telegram.send_message(
                        f"⚠️ *Engine Freeze Detected*\n"
                        f"Last heartbeat: {age/60:.0f} min ago\n"
                        f"Force-killing and restarting...",
                        severity="CRITICAL",
                    )
                    # Kill the frozen process so the main loop triggers a restart
                    try:
                        self.engine_process.kill()
                    except Exception:
                        pass
                    return False
            except Exception as e:
                self.logger.debug("Heartbeat file read error: %s", e)

        return True

    def _read_engine_output(self) -> None:
        """Read and log engine output (Handled by daemon threads on lines 131-140)."""
        pass

    def run(self) -> None:
        """Main watchdog loop."""
        self.logger.info("=" * 70)
        self.logger.info("WATCHDOG SUPERVISOR STARTED")
        self.logger.info("=" * 70)
        
        try:
            # Start engine
            if not self._start_engine():
                self.logger.critical("Failed to start engine on first attempt")
                self.telegram.send_message(
                    "🔴 *Watchdog Critical*\n"
                    "Failed to start engine on first attempt.\n"
                    "Check logs for details."
                )
                return
            
            # Send startup notification
            self.telegram.send_message(
                f"🟢 *Watchdog Active*\n"
                f"Engine: {self.engine_script}\n"
                f"Mode: {CONFIG['PROJECT']['MODE']}\n"
                f"Symbols: {sum(len(v) for v in CONFIG['SYMBOLS'].values())}\n"
                f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
            )
            
            # Main supervision loop
            while self.running:
                try:
                    # Check engine health
                    if not self._check_engine_health():
                        if self.running:
                            self.logger.warning("Engine process died, attempting restart")
                            self.telegram.send_message(
                                f"⚠️  *Engine Crash Detected*\n"
                                f"Restart attempt: {self._get_restart_count_this_hour()}/{self.MAX_RESTARTS_PER_HOUR}\n"
                                f"_{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}_"
                            )
                            
                            if self._can_restart():
                                backoff = self._get_backoff_delay()
                                self.logger.info("Waiting %.0fs before restart...", backoff)
                                time.sleep(backoff)
                                if not self._start_engine():
                                    self.logger.critical("Failed to restart engine")
                                    break
                            else:
                                self.logger.critical("Cannot restart; limit exceeded")
                                break
                    
                    # Read engine output
                    self._read_engine_output()
                    
                    # Sleep before next check
                    time.sleep(self.ENGINE_HEALTH_CHECK_INTERVAL)
                    
                except KeyboardInterrupt:
                    self.logger.info("Keyboard interrupt received")
                    break
                except Exception as e:
                    self.logger.error("Watchdog error: %s", e)
                    time.sleep(5)
            
        finally:
            self.logger.info("=" * 70)
            self.logger.info("WATCHDOG SUPERVISOR SHUTTING DOWN")
            self.logger.info("=" * 70)
            self._stop_engine()
            self.telegram.send_message(
                f"⛔ *Watchdog Offline*\n"
                f"Supervisor stopped\n"
                f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
            )


def main() -> None:
    """Entry point for watchdog supervisor."""
    logger = AG_LOGGER
    try:
        # Step 1: Guarantee Supervisor Exclusivity
        logger.info("Acquiring Supervisor Singleton OS Lock...")
        try:
            singleton = SingletonLock("aegisquant_watchdog.lock")
            singleton.acquire()
        except SingletonException as se:
            logger.critical("🛑 Supervisor Lock Failure: %s", se)
            logger.critical("Execution HALTED. A Watchdog supervisor is already managing the engine.")
            sys.exit(1)
            
        # Step 2: Proceed to supervision once lock is secured    
        supervisor = WatchdogSupervisor(engine_script="Main.py")
        
        # Keep reference to avoid garbage collection of lock
        supervisor.singleton = singleton 
        supervisor.run()
        
    except Exception as e:
        logger.critical("Fatal watchdog error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
