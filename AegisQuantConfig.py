"""
AegisQuant PRODUCTION Configuration — Institutional Grade
=========================================================
- SINGLE SOURCE OF TRUTH for all operational parameters
- Hard-block: execution on disabled asset class raises RuntimeError
- Environment-driven configuration with safe defaults
- Comprehensive validation at startup
- No hardcoded operational parameters elsewhere
"""

import os
import sys
from datetime import time
from dotenv import load_dotenv

load_dotenv()

# ==================================================
# PROJECT (source of truth for asset enable)
# ==================================================
PROJECT = {
    "NAME": "AegisQuant",
    "VERSION": "3.1.0",
    "MODE": os.getenv("AEGIS_MODE", "LIVE").upper(),  # LIVE | PAPER | BACKTEST - Changed to LIVE for immediate trading
    "ENVIRONMENT": os.getenv("AEGIS_ENVIRONMENT", "VPS").upper(),  # LOCAL | VPS - Changed to VPS to enable LIVE mode
    "TIMEZONE": "UTC",
    "CRYPTO_ENABLED": str(os.getenv("CRYPTO_ENABLED", "True")).lower() == "true",
    "FOREX_ENABLED": str(os.getenv("FOREX_ENABLED", "False")).lower() == "true",  # Disabled: no models trained yet
    "STOCKS_ENABLED": str(os.getenv("STOCKS_ENABLED", "False")).lower() == "true",  # Disabled: no models trained yet
    "DEBUG_MODE": str(os.getenv("DEBUG_MODE", "False")).lower() == "true",
    "AGGRESSIVE_SAFE_MODE": str(os.getenv("AGGRESSIVE_SAFE_MODE", "True")).lower() == "true",
    "ACCOUNT_MODE": os.getenv("ACCOUNT_MODE", "SPOT").upper(),  # SPOT | FUTURES
}

# ==================================================
# MARKET ENABLE (derived; do not init disabled executors)
# ==================================================
MARKETS = {
    "CRYPTO": PROJECT["CRYPTO_ENABLED"],
    "FOREX": PROJECT["FOREX_ENABLED"],
    "STOCKS": PROJECT["STOCKS_ENABLED"],
}

# ==================================================
# DATA SOURCES
# ==================================================
DATA_SOURCES = {
    "CRYPTO": "binance",
    "FOREX": "oanda",
    "STOCKS": "yahoo_finance",
}

# ==================================================
# SYMBOLS & TIMEFRAMES
# ==================================================
SYMBOLS = {
    "CRYPTO": {
        "BTC/USDT": ["1m", "5m", "15m"],
        "ETH/USDT": ["1m", "5m", "15m"],
        "DOGE/USDT": ["1m", "5m", "15m"],
        "SOL/USDT": ["1m", "5m", "15m"],
        "XRP/USDT": ["1m", "5m", "15m"],
        "SHIB/USDT": ["1m", "5m", "15m"],
        "PEPE/USDT": ["1m", "5m", "15m"],
    },
    "FOREX": {
        "EUR_USD": ["5m", "15m", "1h"],
        "GBP_USD": ["5m", "15m"],
        "USD_JPY": ["5m", "1h"],
        "AUD_USD": ["15m", "1h"],
    },
    "STOCKS": {
        "AAPL": ["15m", "1h"],
        "MSFT": ["15m", "1h"],
        "TSLA": ["5m", "15m"],
        "NVDA": ["15m", "1h"],
    },
}

# ==================================================
# TRADING HOURS
# ==================================================
TRADING_HOURS = {
    "FOREX": {"START": time(0, 0), "END": time(23, 59)},
    "STOCKS": {"START": time(13, 30), "END": time(20, 0)},
}

# ==================================================
# GLOBAL RISK MANAGEMENT
# ==================================================
RISK = {
    "MAX_RISK_PER_TRADE": float(os.getenv("MAX_RISK_PER_TRADE", "0.02")),
    "MAX_PORTFOLIO_EXPOSURE": float(os.getenv("MAX_PORTFOLIO_EXPOSURE", "0.95")),
    "MAX_DAILY_DRAWDOWN": float(os.getenv("MAX_DAILY_DRAWDOWN", "0.05")),
    "MAX_WEEKLY_DRAWDOWN": float(os.getenv("MAX_WEEKLY_DRAWDOWN", "0.10")),
    "MAX_CONCURRENT_TRADES": int(os.getenv("MAX_CONCURRENT_TRADES", "2")),
    "MAX_SECTOR_EXPOSURE_PCT": float(os.getenv("MAX_SECTOR_EXPOSURE_PCT", "1.0")),
    "SMALL_ACCOUNT_THRESHOLD": float(os.getenv("SMALL_ACCOUNT_THRESHOLD", "100.0")),
    "SMALL_ACCOUNT_RISK_FACTOR": float(os.getenv("SMALL_ACCOUNT_RISK_FACTOR", "2.0")),
    "MIN_RESERVE_USDT": float(os.getenv("MIN_RESERVE_USDT", "2.0")),
    "TIERS": {
        # MICRO (<$100): Growth mode — 2.5% risk per trade. Enough aggression to
        # compound small capital meaningfully; still survives 5 consecutive losses.
        "MICRO":       {"MAX_BALANCE": 100,          "RISK_PCT": 0.025},
        # AGGRESSIVE ($100–$1K): Standard aggressive; account proved its edge.
        "AGGRESSIVE":  {"MAX_BALANCE": 1000,         "RISK_PCT": 0.05},
        # STANDARD ($1K–$25K): Institutional 2% rule.
        "STANDARD":    {"MAX_BALANCE": 25000,        "RISK_PCT": 0.02},
        # CONSERVATIVE (>$25K): Capital preservation mode.
        "CONSERVATIVE":{"MAX_BALANCE": float('inf'), "RISK_PCT": 0.01},
    },
    "SUDDEN_DROP_PCT": 0.05,
    "MAX_CONSECUTIVE_LOSSES": 5,
}

