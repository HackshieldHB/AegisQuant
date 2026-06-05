"""
FeeEdgeAnalyzer — Shadow Mode Profitability Validation
=======================================================
Phase G-2 Fix: Tracks gross edge vs fees + slippage to determine
if the system has positive expected value after execution costs.

CRITICAL CONSTRAINTS:
- Observability ONLY — never blocks signals, alters sizing, or halts system
- Thread-safe via deque (GIL-protected atomic ops)
- Fixed-size sliding window prevents memory growth
- Alert on negative net edge is WARNING only
"""

import logging
import threading
import numpy as np
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, Optional


class FeeEdgeAnalyzer:
    """
    Non-blocking profitability analytics layer for Shadow and Live modes.
    Tracks whether the system's edge survives after execution costs.
    """

    # Default exchange fee rates (can be overridden per-instance)
    DEFAULT_MAKER_FEE_BPS = 1.0   # 0.01% = 1 bps (Binance VIP0 with BNB)
    DEFAULT_TAKER_FEE_BPS = 5.0   # 0.05% = 5 bps (Binance VIP0 market orders)

    def __init__(
        self,
        window_size: int = 500,
        negative_edge_alert_window: int = 100,
        maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        alert_callback=None,
    ):
        self._window_size = window_size
        self._negative_edge_alert_window = negative_edge_alert_window
        self._maker_fee_bps = maker_fee_bps
        self._taker_fee_bps = taker_fee_bps
        self._alert_callback = alert_callback

        # Rolling storage — deque is thread-safe for append under GIL
        self._gross_edge: deque = deque(maxlen=window_size)
        self._slippage: deque = deque(maxlen=window_size)
        self._fees: deque = deque(maxlen=window_size)
        self._net_edge: deque = deque(maxlen=window_size)
        self._realized_pnl: deque = deque(maxlen=window_size)
        self._events: deque = deque(maxlen=window_size)

        self._total_trades = 0
        self._winning_trades_after_costs = 0
        self._alert_count = 0

    # ----------------------------------------------------------------
    # PUBLIC API
    # ----------------------------------------------------------------

    def record_trade(
        self,
        symbol: str,
        expected_edge_bps: float,
        estimated_slippage_bps: float,
        is_market_order: bool = True,
        realized_pnl_bps: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Record a trade's cost structure for profitability tracking.
        Call after each trade execution (or hypothetical in Shadow Mode).
        
        Returns the computed event dict for optional forwarding.
        """
        # Select fee rate based on order type
        fee_bps = self._taker_fee_bps if is_market_order else self._maker_fee_bps
        # Round-trip: entry + exit fees
        total_fee_bps = fee_bps * 2.0

        net_edge = expected_edge_bps - estimated_slippage_bps - total_fee_bps

        # O(1) appends
        self._gross_edge.append(expected_edge_bps)
        self._slippage.append(estimated_slippage_bps)
        self._fees.append(total_fee_bps)
        self._net_edge.append(net_edge)

        if realized_pnl_bps is not None:
            self._realized_pnl.append(realized_pnl_bps)

        self._total_trades += 1
        if net_edge > 0:
            self._winning_trades_after_costs += 1

        event = {
            "symbol": symbol,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "expected_edge_bps": round(expected_edge_bps, 2),
            "estimated_slippage_bps": round(estimated_slippage_bps, 2),
            "estimated_fees_bps": round(total_fee_bps, 2),
            "net_expected_edge_bps": round(net_edge, 2),
            "realized_pnl_bps": round(realized_pnl_bps, 2) if realized_pnl_bps is not None else None,
            "is_market_order": is_market_order,
        }
        self._events.append(event)

        # Structured log
        logging.debug(
            "[FeeEdgeAnalyzer] %s | gross=%.1fbps | slip=%.1fbps | fees=%.1fbps | net=%.1fbps",
            symbol, expected_edge_bps, estimated_slippage_bps, total_fee_bps, net_edge,
        )

        # Check negative edge alert condition
        self._check_negative_edge_alert()

        return event

    # ----------------------------------------------------------------
    # ALERT — WARNING only, never blocks
    # ----------------------------------------------------------------

    def _check_negative_edge_alert(self) -> None:
        """Trigger WARNING if average net edge ≤ 0 over observation window."""
        if len(self._net_edge) < self._negative_edge_alert_window:
            return

        recent = list(self._net_edge)[-self._negative_edge_alert_window:]
        avg_net = float(np.mean(recent))

        if avg_net <= 0:
            self._alert_count += 1
            logging.warning(
                "[FeeEdgeAnalyzer] NEGATIVE EDGE WARNING: avg_net_edge=%.2fbps "
                "over last %d trades. Fees + slippage may exceed model edge.",
                avg_net, self._negative_edge_alert_window,
            )
            if self._alert_callback:
                try:
                    self._alert_callback({
                        "type": "NEGATIVE_EDGE",
                        "avg_net_edge_bps": round(avg_net, 2),
                        "window": self._negative_edge_alert_window,
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception:
                    pass  # Never let callback errors propagate

    # ----------------------------------------------------------------
    # STATISTICS
    # ----------------------------------------------------------------

    def get_statistics(self) -> Dict[str, Any]:
        """Returns rolling profitability statistics for dashboard/telemetry."""
        if self._total_trades == 0:
            return {
                "total_trades": 0,
                "gross_edge": {},
                "net_edge": {},
                "slippage": {},
                "fees": {},
                "win_rate_after_costs": 0.0,
                "alert_count": self._alert_count,
            }

        win_rate = self._winning_trades_after_costs / self._total_trades

        return {
            "total_trades": self._total_trades,
            "gross_edge": self._agg(self._gross_edge),
            "net_edge": self._agg(self._net_edge),
            "slippage": self._agg(self._slippage),
            "fees": self._agg(self._fees),
            "win_rate_after_costs": round(win_rate, 4),
            "alert_count": self._alert_count,
        }

    @staticmethod
    def _agg(data: deque) -> Dict[str, float]:
        if len(data) == 0:
            return {}
        arr = np.array(data)
        return {
            "mean_bps": round(float(np.mean(arr)), 2),
            "median_bps": round(float(np.median(arr)), 2),
            "total_bps": round(float(np.sum(arr)), 2),
        }

    def get_daily_summary(self) -> Dict[str, Any]:
        """Export for daily summary report and telemetry service."""
        stats = self.get_statistics()

        # Compute aggregate fields for the daily report format
        gross = stats.get("gross_edge", {})
        net = stats.get("net_edge", {})
        fees = stats.get("fees", {})
        slip = stats.get("slippage", {})

        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "gross_edge_bps": gross.get("mean_bps", 0.0),
            "net_edge_bps": net.get("mean_bps", 0.0),
            "fees_bps": fees.get("mean_bps", 0.0),
            "slippage_bps": slip.get("mean_bps", 0.0),
            "trade_count": self._total_trades,
            "win_rate_after_costs": stats.get("win_rate_after_costs", 0.0),
            "alert_count": self._alert_count,
        }

    def get_recent_events(self, n: int = 10) -> list:
        """Returns the N most recent trade cost events for diagnostics."""
        return list(self._events)[-n:]
