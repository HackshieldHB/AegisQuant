"""
AlphaDecayTracker — Per-strategy rolling win-rate monitor.
-----------------------------------------------------------
Each TA strategy (RSI, MACD, Bollinger, EMA Cross, VWAP-ATR) has an
independent rolling win-rate window. When a strategy underperforms,
its weight in the EnsembleStrategy is decayed; it recovers as performance
improves.

Usage (in AsyncEngine after each trade close):
    tracker.record_outcome(strategy_name, signal_at_entry, is_win)
    weights = tracker.get_weights()  # pass to EnsembleStrategy
"""

import logging
from collections import deque
from typing import Dict, Optional

from AegisQuantConfig import CONFIG

logger = logging.getLogger("AegisQuant")


class AlphaDecayTracker:
    """Thread-safe (asyncio single-threaded) per-strategy weight manager."""

    # Base weights assigned to each TA strategy (sum to 1.0)
    _BASE_WEIGHTS: Dict[str, float] = {
        "RSI":        0.18,
        "MACD":       0.22,
        "Bollinger":  0.20,
        "EMACross":   0.22,
        "VWAP_ATR":   0.18,
    }

    def __init__(self) -> None:
        self._cfg     = CONFIG.get("ALPHA_DECAY", {})
        self._window  = self._cfg.get("WINDOW", 50)
        self._min_wr  = self._cfg.get("MIN_WIN_RATE", 0.40)
        self._floor   = self._cfg.get("WEIGHT_FLOOR", 0.30)
        # Outcomes per strategy: deque of booleans (True=win)
        self._outcomes: Dict[str, deque] = {
            name: deque(maxlen=self._window)
            for name in self._BASE_WEIGHTS
        }
        # Current weight multiplier (1.0 = full weight, floor = decayed)
        self._multipliers: Dict[str, float] = {
            name: 1.0 for name in self._BASE_WEIGHTS
        }

    def record_outcome(self, strategy_name: str, is_win: bool) -> None:
        """Record a trade outcome for the given strategy."""
        if not self._cfg.get("ENABLED", True):
            return
        canonical = self._canonical(strategy_name)
        if canonical not in self._outcomes:
            return
        self._outcomes[canonical].append(is_win)
        self._update_multiplier(canonical)

    def _canonical(self, name: str) -> str:
        """Normalise strategy names to keys used in _BASE_WEIGHTS."""
        n = name.strip()
        mapping = {
            "rsi": "RSI",
            "macd": "MACD",
            "bollinger": "Bollinger",
            "bollingerbands": "Bollinger",
            "emacross": "EMACross",
            "ema_cross": "EMACross",
            "vwapatr": "VWAP_ATR",
            "vwap_atr": "VWAP_ATR",
        }
        return mapping.get(n.lower().replace(" ", ""), n)

    def _update_multiplier(self, canonical: str) -> None:
        outcomes = self._outcomes[canonical]
        if len(outcomes) < 10:
            return  # need minimum sample
        win_rate = sum(outcomes) / len(outcomes)
        if win_rate < self._min_wr:
            # Decay proportional to how far below minimum
            decay = self._floor + (1.0 - self._floor) * (win_rate / self._min_wr)
            old = self._multipliers[canonical]
            self._multipliers[canonical] = decay
            if abs(old - decay) > 0.05:
                logger.info(
                    "[AlphaDecay] %s WR=%.1f%% → weight multiplier %.2f→%.2f",
                    canonical, win_rate * 100, old, decay,
                )
        else:
            # Gradual recovery
            recovery = self._cfg.get("RECOVERY_RATE", 0.10)
            self._multipliers[canonical] = min(
                1.0, self._multipliers[canonical] + recovery
            )

    def get_weights(self) -> Dict[str, float]:
        """Return absolute weights for each strategy, normalised to sum to 1.0."""
        raw = {
            name: self._BASE_WEIGHTS[name] * self._multipliers[name]
            for name in self._BASE_WEIGHTS
        }
        total = sum(raw.values())
        if total <= 0:
            return {k: 1.0 / len(raw) for k in raw}
        return {k: v / total for k, v in raw.items()}

    def summary(self) -> Dict[str, dict]:
        """Diagnostic summary for Telegram /models command."""
        result = {}
        for name in self._BASE_WEIGHTS:
            outcomes = self._outcomes[name]
            n = len(outcomes)
            wr = (sum(outcomes) / n) if n > 0 else None
            result[name] = {
                "trades":     n,
                "win_rate":   round(wr, 3) if wr is not None else "N/A",
                "multiplier": round(self._multipliers[name], 3),
                "weight":     round(self.get_weights()[name], 3),
            }
        return result