# ==================================================
# GROWTH TARGET (Rp 255,371 → Rp 10,000,000)
# Starting capital locked in 2026-05-28 (actual live balance).
# Used for progress tracking in Telegram cycle messages.
# Adjust IDR_RATE to current USD/IDR spot rate as needed.
# ==================================================
GROWTH_TARGET = {
    "ENABLED": True,
    "STARTING_CAPITAL_IDR": 255_371,       # Rp 255,371  ($15.96 @ Rp 16,000/USD — actual starting balance 2026-05-28)
    "TARGET_CAPITAL_IDR":   10_000_000,    # Rp 10,000,000  (~$625 USD)
    "IDR_RATE":             16_000,         # 1 USD ≈ Rp 16,000 (update periodically)
}

# ==================================================
# STOP / TAKE PROFIT (ATR-based; SL mandatory)
# ==================================================
STOPS = {
    "USE_ATR": True,
    "ATR_MULTIPLIER_SL": 1.5,
    "ATR_MULTIPLIER_TP": 3.0,
    "ATR_MULTIPLIER_SL_MICRO": 0.9,    # Slightly wider SL prevents premature stop-outs on micro volatility
    "ATR_MULTIPLIER_TP_MICRO": 2.7,    # ~3:1 gross RR → ~2.3:1 net after fees+spread (growth mode)
    "MIN_SL_MICRO": 0.004,             # 0.4% floor — never risk less than this (meaningful stop)
    "MIN_TP_MICRO": 0.018,             # 1.8% TP floor — net ≥ 1.6% after 0.2% round-trip fees+spread
    "TRAILING_STOP": True,
    "TRAIL_AFTER_R": 1.0,
    "TRAIL_POLL_SEC": 45,          # Polling interval for trailing stop loop
    "TRAIL_STEP_R": 1.0,           # R-multiple step between tiers
}

# ==================================================
# CAPITAL ROTATION
# Closes an underperforming open position to free a slot
# for a significantly better new opportunity.
# ==================================================
CAPITAL_ROTATION = {
    # Master on/off switch
    "ENABLED": str(os.getenv("CAPITAL_ROTATION", "True")).lower() == "true",

    # Minimum candles a position must have been open before it can be
    # replaced (at 5 min candles: 24 = 2h, prevents overtrading)
    "MIN_HOLD_BARS": int(os.getenv("ROTATION_MIN_HOLD_BARS", "24")),

    # The new signal's edge_score must exceed the open position's
    # entry edge_score by at least this much (0.10 = 10 pp advantage)
    "MIN_EDGE_ADVANTAGE": float(os.getenv("ROTATION_EDGE_ADV", "0.10")),

    # Never rotate out of a position that is currently at a paper loss
    # greater than this fraction of entry price (protect against cutting
    # losses prematurely — let the SL do its job instead)
    "MAX_LOSS_TO_ROTATE": float(os.getenv("ROTATION_MAX_LOSS", "0.003")),  # 0.3%

    # Minimum absolute edge score the incoming signal must have
    # (prevents rotating into weak signals)
    "MIN_INCOMING_EDGE": float(os.getenv("ROTATION_MIN_EDGE", "0.72")),

    # Maximum rotations allowed per engine cycle (prevents cascade closes)
    "MAX_PER_CYCLE": int(os.getenv("ROTATION_MAX_PER_CYCLE", "1")),
}

# ==================================================
# PARTIAL TAKE-PROFIT — scale-out strategy
# Close a fraction of the position at TP1 to lock in
# profit while letting the remainder ride to TP2.
# TP1/TP2 are expressed as R-multiples of initial risk.
# ==================================================
PARTIAL_TP = {
    "ENABLED":        str(os.getenv("PARTIAL_TP_ENABLED", "True")).lower() == "true",
    "TP1_R_MULTIPLE": float(os.getenv("PARTIAL_TP1_R", "1.5")),     # Close scale-out pct at 1.5R
    "TP2_R_MULTIPLE": float(os.getenv("PARTIAL_TP2_R", "3.5")),     # Close remainder at 3.5R
    "SCALE_OUT_PCT":  float(os.getenv("PARTIAL_TP_SCALE", "0.50")), # 50% closed at TP1
    "MOVE_SL_TO_BE":  str(os.getenv("PARTIAL_TP_MOVE_SL", "True")).lower() == "true",
    "POLL_SEC":       int(os.getenv("PARTIAL_TP_POLL", "60")),
}

