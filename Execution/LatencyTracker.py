"""
LatencyTracker — Runtime Decision Latency Observability
========================================================
Phase G-1 Fix: Measures tick-to-signal and signal-to-order latency
with O(1) per-event overhead using monotonic high-precision timers.

CRITICAL CONSTRAINTS:
- Observability ONLY — never blocks the engine loop
- Never halts or throttles trading
- Thread-safe via deque (GIL-protected atomic ops)
- Fixed-size sliding window prevents memory growth
"""

import time
import logging
import threading
import numpy as np
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional, Any


class LatencyTracker:
    """
    Non-blocking, thread-safe latency instrumentation layer.
    Captures tick→signal→order timing with rolling statistics.
    """

    def __init__(
        self,
        window_size: int = 10_000,
        alert_threshold_ms: float = 500.0,
        alert_callback=None,
    ):
        self._window_size = window_size
        self._alert_threshold_ms = alert_threshold_ms
        self._alert_callback = alert_callback

        # Rolling storage — deque is thread-safe for append/popleft under GIL
        self._tick_to_signal: deque = deque(maxlen=window_size)
        self._signal_to_order: deque = deque(maxlen=window_size)
        self._total_latency: deque = deque(maxlen=window_size)
        self._events: deque = deque(maxlen=window_size)

        # Per-event scratch state (keyed by symbol to support concurrent symbols)
        self._pending: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

        self._alert_count = 0

    # ----------------------------------------------------------------
    # PUBLIC API — 3-phase timestamp capture
    # ----------------------------------------------------------------

    def mark_tick_received(self, symbol: str) -> None:
        """Phase 1: Call when market data arrives from WebSocket/REST."""
        ts = time.perf_counter()
        with self._lock:
            self._pending[symbol] = {"tick": ts}

    def mark_signal_generated(self, symbol: str) -> None:
        """Phase 2: Call immediately after model inference completes."""
        ts = time.perf_counter()
        with self._lock:
            if symbol in self._pending:
                self._pending[symbol]["signal"] = ts

    def mark_order_submitted(self, symbol: str) -> None:
        """Phase 3: Call right before sending order to exchange (or hypothetical in Shadow)."""
        ts = time.perf_counter()
        with self._lock:
            entry = self._pending.pop(symbol, None)

        if entry is None or "tick" not in entry:
            return

        tick_ts = entry["tick"]
        signal_ts = entry.get("signal", ts)  # fallback if signal wasn't marked

        tick_to_signal_ms = (signal_ts - tick_ts) * 1000.0
        signal_to_order_ms = (ts - signal_ts) * 1000.0
        total_ms = (ts - tick_ts) * 1000.0

        # O(1) append to fixed-size deques
        self._tick_to_signal.append(tick_to_signal_ms)
        self._signal_to_order.append(signal_to_order_ms)
        self._total_latency.append(total_ms)

        event = {
            "symbol": symbol,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "latency_tick_to_signal_ms": round(tick_to_signal_ms, 2),
            "latency_signal_to_order_ms": round(signal_to_order_ms, 2),
            "latency_total_ms": round(total_ms, 2),
        }
        self._events.append(event)

        # Structured log (non-blocking)
        logging.debug(
            "[LatencyTracker] %s | tick→signal: %.1fms | signal→order: %.1fms | total: %.1fms",
            symbol, tick_to_signal_ms, signal_to_order_ms, total_ms,
        )

        # Alert on threshold breach (non-blocking, never halts trading)
        if total_ms > self._alert_threshold_ms:
            self._alert_count += 1
            logging.warning(
                "[LatencyTracker] HIGH LATENCY ALERT: %s total=%.1fms (threshold=%.0fms)",
                symbol, total_ms, self._alert_threshold_ms,
            )
            if self._alert_callback:
                try:
                    self._alert_callback(event)
                except Exception:
                    pass  # Never let callback errors propagate

    # ----------------------------------------------------------------
    # STATISTICS — O(N) over window, called on-demand only
    # ----------------------------------------------------------------

    def get_statistics(self) -> Dict[str, Any]:
        """Returns rolling latency statistics for monitoring/dashboard export."""
        if len(self._total_latency) == 0:
            return {
                "sample_count": 0,
                "tick_to_signal": {},
                "signal_to_order": {},
                "total": {},
                "alert_count": self._alert_count,
            }

        return {
            "sample_count": len(self._total_latency),
            "tick_to_signal": self._compute_stats(self._tick_to_signal),
            "signal_to_order": self._compute_stats(self._signal_to_order),
            "total": self._compute_stats(self._total_latency),
            "alert_count": self._alert_count,
        }

    @staticmethod
    def _compute_stats(data: deque) -> Dict[str, float]:
        arr = np.array(data)
        return {
            "mean_ms": round(float(np.mean(arr)), 2),
            "median_ms": round(float(np.median(arr)), 2),
            "p95_ms": round(float(np.percentile(arr, 95)), 2),
            "p99_ms": round(float(np.percentile(arr, 99)), 2),
            "max_ms": round(float(np.max(arr)), 2),
        }

    def get_recent_events(self, n: int = 10) -> list:
        """Returns the N most recent latency events for diagnostics."""
        return list(self._events)[-n:]

    def get_daily_summary(self) -> Dict[str, Any]:
        """Export for daily summary report and telemetry service."""
        stats = self.get_statistics()
        stats["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        stats["alert_threshold_ms"] = self._alert_threshold_ms
        return stats
