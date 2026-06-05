"""
SentimentService — Multi-source sentiment overlay for AegisQuant.
------------------------------------------------------------------
Sources:
  1. Alternative.me Fear & Greed Index (no API key required)
  2. Crypto RSS feeds — panic keyword detection from headlines
     - CoinDesk, CoinTelegraph, Decrypt, CryptoSlate
     No API key needed. All feeds are publicly available.

Return values:
  is_blocked    — True if new entries should be suppressed
  block_reason  — human-readable reason
  sentiment_score — float [-1.0, +1.0] (+1 = extreme greed, -1 = extreme fear)
  fg_index      — raw 0–100 Fear & Greed index
  panic_news    — list of matching panic headlines
"""

import time
import logging
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from AegisQuantConfig import CONFIG

logger = logging.getLogger("AegisQuant")

# RSS feeds — all free, no API key required
_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
]

# How far back to look in RSS items (seconds)
_RSS_MAX_AGE_SEC = 7200  # 2 hours

@dataclass
class SentimentState:
    is_blocked:      bool
    block_reason:    str
    sentiment_score: float        # -1.0 to +1.0
    fg_index:        int          # 0–100
    panic_news:      List[str]    = field(default_factory=list)


class SentimentService:
    """
    Aggregates Fear & Greed + RSS news sentiment into a single per-cycle state.
    All results are cached to avoid hammering external endpoints.
    """

    def __init__(self) -> None:
        self._cfg            = CONFIG.get("SENTIMENT", {})
        self._fg_cache:      Optional[int]    = None
        self._fg_ts:         float            = 0.0
        self._rss_headlines: List[Dict]       = []   # [{title, age_sec, symbols}]
        self._rss_ts:        float            = 0.0
        self._panic_until:   Dict[str, float] = {}   # symbol → unix ts when block lifts
        self._cache_ttl      = self._cfg.get("NEWS_CACHE_TTL_SEC", 300)
        self._panic_keywords = [k.lower() for k in self._cfg.get("PANIC_KEYWORDS", [])]
        self._panic_block_m  = self._cfg.get("NEWS_PANIC_BLOCK_MINUTES", 30)

    # ── Fear & Greed ──────────────────────────────────────────────────────────

    def get_fear_greed_index(self) -> int:
        """Return cached Fear & Greed 0–100 index."""
        if time.time() - self._fg_ts < self._cache_ttl and self._fg_cache is not None:
            return self._fg_cache
        try:
            url  = self._cfg.get("FEAR_GREED_API", "https://api.alternative.me/fng/")
            resp = requests.get(url, params={"limit": 1}, timeout=8)
            val  = int(resp.json()["data"][0]["value"])
            self._fg_cache = val
            self._fg_ts    = time.time()
            return val
        except Exception as e:
            logger.debug("[Sentiment] Fear/Greed fetch failed: %s", e)
            return self._fg_cache if self._fg_cache is not None else 50

    # ── RSS news ──────────────────────────────────────────────────────────────

    def _fetch_rss(self) -> List[Dict]:
        """
        Fetch and parse RSS from all configured feeds.
        Returns a list of {title, age_sec, text} dicts for items published
        within _RSS_MAX_AGE_SEC. Cached for NEWS_CACHE_TTL_SEC.
        """
        if time.time() - self._rss_ts < self._cache_ttl:
            return self._rss_headlines

        now = time.time()
        items: List[Dict] = []

        for feed_url in _RSS_FEEDS:
            try:
                resp = requests.get(feed_url, timeout=10,
                                    headers={"User-Agent": "AegisQuant/3.1 RSS Reader"})
                if resp.status_code != 200:
                    continue
                root = ET.fromstring(resp.content)
                # Handle both RSS 2.0 (<channel><item>) and Atom (<entry>)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
                for entry in entries:
                    title_el = entry.find("title")
                    title = (title_el.text or "").strip() if title_el is not None else ""
                    if not title:
                        continue

                    # Parse pubDate / updated to determine age
                    age_sec = 0
                    for date_tag in ("pubDate", "updated", "published",
                                     "{http://www.w3.org/2005/Atom}updated",
                                     "{http://www.w3.org/2005/Atom}published"):
                        date_el = entry.find(date_tag)
                        if date_el is not None and date_el.text:
                            try:
                                pub_ts = parsedate_to_datetime(date_el.text).timestamp()
                                age_sec = now - pub_ts
                                break
                            except Exception:
                                try:
                                    from datetime import datetime, timezone
                                    pub_ts = datetime.fromisoformat(
                                        date_el.text.replace("Z", "+00:00")
                                    ).timestamp()
                                    age_sec = now - pub_ts
                                    break
                                except Exception:
                                    pass

                    if age_sec > _RSS_MAX_AGE_SEC:
                        continue

                    # Also grab description text for keyword matching
                    desc_el = entry.find("description") or entry.find(
                        "{http://www.w3.org/2005/Atom}summary"
                    )
                    desc = (desc_el.text or "") if desc_el is not None else ""
                    text = (title + " " + desc).lower()

                    items.append({"title": title, "age_sec": age_sec, "text": text})
            except Exception as e:
                logger.debug("[Sentiment] RSS fetch failed for %s: %s", feed_url, e)

        self._rss_headlines = items
        self._rss_ts        = time.time()
        return items

    def _check_panic_news(self, symbol: str) -> Tuple[bool, str]:
        """Return (is_panic, headline) if a recent headline matches symbol + panic keyword."""
        items = self._fetch_rss()
        base  = symbol.split("/")[0].replace("USDT", "").upper()  # BTC, ETH, etc.

        for item in items:
            text = item["text"]
            sym_match = base.lower() in text or symbol.lower().split("/")[0] in text
            kw_match  = any(kw in text for kw in self._panic_keywords)
            if sym_match and kw_match:
                return True, item["title"]
        return False, ""

    def _check_news_spike(self, symbol: str) -> bool:
        """
        High headline frequency for a symbol in the last 2 hours = crowd-panic signal.
        More than 5 articles about the same coin in 2h is unusual and warrants caution.
        """
        items = self._fetch_rss()
        base  = symbol.split("/")[0].replace("USDT", "").upper()
        count = sum(1 for item in items if base.lower() in item["text"])
        threshold = self._cfg.get("SOCIAL_VOLUME_SPIKE_THRESHOLD", 5)
        return count >= threshold

    # ── Main entry point ─────────────────────────────────────────────────────

    def evaluate(self, symbol: str) -> SentimentState:
        """Return a SentimentState for the given symbol."""
        if not self._cfg.get("ENABLED", True):
            return SentimentState(False, "", 0.0, 50)

        fg = self.get_fear_greed_index()
        sentiment_score = (fg - 50) / 50.0  # map 0–100 → -1 to +1

        # Check active panic block for this symbol
        if time.time() < self._panic_until.get(symbol, 0):
            remaining = int((self._panic_until[symbol] - time.time()) / 60)
            return SentimentState(
                True,
                f"NEWS_PANIC_BLOCK: {symbol} suppressed {remaining}m (recent panic headline)",
                sentiment_score, fg,
            )

        is_panic, headline = self._check_panic_news(symbol)
        if is_panic:
            self._panic_until[symbol] = time.time() + self._panic_block_m * 60
            logger.warning("[Sentiment] PANIC NEWS for %s: %s", symbol, headline)
            return SentimentState(
                True,
                f"NEWS_PANIC_BLOCK: {headline[:80]}",
                sentiment_score, fg, [headline],
            )

        if self._check_news_spike(symbol):
            logger.info("[Sentiment] News spike detected for %s — softening score", symbol)
            sentiment_score = min(sentiment_score, -0.3)

        return SentimentState(False, "", sentiment_score, fg)