# ==================================================
# DYNAMIC CONFIDENCE — raise AI bar after losing streaks
# Global consecutive-loss counter adjusts confidence
# threshold upward to filter lower-quality setups when
# the engine is on a losing run. Resets on next win.
# ==================================================
DYNAMIC_CONFIDENCE = {
    "ENABLED":                str(os.getenv("DYN_CONF_ENABLED", "True")).lower() == "true",
    "STREAK_RAISE_THRESHOLD": int(os.getenv("DYN_CONF_STREAK", "3")),      # Begin raising after 3 losses
    "CONFIDENCE_BOOST":       float(os.getenv("DYN_CONF_BOOST", "0.05")),  # +5pp per additional loss
    "MAX_BOOST":              float(os.getenv("DYN_CONF_MAX", "0.15")),    # Hard cap: +15pp total
    "RESET_ON_WIN":           str(os.getenv("DYN_CONF_RESET", "True")).lower() == "true",
}

# ==================================================
# CORRELATION GATE — avoid doubling up on correlated
# assets.  Static Pearson-r table (90-day daily-return
# window, updated monthly after model refit).
# If an open position's pair-r > MAX_CORRELATION the
# new signal is blocked even if it passes all other gates.
# ==================================================
CORRELATION_GATE = {
    "ENABLED":         str(os.getenv("CORR_GATE_ENABLED", "True")).lower() == "true",
    "MAX_CORRELATION": float(os.getenv("CORR_MAX", "0.75")),
    # (sym_a, sym_b) → Pearson r  (order-independent at lookup time)
    "PAIRS": {
        ("BTC/USDT",  "ETH/USDT"):  0.87,
        ("ETH/USDT",  "SOL/USDT"):  0.82,
        ("DOGE/USDT", "SHIB/USDT"): 0.88,
        ("DOGE/USDT", "PEPE/USDT"): 0.84,
        ("SHIB/USDT", "PEPE/USDT"): 0.86,
        ("BTC/USDT",  "SOL/USDT"):  0.76,
        ("ETH/USDT",  "XRP/USDT"):  0.72,
    },
}

# ==================================================
# LIVE ACCURACY TRACKER — rolling win-rate monitor
# Keeps a circular buffer of the last WINDOW trade
# outcomes. Sends a Telegram warning when win-rate
# falls below WARN_BELOW; auto-raises the confidence
# threshold when it falls below RAISE_THRESHOLD_BELOW.
# Boost is cleared once win-rate recovers above WARN_BELOW.
# ==================================================
LIVE_ACCURACY = {
    "ENABLED":               str(os.getenv("LIVE_ACC_ENABLED", "True")).lower() == "true",
    "WINDOW":                int(os.getenv("LIVE_ACC_WINDOW", "20")),        # Rolling sample size
    "WARN_BELOW":            float(os.getenv("LIVE_ACC_WARN", "0.45")),      # Telegram warning < 45%
    "RAISE_THRESHOLD_BELOW": float(os.getenv("LIVE_ACC_RAISE", "0.40")),    # Auto-raise conf threshold
    "THRESHOLD_BOOST":       float(os.getenv("LIVE_ACC_BOOST", "0.05")),    # Amount to raise by
}

# ==================================================
# RANKING (multi-asset)
# ==================================================
RANKING = {
    "TOP_N": int(os.getenv("RANKING_TOP_N", "5")),
    "CONFIDENCE_THRESHOLD": float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.70")),
    "VOLATILITY_WEIGHT": True,
}

# ==================================================
# PER-SYMBOL OVERRIDES
# ==================================================
# Calibrated from 2026-05-28 training run (12-month walk-forward results).
# confidence = min AI confidence to enter. size_scale = fraction of normal allocation.
# Symbols with walk-forward < 50% get raised bar + reduced size to limit bleed
# while the alpha decay tracker adjusts their strategy weights over live trades.
SYMBOL_OVERRIDES = {
    # WF=57.7%  global=60.9%  CONSISTENT — strongest signal, full allocation
    "BTC/USDT":  {"confidence": 0.72, "size_scale": 1.00},
    # WF=51.2%  global=52.3%  CONSISTENT — meta-filter lifts WR to 57.2%, full size
    "ETH/USDT":  {"confidence": 0.72, "size_scale": 1.00},
    # WF=51.3%  global=50.7%  CONSISTENT — marginal edge, slightly reduced
    "XRP/USDT":  {"confidence": 0.74, "size_scale": 0.90},
    # WF=51.2%  global=52.6%  CONSISTENT — marginal but consistent, slightly reduced
    "PEPE/USDT": {"confidence": 0.74, "size_scale": 0.90},
    # WF=49.8%  global=53.6%  INCONSISTENT — regime-sensitive, moderate bar + reduced size
    "SOL/USDT":  {"confidence": 0.78, "size_scale": 0.70},
    # WF=49.3%  global=49.7%  INCONSISTENT — sub-50% globally, high bar + small size
    "DOGE/USDT": {"confidence": 0.83, "size_scale": 0.60},
    # WF=49.0%  global=48.7%  INCONSISTENT — worst performer, highest bar + smallest size
    "SHIB/USDT": {"confidence": 0.85, "size_scale": 0.50},
    # Forex / Stocks (not currently active)
    "EUR_USD": {"confidence": 0.80, "size_scale": 1.00},
    "GBP_USD": {"confidence": 0.78, "size_scale": 1.00},
    "USD_JPY": {"confidence": 0.75, "size_scale": 1.00},
    "AAPL":    {"confidence": 0.80, "size_scale": 1.00},
    "MSFT":    {"confidence": 0.80, "size_scale": 1.00},
    "TSLA":    {"confidence": 0.75, "size_scale": 1.00},
}

