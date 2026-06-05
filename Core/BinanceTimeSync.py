"""
BinanceTimeSync — Exchange Clock Synchronization
=================================================
Solves Binance error -1021 (Timestamp for this request was 1000ms
ahead of the server's time) by computing and applying offset.

SAFETY CONSTRAINTS:
- Blocks startup if offset > 5000ms (severe clock drift)
- Re-syncs every 60 seconds via lazy refresh
- Thread-safe for concurrent symbol workers
- Never alters trading logic
"""

import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional


class CriticalStartupException(Exception):
    """Raised when a condition unsafe for trading is detected at startup."""
    pass


class BinanceTimeSync:
    """
    Singleton-style exchange time synchronizer.
    Computes local-to-exchange offset and applies to all signed requests.
    """
    _instance: Optional['BinanceTimeSync'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, exchange=None, max_offset_ms: int = 5000, sync_interval_sec: float = 60.0):
        if getattr(self, "_initialized", False):
            # Allow injection or replacement (reconnect-safe)
            if exchange is not None:
                if getattr(self, "_exchange", None) is None or self._exchange is not exchange:
                    self.set_exchange(exchange)
            return

        self._exchange = None
        self._max_offset_ms = max_offset_ms
        self._sync_interval = sync_interval_sec
        self._offset_ms: int = 0
        self._last_sync: float = 0.0
        self._sync_count: int = 0
        self._sync_lock = threading.Lock()
        self._initialized = True

        if exchange is not None:
            self.set_exchange(exchange)

    def set_exchange(self, exchange) -> None:
        """Set/update the CCXT exchange instance."""
        self._exchange = exchange

    def sync(self) -> int:
        """
        Query Binance server time and compute offset.
        Returns offset in milliseconds.
        Raises CriticalStartupException if offset exceeds safety limit.
        """
        with self._sync_lock:
            if self._exchange is None:
                logging.warning("[BinanceTimeSync] No exchange set, skipping sync")
                return self._offset_ms

            try:
                # Measure round-trip to minimize network-induced error
                t_before = time.time()
                server_time = self._exchange.fetch_time()  # CCXT unified
                t_after = time.time()

                # Estimate one-way latency
                rtt_ms = (t_after - t_before) * 1000
                local_time_ms = int(((t_before + t_after) / 2) * 1000)

                self._offset_ms = server_time - local_time_ms
                self._last_sync = time.time()
                self._sync_count += 1

                logging.info(
                    "[BinanceTimeSync] Sync #%d: offset=%dms, rtt=%.0fms, "
                    "server=%s, local=%s",
                    self._sync_count,
                    self._offset_ms,
                    rtt_ms,
                    datetime.fromtimestamp(server_time / 1000, tz=timezone.utc).isoformat(),
                    datetime.fromtimestamp(local_time_ms / 1000, tz=timezone.utc).isoformat(),
                )

                if abs(self._offset_ms) > self._max_offset_ms:
                    msg = (
                        f"[BinanceTimeSync] CRITICAL: Clock offset {self._offset_ms}ms "
                        f"exceeds safety limit ({self._max_offset_ms}ms). "
                        f"Signed requests WILL be rejected by exchange."
                    )
                    logging.critical(msg)
                    raise CriticalStartupException(msg)

                return self._offset_ms

            except CriticalStartupException:
                raise
            except Exception as e:
                logging.error("[BinanceTimeSync] Sync failed: %s", e)
                return self._offset_ms

    def get_timestamp(self) -> int:
        """
        Returns exchange-corrected timestamp in milliseconds.
        Lazy re-sync if interval has elapsed.
        """
        if time.time() - self._last_sync > self._sync_interval:
            try:
                self.sync()
            except CriticalStartupException:
                # During runtime, log but don't crash — use last known offset
                logging.warning("[BinanceTimeSync] Re-sync failed, using last offset: %dms", self._offset_ms)

        return int(time.time() * 1000) + self._offset_ms

    @property
    def offset_ms(self) -> int:
        return self._offset_ms

    @property
    def last_sync_age_sec(self) -> float:
        if self._last_sync == 0:
            return float('inf')
        return time.time() - self._last_sync

    @property
    def sync_count(self) -> int:
        return self._sync_count

    def get_diagnostics(self) -> dict:
        """Export for telemetry and diagnostics."""
        return {
            "offset_ms": self._offset_ms,
            "last_sync_age_sec": round(self.last_sync_age_sec, 1),
            "sync_count": self._sync_count,
            "max_offset_ms": self._max_offset_ms,
            "sync_interval_sec": self._sync_interval,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def reset_singleton(cls):
        """Test-only: reset singleton for clean test isolation."""
        with cls._lock:
            cls._instance = None
