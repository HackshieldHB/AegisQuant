"""
EventCalendar — High-impact event detection for AegisQuant.
------------------------------------------------------------
Detects two types of events:

1. Scheduled events (hardcoded recurrence rules):
   - US CPI (monthly, ~13th)
   - US FOMC (8× per year, approximate)
   - Crypto-specific: quarterly Binance futures expiry

2. Surprise moves: if BTC has moved > SURPRISE_MOVE_PCT in the
   last 4 hours, reduce position size and raise confidence bar.

Returns an EventState consumed by _process_symbol in AsyncEngine.
"""

import time
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional

from AegisQuantConfig import CONFIG

logger = logging.getLogger("AegisQuant")


@dataclass
class EventState:
    in_event_window: bool
    event_name:      str
    confidence_boost: float    # amount to ADD to required confidence
    size_scale:       float    # factor to MULTIPLY position size (0.5 = half)


# Approximate FOMC meeting dates for 2026 (UTC, 14:00 ET = 19:00 UTC announcement)
_FOMC_2026 = [
    datetime(2026, 1, 29, 19, 0, tzinfo=timezone.utc),
    datetime(2026, 3, 19, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 5,  7, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 11,  5, 19, 0, tzinfo=timezone.utc),
    datetime(2026, 12, 17, 19, 0, tzinfo=timezone.utc),
]

# Quarterly Binance futures expiry (last Friday of March/June/Sep/Dec at 08:00 UTC)
_BINANCE_EXPIRY_2026 = [
    datetime(2026, 3, 27,  8, 0, tzinfo=timezone.utc),
    datetime(2026, 6, 26,  8, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 25,  8, 0, tzinfo=timezone.utc),
    datetime(2026, 12, 25, 8, 0, tzinfo=timezone.utc),
]


class EventCalendar:
    """
    Checks whether the current UTC datetime falls within the protection
    window of a known high-impact event.
    """

    def __init__(self) -> None:
        self._cfg = CONFIG.get("EVENT_CALENDAR", {})

    def _is_cpi_window(self, now: datetime) -> bool:
        """US CPI is released on the 2nd or 3rd Wednesday each month, ~08:30 ET."""
        pre_h  = self._cfg.get("PRE_EVENT_HOURS", 2)
        post_h = self._cfg.get("POST_EVENT_HOURS", 1)
        # Approximate: any time on the 10th–16th of any month is within CPI risk window
        if 10 <= now.day <= 16:
            # Assume release at 13:30 UTC (08:30 ET)
            release = now.replace(hour=13, minute=30, second=0, microsecond=0)
            delta = (now - release).total_seconds() / 3600
            if -pre_h <= delta <= post_h:
                return True
        return False

    def _is_fomc_window(self, now: datetime) -> bool:
        pre_h  = self._cfg.get("PRE_EVENT_HOURS", 2)
        post_h = self._cfg.get("POST_EVENT_HOURS", 1)
        for dt in _FOMC_2026:
            delta = (now - dt).total_seconds() / 3600
            if -pre_h <= delta <= post_h:
                return True
        return False

    def _is_expiry_window(self, now: datetime) -> bool:
        pre_h  = self._cfg.get("PRE_EVENT_HOURS", 2)
        post_h = self._cfg.get("POST_EVENT_HOURS", 1)
        for dt in _BINANCE_EXPIRY_2026:
            delta = (now - dt).total_seconds() / 3600
            if -pre_h <= delta <= post_h:
                return True
        return False

    def check(self, btc_4h_move_pct: float = 0.0) -> EventState:
        """
        Returns EventState for the current moment.
        btc_4h_move_pct: abs % move in BTC over the last 4 hours.
        """
        if not self._cfg.get("ENABLED", True):
            return EventState(False, "", 0.0, 1.0)

        now = datetime.now(timezone.utc)
        conf_boost = self._cfg.get("CONFIDENCE_BOOST", 0.08)
        size_scale = self._cfg.get("SIZE_SCALE", 0.50)

        if self._is_fomc_window(now):
            return EventState(True, "FOMC", conf_boost, size_scale)

        if self._is_cpi_window(now):
            return EventState(True, "US_CPI", conf_boost, size_scale)

        if self._is_expiry_window(now):
            return EventState(True, "FUTURES_EXPIRY", conf_boost * 0.5, size_scale * 1.2)

        # Surprise move detection — large BTC move = macro surprise event
        surprise_pct = self._cfg.get("SURPRISE_MOVE_PCT", 0.05)
        if btc_4h_move_pct >= surprise_pct:
            return EventState(
                True,
                f"SURPRISE_MOVE ({btc_4h_move_pct:.1%})",
                conf_boost * 0.5,
                size_scale * 1.2,
            )

        return EventState(False, "", 0.0, 1.0)