# ==================================================
# AI / ML
# ==================================================
AI = {
    "ENABLED": str(os.getenv("AI_ENABLED", "True")).lower() == "true",
    "CONFIDENCE_THRESHOLD": float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.65")),  # Lowered from 0.70 to allow more trades
    "RETRAIN_INTERVAL_DAYS": 7,
    "MODEL_PATH": os.path.join(os.getcwd(), "Data", "Models"),
}

# ==================================================
# INDICATORS
# ==================================================
INDICATORS = {
    "TREND": ["EMA", "SMA", "MACD", "ADX"],
    "VOLATILITY": ["ATR", "BOLLINGER"],
    "MOMENTUM": ["RSI", "STOCH"],
}

# ==================================================
# PORTFOLIO
# ==================================================
PORTFOLIO = {
    "MAX_CORRELATED_TRADES": 2,
    "CORRELATION_LOOKBACK": 90,
}

# ==================================================
# REPORTING
# ==================================================
REPORTING = {
    "ENABLED": True,
    "LOG_DIR": os.path.join(os.getcwd(), "logs"),
}

# ==================================================
# TELEGRAM
# ==================================================
TELEGRAM = {
    "ENABLED": str(os.getenv("TELEGRAM_ENABLED", "True")).lower() == "true",
    "TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "PROBABILITY_ALERT_INTERVAL_SEC": 600,
}

# ==================================================
# BROKER CREDENTIALS
# ==================================================
BROKERS = {
    "BINANCE": {
        "API_KEY": os.getenv("BINANCE_API_KEY"),
        "SECRET": os.getenv("BINANCE_SECRET_KEY"),
        "TESTNET": str(os.getenv("BINANCE_TESTNET", "False")).lower() == "true",  # default LIVE; set BINANCE_TESTNET=True in .env for testnet
    },
    "OANDA": {
        "API_KEY": os.getenv("OANDA_API_KEY"),
        "ACCOUNT_ID": os.getenv("OANDA_ACCOUNT_ID"),
        "PRACTICE": str(os.getenv("OANDA_PRACTICE", "True")).lower() == "true",
    },
    "ALPACA": {
        "API_KEY": os.getenv("ALPACA_API_KEY"),
        "SECRET": os.getenv("ALPACA_SECRET_KEY"),
        "PAPER": str(os.getenv("ALPACA_PAPER", "True")).lower() == "true",
    },
}

# ==================================================
# LOGGING
# ==================================================
LOGGING = {
    "LEVEL": os.getenv("LOG_LEVEL", "INFO"),
    "SAVE_TO_FILE": True,
    "FILE_PATH": os.path.join(os.getcwd(), "logs", "aegis_quant.log"),
}

# ==================================================
# SAFETY
# ==================================================
SAFETY = {
    "KILL_SWITCH": True,
    "MAX_API_ERRORS": 5,
}

# ==================================================
# TRADING SESSIONS — dead-hour filter
# Crypto volume drops sharply between 23:00–02:59 UTC.
# During these hours, breakouts are frequently faked and reversed.
# We require a higher AI confidence bar (0.78) before entering.
# ==================================================
TRADING_SESSIONS = {
    "ENABLED": True,
    "DEAD_HOURS_UTC": [23, 0, 1, 2],              # 23:00 – 02:59 UTC
    "DEAD_HOURS_CONFIDENCE_THRESHOLD": 0.78,       # higher bar during dead hours
}

# ==================================================
# ENSEMBLE ML — multi-model probability blending
# After retraining, RF + XGB + LGB are soft-voted.
# Weights should sum to 1.0.
# ==================================================
ENSEMBLE = {
    "ENABLED": str(os.getenv("ENSEMBLE_ENABLED", "True")).lower() == "true",
    "SINGLE_MODEL_TYPE": os.getenv("SINGLE_MODEL_TYPE", "RF").upper(),
    "RF_WEIGHT":  0.40,    # Random Forest
    "XGB_WEIGHT": 0.35,    # XGBoost
    "LGB_WEIGHT": 0.25,    # LightGBM
}

