"""
FundingRateService — Real-time Market Sentiment Signals
========================================================
Provides two macro signals for AegisQuant's signal-gate pipeline:

1. Binance Perpetual Funding Rate  (per-symbol, every 8h)
   - Positive  = longs paying shorts → crowded long → fade signal
   - Negative  = shorts paying longs → crowded short → fade signal

2. Crypto Fear & Greed Index (market-wide, daily)
   - < 20 Extreme Fear    → historically good BUY environment
   - > 80 Extreme Greed   → historically high reversal risk for BUY entries

Both are cached in-process (5-minute TTL) so they never slow the trading loop.
Network failures return neutral defaults — they NEVER block a trade cycle.
"""

import time
from typing import Optional, Dict

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    from Core.Logger import AG_LOGGER as _logger
except ImportError:
    import logging
    _logger = logging.getLogger("FundingRateService")


BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"
CACHE_TTL_SEC = 300  # 5 minutes


class FundingRateService:
    """
    Thread-safe, cache-backed service for funding rates and Fear & Greed index.
    Always non-blocking: returns None / 50 on any network failure.
    """

    def __init__(self) -> None:
        self._funding_cache: Dict[str, tuple] = {}   # symbol -> (rate, fetched_ts)
        self._fg_cache: tuple = (50, 0.0)             # (index_value, fetched_ts)
        self._timeout = 4  # seconds per HTTP call

    # ------------------------------------------------------------------
    # Funding Rate
    # ------------------------------------------------------------------
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Returns the latest perpetual funding rate for *symbol* (e.g. 'BTC/USDT').

        Interpretation:
            rate > +0.001  (>+0.1%)  → longs aggressively paid → fade BUY
            rate < -0.0005 (<-0.05%) → shorts aggressively paid → fade SELL

        Returns None if fetch fails or requests library unavailable.
        """
        if not _REQUESTS_AVAILABLE:
            return None

        now = time.time()
        cached_rate, cached_ts = self._funding_cache.get(symbol, (None, 0.0))
        if cached_rate is not None and now - cached_ts < CACHE_TTL_SEC:
            return cached_rate

        sym_fmt = symbol.replace("/", "")
        try:
            resp = _requests.get(
                BINANCE_FUNDING_URL,
                params={"symbol": sym_fmt, "limit": 1},
                timeout=self._timeout,
            )
            data = resp.json()
            if isinstance(data, list) and data:
                rate = float(data[-1].get("fundingRate", 0.0))
                self._funding_cache[symbol] = (rate, now)
                return rate
        except Exception as exc:
            _logger.debug("FundingRateService: %s fetch failed: %s", symbol, exc)

        # Return cached stale value if available rather than None
        return cached_rate

    # ------------------------------------------------------------------
    # Fear & Greed Index
    # ------------------------------------------------------------------
    def get_fear_greed_index(self) -> int:
        """
        Returns the current Crypto Fear & Greed Index (0–100).

        Scale:
            0–20   Extreme Fear   (contrarian BUY environment)
            21–40  Fear
            41–59  Neutral
            60–79  Greed
            80–100 Extreme Greed  (reversal risk, tighten TP on longs)

        Returns 50 (neutral) on any failure — never blocks trading.
        """
        if not _REQUESTS_AVAILABLE:
            return 50

        now = time.time()
        value, cached_ts = self._fg_cache
        if now - cached_ts < CACHE_TTL_SEC:
            return value

        try:
            resp = _requests.get(FEAR_GREED_URL, timeout=self._timeout)
            data = resp.json()
            raw = data.get("data", [{}])[0].get("value")
            if raw is not None:
                value = int(raw)
                self._fg_cache = (value, now)
                return value
        except Exception as exc:
            _logger.debug("FundingRateService: Fear & Greed fetch failed: %s", exc)

        return value  # stale cached value

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def fear_greed_label(self, value: int) -> str:
        """Returns human-readable label for a Fear & Greed value."""
        if value <= 20:  return "Extreme Fear 😱"
        if value <= 40:  return "Fear 😟"
        if value <= 59:  return "Neutral 😐"
        if value <= 79:  return "Greed 😏"
        return "Extreme Greed 🤑"

    def funding_rate_label(self, rate: float) -> str:
        """Returns a human-readable description of the funding rate."""
        if rate is None:
            return "N/A"
        pct = rate * 100
        if rate > 0.001:   return f"+{pct:.3f}% ⚠️ Crowded Longs"
        if rate < -0.0005: return f"{pct:.3f}% ⚠️ Crowded Shorts"
        return f"{pct:+.3f}% ✓ Neutral"
