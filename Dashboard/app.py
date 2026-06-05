"""
AegisQuant Dashboard v3.2.0
============================
6-page professional trading dashboard.
New in v3.2: Live market ticker, BTC benchmark, Monte Carlo simulation,
             PnL calendar heatmap, trade replay viewer, AI trade explanation,
             paper trading mode indicator, advanced statistics.
Pages: Command Center | Equity & PnL | Symbol Breakdown |
       Trade History  | System Health | Advanced Analytics
"""

import os, sys, json, math, time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from AegisQuantConfig import CONFIG
from Core.StateManager import StateManager

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="AegisQuant v3.2",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# CUSTOM CSS — enhanced dark professional theme
# ─────────────────────────────────────────────
st.markdown("""
<style>
    /* ── Fonts ─────────────────────────────── */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

    /* ── Keyframe Animations ────────────────── */
    @keyframes gradient-shift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }
    @keyframes pulse-live {
        0%, 100% { box-shadow: 0 0 0 0 rgba(100, 255, 218, 0.5); }
        50%       { box-shadow: 0 0 0 6px rgba(100, 255, 218, 0); }
    }
    @keyframes glow-border {
        0%, 100% { border-color: #3a7bd5; box-shadow: 0 0 8px rgba(58,123,213,0.3); }
        50%       { border-color: #64ffda; box-shadow: 0 0 18px rgba(100,255,218,0.25); }
    }
    @keyframes slide-in {
        from { opacity: 0; transform: translateY(-8px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes shimmer {
        0%   { background-position: -200% 0; }
        100% { background-position:  200% 0; }
    }
    @keyframes count-up {
        from { opacity: 0; transform: scale(0.95); }
        to   { opacity: 1; transform: scale(1); }
    }

    /* ── Global ─────────────────────────────── */
    .stApp {
        background: radial-gradient(ellipse at top, #0d1520 0%, #080d14 60%, #060a10 100%);
        font-family: 'Inter', sans-serif;
    }

    /* ── Metric cards ───────────────────────── */
    [data-testid="metric-container"] {
        background: linear-gradient(145deg, #131c2e 0%, #0e1520 100%);
        border: 1px solid rgba(58,123,213,0.35);
        border-radius: 14px;
        padding: 18px 22px;
        box-shadow: 0 4px 24px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.04);
        transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
        animation: slide-in 0.35s ease;
    }
    [data-testid="metric-container"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 30px rgba(58,123,213,0.25), inset 0 1px 0 rgba(255,255,255,0.06);
        border-color: rgba(100,255,218,0.4);
    }
    [data-testid="metric-container"] label {
        color: #6b7db3 !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        letter-spacing: 0.1em !important;
        text-transform: uppercase !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        color: #e2e8f6 !important;
        font-size: 1.65rem !important;
        font-weight: 800 !important;
        letter-spacing: -0.02em !important;
        animation: count-up 0.4s ease;
    }
    [data-testid="metric-container"] [data-testid="stMetricDelta"] {
        font-size: 0.82rem !important;
        font-weight: 600 !important;
    }

    /* ── Header banner ──────────────────────── */
    .aegis-header {
        background: linear-gradient(270deg, #0a1628, #0f2040, #091628, #0d1b35);
        background-size: 300% 300%;
        animation: gradient-shift 8s ease infinite;
        border: 1px solid rgba(58,123,213,0.5);
        border-radius: 16px;
        padding: 22px 32px;
        margin-bottom: 24px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        box-shadow: 0 4px 32px rgba(58,123,213,0.15), inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .aegis-header h1 {
        color: #64ffda;
        margin: 0;
        font-size: 1.85rem;
        font-weight: 800;
        letter-spacing: -0.02em;
        text-shadow: 0 0 30px rgba(100,255,218,0.3);
    }
    .aegis-header p { color: #6b7db3; margin: 4px 0 0 0; font-size: 0.82rem; }

    /* ── Growth card ────────────────────────── */
    .growth-card {
        background: linear-gradient(145deg, #0c1e35 0%, #091628 100%);
        border: 1px solid rgba(58,123,213,0.4);
        border-radius: 16px;
        padding: 24px;
        margin-bottom: 20px;
        box-shadow: 0 4px 24px rgba(0,0,0,0.35);
        animation: glow-border 4s ease infinite;
    }
    .growth-title {
        color: #64ffda;
        font-size: 1.05rem;
        font-weight: 700;
        margin-bottom: 10px;
        letter-spacing: -0.01em;
    }
    .growth-amounts { color: #b8c5e0; font-size: 0.88rem; margin-bottom: 14px; }
    .progress-outer {
        background: rgba(26,39,68,0.8);
        border-radius: 999px;
        height: 20px;
        width: 100%;
        overflow: hidden;
        border: 1px solid rgba(45,53,72,0.8);
        box-shadow: inset 0 2px 4px rgba(0,0,0,0.3);
    }
    .progress-inner {
        height: 100%;
        border-radius: 999px;
        background: linear-gradient(90deg, #1a4db5, #3a7bd5, #64ffda);
        background-size: 200% 100%;
        animation: shimmer 3s linear infinite;
        display: flex;
        align-items: center;
        justify-content: flex-end;
        padding-right: 10px;
        font-size: 0.72rem;
        font-weight: 800;
        color: #050d1a;
        min-width: 36px;
    }
    .growth-milestones { color: #5c6d90; font-size: 0.76rem; margin-top: 12px; }

    /* ── Status badges ──────────────────────── */
    .badge-green  { background: rgba(0,230,118,0.1); color:#64ffda;
                    border:1px solid rgba(0,230,118,0.4);
                    padding:4px 12px; border-radius:999px; font-size:0.72rem; font-weight:700;
                    animation: pulse-live 2s ease infinite; display:inline-block; }
    .badge-red    { background: rgba(255,82,82,0.1); color:#ff6b6b;
                    border:1px solid rgba(255,82,82,0.4);
                    padding:4px 12px; border-radius:999px; font-size:0.72rem; font-weight:700; }
    .badge-yellow { background: rgba(255,215,64,0.1); color:#ffd740;
                    border:1px solid rgba(255,215,64,0.35);
                    padding:4px 12px; border-radius:999px; font-size:0.72rem; font-weight:700; }
    .badge-blue   { background: rgba(100,181,246,0.1); color:#64b5f6;
                    border:1px solid rgba(100,181,246,0.35);
                    padding:4px 12px; border-radius:999px; font-size:0.72rem; font-weight:700; }
    .badge-paper  { background: rgba(255,171,64,0.1); color:#ffab40;
                    border:1px solid rgba(255,171,64,0.35);
                    padding:4px 12px; border-radius:999px; font-size:0.72rem; font-weight:700; }
    .badge-teal   { background: rgba(0,229,204,0.1); color:#00e5cc;
                    border:1px solid rgba(0,229,204,0.35);
                    padding:4px 12px; border-radius:999px; font-size:0.72rem; font-weight:700; }

    /* ── Section headers ────────────────────── */
    .section-header {
        color: #64ffda;
        font-size: 0.8rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        padding-bottom: 10px;
        border-bottom: 1px solid rgba(45,53,72,0.7);
        margin-bottom: 18px;
    }

    /* ── Paper mode banner ──────────────────── */
    .paper-banner {
        background: linear-gradient(90deg, rgba(42,26,0,0.9) 0%, rgba(59,40,0,0.9) 100%);
        border: 1px solid rgba(255,171,64,0.5);
        border-radius: 10px;
        padding: 11px 18px;
        margin-bottom: 18px;
        color: #ffab40;
        font-weight: 700;
        font-size: 0.88rem;
        display: flex;
        align-items: center;
        gap: 10px;
        box-shadow: 0 0 20px rgba(255,171,64,0.08);
    }

    /* ── Live ticker strip ──────────────────── */
    .ticker-strip {
        background: linear-gradient(135deg, #0b1628 0%, #0e1a2e 100%);
        border: 1px solid rgba(45,53,72,0.7);
        border-radius: 12px;
        padding: 14px 22px;
        margin-bottom: 18px;
        overflow: hidden;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }
    .ticker-item {
        display: inline-block;
        margin-right: 32px;
        text-align: center;
        transition: transform 0.2s;
    }
    .ticker-item:hover { transform: scale(1.05); }
    .ticker-symbol { color: #5c6d90; font-size: 0.68rem; font-weight: 700; letter-spacing: 0.08em; }
    .ticker-price  { color: #dce6f8; font-size: 1rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
    .ticker-up     { color: #64ffda; font-size: 0.75rem; font-weight: 700; }
    .ticker-down   { color: #ff6b6b; font-size: 0.75rem; font-weight: 700; }

    /* ── Benchmark card ─────────────────────── */
    .benchmark-card {
        background: linear-gradient(145deg, #0c1e35 0%, #091628 100%);
        border: 1px solid rgba(58,123,213,0.35);
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 16px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.25);
    }

    /* ── Sidebar ────────────────────────────── */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #07101e 0%, #050d18 100%);
        border-right: 1px solid rgba(45,53,72,0.6);
    }
    [data-testid="stSidebar"] .block-container { padding-top: 0.75rem; }

    /* Sidebar radio nav — pill style */
    [data-testid="stSidebar"] [data-testid="stRadio"] label {
        border-radius: 8px !important;
        padding: 8px 12px !important;
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        color: #7a8aac !important;
        transition: all 0.2s ease !important;
        display: block !important;
        margin-bottom: 4px !important;
    }
    [data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
        background: rgba(58,123,213,0.12) !important;
        color: #b8c5e0 !important;
    }
    [data-testid="stSidebar"] [data-testid="stRadio"] [aria-checked="true"] + label,
    [data-testid="stSidebar"] [data-testid="stRadio"] input:checked + div label {
        background: rgba(100,255,218,0.08) !important;
        color: #64ffda !important;
        border-left: 3px solid #64ffda !important;
    }

    /* ── Hide streamlit branding ────────────── */
    #MainMenu, footer { visibility: hidden; }
    header { visibility: hidden; }

    /* ── Divider ────────────────────────────── */
    hr { border-color: rgba(45,53,72,0.5) !important; }

    /* ── AI Explain card ────────────────────── */
    .ai-explain-card {
        background: linear-gradient(145deg, #0c1828 0%, #091420 100%);
        border: 1px solid rgba(30,39,56,0.9);
        border-radius: 10px;
        padding: 18px;
        margin-top: 10px;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }
    .feature-bar-label { color: #6b7db3; font-size: 0.76rem; font-weight: 500; }
    .feature-bar-outer {
        background: rgba(26,39,68,0.7);
        border-radius: 999px;
        height: 8px;
        width: 100%;
        overflow: hidden;
    }
    .feature-bar-inner {
        height: 100%;
        border-radius: 999px;
        background: linear-gradient(90deg, #3a7bd5, #64ffda);
    }

    /* ── Monte Carlo info box ───────────────── */
    .mc-info {
        background: linear-gradient(145deg, #0c1828 0%, #091420 100%);
        border: 1px solid rgba(30,39,56,0.9);
        border-radius: 10px;
        padding: 14px 18px;
        color: #6b7db3;
        font-size: 0.8rem;
        line-height: 1.6;
    }

    /* ── Glass info cards ───────────────────── */
    .glass-card {
        background: rgba(13,27,45,0.6);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        border: 1px solid rgba(58,123,213,0.2);
        border-radius: 14px;
        padding: 20px 24px;
        margin-bottom: 16px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.3);
    }

    /* ── Stat number highlight ──────────────── */
    .stat-big {
        font-size: 2rem;
        font-weight: 800;
        letter-spacing: -0.03em;
        color: #e2e8f6;
        font-family: 'JetBrains Mono', monospace;
    }
    .stat-label {
        font-size: 0.7rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: #5c6d90;
        margin-bottom: 4px;
    }

    /* ── Buttons ────────────────────────────── */
    .stButton > button {
        background: linear-gradient(135deg, #1a3a6b 0%, #2a5298 100%);
        color: #e2e8f6;
        border: 1px solid rgba(100,181,246,0.3);
        border-radius: 8px;
        font-weight: 600;
        font-size: 0.85rem;
        transition: all 0.2s ease;
    }
    .stButton > button:hover {
        background: linear-gradient(135deg, #2a4a7b 0%, #3a62a8 100%);
        border-color: rgba(100,255,218,0.4);
        box-shadow: 0 4px 16px rgba(58,123,213,0.3);
        transform: translateY(-1px);
    }

    /* ── Scrollbar ──────────────────────────── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #07101e; }
    ::-webkit-scrollbar-thumb { background: #2d3548; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #3a7bd5; }

    /* ── Dataframe ──────────────────────────── */
    [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
LOG_DIR          = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
TRADES_FILE      = os.path.join(LOG_DIR, "trades.csv")
BAL_FILE         = os.path.join(LOG_DIR, "balances.csv")
PAPER_FILE       = os.path.join(LOG_DIR, "paper_trades.csv")
MODEL_DIR        = CONFIG["AI"]["MODEL_PATH"]
BENCHMARK_FILE   = CONFIG.get("BENCHMARK", {}).get(
    "START_PRICE_FILE", os.path.join(LOG_DIR, "benchmark_start.json")
)
PAPER_CFG        = CONFIG.get("PAPER_TRADING", {})
PAPER_MODE_ON    = PAPER_CFG.get("ENABLED", False) or \
                   CONFIG["PROJECT"].get("MODE", "LIVE") == "PAPER"

CHART_THEME = dict(
    paper_bgcolor="#0e1117",
    plot_bgcolor="#0e1117",
    font=dict(color="#8892b0", size=12),
    xaxis=dict(gridcolor="#1e2738", linecolor="#2d3548", tickcolor="#8892b0"),
    yaxis=dict(gridcolor="#1e2738", linecolor="#2d3548", tickcolor="#8892b0"),
    margin=dict(l=40, r=20, t=40, b=40),
)

GROWTH_CFG  = CONFIG.get("GROWTH_TARGET", {})
IDR_RATE    = GROWTH_CFG.get("IDR_RATE", 16_000)
START_IDR   = GROWTH_CFG.get("STARTING_CAPITAL_IDR", 300_000)
TARGET_IDR  = GROWTH_CFG.get("TARGET_CAPITAL_IDR", 10_000_000)

# Binance endpoints
_BINANCE_TICKER_URL  = "https://api.binance.com/api/v3/ticker/24hr"
_BINANCE_KLINES_URL  = "https://api.binance.com/api/v3/klines"
_BINANCE_PRICE_URL   = "https://api.binance.com/api/v3/ticker/price"


# ─────────────────────────────────────────────
# DATA LOADERS  (cached)
# ─────────────────────────────────────────────
@st.cache_data(ttl=10)
def load_trades() -> pd.DataFrame:
    if not os.path.exists(TRADES_FILE):
        return pd.DataFrame()
    df = pd.read_csv(TRADES_FILE)
    if "Timestamp" in df.columns:
        try:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], format="ISO8601", utc=True)
        except Exception:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True, errors="coerce")
    for col in ("PnL", "Confidence", "Edge_Score", "Price", "Quantity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=10)
def load_balance_history() -> pd.DataFrame:
    if not os.path.exists(BAL_FILE):
        return pd.DataFrame()
    df = pd.read_csv(BAL_FILE)
    if "Timestamp" in df.columns:
        try:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], format="ISO8601", utc=True)
        except Exception:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True, errors="coerce")
    return df


@st.cache_data(ttl=10)
def load_current_balance() -> float:
    try:
        sm = StateManager()
        return float(sm.load_balance() or 0.0)
    except Exception:
        return 0.0


@st.cache_data(ttl=10)
def load_positions() -> list:
    try:
        sm = StateManager()
        return sm.load_positions() or []
    except Exception:
        return []


@st.cache_data(ttl=10)
def load_paper_trades() -> pd.DataFrame:
    """Load paper trading simulation log."""
    if not os.path.exists(PAPER_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(PAPER_FILE)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        for col in ("simulated_fill", "pnl"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_model_health() -> list:
    rows = []
    if not os.path.exists(MODEL_DIR):
        return rows
    for fname in sorted(os.listdir(MODEL_DIR)):
        if not fname.endswith("_metadata.json"):
            continue
        fpath = os.path.join(MODEL_DIR, fname)
        try:
            with open(fpath) as f:
                m = json.load(f)
            sym_raw = fname.replace("RandomForest_CRYPTO_", "").replace("_metadata.json", "")
            symbol  = sym_raw[:-4] + "/" + sym_raw[-4:] if len(sym_raw) > 4 else sym_raw
            acc     = m.get("global_test_accuracy", 0.0)
            n_train = m.get("n_train", 0)
            n_test  = m.get("n_test", 0)
            version = m.get("version", "unknown")
            wf      = m.get("walk_forward", {})
            wf_mean = wf.get("mean_accuracy", None)
            wf_ok   = wf.get("consistent", None)
            rows.append({
                "Symbol":        symbol,
                "Accuracy":      acc,
                "WF Accuracy":   round(wf_mean, 3) if wf_mean is not None else "—",
                "WF Consistent": ("✅" if wf_ok else "⚠️") if wf_ok is not None else "—",
                "Train Samples": n_train,
                "Test Samples":  n_test,
                "Trained On":    version[:10] if version else "—",
                "Status":        "✅ Good" if acc >= 0.55 and n_train >= 100 else
                                 ("⚠️ Weak" if acc >= 0.50 else "❌ Below Random"),
            })
        except Exception:
            pass
    return rows


@st.cache_data(ttl=15)
def fetch_live_prices() -> dict:
    """Fetch current 24hr ticker from Binance REST API (15s cache)."""
    if not _REQUESTS_AVAILABLE:
        return {}
    active_symbols = set(CONFIG["SYMBOLS"].get("CRYPTO", {}).keys())
    prices = {}
    try:
        resp = _requests.get(_BINANCE_TICKER_URL, timeout=5)
        if resp.status_code == 200:
            for item in resp.json():
                sym = item.get("symbol", "")
                if sym.endswith("USDT"):
                    pair = sym[:-4] + "/USDT"
                    if pair in active_symbols:
                        prices[pair] = {
                            "price":      float(item.get("lastPrice", 0)),
                            "change_pct": float(item.get("priceChangePercent", 0)),
                            "volume":     float(item.get("volume", 0)),
                            "high":       float(item.get("highPrice", 0)),
                            "low":        float(item.get("lowPrice", 0)),
                        }
    except Exception:
        pass
    return prices


@st.cache_data(ttl=30)
def load_benchmark() -> Optional[dict]:
    """Load BTC benchmark start data from logs/benchmark_start.json."""
    bench_cfg = CONFIG.get("BENCHMARK", {})
    if not bench_cfg.get("ENABLED", True):
        return None
    if not os.path.exists(BENCHMARK_FILE):
        return None
    try:
        with open(BENCHMARK_FILE) as f:
            return json.load(f)
    except Exception:
        return None


@st.cache_data(ttl=30)
def fetch_btc_current_price() -> Optional[float]:
    """Fetch current BTC price from Binance."""
    if not _REQUESTS_AVAILABLE:
        return None
    try:
        resp = _requests.get(_BINANCE_PRICE_URL, params={"symbol": "BTCUSDT"}, timeout=5)
        if resp.status_code == 200:
            return float(resp.json().get("price", 0))
    except Exception:
        pass
    return None


@st.cache_data(ttl=60)
def fetch_candles_for_replay(symbol: str, interval: str = "5m", limit: int = 120) -> pd.DataFrame:
    """Fetch OHLCV candlestick data from Binance for trade replay."""
    if not _REQUESTS_AVAILABLE:
        return pd.DataFrame()
    try:
        binance_sym = symbol.replace("/", "")
        resp = _requests.get(
            _BINANCE_KLINES_URL,
            params={"symbol": binance_sym, "interval": interval, "limit": limit},
            timeout=8,
        )
        if resp.status_code == 200:
            cols = ["timestamp", "open", "high", "low", "close", "volume",
                    "close_time", "quote_vol", "trades", "tb_base", "tb_quote", "_"]
            df = pd.DataFrame(resp.json(), columns=cols)
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c])
            return df[["timestamp", "open", "high", "low", "close", "volume"]]
    except Exception:
        pass
    return pd.DataFrame()


def load_trade_explanation(symbol: str) -> Optional[dict]:
    """Load feature importances from model metadata JSON for AI explanation."""
    base      = f"RandomForest_CRYPTO_{symbol.replace('/', '')}"
    meta_path = os.path.join(MODEL_DIR, f"{base}_metadata.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────
# ANALYTICS CALCULATORS
# ─────────────────────────────────────────────
def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    mask = df["Result"].str.upper().isin(["CLOSE", "CLOSED", "SELL"]) \
        if "Result" in df.columns else pd.Series(False, index=df.index)
    closed = df[mask].copy()
    if "PnL" in df.columns:
        pnl_mask = df["PnL"] != 0
        closed = df[mask | pnl_mask].copy()
    return closed


def calc_stats(df: pd.DataFrame) -> dict:
    ct = closed_trades(df)
    empty = dict(
        win_rate=0.0, profit_factor=0.0, sharpe=0.0, sortino=0.0, calmar=0.0,
        expectancy=0.0, avg_win=0.0, avg_loss=0.0,
        max_dd=0.0, total_pnl=0.0, total_closed=0,
        best_trade=0.0, worst_trade=0.0, avg_rr=0.0,
    )
    if ct.empty or "PnL" not in ct.columns:
        return empty

    pnl    = ct["PnL"].dropna()
    wins   = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    n      = len(pnl)
    if n == 0:
        return empty

    wr     = len(wins) / n
    avg_w  = wins.mean()   if len(wins)   > 0 else 0.0
    avg_l  = losses.mean() if len(losses) > 0 else 0.0
    pf     = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    exp    = wr * avg_w + (1 - wr) * avg_l
    avg_rr = abs(avg_w / avg_l) if avg_l != 0 else 0.0

    sharpe  = (pnl.mean() / pnl.std() * math.sqrt(n)) if pnl.std() > 0 else 0.0
    sortino_denom = losses.std() if len(losses) > 1 else 0.0
    sortino = (pnl.mean() / sortino_denom * math.sqrt(n)) if sortino_denom > 0 else 0.0

    cum      = pnl.cumsum()
    roll_max = cum.cummax()
    dd       = cum - roll_max
    max_dd   = dd.min() if len(dd) > 0 else 0.0
    calmar   = pnl.sum() / abs(max_dd) if max_dd != 0 else 0.0

    return dict(
        win_rate=wr, profit_factor=pf, sharpe=sharpe, sortino=sortino, calmar=calmar,
        expectancy=exp, avg_win=avg_w, avg_loss=avg_l,
        max_dd=max_dd, total_pnl=pnl.sum(), total_closed=n,
        best_trade=pnl.max(), worst_trade=pnl.min(), avg_rr=avg_rr,
    )


def growth_progress(balance_usd: float) -> dict:
    current_idr  = balance_usd * IDR_RATE
    gain_idr     = current_idr - START_IDR
    needed_idr   = max(0, TARGET_IDR - current_idr)
    pct          = max(0.0, min(100.0, gain_idr / (TARGET_IDR - START_IDR) * 100))
    x_from_start = current_idr / START_IDR if START_IDR > 0 else 1.0
    return dict(
        current_idr=current_idr, gain_idr=gain_idr,
        needed_idr=needed_idr, pct=pct, x_from_start=x_from_start,
    )


def risk_tier_info(balance_usd: float) -> Tuple[str, str, str]:
    tiers = CONFIG["RISK"]["TIERS"]
    if balance_usd < tiers["MICRO"]["MAX_BALANCE"]:
        return "MICRO", "🔥 Growth Mode", "badge-red"
    if balance_usd < tiers["AGGRESSIVE"]["MAX_BALANCE"]:
        return "AGGRESSIVE", "⚡ Aggressive", "badge-yellow"
    if balance_usd < tiers["STANDARD"]["MAX_BALANCE"]:
        return "STANDARD", "⚖️ Standard", "badge-blue"
    return "CONSERVATIVE", "🛡️ Conservative", "badge-green"


# ─────────────────────────────────────────────
# CHART BUILDERS
# ─────────────────────────────────────────────
def chart_equity_curve(ct: pd.DataFrame) -> Optional[go.Figure]:
    if ct.empty or "PnL" not in ct.columns:
        return None
    ct = ct.sort_values("Timestamp") if "Timestamp" in ct.columns else ct
    ct = ct.copy()
    ct["Cumulative PnL"] = ct["PnL"].cumsum()
    roll_max = ct["Cumulative PnL"].cummax()
    ct["Drawdown"] = ct["Cumulative PnL"] - roll_max

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.06,
        subplot_titles=("Equity Curve (Cumulative PnL $)", "Drawdown ($)"),
    )
    x_axis = ct["Timestamp"] if "Timestamp" in ct.columns else ct.index

    fig.add_trace(go.Scatter(
        x=x_axis, y=ct["Cumulative PnL"],
        mode="lines", name="Equity",
        line=dict(color="#64ffda", width=2.5),
        fill="tozeroy", fillcolor="rgba(100,255,218,0.07)",
    ), row=1, col=1)
    fig.add_hline(y=0, line_dash="dot", line_color="#8892b0", line_width=1, row=1, col=1)
    fig.add_trace(go.Scatter(
        x=x_axis, y=ct["Drawdown"],
        mode="lines", name="Drawdown",
        line=dict(color="#ff5252", width=1.5),
        fill="tozeroy", fillcolor="rgba(255,82,82,0.15)",
    ), row=2, col=1)
    fig.update_layout(
        **CHART_THEME, height=450, showlegend=True,
        legend=dict(orientation="h", y=1.08, x=0),
    )
    for i in (1, 2):
        fig.update_xaxes(row=i, col=1, **CHART_THEME["xaxis"])
        fig.update_yaxes(row=i, col=1, **CHART_THEME["yaxis"])
    return fig


def chart_rolling_winrate(ct: pd.DataFrame, window: int = 20) -> Optional[go.Figure]:
    if ct.empty or len(ct) < 5:
        return None
    ct = ct.sort_values("Timestamp") if "Timestamp" in ct.columns else ct
    ct = ct.copy()
    ct["win"]       = (ct["PnL"] > 0).astype(float)
    ct["RollingWR"] = ct["win"].rolling(window, min_periods=3).mean() * 100
    x_axis = ct["Timestamp"] if "Timestamp" in ct.columns else ct.index

    fig = go.Figure()
    fig.add_hline(y=50, line_dash="dash", line_color="#ffd740", line_width=1,
                  annotation_text="Break-even 50%", annotation_font_color="#ffd740")
    fig.add_trace(go.Scatter(
        x=x_axis, y=ct["RollingWR"],
        mode="lines+markers", name=f"Rolling {window}-trade WR",
        line=dict(color="#3a7bd5", width=2), marker=dict(size=5),
    ))
    fig.update_layout(
        **CHART_THEME, title=f"Rolling {window}-Trade Win Rate (%)",
        height=300, yaxis_range=[0, 100],
    )
    return fig


def chart_daily_pnl(ct: pd.DataFrame) -> Optional[go.Figure]:
    if ct.empty or "Timestamp" not in ct.columns:
        return None
    ct = ct.copy()
    ct["Date"] = ct["Timestamp"].dt.date
    daily  = ct.groupby("Date")["PnL"].sum().reset_index()
    colors = ["#64ffda" if v >= 0 else "#ff5252" for v in daily["PnL"]]
    fig = go.Figure(go.Bar(x=daily["Date"], y=daily["PnL"], marker_color=colors))
    fig.add_hline(y=0, line_color="#8892b0", line_width=1)
    fig.update_layout(**CHART_THEME, title="Daily PnL ($)", height=280)
    return fig


def chart_pnl_distribution(ct: pd.DataFrame) -> Optional[go.Figure]:
    if ct.empty or "PnL" not in ct.columns:
        return None
    pnl = ct["PnL"].dropna()
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=pnl[pnl > 0], nbinsx=15, name="Wins",
                               marker_color="rgba(100,255,218,0.7)"))
    fig.add_trace(go.Histogram(x=pnl[pnl < 0], nbinsx=15, name="Losses",
                               marker_color="rgba(255,82,82,0.7)"))
    fig.add_vline(x=pnl.mean(), line_dash="dash", line_color="#ffd740",
                  annotation_text=f"Mean ${pnl.mean():.3f}", annotation_font_color="#ffd740")
    fig.update_layout(**CHART_THEME, barmode="overlay", title="PnL Distribution", height=300)
    return fig


def chart_symbol_pnl(ct: pd.DataFrame) -> Optional[go.Figure]:
    if ct.empty or "Symbol" not in ct.columns:
        return None
    grp = ct.groupby("Symbol").agg(
        Total_PnL=("PnL", "sum"),
        Trades=("PnL", "count"),
        WinRate=("PnL", lambda x: (x > 0).mean() * 100),
    ).reset_index().sort_values("Total_PnL", ascending=True)
    colors = ["#64ffda" if v >= 0 else "#ff5252" for v in grp["Total_PnL"]]
    fig = go.Figure(go.Bar(
        x=grp["Total_PnL"], y=grp["Symbol"], orientation="h", marker_color=colors,
        text=[f"${v:.3f} ({w:.0f}%)" for v, w in zip(grp["Total_PnL"], grp["WinRate"])],
        textposition="outside",
    ))
    fig.add_vline(x=0, line_color="#8892b0", line_width=1)
    fig.update_layout(**CHART_THEME, title="PnL by Symbol ($)", height=320)
    return fig


def chart_confidence_vs_pnl(ct: pd.DataFrame) -> Optional[go.Figure]:
    if ct.empty or "Confidence" not in ct.columns or "PnL" not in ct.columns:
        return None
    ct = ct[ct["Confidence"] > 0].copy()
    if len(ct) < 3:
        return None
    colors = ["#64ffda" if v >= 0 else "#ff5252" for v in ct["PnL"]]
    fig = go.Figure(go.Scatter(
        x=ct["Confidence"] * 100, y=ct["PnL"],
        mode="markers",
        marker=dict(color=colors, size=8, opacity=0.8,
                    line=dict(width=0.5, color="#2d3548")),
        text=ct.get("Symbol", ""),
        hovertemplate="<b>%{text}</b><br>Confidence: %{x:.1f}%<br>PnL: $%{y:.4f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="#8892b0", line_width=1)
    fig.add_vline(x=65, line_dash="dot", line_color="#ffd740", line_width=1,
                  annotation_text="65% threshold", annotation_font_color="#ffd740")
    fig.update_layout(
        **CHART_THEME, title="AI Confidence vs Trade PnL",
        xaxis_title="AI Confidence (%)", yaxis_title="PnL ($)", height=320,
    )
    return fig


def chart_symbol_winrate(ct: pd.DataFrame) -> Optional[go.Figure]:
    if ct.empty or "Symbol" not in ct.columns:
        return None
    grp = ct.groupby("Symbol").agg(
        WinRate=("PnL", lambda x: (x > 0).mean() * 100),
        Trades=("PnL", "count"),
    ).reset_index()
    grp = grp[grp["Trades"] >= 2].sort_values("WinRate", ascending=False)
    if grp.empty:
        return None
    colors = ["#64ffda" if v >= 50 else "#ff5252" for v in grp["WinRate"]]
    fig = go.Figure(go.Bar(
        x=grp["Symbol"], y=grp["WinRate"], marker_color=colors,
        text=[f"{v:.0f}%<br>({n} trades)" for v, n in zip(grp["WinRate"], grp["Trades"])],
        textposition="outside",
    ))
    fig.add_hline(y=50, line_dash="dash", line_color="#ffd740", line_width=1)
    fig.update_layout(**CHART_THEME, title="Win Rate by Symbol (%)", height=300, yaxis_range=[0, 110])
    return fig


def chart_edge_score_distribution(df: pd.DataFrame) -> Optional[go.Figure]:
    if df.empty or "Edge_Score" not in df.columns:
        return None
    es = df[df["Edge_Score"] > 0]["Edge_Score"]
    if len(es) < 3:
        return None
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=es, nbinsx=20, marker_color="rgba(58,123,213,0.7)"))
    fig.add_vline(x=0.65, line_dash="dash", line_color="#64ffda", line_width=1.5,
                  annotation_text="0.65 threshold", annotation_font_color="#64ffda")
    fig.update_layout(**CHART_THEME, title="Edge Score Distribution", height=280)
    return fig


def chart_growth_gauge(progress_pct: float) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=progress_pct,
        domain={"x": [0, 1], "y": [0, 1]},
        number={"suffix": "%", "font": {"size": 36, "color": "#64ffda"}},
        delta={"reference": 0, "increasing": {"color": "#64ffda"}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": "#8892b0", "tickfont": {"color": "#8892b0"}},
            "bar": {"color": "#3a7bd5", "thickness": 0.25},
            "bgcolor": "#141824",
            "borderwidth": 1,
            "bordercolor": "#2d3548",
            "steps": [
                {"range": [0, 10],   "color": "#0d1b2a"},
                {"range": [10, 25],  "color": "#0f2240"},
                {"range": [25, 50],  "color": "#0d2d5a"},
                {"range": [50, 75],  "color": "#0d3d6e"},
                {"range": [75, 100], "color": "#0d4d82"},
            ],
            "threshold": {"line": {"color": "#64ffda", "width": 3}, "thickness": 0.75, "value": progress_pct},
        },
    ))
    fig.update_layout(
        paper_bgcolor="#0e1117", font={"color": "#8892b0"},
        height=220, margin=dict(l=20, r=20, t=20, b=10),
    )
    return fig


def chart_balance_history(bh: pd.DataFrame) -> Optional[go.Figure]:
    if bh.empty or "Total" not in bh.columns:
        return None
    bh = bh.sort_values("Timestamp") if "Timestamp" in bh.columns else bh
    x_axis = bh["Timestamp"] if "Timestamp" in bh.columns else bh.index
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_axis, y=bh["Total"],
        mode="lines", line=dict(color="#64b5f6", width=2),
        fill="tozeroy", fillcolor="rgba(100,181,246,0.07)",
    ))
    fig.update_layout(**CHART_THEME, title="Balance History (USDT)", height=250)
    return fig


# ── NEW CHART BUILDERS ────────────────────────────────────────────────────────

def chart_monte_carlo(ct: pd.DataFrame, n_sims: int = 1000) -> Optional[go.Figure]:
    """
    Monte Carlo equity path simulation.
    Bootstrap-resamples trade PnL 1000 times and plots percentile fan bands.
    """
    if ct.empty or "PnL" not in ct.columns or len(ct) < 5:
        return None

    pnl = ct["PnL"].dropna().values
    n   = len(pnl)
    rng = np.random.default_rng(seed=42)

    # Bootstrap resample paths
    paths = np.cumsum(
        rng.choice(pnl, size=(n_sims, n), replace=True), axis=1
    )

    p5   = np.percentile(paths, 5,  axis=0)
    p25  = np.percentile(paths, 25, axis=0)
    p50  = np.percentile(paths, 50, axis=0)
    p75  = np.percentile(paths, 75, axis=0)
    p95  = np.percentile(paths, 95, axis=0)
    x    = list(range(1, n + 1))

    fig = go.Figure()

    # 5–95% band
    fig.add_trace(go.Scatter(
        x=x + x[::-1], y=list(p95) + list(p5[::-1]),
        fill="toself", fillcolor="rgba(58,123,213,0.08)",
        line=dict(color="rgba(0,0,0,0)"), name="5–95th percentile",
        showlegend=True,
    ))
    # 25–75% band
    fig.add_trace(go.Scatter(
        x=x + x[::-1], y=list(p75) + list(p25[::-1]),
        fill="toself", fillcolor="rgba(58,123,213,0.18)",
        line=dict(color="rgba(0,0,0,0)"), name="25–75th percentile",
        showlegend=True,
    ))
    # Median path
    fig.add_trace(go.Scatter(
        x=x, y=p50, mode="lines",
        line=dict(color="#3a7bd5", width=2), name="Median simulation",
    ))
    # Actual equity
    actual = np.cumsum(pnl)
    fig.add_trace(go.Scatter(
        x=x, y=actual, mode="lines",
        line=dict(color="#64ffda", width=2.5, dash="dot"), name="Actual equity",
    ))

    fig.add_hline(y=0, line_dash="dot", line_color="#8892b0", line_width=1)
    fig.update_layout(
        **CHART_THEME,
        title=f"🎲 Monte Carlo Equity Path Simulation ({n_sims:,} resamples)",
        xaxis_title="Trade #",
        yaxis_title="Cumulative PnL ($)",
        height=420,
        legend=dict(orientation="h", y=-0.15, x=0),
    )
    return fig


def chart_pnl_calendar(ct: pd.DataFrame) -> Optional[go.Figure]:
    """
    GitHub-style PnL calendar heatmap.
    Each cell = one calendar day, colour = daily PnL sum.
    """
    if ct.empty or "Timestamp" not in ct.columns or "PnL" not in ct.columns:
        return None
    ct = ct.copy()
    ct["Date"] = ct["Timestamp"].dt.date
    daily = ct.groupby("Date")["PnL"].sum().reset_index()
    daily["Date"] = pd.to_datetime(daily["Date"])
    if len(daily) < 2:
        return None

    start = daily["Date"].min()
    end   = daily["Date"].max()
    all_dates  = pd.date_range(start=start, end=end, freq="D")
    daily_full = (
        pd.DataFrame({"Date": all_dates})
        .merge(daily, on="Date", how="left")
    )
    daily_full["PnL"]     = daily_full["PnL"].fillna(0.0)
    daily_full["week_num"] = (daily_full["Date"] - start).dt.days // 7
    daily_full["dow"]      = daily_full["Date"].dt.dayofweek   # 0 = Mon

    max_week = int(daily_full["week_num"].max())
    z    = np.full((7, max_week + 1), np.nan)
    text = [["" for _ in range(max_week + 1)] for _ in range(7)]

    for _, row in daily_full.iterrows():
        w, d = int(row["week_num"]), int(row["dow"])
        z[d][w] = row["PnL"]
        sign = "+" if row["PnL"] >= 0 else ""
        text[d][w] = f"{row['Date'].strftime('%Y-%m-%d')}<br>{sign}${row['PnL']:.4f}"

    colorscale = [
        [0.0,  "#7b0000"],   # big loss
        [0.35, "#ff5252"],   # small loss
        [0.5,  "#1a2744"],   # zero / break-even
        [0.65, "#00b894"],   # small win
        [1.0,  "#00ff88"],   # big win
    ]

    fig = go.Figure(go.Heatmap(
        z=z, text=text,
        hovertemplate="%{text}<extra></extra>",
        colorscale=colorscale,
        colorbar=dict(title=dict(text="PnL ($)", font=dict(color="#8892b0")),
                      tickfont=dict(color="#8892b0")),
        xgap=3, ygap=3,
        y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        zmid=0,
    ))
    # Do NOT spread **CHART_THEME here — it contains xaxis/yaxis which would
    # conflict with the explicit xaxis/yaxis overrides below (TypeError).
    fig.update_layout(
        paper_bgcolor=CHART_THEME["paper_bgcolor"],
        plot_bgcolor=CHART_THEME["plot_bgcolor"],
        font=CHART_THEME["font"],
        margin=CHART_THEME["margin"],
        title="📅 PnL Calendar Heatmap",
        height=240,
        xaxis=dict(showticklabels=False, gridcolor="rgba(0,0,0,0)", linecolor="rgba(0,0,0,0)"),
        yaxis=dict(tickfont=dict(color="#8892b0", size=11), gridcolor="rgba(0,0,0,0)"),
    )
    return fig


def chart_trade_replay(candles: pd.DataFrame, trade_row: pd.Series) -> go.Figure:
    """Candlestick chart with entry/exit price markers for a single trade."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
                        vertical_spacing=0.04)

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=candles["timestamp"],
        open=candles["open"], high=candles["high"],
        low=candles["low"],   close=candles["close"],
        name="OHLCV",
        increasing_line_color="#64ffda", decreasing_line_color="#ff5252",
        increasing_fillcolor="rgba(100,255,218,0.3)",
        decreasing_fillcolor="rgba(255,82,82,0.3)",
    ), row=1, col=1)

    # Volume bars
    vol_colors = ["rgba(100,255,218,0.4)" if c >= o else "rgba(255,82,82,0.4)"
                  for c, o in zip(candles["close"], candles["open"])]
    fig.add_trace(go.Bar(
        x=candles["timestamp"], y=candles["volume"],
        marker_color=vol_colors, name="Volume",
    ), row=2, col=1)

    # Entry price level
    entry_price = trade_row.get("Price", None) if isinstance(trade_row, pd.Series) else None
    if entry_price and pd.notna(entry_price) and float(entry_price) > 0:
        fig.add_hline(
            y=float(entry_price), line_dash="dash",
            line_color="#ffd740", line_width=1.5,
            annotation_text=f"Entry ${float(entry_price):,.4f}",
            annotation_font_color="#ffd740",
            row=1, col=1,
        )

    symbol = trade_row.get("Symbol", "") if isinstance(trade_row, pd.Series) else ""
    side   = trade_row.get("Side", "") if isinstance(trade_row, pd.Series) else ""
    pnl    = trade_row.get("PnL", 0) if isinstance(trade_row, pd.Series) else 0

    fig.update_layout(
        **CHART_THEME,
        title=f"🔁 Trade Replay — {symbol} | {side} | PnL: ${float(pnl):+.4f}",
        xaxis_rangeslider_visible=False,
        height=420,
        showlegend=False,
    )
    for i in (1, 2):
        fig.update_xaxes(row=i, col=1, **CHART_THEME["xaxis"])
        fig.update_yaxes(row=i, col=1, **CHART_THEME["yaxis"])
    return fig