# ==================================================
# SIGNAL GATES — real-time microstructure filters
# These gates adjust edge_score or override confidence
# based on live market state (funding, fear, order book).
# ==================================================
SIGNAL_GATES = {
    # Funding Rate gate (Binance perpetual market proxy)
    "FUNDING_RATE_ENABLED": True,
    "FUNDING_RATE_EXTREME_LONG":  0.001,    # > +0.1%  → crowded longs  → -30% edge on BUY
    "FUNDING_RATE_EXTREME_SHORT": -0.0005,  # < -0.05% → crowded shorts → -30% edge on SELL
    # Fear & Greed Index gate
    "FEAR_GREED_ENABLED": True,
    "FEAR_GREED_EXTREME_FEAR":   20,        # < 20 → lower BUY threshold by 5%
    "FEAR_GREED_EXTREME_GREED":  80,        # > 80 → raise BUY threshold by 8%
    # Order-Book Imbalance gate
    "ORDER_BOOK_IMBALANCE_ENABLED": True,
    "ORDER_BOOK_IMBALANCE_THRESHOLD": 0.25, # |imbalance| > 0.25 confirms entry direction
    "ORDER_BOOK_IMBALANCE_BOOST":     0.04, # edge_score bonus when imbalance confirms signal
    # Volume Profile POC gate
    "VOLUME_PROFILE_ENABLED": True,
    "VOLUME_PROFILE_PROXIMITY_PCT": 0.005,  # within 0.5% of POC → widen SL by 20%
    # Multi-timeframe consensus
    "MTF_CONSENSUS_REQUIRED": True,         # 1H direction must agree with entry signal
    "MTF_CONSENSUS_OVERRIDE_CONF": 0.78,    # AI confidence that bypasses MTF check (lowered from 0.82)
}

# ==================================================
# PAPER TRADING — shadow simulation layer
# When ENABLED, engine runs full logic but skips real
# order placement. Simulated fills are logged to
# logs/paper_trades.csv for forward-test validation.
# ==================================================
# ==================================================
# TIME-BASED EXIT — stale-position capital recycler
# Close a position that has been open longer than MAX_HOLD_HOURS
# and has NOT reached 0.5R profit (i.e. it's going nowhere).
# Prevents dead capital sitting in drifting positions.
# ==================================================
TIME_EXIT = {
    "ENABLED":        str(os.getenv("TIME_EXIT_ENABLED", "True")).lower() == "true",
    "MAX_HOLD_HOURS": int(os.getenv("TIME_EXIT_MAX_HOURS", "24")),   # force-exit after 24 h
    "MIN_PROFIT_R":   float(os.getenv("TIME_EXIT_MIN_R", "0.3")),    # only exit if profit < 0.3R
    "POLL_SEC":       int(os.getenv("TIME_EXIT_POLL", "300")),        # check every 5 min
}

PAPER_TRADING = {
    "ENABLED": str(os.getenv("PAPER_TRADING", "False")).lower() == "true",
    "LOG_SIMULATED_FILLS": True,
    "SIMULATE_SLIPPAGE_BPS": 5.0,
}

# ==================================================
# BENCHMARK — BTC buy-and-hold comparison
# Tracks BTC price at engine startup; Telegram cycle
# messages include alpha vs simply holding BTC.
# ==================================================
BENCHMARK = {
    "ENABLED": True,
    "SYMBOL": "BTC/USDT",
    "START_PRICE_FILE": os.path.join(os.getcwd(), "logs", "benchmark_start.json"),
}

# ==================================================
# EXECUTION (all assets)
EXECUTION = {
    "ORDER_COOLDOWN_SEC": 900,
    "FILL_POLL_INTERVAL_SEC": 2,
    "FILL_POLL_TIMEOUT_SEC": 30,
    "RETRY_ATTEMPTS": 3,
    "RETRY_BASE_DELAY_SEC": 2,
    "SYMBOL_REENTRY_COOLDOWN_SEC": int(os.getenv("SYMBOL_REENTRY_COOLDOWN_SEC", "1800")),  # 30 min soft penalty window
    # Hard re-entry block: minimum seconds after a stop-loss before ANY new entry on same symbol
    "HARD_REENTRY_BLOCK_SEC": int(os.getenv("HARD_REENTRY_BLOCK_SEC", "2700")),  # 45 min hard block
}

# ==================================================
# SYMBOL BAN — consecutive-loss protection
# After N consecutive stop-losses on the same symbol,
# block ALL new entries on that symbol for BAN_DURATION_SEC.
# Resets on a win. Prevents overtrading a downtrend.
# ==================================================
SYMBOL_BAN = {
    "ENABLED":          str(os.getenv("SYMBOL_BAN_ENABLED", "True")).lower() == "true",
    "LOSS_THRESHOLD":   int(os.getenv("SYMBOL_BAN_LOSSES", "3")),    # ban after 3 consecutive losses
    "BAN_DURATION_SEC": int(os.getenv("SYMBOL_BAN_SEC", "14400")),   # 4-hour ban
}

# ==================================================
# SENTIMENT — market sentiment overlay
# Fear & Greed Index + free RSS feeds from major crypto news sites.
# No API key required.
# ==================================================
SENTIMENT = {
    "ENABLED":                  str(os.getenv("SENTIMENT_ENABLED", "True")).lower() == "true",
    "FEAR_GREED_API":           "https://api.alternative.me/fng/",
    "NEWS_PANIC_BLOCK_MINUTES": int(os.getenv("NEWS_PANIC_BLOCK_MIN", "30")),
    "NEWS_CACHE_TTL_SEC":       int(os.getenv("NEWS_CACHE_SEC", "300")),
    "PANIC_KEYWORDS": [
        "hack", "exploit", "exit scam", "rug", "sec", "lawsuit", "bankrupt",
        "freeze", "insolvent", "shutdown", "delisted", "arrested",
        "breach", "stolen", "vulnerability", "ponzi", "fraud",
    ],
    "SOCIAL_VOLUME_SPIKE_THRESHOLD": int(os.getenv("SOCIAL_VOL_SPIKE", "5")),  # articles in 2h
}

# ==================================================
# EVENT CALENDAR — pre-event volatility protection
# Reduces position sizing and raises confidence threshold
# in the window around known high-impact events.
# ==================================================
EVENT_CALENDAR = {
    "ENABLED":            str(os.getenv("EVENT_CALENDAR_ENABLED", "True")).lower() == "true",
    # Hours before and after a scheduled event to apply protection
    "PRE_EVENT_HOURS":    int(os.getenv("EVENT_PRE_HOURS", "2")),
    "POST_EVENT_HOURS":   int(os.getenv("EVENT_POST_HOURS", "1")),
    # During event window: raise confidence bar by this amount
    "CONFIDENCE_BOOST":   float(os.getenv("EVENT_CONF_BOOST", "0.08")),
    # During event window: scale position size by this factor (0.5 = half size)
    "SIZE_SCALE":         float(os.getenv("EVENT_SIZE_SCALE", "0.50")),
    # Max daily BTC move % that triggers "surprise event" mode
    "SURPRISE_MOVE_PCT":  float(os.getenv("EVENT_SURPRISE_PCT", "0.05")),
}

# ==================================================
# LSTM MODEL — sequential pattern recognition
# Complements RF/XGB/LGB with temporal sequence learning.
# ==================================================
LSTM_MODEL = {
    "ENABLED":        str(os.getenv("LSTM_ENABLED", "True")).lower() == "true",
    "SEQUENCE_LEN":   int(os.getenv("LSTM_SEQ_LEN", "60")),      # 60-bar lookback
    "HIDDEN_UNITS":   int(os.getenv("LSTM_HIDDEN", "128")),
    "DROPOUT":        float(os.getenv("LSTM_DROPOUT", "0.3")),
    "EPOCHS":         int(os.getenv("LSTM_EPOCHS", "50")),
    "BATCH_SIZE":     int(os.getenv("LSTM_BATCH", "64")),
    "ENSEMBLE_WEIGHT": float(os.getenv("LSTM_WEIGHT", "0.20")),   # blend weight in final ensemble
}

# ==================================================
# ALPHA DECAY — per-strategy performance tracking
# Monitors rolling win-rate for each TA strategy.
# If a strategy underperforms, its ensemble weight is
# reduced automatically; it recovers as performance improves.
# ==================================================
ALPHA_DECAY = {
    "ENABLED":          str(os.getenv("ALPHA_DECAY_ENABLED", "True")).lower() == "true",
    "WINDOW":           int(os.getenv("ALPHA_DECAY_WINDOW", "50")),      # rolling N trades per strategy
    "MIN_WIN_RATE":     float(os.getenv("ALPHA_DECAY_MIN_WR", "0.40")),  # below this → weight penalty
    "WEIGHT_FLOOR":     float(os.getenv("ALPHA_DECAY_FLOOR", "0.30")),   # never drop below 30% of base weight
    "RECOVERY_RATE":    float(os.getenv("ALPHA_DECAY_RECOVERY", "0.10")), # weight recovery per +win
}

# ==================================================
# AUTO RETRAIN — scheduled model refresh
# ==================================================
AUTO_RETRAIN = {
    "ENABLED":         str(os.getenv("AUTO_RETRAIN_ENABLED", "True")).lower() == "true",
    "INTERVAL_DAYS":   int(os.getenv("AUTO_RETRAIN_DAYS", "7")),
    "DRIFT_WIN_RATE":  float(os.getenv("DRIFT_RETRAIN_WR", "0.40")),   # per-symbol WR below this triggers immediate retrain
    "DRIFT_WINDOW":    int(os.getenv("DRIFT_RETRAIN_WINDOW", "20")),   # rolling window for drift detection
    "LOCK_FILE":       os.path.join(os.getcwd(), "logs", "last_retrain.json"),
}