def chart_feature_importances(fi_list: list, top_n: int = 15) -> go.Figure:
    """Horizontal bar chart of top-N feature importances."""
    fi_list = sorted(fi_list, key=lambda x: x.get("importance", 0), reverse=True)[:top_n]
    features = [r["feature"] for r in fi_list]
    imports  = [r["importance"] for r in fi_list]

    fig = go.Figure(go.Bar(
        x=imports[::-1], y=features[::-1],
        orientation="h",
        marker=dict(
            color=imports[::-1],
            colorscale=[[0, "#1e3a5f"], [0.5, "#3a7bd5"], [1, "#64ffda"]],
            showscale=False,
        ),
        text=[f"{v:.4f}" for v in imports[::-1]],
        textposition="outside",
        textfont=dict(color="#8892b0", size=10),
    ))
    fig.add_vline(x=0.005, line_dash="dot", line_color="#ffd740", line_width=1,
                  annotation_text="prune <0.005", annotation_font_color="#ffd740",
                  annotation_position="top right")
    fig.update_layout(
        **CHART_THEME,
        title=f"🤖 Top {top_n} Feature Importances",
        xaxis_title="Importance",
        height=max(280, top_n * 26),
    )
    return fig


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    # ── Logo & version ────────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    mode_color = "#ffab40" if PAPER_MODE_ON else "#64ffda"
    mode_text  = "📄 PAPER" if PAPER_MODE_ON else "● LIVE"

    st.markdown(f"""
    <div style='text-align:center; padding: 14px 0 10px 0;'>
        <div style='font-size:2.8rem; filter: drop-shadow(0 0 12px rgba(100,255,218,0.5));'>🛡️</div>
        <div style='color:#64ffda; font-weight:800; font-size:1.25rem;
                    letter-spacing:-0.02em; margin-top:4px;
                    text-shadow: 0 0 20px rgba(100,255,218,0.35);'>AegisQuant</div>
        <div style='color:#4a5980; font-size:0.7rem; font-weight:600;
                    letter-spacing:0.12em; text-transform:uppercase; margin-top:2px;'>
            v3.2.0 · Institutional AI
        </div>
        <div style='margin-top:10px;'>
            <span style='background:rgba(100,255,218,0.08); color:{mode_color};
                         border:1px solid rgba(100,255,218,0.25);
                         padding:3px 14px; border-radius:999px; font-size:0.72rem; font-weight:700;'>
                {mode_text}
            </span>
        </div>
        <div style='color:#3a4a6a; font-size:0.7rem; margin-top:8px; font-family:monospace;'>
            {now_utc.strftime('%H:%M:%S UTC  ·  %d %b %Y')}
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<hr style='border-color:rgba(45,53,72,0.4); margin:8px 0;'>", unsafe_allow_html=True)

    page = st.radio(
        "Navigation",
        options=[
            "🏠 Command Center",
            "📈 Equity & PnL",
            "🎯 Symbol Breakdown",
            "📋 Trade History",
            "⚙️ System Health",
            "🔬 Advanced Analytics",
        ],
        label_visibility="collapsed",
    )

    st.markdown("<hr style='border-color:rgba(45,53,72,0.4); margin:8px 0;'>", unsafe_allow_html=True)

    col_r, col_a = st.columns(2)
    with col_r:
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    with col_a:
        auto_refresh = st.toggle("Auto 30s", value=False)

    st.markdown("<hr style='border-color:rgba(45,53,72,0.4); margin:8px 0;'>", unsafe_allow_html=True)

    # ── Quick stats panel ─────────────────────────────────────────────
    bal = load_current_balance()
    gp  = growth_progress(bal)
    _, tier_label, tier_badge = risk_tier_info(bal)
    bar_w = max(2, min(100, int(gp["pct"])))

    st.markdown(f"""
    <div style='font-size:0.62rem; font-weight:800; letter-spacing:0.12em;
                text-transform:uppercase; color:#3a4a6a; margin-bottom:10px;'>
        Portfolio Snapshot
    </div>

    <div style='background:rgba(10,22,40,0.7); border:1px solid rgba(45,53,72,0.5);
                border-radius:10px; padding:12px 14px; margin-bottom:8px;'>
        <div style='color:#4a5980; font-size:0.65rem; font-weight:700;
                    letter-spacing:0.08em; text-transform:uppercase;'>Balance</div>
        <div style='color:#dce6f8; font-size:1.2rem; font-weight:800;
                    letter-spacing:-0.02em; font-family:monospace; margin-top:2px;'>
            ${bal:,.4f} <span style='color:#4a5980; font-size:0.75rem; font-weight:600;'>USDT</span>
        </div>
        <div style='color:#64ffda; font-size:0.85rem; font-weight:700; margin-top:2px;'>
            Rp {gp['current_idr']:,.0f}
        </div>
    </div>

    <div style='background:rgba(10,22,40,0.7); border:1px solid rgba(45,53,72,0.5);
                border-radius:10px; padding:10px 14px; margin-bottom:8px;'>
        <div style='color:#4a5980; font-size:0.65rem; font-weight:700;
                    letter-spacing:0.08em; text-transform:uppercase; margin-bottom:6px;'>
            Growth Progress
        </div>
        <div style='background:rgba(15,30,55,0.8); border-radius:999px; height:8px;
                    overflow:hidden; border:1px solid rgba(45,53,72,0.5);'>
            <div style='width:{bar_w}%; height:100%; border-radius:999px;
                        background:linear-gradient(90deg,#1a4db5,#64ffda);'></div>
        </div>
        <div style='display:flex; justify-content:space-between; margin-top:5px;'>
            <span style='color:#4a5980; font-size:0.65rem;'>Rp {START_IDR//1000:.0f}K</span>
            <span style='color:#64ffda; font-size:0.65rem; font-weight:700;'>{gp['pct']:.1f}%</span>
            <span style='color:#4a5980; font-size:0.65rem;'>Rp {TARGET_IDR//1000000:.0f}M</span>
        </div>
    </div>

    <div style='display:flex; gap:6px; margin-bottom:8px;'>
        <span class='{tier_badge}' style='font-size:0.65rem;'>{tier_label}</span>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# LOAD GLOBAL DATA
# ─────────────────────────────────────────────
df_all    = load_trades()
bh        = load_balance_history()
balance   = load_current_balance()
positions = load_positions()
ct        = closed_trades(df_all)
stats     = calc_stats(df_all)
gp        = growth_progress(balance)
_, tier_label, tier_badge = risk_tier_info(balance)


# ═════════════════════════════════════════════
# PAGE 1 — COMMAND CENTER
# ═════════════════════════════════════════════
if page == "🏠 Command Center":

    # Paper mode banner
    if PAPER_MODE_ON:
        st.markdown("""
        <div class='paper-banner'>
            📄 PAPER TRADING MODE ACTIVE — All trades are simulated. No real orders are placed.
        </div>
        """, unsafe_allow_html=True)

    # Header
    mode_str   = "PAPER" if PAPER_MODE_ON else CONFIG["PROJECT"]["MODE"]
    _now_hdr   = datetime.now(timezone.utc)
    _idr_bal   = balance * IDR_RATE
    st.markdown(f"""
    <div class='aegis-header'>
        <div style='flex:1;'>
            <h1>🛡️ AegisQuant</h1>
            <p style='font-size:0.78rem; margin-top:4px;'>
                Trade Decision Authority (TDA) &nbsp;·&nbsp; Mode: <strong style='color:#64ffda;'>{mode_str}</strong>
                &nbsp;·&nbsp; {_now_hdr.strftime('%a %d %b %Y · %H:%M UTC')}
            </p>
        </div>
        <div style='text-align:right; min-width:160px;'>
            <div style='font-size:1.6rem; font-weight:800; color:#e2e8f6;
                        font-family:monospace; letter-spacing:-0.02em;'>
                ${balance:,.4f}
            </div>
            <div style='color:#4a5980; font-size:0.72rem; margin-bottom:6px;'>
                Rp {_idr_bal:,.0f}
            </div>
            <span class='{"badge-paper" if PAPER_MODE_ON else "badge-green"}'>
                {"📄 PAPER MODE" if PAPER_MODE_ON else "● ENGINE LIVE"}
            </span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── LIVE MARKET SNAPSHOT ──────────────────────────────────────────────
    live_prices = fetch_live_prices()
    if live_prices:
        ticker_html_parts = []
        for sym, data in sorted(live_prices.items()):
            price     = data["price"]
            chg       = data["change_pct"]
            arrow     = "▲" if chg >= 0 else "▼"
            chg_class = "ticker-up" if chg >= 0 else "ticker-down"
            # Format price: more decimals for low-value assets
            price_fmt = f"${price:,.6f}" if price < 0.01 else (f"${price:,.4f}" if price < 10 else f"${price:,.2f}")
            ticker_html_parts.append(f"""
            <div class='ticker-item'>
                <div class='ticker-symbol'>{sym}</div>
                <div class='ticker-price'>{price_fmt}</div>
                <div class='{chg_class}'>{arrow} {abs(chg):.2f}%</div>
            </div>""")
        st.markdown(
            "<div class='ticker-strip'><div style='font-size:0.7rem; color:#8892b0; margin-bottom:8px; "
            "letter-spacing:0.1em;'>🔴 LIVE MARKET SNAPSHOT</div>"
            + "".join(ticker_html_parts) + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='ticker-strip' style='color:#8892b0; font-size:0.82rem;'>"
            "⚠️ Live prices unavailable (no internet / requests library missing)</div>",
            unsafe_allow_html=True,
        )

    # ── GROWTH TRACKER ────────────────────────────────────────────────────
    pct       = gp["pct"]
    bar_width = max(2, min(100, int(pct)))
    bar_text  = f"{pct:.1f}%" if pct >= 5 else ""

    milestones = [
        (1_000_000, "Rp 1M"),
        (2_000_000, "Rp 2M"),
        (5_000_000, "Rp 5M"),
        (10_000_000, "Rp 10M"),
    ]
    milestone_html = " &nbsp;|&nbsp; ".join(
        f"<span style='color:{'#64ffda' if gp['current_idr'] >= m else '#4a5568'}'>{lbl}</span>"
        for m, lbl in milestones
    )

    col_gauge, col_growth = st.columns([1, 2])
    with col_gauge:
        st.plotly_chart(chart_growth_gauge(pct), width='stretch', config={"displayModeBar": False})
    with col_growth:
        st.markdown(f"""
        <div class='growth-card'>
            <div class='growth-title'>🎯 Growth Mission: Rp {START_IDR:,.0f} → Rp {TARGET_IDR:,.0f}</div>
            <div class='growth-amounts'>
                Current: <strong style='color:#64ffda;'>Rp {gp['current_idr']:,.0f}</strong>
                &nbsp;≈&nbsp; <strong style='color:#64b5f6;'>${balance:,.4f} USDT</strong>
                &nbsp;&nbsp;|&nbsp;&nbsp; {gp['x_from_start']:.2f}× from start
            </div>
            <div class='progress-outer'>
                <div class='progress-inner' style='width:{bar_width}%;'>{bar_text}</div>
            </div>
            <div class='growth-milestones'>
                Milestones: {milestone_html}
                &nbsp;&nbsp;|&nbsp;&nbsp;
                Still needed: <span style='color:#ffd740;'>Rp {gp['needed_idr']:,.0f}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

    # ── BTC BENCHMARK ─────────────────────────────────────────────────────
    bench_data = load_benchmark()
    if bench_data:
        btc_start  = bench_data.get("btc_start_price", 0)
        bal_start  = bench_data.get("start_balance",  0)
        ts_start   = bench_data.get("timestamp", "")
        btc_now    = fetch_btc_current_price()

        if btc_start > 0 and bal_start > 0:
            btc_ret   = ((btc_now / btc_start) - 1) * 100 if btc_now else None
            strat_ret = ((balance / bal_start) - 1) * 100 if bal_start > 0 else 0.0
            alpha     = (strat_ret - btc_ret) if btc_ret is not None else None

            alpha_color = "#64ffda" if (alpha or 0) >= 0 else "#ff5252"
            btc_color   = "#64ffda" if (btc_ret or 0) >= 0 else "#ff5252"
            str_color   = "#64ffda" if strat_ret >= 0 else "#ff5252"

            st.markdown(f"""
            <div class='benchmark-card'>
                <div style='color:#8892b0; font-size:0.7rem; letter-spacing:0.1em; margin-bottom:8px;'>
                    ⚡ BTC BUY-AND-HOLD BENCHMARK (since {ts_start[:10] if ts_start else 'start'})
                </div>
                <div style='display:flex; gap:32px; flex-wrap:wrap;'>
                    <div>
                        <div style='color:#8892b0; font-size:0.72rem;'>Strategy Return</div>
                        <div style='color:{str_color}; font-size:1.3rem; font-weight:700;'>{strat_ret:+.2f}%</div>
                    </div>
                    <div>
                        <div style='color:#8892b0; font-size:0.72rem;'>BTC Hold Return</div>
                        <div style='color:{btc_color}; font-size:1.3rem; font-weight:700;'>
                            {f"{btc_ret:+.2f}%" if btc_ret is not None else "—"}
                        </div>
                    </div>
                    <div>
                        <div style='color:#8892b0; font-size:0.72rem;'>Alpha (Strat − BTC)</div>
                        <div style='color:{alpha_color}; font-size:1.3rem; font-weight:700;'>
                            {f"{alpha:+.2f}%" if alpha is not None else "—"}
                        </div>
                    </div>
                    <div>
                        <div style='color:#8892b0; font-size:0.72rem;'>BTC Price Start → Now</div>
                        <div style='color:#ccd6f6; font-size:0.9rem; font-weight:600;'>
                            ${btc_start:,.0f} → {f"${btc_now:,.0f}" if btc_now else "—"}
                        </div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("---")

    # ── ROW 1: TOP METRICS ────────────────────────────────────────────────
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1: st.metric("Balance (USDT)", f"${balance:,.4f}")
    with c2: st.metric("Balance (IDR)",  f"Rp {gp['current_idr']:,.0f}")
    with c3:
        wr_pct = stats["win_rate"] * 100
        st.metric("Win Rate", f"{wr_pct:.1f}%",
                  delta="▲ above 50%" if wr_pct > 50 else "▼ below 50%")
    with c4:
        pf     = stats["profit_factor"]
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        st.metric("Profit Factor", pf_str,
                  delta="Good" if pf > 1.5 else ("OK" if pf >= 1.0 else "Negative"))
    with c5:
        st.metric("Sharpe Ratio", f"{stats['sharpe']:.2f}")
    with c6:
        st.metric("Expectancy", f"${stats['expectancy']:+.4f}")

    st.markdown("---")

    # ── ROW 2: SECONDARY METRICS ──────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Total Entries", len(df_all) if not df_all.empty else 0)
    with c2:
        st.metric("Closed Trades", stats["total_closed"])
    with c3:
        st.metric("Best Trade",  f"${stats['best_trade']:+.4f}")
    with c4:
        st.metric("Worst Trade", f"${stats['worst_trade']:+.4f}")
    with c5:
        st.metric("Max Drawdown", f"${stats['max_dd']:+.4f}")

    st.markdown("---")

    # ── ROW 3: POSITIONS + RECENT ACTIVITY ────────────────────────────────
    col_pos, col_recent = st.columns([1, 2])

    with col_pos:
        st.markdown("<div class='section-header'>Active Positions</div>", unsafe_allow_html=True)

        # Build open-position view from trade log (OPEN entries without a matching CLOSE)
        _open_rows: list = []
        if not df_all.empty and "Result" in df_all.columns:
            _opened = df_all[df_all["Result"].str.upper() == "OPEN"].copy()
            _closed_syms: set = set(
                df_all.loc[df_all["Result"].str.upper().isin(["CLOSE", "CLOSED"]), "Symbol"].tolist()
            ) if "Symbol" in df_all.columns else set()
            # Keep only symbols that were opened more recently than their last close
            for _sym, _grp in _opened.groupby("Symbol") if "Symbol" in _opened.columns else []:
                _last_open = _grp["Timestamp"].max() if "Timestamp" in _grp.columns else None
                _last_close_rows = df_all[
                    (df_all["Symbol"] == _sym) &
                    (df_all["Result"].str.upper().isin(["CLOSE", "CLOSED"]))
                ]
                _last_close = _last_close_rows["Timestamp"].max() if not _last_close_rows.empty and "Timestamp" in _last_close_rows.columns else None
                if _last_open is None or (_last_close is not None and _last_close >= _last_open):
                    continue  # position closed
                _entry_row = _grp.sort_values("Timestamp").iloc[-1]
                _open_rows.append({
                    "Symbol":      _sym,
                    "Side":        _entry_row.get("Side", "BUY"),
                    "Entry Price": float(_entry_row.get("Price", 0) or 0),
                    "Confidence":  f"{float(_entry_row.get('Confidence', 0) or 0):.0%}",
                    "Edge Score":  f"{float(_entry_row.get('EdgeScore', 0) or 0):.3f}",
                })

        if _open_rows or positions:
            # Augment with live unrealized PnL
            live_px = fetch_live_prices()
            _display_rows = []
            for _row in _open_rows:
                _sym   = _row["Symbol"]
                _entry = _row["Entry Price"]
                _live  = live_px.get(_sym, {}).get("price", 0.0) if live_px else 0.0
                if _entry > 0 and _live > 0:
                    _upnl_pct = (_live - _entry) / _entry * 100
                    _upnl_icon = "🟢" if _upnl_pct >= 0 else "🔴"
                    _row["Live Price"]    = f"${_live:,.5g}"
                    _row["Unreal. PnL"]  = f"{_upnl_icon} {_upnl_pct:+.2f}%"
                else:
                    _row["Live Price"]   = "—"
                    _row["Unreal. PnL"] = "—"
                _display_rows.append(_row)

            if _display_rows:
                pos_df = pd.DataFrame(_display_rows)
                st.dataframe(pos_df, width='stretch', height=min(250, 60 + len(_display_rows) * 40))
            elif positions:
                # Fallback: StateManager positions (no entry price available)
                pos_df        = pd.DataFrame(positions)
                display_cols  = [c for c in pos_df.columns if c not in ("id", "info", "timestamp")]
                st.dataframe(pos_df[display_cols], width='stretch', height=250)
        else:
            st.markdown("""
            <div style='background:#0d1b2a; border:1px solid #2d3548; border-radius:8px;
                        padding:24px; text-align:center; color:#8892b0;'>
                <div style='font-size:2rem; margin-bottom:8px;'>🔍</div>
                No open positions
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("<div class='section-header'>Engine Status</div>", unsafe_allow_html=True)

        ens_enabled = CONFIG.get("ENSEMBLE", {}).get("ENABLED", False)
        fr_enabled  = CONFIG.get("SIGNAL_GATES", {}).get("FUNDING_RATE_ENABLED", False)
        mtf_enabled = CONFIG.get("SIGNAL_GATES", {}).get("MTF_CONSENSUS_REQUIRED", False)
        sess_enabled = CONFIG.get("TRADING_SESSIONS", {}).get("ENABLED", False)

        st.markdown(f"""
        <div style='display:flex; flex-direction:column; gap:8px;'>
            <div><span class='badge-green'>● Binance Spot</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>Connected</span></div>
            <div><span class='badge-{"green" if CONFIG["AI"]["ENABLED"] else "red"}'>● AI Engine</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>
                 {"Enabled" if CONFIG["AI"]["ENABLED"] else "Disabled"}</span></div>
            <div><span class='badge-{"teal" if ens_enabled else "yellow"}'>● Ensemble ML</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>
                 {"RF+XGB+LGB" if ens_enabled else "RF only"}</span></div>
            <div><span class='badge-{"teal" if fr_enabled else "blue"}'>● Funding Gate</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>
                 {"On" if fr_enabled else "Off"}</span></div>
            <div><span class='badge-{"teal" if mtf_enabled else "blue"}'>● MTF Consensus</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>
                 {"Required" if mtf_enabled else "Off"}</span></div>
            <div><span class='badge-{"teal" if sess_enabled else "blue"}'>● Session Filter</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>
                 {"Active" if sess_enabled else "Off"}</span></div>
            <div><span class='badge-blue'>● Risk Tier</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>{tier_label}</span></div>
            <div><span class='badge-yellow'>● Symbols Active</span>
                 &nbsp; <span style='color:#8892b0; font-size:0.8rem;'>
                 {len(CONFIG["SYMBOLS"].get("CRYPTO", {}))} crypto pairs</span></div>
        </div>
        """, unsafe_allow_html=True)

    with col_recent:
        st.markdown("<div class='section-header'>Recent Trade Activity</div>", unsafe_allow_html=True)
        if not df_all.empty:
            recent = df_all.sort_values("Timestamp", ascending=False).head(12).copy()
            cols_show = [c for c in (
                "Timestamp", "Symbol", "Side", "Price", "PnL",
                "Confidence", "Edge_Score", "Result"
            ) if c in recent.columns]
            if "Timestamp" in recent.columns:
                recent["Timestamp"] = recent["Timestamp"].dt.strftime("%m-%d %H:%M")
            def _color_pnl(val):
                if isinstance(val, (int, float)):
                    c = "#64ffda" if val > 0 else ("#ff5252" if val < 0 else "#8892b0")
                    return f"color: {c}; font-weight: 600"
                return ""
            styled = recent[cols_show].style.map(
                _color_pnl, subset=["PnL"] if "PnL" in cols_show else []
            )
            st.dataframe(styled, width='stretch', height=340)
        else:
            st.info("No trades yet. Start the engine to see activity.")

    # ── BALANCE HISTORY ───────────────────────────────────────────────────
    if not bh.empty:
        st.markdown("---")
        st.markdown("<div class='section-header'>Balance History</div>", unsafe_allow_html=True)
        fig_bh = chart_balance_history(bh)
        if fig_bh:
            st.plotly_chart(fig_bh, width='stretch', config={"displayModeBar": False})


# ═════════════════════════════════════════════
# PAGE 2 — EQUITY & PnL
# ═════════════════════════════════════════════
elif page == "📈 Equity & PnL":
    st.markdown("<h2 style='color:#64ffda;'>📈 Equity & PnL Analysis</h2>", unsafe_allow_html=True)

    if ct.empty:
        st.info("No closed trades yet. Start trading to see analytics.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("Total PnL", f"${stats['total_pnl']:+.4f}")
        with c2: st.metric("Win Rate",   f"{stats['win_rate']*100:.1f}%")
        with c3: st.metric("Avg Win",    f"${stats['avg_win']:+.4f}")
        with c4: st.metric("Avg Loss",   f"${stats['avg_loss']:+.4f}")

        st.markdown("---")

        fig_eq = chart_equity_curve(ct)
        if fig_eq:
            st.plotly_chart(fig_eq, width='stretch', config={"displayModeBar": False})

        col_rwr, col_daily = st.columns(2)
        with col_rwr:
            window = st.slider("Rolling win-rate window", 5, min(50, len(ct)), min(20, len(ct)), key="wr_window")
            fig_rwr = chart_rolling_winrate(ct, window)
            if fig_rwr:
                st.plotly_chart(fig_rwr, width='stretch', config={"displayModeBar": False})
        with col_daily:
            fig_dpnl = chart_daily_pnl(ct)
            if fig_dpnl:
                st.plotly_chart(fig_dpnl, width='stretch', config={"displayModeBar": False})

        fig_dist = chart_pnl_distribution(ct)
        if fig_dist:
            st.plotly_chart(fig_dist, width='stretch', config={"displayModeBar": False})


# ═════════════════════════════════════════════
# PAGE 3 — SYMBOL BREAKDOWN
# ═════════════════════════════════════════════
elif page == "🎯 Symbol Breakdown":
    st.markdown("<h2 style='color:#64ffda;'>🎯 Symbol Performance Breakdown</h2>", unsafe_allow_html=True)

    all_symbols = sorted(df_all["Symbol"].unique().tolist()) if not df_all.empty else []
    selected    = st.multiselect("Filter symbols", all_symbols, default=all_symbols)
    ct_f = ct[ct["Symbol"].isin(selected)] if selected and not ct.empty else ct

    if ct_f.empty:
        st.info("No closed trades to analyze for selected symbols.")
    else:
        grp = ct_f.groupby("Symbol").agg(
            Closed_Trades=("PnL", "count"),
            Total_PnL=("PnL", "sum"),
            Win_Rate=("PnL", lambda x: f"{(x>0).mean()*100:.1f}%"),
            Avg_Win=("PnL", lambda x: x[x>0].mean() if (x>0).any() else 0.0),
            Avg_Loss=("PnL", lambda x: x[x<0].mean() if (x<0).any() else 0.0),
            Best=("PnL", "max"),
            Worst=("PnL", "min"),
            Avg_Confidence=("Confidence", "mean"),
        ).reset_index()

        st.dataframe(
            grp.style.background_gradient(subset=["Total_PnL"], cmap="RdYlGn"),
            width='stretch',
        )
        st.markdown("---")

        col_l, col_r = st.columns(2)
        with col_l:
            fig_spnl = chart_symbol_pnl(ct_f)
            if fig_spnl:
                st.plotly_chart(fig_spnl, width='stretch', config={"displayModeBar": False})
        with col_r:
            fig_swr = chart_symbol_winrate(ct_f)
            if fig_swr:
                st.plotly_chart(fig_swr, width='stretch', config={"displayModeBar": False})

        fig_conf = chart_confidence_vs_pnl(ct_f)
        if fig_conf:
            st.plotly_chart(fig_conf, width='stretch', config={"displayModeBar": False})

        df_f = df_all[df_all["Symbol"].isin(selected)] if selected and not df_all.empty else df_all
        fig_es = chart_edge_score_distribution(df_f)
        if fig_es:
            st.plotly_chart(fig_es, width='stretch', config={"displayModeBar": False})


# ═════════════════════════════════════════════
# PAGE 4 — TRADE HISTORY
# ═════════════════════════════════════════════
elif page == "📋 Trade History":
    st.markdown("<h2 style='color:#64ffda;'>📋 Full Trade History</h2>", unsafe_allow_html=True)

    if df_all.empty:
        st.info("No trade history found.")
    else:
        # ── Filters ───────────────────────────────────────────────────────
        fc1, fc2, fc3, fc4 = st.columns(4)
        with fc1:
            sym_f = st.multiselect("Symbol", sorted(df_all["Symbol"].unique()))
        with fc2:
            res_f = st.multiselect("Result", sorted(df_all["Result"].unique()))
        with fc3:
            if "Timestamp" in df_all.columns and df_all["Timestamp"].notna().any():
                min_d = df_all["Timestamp"].min().date()
                max_d = df_all["Timestamp"].max().date()
                date_range = st.date_input("Date range", value=(min_d, max_d),
                                           min_value=min_d, max_value=max_d)
            else:
                date_range = None
        with fc4:
            sort_col = st.selectbox("Sort by", ["Timestamp", "PnL", "Edge_Score", "Confidence"])
            sort_asc = st.checkbox("Ascending", value=False)

        view = df_all.copy()
        if sym_f:
            view = view[view["Symbol"].isin(sym_f)]
        if res_f:
            view = view[view["Result"].isin(res_f)]
        if date_range and len(date_range) == 2 and "Timestamp" in view.columns:
            start_dt = pd.Timestamp(date_range[0], tz="UTC")
            end_dt   = pd.Timestamp(date_range[1], tz="UTC") + pd.Timedelta(days=1)
            view = view[(view["Timestamp"] >= start_dt) & (view["Timestamp"] <= end_dt)]
        if sort_col in view.columns:
            view = view.sort_values(sort_col, ascending=sort_asc)

        st.markdown(f"**{len(view)} records** (filtered from {len(df_all)} total)")

        # ── Styled table ──────────────────────────────────────────────────
        def _row_style(row):
            result = str(row.get("Result", "")).upper()
            pnl    = float(row.get("PnL", 0) or 0)
            if result in ("CLOSE", "CLOSED"):
                bg = "rgba(100,255,218,0.05)" if pnl >= 0 else "rgba(255,82,82,0.05)"
            elif result == "OPEN":
                bg = "rgba(255,215,64,0.05)"
            else:
                bg = "transparent"
            return [f"background-color: {bg}"] * len(row)

        styled = view.style.apply(_row_style, axis=1)
        if "PnL" in view.columns:
            styled = styled.map(
                lambda v: "color:#64ffda; font-weight:600" if isinstance(v, (int, float)) and v > 0
                     else ("color:#ff5252; font-weight:600" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["PnL"],
            )

        st.dataframe(styled, width='stretch', height=400)

        # Download
        csv = view.to_csv(index=False)
        st.download_button(
            label="⬇️ Export Filtered CSV",
            data=csv,
            file_name=f"aegisquant_trades_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            width='stretch',
        )

        st.markdown("---")

        # ── TRADE REPLAY & AI EXPLAIN ─────────────────────────────────────
        st.markdown("<div class='section-header'>🔁 Trade Replay & 🤖 AI Explain</div>",
                    unsafe_allow_html=True)

        closed_view = view[view["Result"].str.upper().isin(["CLOSE", "CLOSED", "SELL"])] \
            if "Result" in view.columns else pd.DataFrame()

        if closed_view.empty:
            st.info("No closed trades in current filter to replay or explain.")
        else:
            # Build a label for each trade
            if "Timestamp" in closed_view.columns:
                labels = [
                    f"{row['Timestamp'].strftime('%m-%d %H:%M')} | {row.get('Symbol','?')} | "
                    f"{row.get('Side','?')} | PnL: ${row.get('PnL', 0):+.4f}"
                    for _, row in closed_view.iterrows()
                ]
            else:
                labels = [
                    f"#{i} | {row.get('Symbol','?')} | {row.get('Side','?')} | PnL: ${row.get('PnL', 0):+.4f}"
                    for i, (_, row) in enumerate(closed_view.iterrows())
                ]

            selected_label = st.selectbox(
                "Select a trade to inspect:", options=labels, key="trade_selector"
            )
            selected_idx   = labels.index(selected_label)
            selected_trade = closed_view.iloc[selected_idx]

            col_replay, col_explain = st.columns(2)

            # ── Trade Replay ──────────────────────────────────────────────
            with col_replay:
                with st.expander("🔁 Trade Replay Viewer", expanded=True):
                    sym_for_replay = str(selected_trade.get("Symbol", ""))
                    tf_options     = ["1m", "5m", "15m", "1h"]
                    replay_tf      = st.selectbox("Timeframe", tf_options, index=1, key="replay_tf")

                    if sym_for_replay and _REQUESTS_AVAILABLE:
                        candle_df = fetch_candles_for_replay(sym_for_replay, interval=replay_tf, limit=120)
                        if not candle_df.empty:
                            fig_replay = chart_trade_replay(candle_df, selected_trade)
                            st.plotly_chart(fig_replay, width='stretch',
                                            config={"displayModeBar": False})
                        else:
                            st.warning(f"Could not fetch candles for {sym_for_replay}.")
                    elif not _REQUESTS_AVAILABLE:
                        st.warning("Install `requests` to enable Trade Replay.")
                    else:
                        st.warning("Symbol not found in trade record.")

            # ── AI Explain ────────────────────────────────────────────────
            with col_explain:
                with st.expander("🤖 AI Explain This Trade", expanded=True):
                    sym_for_expl = str(selected_trade.get("Symbol", ""))
                    meta         = load_trade_explanation(sym_for_expl)

                    if meta is None:
                        st.warning(f"No model metadata found for {sym_for_expl}. Train models first.")
                    else:
                        # Model stats
                        acc   = meta.get("global_test_accuracy", 0)
                        n_tr  = meta.get("n_train", 0)
                        wf    = meta.get("walk_forward", {})
                        wf_m  = wf.get("mean_accuracy", None)
                        wf_ok = wf.get("consistent", None)

                        st.markdown(f"""
                        <div class='ai-explain-card'>
                            <div style='color:#64ffda; font-weight:700; margin-bottom:8px;'>
                                Model: {sym_for_expl}
                            </div>
                            <div style='display:grid; grid-template-columns:1fr 1fr; gap:6px;'>
                                <div>
                                    <div style='color:#8892b0; font-size:0.72rem;'>Test Accuracy</div>
                                    <div style='color:{"#64ffda" if acc >= 0.55 else "#ffd740"}; font-weight:600;'>
                                        {acc:.1%}
                                    </div>
                                </div>
                                <div>
                                    <div style='color:#8892b0; font-size:0.72rem;'>Train Samples</div>
                                    <div style='color:#ccd6f6; font-weight:600;'>{n_tr:,}</div>
                                </div>
                                <div>
                                    <div style='color:#8892b0; font-size:0.72rem;'>Walk-Forward Acc</div>
                                    <div style='color:#ccd6f6; font-weight:600;'>
                                        {f"{wf_m:.1%}" if wf_m is not None else "—"}
                                    </div>
                                </div>
                                <div>
                                    <div style='color:#8892b0; font-size:0.72rem;'>WF Consistent</div>
                                    <div style='color:#ccd6f6; font-weight:600;'>
                                        {"✅ Yes" if wf_ok else ("⚠️ No" if wf_ok is not None else "—")}
                                    </div>
                                </div>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

                        # Feature importance chart
                        fi_list = meta.get("feature_importances", [])
                        low_imp = meta.get("low_importance_features", [])

                        if fi_list:
                            fig_fi = chart_feature_importances(fi_list, top_n=15)
                            st.plotly_chart(fig_fi, width='stretch',
                                            config={"displayModeBar": False})
                            if low_imp:
                                st.caption(
                                    f"⚠️ {len(low_imp)} low-importance features (< 0.005): "
                                    + ", ".join(low_imp[:8])
                                    + ("..." if len(low_imp) > 8 else "")
                                )
                        else:
                            # Legacy metadata without feature_importances
                            fl = meta.get("feature_list", [])
                            if fl:
                                st.markdown(f"**{len(fl)} features used by this model:**")
                                st.markdown(
                                    " · ".join(f"`{f}`" for f in fl[:20])
                                    + ("..." if len(fl) > 20 else "")
                                )
                            else:
                                st.info("Feature importance data not available in metadata.")

                        # Signal explanation for this specific trade
                        conf  = selected_trade.get("Confidence", None)
                        edge  = selected_trade.get("Edge_Score", None)
                        side  = selected_trade.get("Side", "")
                        pnl   = selected_trade.get("PnL", 0)

                        if pd.notna(conf) and conf > 0:
                            conf_pct = float(conf) * 100
                            conf_bar = min(100, int(conf_pct))
                            outcome  = "✅ WIN" if float(pnl) > 0 else ("❌ LOSS" if float(pnl) < 0 else "— BE")
                            st.markdown(f"""
                            <div style='margin-top:12px;'>
                                <div style='color:#8892b0; font-size:0.72rem; margin-bottom:4px;'>
                                    AI SIGNAL SUMMARY
                                </div>
                                <div style='display:flex; gap:16px; margin-bottom:8px;'>
                                    <div>
                                        <span style='color:#8892b0; font-size:0.72rem;'>Direction</span><br>
                                        <span style='color:{"#64ffda" if side=="BUY" else "#ff5252"}; font-weight:700;'>
                                            {side}
                                        </span>
                                    </div>
                                    <div>
                                        <span style='color:#8892b0; font-size:0.72rem;'>Confidence</span><br>
                                        <span style='color:#ccd6f6; font-weight:700;'>{conf_pct:.1f}%</span>
                                    </div>
                                    <div>
                                        <span style='color:#8892b0; font-size:0.72rem;'>Edge Score</span><br>
                                        <span style='color:#ccd6f6; font-weight:700;'>
                                            {f"{float(edge):.3f}" if pd.notna(edge) else "—"}
                                        </span>
                                    </div>
                                    <div>
                                        <span style='color:#8892b0; font-size:0.72rem;'>Outcome</span><br>
                                        <span style='font-weight:700;'>{outcome}</span>
                                    </div>
                                </div>
                                <div class='feature-bar-outer'>
                                    <div class='feature-bar-inner' style='width:{conf_bar}%;'></div>
                                </div>
                                <div style='color:#8892b0; font-size:0.7rem; margin-top:4px;'>
                                    Confidence: {conf_pct:.1f}% — threshold was 65%
                                </div>
                            </div>
                            """, unsafe_allow_html=True)


# ═════════════════════════════════════════════
# PAGE 5 — SYSTEM HEALTH
# ═════════════════════════════════════════════
elif page == "⚙️ System Health":
    st.markdown("<h2 style='color:#64ffda;'>⚙️ System Health & Configuration</h2>",
                unsafe_allow_html=True)

    # Model health table
    st.markdown("<div class='section-header'>AI Model Health</div>", unsafe_allow_html=True)
    model_rows = load_model_health()
    if model_rows:
        mdf = pd.DataFrame(model_rows)
        def _acc_color(val):
            if isinstance(val, float):
                if val >= 0.58: return "color: #64ffda; font-weight: 600"
                if val >= 0.50: return "color: #ffd740"
                return "color: #ff5252; font-weight: 600"
            return ""
        def _n_color(val):
            if isinstance(val, int):
                if val >= 200: return "color: #64ffda"
                if val >= 50:  return "color: #ffd740"
                return "color: #ff5252"
            return ""

        acc_cols = ["Accuracy"]
        if "WF Accuracy" in mdf.columns:
            # Only apply to numeric entries
            numeric_mask = mdf["WF Accuracy"].apply(lambda x: isinstance(x, float))
            if numeric_mask.any():
                acc_cols.append("WF Accuracy")

        styled_m = mdf.style.map(_acc_color, subset=["Accuracy"]) \
                             .map(_n_color,   subset=["Train Samples"]) \
                             .background_gradient(subset=["Accuracy"], cmap="RdYlGn", vmin=0.40, vmax=0.75)
        st.dataframe(styled_m, width='stretch')

        needs_retrain = [r["Symbol"] for r in model_rows
                         if r["Train Samples"] < 100 or r["Accuracy"] < 0.50]
        if needs_retrain:
            st.warning(f"**Retrain needed for:** {', '.join(needs_retrain)}  "
                       "— run `python Data/Trainer/AegisQuantTrainer.py`")
        else:
            st.success("All models are trained on sufficient data.")
    else:
        st.warning("No model metadata found. Train models first.")

    st.markdown("---")

    # Active signals configuration
    st.markdown("<div class='section-header'>Signal Gate Configuration</div>", unsafe_allow_html=True)
    sg = CONFIG.get("SIGNAL_GATES", {})
    sess = CONFIG.get("TRADING_SESSIONS", {})
    ens  = CONFIG.get("ENSEMBLE", {})

    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        st.markdown("**Signal Gates**")
        gates = {
            "Funding Rate":  sg.get("FUNDING_RATE_ENABLED", False),
            "Fear & Greed":  sg.get("FEAR_GREED_ENABLED", False),
            "Order Book":    sg.get("ORDER_BOOK_IMBALANCE_ENABLED", False),
            "Volume Profile":sg.get("VOLUME_PROFILE_ENABLED", False),
            "MTF Consensus": sg.get("MTF_CONSENSUS_REQUIRED", False),
        }
        for name, enabled in gates.items():
            badge = "badge-teal" if enabled else "badge-red"
            label = "ON" if enabled else "OFF"
            st.markdown(f"<span class='{badge}'>{label}</span> {name}", unsafe_allow_html=True)

    with sc2:
        st.markdown("**Ensemble Configuration**")
        st.markdown(f"Enabled: **{'Yes' if ens.get('ENABLED') else 'No'}**")
        st.markdown(f"RF Weight: `{ens.get('RF_WEIGHT', 0.40):.0%}`")
        st.markdown(f"XGB Weight: `{ens.get('XGB_WEIGHT', 0.35):.0%}`")
        st.markdown(f"LGB Weight: `{ens.get('LGB_WEIGHT', 0.25):.0%}`")

    with sc3:
        st.markdown("**Session Filter**")
        dead_hours = sess.get("DEAD_HOURS_UTC", [])
        dead_thresh = sess.get("DEAD_HOURS_CONFIDENCE_THRESHOLD", 0.78)
        st.markdown(f"Enabled: **{'Yes' if sess.get('ENABLED') else 'No'}**")
        st.markdown(f"Dead hours UTC: `{dead_hours}`")
        st.markdown(f"Dead-hour conf. threshold: `{dead_thresh:.0%}`")

    st.markdown("---")

    # Growth target config
    st.markdown("<div class='section-header'>Growth Target</div>", unsafe_allow_html=True)
    gc1, gc2, gc3, gc4 = st.columns(4)
    with gc1: st.metric("Starting Capital", f"Rp {START_IDR:,.0f}")
    with gc2: st.metric("Target Capital",   f"Rp {TARGET_IDR:,.0f}")
    with gc3: st.metric("USD/IDR Rate",     f"{IDR_RATE:,.0f}")
    with gc4: st.metric("Target (USDT)",    f"${TARGET_IDR / IDR_RATE:,.2f}")

    st.markdown("---")

    # Risk configuration
    st.markdown("<div class='section-header'>Risk Configuration</div>", unsafe_allow_html=True)
    risk = CONFIG["RISK"]
    rc1, rc2, rc3, rc4 = st.columns(4)
    with rc1:
        st.metric("Max Risk/Trade",  f"{risk['MAX_RISK_PER_TRADE']*100:.1f}%")
        st.metric("Max Daily DD",    f"{risk['MAX_DAILY_DRAWDOWN']*100:.1f}%")
    with rc2:
        st.metric("Max Concurrent",  risk["MAX_CONCURRENT_TRADES"])
        st.metric("Max Weekly DD",   f"{risk['MAX_WEEKLY_DRAWDOWN']*100:.1f}%")
    with rc3:
        st.metric("Max Consec Loss", risk["MAX_CONSECUTIVE_LOSSES"])
        st.metric("Reserve (USDT)",  risk["MIN_RESERVE_USDT"])
    with rc4:
        tiers_df = pd.DataFrame([
            {"Tier": k, "Max Balance": f"${v['MAX_BALANCE']:,.0f}", "Risk %": f"{v['RISK_PCT']*100:.1f}%"}
            for k, v in risk["TIERS"].items()
        ])
        st.dataframe(tiers_df, width='stretch', hide_index=True)

    st.markdown("---")

    # Active symbols
    st.markdown("<div class='section-header'>Active Symbols & Timeframes</div>", unsafe_allow_html=True)
    sym_rows = []
    for sector, syms in CONFIG["SYMBOLS"].items():
        if CONFIG["MARKETS"][sector.upper()]:
            for sym, tfs in syms.items():
                sym_rows.append({"Sector": sector, "Symbol": sym, "Timeframes": ", ".join(tfs)})
    if sym_rows:
        st.dataframe(pd.DataFrame(sym_rows), width='stretch', hide_index=True)

    st.markdown("---")

    # Recent events
    st.markdown("<div class='section-header'>Recent System Events</div>", unsafe_allow_html=True)
    events_file = os.path.join(LOG_DIR, "state", "events.json")
    if os.path.exists(events_file):
        try:
            with open(events_file) as f:
                ev_data = json.load(f)
            events = ev_data.get("events", [])[-15:]
            if events:
                ev_df = pd.DataFrame(events)
                if all(k in events[0] for k in ("timestamp", "type", "severity", "message")):
                    ev_df = ev_df[["timestamp", "type", "severity", "message"]]
                def _sev_color(val):
                    return {"CRITICAL": "color:#ff5252", "WARNING": "color:#ffd740",
                            "INFO": "color:#64ffda"}.get(str(val).upper(), "")
                styled_ev = ev_df.style.map(
                    _sev_color, subset=["severity"] if "severity" in ev_df.columns else []
                )
                st.dataframe(styled_ev, width='stretch', height=280)
            else:
                st.info("No events logged yet.")
        except Exception as e:
            st.warning(f"Could not load events: {e}")
    else:
        st.info("Events file not found.")


# ═════════════════════════════════════════════
# PAGE 6 — ADVANCED ANALYTICS
# ═════════════════════════════════════════════
elif page == "🔬 Advanced Analytics":
    st.markdown("<h2 style='color:#64ffda;'>🔬 Advanced Analytics</h2>", unsafe_allow_html=True)

    if ct.empty:
        st.info("No closed trades yet. Closed trade data is required for advanced analytics.")
    else:
        # ── SECTION 1: MONTE CARLO ────────────────────────────────────────
        st.markdown("<div class='section-header'>🎲 Monte Carlo Equity Path Simulation</div>",
                    unsafe_allow_html=True)

        mc_col_chart, mc_col_info = st.columns([3, 1])
        with mc_col_chart:
            n_sims = st.slider("Number of simulations", 200, 2000, 1000, step=100, key="mc_sims")
            fig_mc = chart_monte_carlo(ct, n_sims=n_sims)
            if fig_mc:
                st.plotly_chart(fig_mc, width='stretch', config={"displayModeBar": False})

        with mc_col_info:
            pnl_arr = ct["PnL"].dropna().values
            if len(pnl_arr) >= 5:
                rng  = np.random.default_rng(42)
                sims = np.cumsum(
                    rng.choice(pnl_arr, size=(n_sims, len(pnl_arr)), replace=True), axis=1
                )
                final_pnls = sims[:, -1]
                prob_profit = (final_pnls > 0).mean() * 100
                p10_final   = np.percentile(final_pnls, 10)
                p90_final   = np.percentile(final_pnls, 90)
                median_final = np.median(final_pnls)
                actual_final = float(pnl_arr.sum())

                st.markdown(f"""
                <div class='mc-info'>
                    <div style='color:#64ffda; font-weight:700; margin-bottom:10px;'>
                        Simulation Results
                    </div>
                    <div style='margin-bottom:6px;'>
                        <div style='color:#8892b0; font-size:0.7rem;'>Prob. of Profit</div>
                        <div style='color:{"#64ffda" if prob_profit > 50 else "#ff5252"}; font-size:1.2rem; font-weight:700;'>
                            {prob_profit:.1f}%
                        </div>
                    </div>
                    <div style='margin-bottom:6px;'>
                        <div style='color:#8892b0; font-size:0.7rem;'>Median Final PnL</div>
                        <div style='color:#ccd6f6; font-size:1rem; font-weight:600;'>
                            ${median_final:+.4f}
                        </div>
                    </div>
                    <div style='margin-bottom:6px;'>
                        <div style='color:#8892b0; font-size:0.7rem;'>10th–90th Percentile</div>
                        <div style='color:#ccd6f6; font-size:0.85rem;'>
                            ${p10_final:+.4f} → ${p90_final:+.4f}
                        </div>
                    </div>
                    <div>
                        <div style='color:#8892b0; font-size:0.7rem;'>Actual Final PnL</div>
                        <div style='color:{"#64ffda" if actual_final >= 0 else "#ff5252"}; font-size:1rem; font-weight:700;'>
                            ${actual_final:+.4f}
                        </div>
                    </div>
                    <hr style='border-color:#2d3548;'>
                    <div style='color:#8892b0; font-size:0.7rem;'>
                        Based on {n_sims:,} bootstrap resamples<br>
                        of {len(pnl_arr)} closed trades.
                    </div>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("---")

        # ── SECTION 2: PnL CALENDAR HEATMAP ──────────────────────────────
        st.markdown("<div class='section-header'>📅 PnL Calendar Heatmap</div>",
                    unsafe_allow_html=True)

        fig_cal = chart_pnl_calendar(ct)
        if fig_cal:
            st.plotly_chart(fig_cal, width='stretch', config={"displayModeBar": False})

            # Daily summary stats below
            ct_cal = ct.copy()
            ct_cal["Date"] = ct_cal["Timestamp"].dt.date if "Timestamp" in ct_cal.columns else None
            if ct_cal["Date"].notna().any():
                daily_grp = ct_cal.groupby("Date")["PnL"].sum()
                best_day  = daily_grp.idxmax()
                worst_day = daily_grp.idxmin()
                green_days = (daily_grp > 0).sum()
                red_days   = (daily_grp < 0).sum()

                dc1, dc2, dc3, dc4 = st.columns(4)
                with dc1: st.metric("Green Days",  green_days)
                with dc2: st.metric("Red Days",    red_days)
                with dc3: st.metric("Best Day",    f"${daily_grp[best_day]:+.4f} ({best_day})")
                with dc4: st.metric("Worst Day",   f"${daily_grp[worst_day]:+.4f} ({worst_day})")
        else:
            st.info("Not enough date data to render calendar heatmap.")

        st.markdown("---")

        # ── SECTION 3: ADVANCED STATISTICS ───────────────────────────────
        st.markdown("<div class='section-header'>📊 Advanced Performance Statistics</div>",
                    unsafe_allow_html=True)

        stat_col1, stat_col2, stat_col3 = st.columns(3)

        with stat_col1:
            st.markdown("**Profitability**")
            st.metric("Total PnL",      f"${stats['total_pnl']:+.4f}")
            st.metric("Win Rate",       f"{stats['win_rate']:.1%}")
            st.metric("Profit Factor",  f"{stats['profit_factor']:.3f}" if stats['profit_factor'] != float('inf') else "∞")
            st.metric("Expectancy",     f"${stats['expectancy']:+.4f}")

        with stat_col2:
            st.markdown("**Risk Metrics**")
            st.metric("Sharpe Ratio",   f"{stats['sharpe']:.3f}")
            st.metric("Sortino Ratio",  f"{stats['sortino']:.3f}")
            st.metric("Calmar Ratio",   f"{stats['calmar']:.3f}")
            st.metric("Max Drawdown",   f"${stats['max_dd']:+.4f}")

        with stat_col3:
            st.markdown("**Trade Quality**")
            st.metric("Avg Win",        f"${stats['avg_win']:+.4f}")
            st.metric("Avg Loss",       f"${stats['avg_loss']:+.4f}")
            st.metric("Avg R:R Ratio",  f"{stats['avg_rr']:.2f}:1")
            st.metric("Best / Worst",   f"${stats['best_trade']:+.4f} / ${stats['worst_trade']:+.4f}")

        # Streak analysis
        if "PnL" in ct.columns and len(ct) >= 5:
            st.markdown("---")
            st.markdown("**Streak Analysis**")
            pnl_signs = (ct.sort_values("Timestamp")["PnL"] > 0).tolist() \
                if "Timestamp" in ct.columns else (ct["PnL"] > 0).tolist()

            # Compute winning / losing streaks
            max_win_streak = cur_win = max_loss_streak = cur_loss = 0
            for w in pnl_signs:
                if w:
                    cur_win += 1; cur_loss = 0
                else:
                    cur_loss += 1; cur_win = 0
                max_win_streak  = max(max_win_streak,  cur_win)
                max_loss_streak = max(max_loss_streak, cur_loss)

            sc1, sc2, sc3 = st.columns(3)
            with sc1: st.metric("Max Win Streak",  f"{max_win_streak} trades")
            with sc2: st.metric("Max Loss Streak", f"{max_loss_streak} trades")
            with sc3:
                current_streak_color = "W" if pnl_signs[-1] else "L"
                current_count = 0
                for w in reversed(pnl_signs):
                    if (w and current_streak_color == "W") or (not w and current_streak_color == "L"):
                        current_count += 1
                    else:
                        break
                st.metric("Current Streak", f"{current_count} {current_streak_color}")

        st.markdown("---")

        # ── SECTION 4: PAPER TRADING LOG ─────────────────────────────────
        paper_df = load_paper_trades()
        if not paper_df.empty:
            st.markdown("<div class='section-header'>📄 Paper Trading Log</div>",
                        unsafe_allow_html=True)

            st.markdown(f"""
            <div class='paper-banner'>
                📄 Paper trades are simulated fills only — no real capital at risk.
                Showing {len(paper_df)} simulated entries.
            </div>
            """, unsafe_allow_html=True)

            # Summary stats for paper trades
            if "pnl" in paper_df.columns:
                ppnl = paper_df["pnl"].dropna()
                ppc1, ppc2, ppc3, ppc4 = st.columns(4)
                with ppc1: st.metric("Paper Trades",    len(ppnl))
                with ppc2: st.metric("Paper Total PnL", f"${ppnl.sum():+.4f}")
                with ppc3: st.metric("Paper Win Rate",  f"{(ppnl>0).mean():.1%}")
                with ppc4: st.metric("Paper Avg PnL",   f"${ppnl.mean():+.4f}")

            display_cols = [c for c in paper_df.columns if c not in ("_",)]
            st.dataframe(paper_df[display_cols].tail(50), width='stretch', height=320)

            csv_paper = paper_df.to_csv(index=False)
            st.download_button(
                "⬇️ Export Paper Trades CSV",
                data=csv_paper,
                file_name=f"paper_trades_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )
        else:
            with st.expander("📄 Paper Trading Log"):
                st.info("No paper trades logged yet. Enable PAPER_TRADING.ENABLED in config "
                        "or set MODE=PAPER to start simulating.")


# ─────────────────────────────────────────────
# AUTO REFRESH
# ─────────────────────────────────────────────
if auto_refresh:
    time.sleep(30)
    st.cache_data.clear()
    st.rerun()