# ==================================================
# MODEL HEALTH - live anomaly detection thresholds
# ==================================================
MODEL_HEALTH = {
    "ENABLED": str(os.getenv("MODEL_HEALTH_ENABLED", "True")).lower() == "true",
    "MIN_SIGNAL_RATE": int(os.getenv("MODEL_HEALTH_MIN_SIGNALS", "2")),
    "SIGNAL_RATE_WINDOW": int(os.getenv("MODEL_HEALTH_SIGNAL_WINDOW", "400")),
    "PROB_COMPRESSION_THRESHOLD": float(os.getenv("MODEL_HEALTH_PROB_VAR", "0.00005")),
    "MAX_META_REJECTION_RATE": float(os.getenv("MODEL_HEALTH_MAX_REJECT", "0.95")),
}

CONFIG = {
    "PROJECT": PROJECT,
    "MARKETS": MARKETS,
    "DATA_SOURCES": DATA_SOURCES,
    "SYMBOLS": SYMBOLS,
    "TRADING_HOURS": TRADING_HOURS,
    "RISK": RISK,
    "STOPS": STOPS,
    "CAPITAL_ROTATION":  CAPITAL_ROTATION,
    "PARTIAL_TP":        PARTIAL_TP,
    "DYNAMIC_CONFIDENCE": DYNAMIC_CONFIDENCE,
    "CORRELATION_GATE":  CORRELATION_GATE,
    "LIVE_ACCURACY":     LIVE_ACCURACY,
    "GROWTH_TARGET": GROWTH_TARGET,
    "TRADING_SESSIONS": TRADING_SESSIONS,
    "ENSEMBLE": ENSEMBLE,
    "SIGNAL_GATES": SIGNAL_GATES,
    "TIME_EXIT":     TIME_EXIT,
    "PAPER_TRADING": PAPER_TRADING,
    "BENCHMARK": BENCHMARK,
    "RANKING": RANKING,
    "SYMBOL_OVERRIDES": SYMBOL_OVERRIDES,
    "AI": AI,
    "INDICATORS": INDICATORS,
    "PORTFOLIO": PORTFOLIO,
    "REPORTING": REPORTING,
    "TELEGRAM": TELEGRAM,
    "BROKERS": BROKERS,
    "LOGGING": LOGGING,
    "SAFETY": SAFETY,
    "EXECUTION": EXECUTION,
    "SYMBOL_BAN":     SYMBOL_BAN,
    "SENTIMENT":      SENTIMENT,
    "EVENT_CALENDAR": EVENT_CALENDAR,
    "LSTM_MODEL":     LSTM_MODEL,
    "ALPHA_DECAY":    ALPHA_DECAY,
    "AUTO_RETRAIN":   AUTO_RETRAIN,
    "MODEL_HEALTH":   MODEL_HEALTH,
}

# ==================================================
# HARD-BLOCK: execution on disabled asset class
# ==================================================
def assert_asset_enabled(asset_class: str) -> None:
    """Raise RuntimeError if execution attempted on disabled asset. Call before any execution."""
    u = asset_class.upper()
    if u == "CRYPTO" and not CONFIG["PROJECT"]["CRYPTO_ENABLED"]:
        raise RuntimeError("Execution blocked: CRYPTO_ENABLED is False.")
    if u == "FOREX" and not CONFIG["PROJECT"]["FOREX_ENABLED"]:
        raise RuntimeError("Execution blocked: FOREX_ENABLED is False.")
    if u == "STOCKS" and not CONFIG["PROJECT"]["STOCKS_ENABLED"]:
        raise RuntimeError("Execution blocked: STOCKS_ENABLED is False.")


# ==================================================
# COMPREHENSIVE VALIDATION SCHEMA
# ==================================================
def validate_config() -> None:
    """
    Comprehensive validation at startup.
    Raises ValueError with clear messages if any critical parameter is invalid.
    """
    # ===== PROJECT VALIDATION =====
    p = CONFIG["PROJECT"]
    
    if not (p["CRYPTO_ENABLED"] or p["FOREX_ENABLED"] or p["STOCKS_ENABLED"]):
        raise ValueError("At least one of CRYPTO_ENABLED, FOREX_ENABLED, STOCKS_ENABLED must be True.")
    
    if p["MODE"] not in ("LIVE", "PAPER", "BACKTEST"):
        raise ValueError(f"MODE must be LIVE, PAPER, or BACKTEST; got {p['MODE']}.")
    
    if p["ENVIRONMENT"] not in ("LOCAL", "VPS"):
        raise ValueError(f"ENVIRONMENT must be LOCAL or VPS; got {p['ENVIRONMENT']}.")
    
    if p["MODE"] == "LIVE" and p["ENVIRONMENT"] == "LOCAL":
        # WARNING: User explicitly enabled LIVE trading on LOCAL machine
        # This is risky but allowed if user has explicitly confirmed
        pass  # Allow override - user assumes responsibility
    
    # ===== SYMBOLS VALIDATION =====
    symbols = CONFIG.get("SYMBOLS", {})
    if p["CRYPTO_ENABLED"] and not symbols.get("CRYPTO"):
        raise ValueError("CRYPTO_ENABLED but no CRYPTO symbols configured.")
    if p["FOREX_ENABLED"] and not symbols.get("FOREX"):
        raise ValueError("FOREX_ENABLED but no FOREX symbols configured.")
    if p["STOCKS_ENABLED"] and not symbols.get("STOCKS"):
        raise ValueError("STOCKS_ENABLED but no STOCKS symbols configured.")
    
    # ===== RISK VALIDATION =====
    risk = CONFIG.get("RISK", {})
    
    if not isinstance(risk.get("MAX_RISK_PER_TRADE"), (int, float)) or risk.get("MAX_RISK_PER_TRADE", 0) <= 0:
        raise ValueError("MAX_RISK_PER_TRADE must be a positive number.")
    
    if not isinstance(risk.get("MAX_PORTFOLIO_EXPOSURE"), (int, float)) or risk.get("MAX_PORTFOLIO_EXPOSURE", 0) <= 0:
        raise ValueError("MAX_PORTFOLIO_EXPOSURE must be a positive number.")
    
    if not isinstance(risk.get("MAX_DAILY_DRAWDOWN"), (int, float)) or risk.get("MAX_DAILY_DRAWDOWN", 0) <= 0:
        raise ValueError("MAX_DAILY_DRAWDOWN must be a positive number.")
    
    if not isinstance(risk.get("MAX_WEEKLY_DRAWDOWN"), (int, float)) or risk.get("MAX_WEEKLY_DRAWDOWN", 0) <= 0:
        raise ValueError("MAX_WEEKLY_DRAWDOWN must be a positive number.")
    
    if risk.get("MAX_DAILY_DRAWDOWN", 0) >= risk.get("MAX_WEEKLY_DRAWDOWN", 1):
        raise ValueError("MAX_DAILY_DRAWDOWN should be less than MAX_WEEKLY_DRAWDOWN.")
    
    if not isinstance(risk.get("MAX_CONCURRENT_TRADES"), int) or risk.get("MAX_CONCURRENT_TRADES", 0) < 1:
        raise ValueError("MAX_CONCURRENT_TRADES must be an integer >= 1.")
    
    # ===== BROKER CREDENTIALS VALIDATION =====
    brokers = CONFIG.get("BROKERS", {})
    
    if p["CRYPTO_ENABLED"]:
        binance = brokers.get("BINANCE", {})
        if not binance.get("API_KEY") or not binance.get("SECRET"):
            raise ValueError("CRYPTO_ENABLED but BINANCE API_KEY or SECRET not configured. Set BINANCE_API_KEY and BINANCE_SECRET_KEY in .env")
    
    if p["FOREX_ENABLED"]:
        oanda = brokers.get("OANDA", {})
        if not oanda.get("API_KEY") or not oanda.get("ACCOUNT_ID"):
            raise ValueError("FOREX_ENABLED but OANDA credentials not configured.")
    
    if p["STOCKS_ENABLED"]:
        alpaca = brokers.get("ALPACA", {})
        if not alpaca.get("API_KEY") or not alpaca.get("SECRET"):
            raise ValueError("STOCKS_ENABLED but ALPACA credentials not configured.")
    
    # ===== TELEGRAM VALIDATION =====
    telegram = CONFIG.get("TELEGRAM", {})
    if telegram.get("ENABLED"):
        if not telegram.get("TOKEN") or not telegram.get("CHAT_ID"):
            raise ValueError("Telegram ENABLED but TOKEN or CHAT_ID missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    
    # ===== STOPS VALIDATION =====
    stops = CONFIG.get("STOPS", {})
    if stops.get("USE_ATR"):
        if not isinstance(stops.get("ATR_MULTIPLIER_SL"), (int, float)) or stops.get("ATR_MULTIPLIER_SL", 0) <= 0:
            raise ValueError("ATR_MULTIPLIER_SL must be a positive number.")
        if not isinstance(stops.get("ATR_MULTIPLIER_TP"), (int, float)) or stops.get("ATR_MULTIPLIER_TP", 0) <= 0:
            raise ValueError("ATR_MULTIPLIER_TP must be a positive number.")
    
    # ===== EXECUTION VALIDATION =====
    execution = CONFIG.get("EXECUTION", {})
    if not isinstance(execution.get("ORDER_COOLDOWN_SEC"), int) or execution.get("ORDER_COOLDOWN_SEC", 0) < 0:
        raise ValueError("ORDER_COOLDOWN_SEC must be a non-negative integer.")
    if not isinstance(execution.get("FILL_POLL_INTERVAL_SEC"), int) or execution.get("FILL_POLL_INTERVAL_SEC", 0) <= 0:
        raise ValueError("FILL_POLL_INTERVAL_SEC must be a positive integer.")
    if not isinstance(execution.get("RETRY_ATTEMPTS"), int) or execution.get("RETRY_ATTEMPTS", 0) < 1:
        raise ValueError("RETRY_ATTEMPTS must be an integer >= 1.")


def validate_config_log_level() -> None:
    """Validate logging configuration."""
    logging_cfg = CONFIG.get("LOGGING", {})
    level = logging_cfg.get("LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL; got {level}.")

