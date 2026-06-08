"""
AsyncEngine — Multi-asset async trading loop with ranking and recovery.
----------------------------------------------------------------------
v3.2.0 — Enhancements:
  • Session Filter       — dead-hour (23:00–02:59 UTC) confidence gate
  • Funding Rate Gate    — Binance perp funding rate penalty on crowded moves
  • Fear & Greed Gate    — alternative.me index adjusts confidence threshold
  • Order Book Imbalance — bid/ask imbalance boosts edge score
  • Volume Profile POC   — POC proximity widens SL guard
  • Multi-TF Consensus   — 1H direction must agree with entry signal
  • Regime-Aware Alerts  — Telegram notification on regime transitions
  • Adaptive Stop-Loss   — ATR percentile widens SL during high-vol regimes
  • Paper Trading Mode   — Full shadow simulation without real orders
  • BTC Benchmark        — Buy-and-hold comparison in cycle telegrams
  • Weekly Report        — Auto-sends PDF every Monday via Telegram
  • Interactive Telegram — /status /pause /resume /close /report /models
"""

import asyncio
import json
import math
import os
import signal
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

import numpy as np

from AegisQuantConfig import CONFIG, assert_asset_enabled
from Core.Logger import AG_LOGGER
from Core.Risk_Manager import RiskManager
from Core.Portfolio_Manager import PortfolioManager
from Core.Reporter import Reporter
from Execution.Execution_Router import ExecutionRouter
from Execution.CryptoExecution import CryptoExecution
from Execution.ForexExecution import ForexExecution
from Execution.StockExecution import StockExecution
from Services.TelegramService import TelegramService
from Data.ModelLoader import load_all_models
from AI.Predictor import AIPredictor
from Strategies.Ensemble import EnsembleStrategy
from Strategies.RSI_Strategy import RSIStrategy
from Strategies.MACD_Strategy import MACDStrategy
from Strategies.Bollinger_Strategy import BollingerStrategy
from Strategies.EMA_Cross_Strategy import EMACrossStrategy
from Strategies.VWAP_ATR_Strategy import VWAPATRStrategy
from Data.Features.OrderFlow import add_order_flow_features
from Data.Features.MultiTimeframe import add_mtf_features

try:
    from Services.FundingRateService import FundingRateService as _FundingRateService
    _FUNDING_SERVICE_AVAILABLE = True
except ImportError:
    _FUNDING_SERVICE_AVAILABLE = False

try:
    from Services.ReportGenerator import ReportGenerator as _ReportGenerator
    _REPORT_GENERATOR_AVAILABLE = True
except ImportError:
    _REPORT_GENERATOR_AVAILABLE = False

try:
    from Services.SentimentService import SentimentService as _SentimentService
    _SENTIMENT_AVAILABLE = True
except ImportError:
    _SENTIMENT_AVAILABLE = False

try:
    from Services.EventCalendar import EventCalendar as _EventCalendar
    _EVENT_CALENDAR_AVAILABLE = True
except ImportError:
    _EVENT_CALENDAR_AVAILABLE = False

try:
    from Execution.AlphaDecayTracker import AlphaDecayTracker as _AlphaDecayTracker
    _ALPHA_DECAY_AVAILABLE = True
except ImportError:
    _ALPHA_DECAY_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Module helpers
# ─────────────────────────────────────────────────────────────────────────────

def _atr_from_candles(candles: List[Any]) -> float:
    if len(candles) < 15:
        return 0.0
    try:
        from Core.Indicator_Engine import IndicatorEngine
        df = IndicatorEngine().calculate_indicators(candles)
        if df.empty or "ATRr_14" not in df.columns:
            return 0.0
        return float(df["ATRr_14"].iloc[-1])
    except Exception:
        return 0.0


def enforce_account_constraints(signal: str, symbol: str, portfolio_manager) -> str:
    if CONFIG["PROJECT"].get("ACCOUNT_MODE", "SPOT") == "SPOT":
        if signal == "SELL" and portfolio_manager.get_exposure_for_symbol(symbol) <= 0:
            return "HOLD"
    return signal


def calculate_effective_rr(sl_pct: float, tp_pct: float, fee_pct: float, spread_pct: float = 0.0) -> float:
    effective_sl = sl_pct + fee_pct + spread_pct
    effective_tp = tp_pct - fee_pct - spread_pct
    if effective_sl <= 0 or effective_tp <= 0:
        return 0.0
    return effective_tp / effective_sl


def _compute_volume_poc(candles: list, price_levels: int = 50) -> float:
    """Point of Control: price level with highest volume over candle set."""
    try:
        prices  = [float(c.close) for c in candles]
        volumes = [float(c.volume) for c in candles]
        min_p, max_p = min(prices), max(prices)
        if max_p <= min_p:
            return min_p
        bin_edges  = np.linspace(min_p, max_p, price_levels + 1)
        vol_bins   = np.zeros(price_levels)
        for p, v in zip(prices, volumes):
            idx = min(int((p - min_p) / (max_p - min_p) * price_levels), price_levels - 1)
            vol_bins[idx] += v
        poc_idx = int(np.argmax(vol_bins))
        return float((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# AsyncEngine
# ─────────────────────────────────────────────────────────────────────────────

class AsyncEngine:
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self._capital_lock = asyncio.Lock()
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._last_traded_candle: Dict[str, float] = {}
        self._last_trade_executed_ts: Dict[str, float] = {}

        # Balance TTL cache — reduces redundant /api/v3/account calls when 7
        # symbol coroutines and _run_cycle all need the balance in the same cycle.
        # Cache is invalidated on each trade execution so execution sizing is fresh.
        self._balance_cache: Optional[float] = None
        self._balance_cache_ts: float = 0.0
        self._balance_cache_ttl: float = 20.0   # seconds
        self._emergency_trading_halt = False
        self._halt_reason: str = ""
        self._api_failures = 0
        self._trade_context: Dict[str, Dict] = {}
        self._telegram_paused: bool = False         # /pause command support

        # Regime change tracking for alerts
        self._last_regime_per_symbol: Dict[str, str] = {}

        # BTC benchmark
        self._btc_benchmark_price: Optional[float] = None
        self._btc_benchmark_balance: Optional[float] = None

        # Weekly report tracking
        self._weekly_report_last_ts: float = 0.0
        self._weekly_report_day: Optional[int] = None   # weekday 0=Mon

        # Paper trade log (in-memory accumulator)
        self._paper_trades: List[Dict] = []

        # Dynamic confidence: global consecutive-loss streak counter
        self._global_loss_streak: int = 0
        # Live accuracy: circular buffer of trade outcomes (True=win)
        self._live_trade_results: List[bool] = []
        # Additive confidence threshold boost when live WR drops < 40%
        self._live_accuracy_boost: float = 0.0
        # Partial TP state per symbol: "WATCHING" | "TP1_HIT"
        self._ptp_state: Dict[str, str] = {}

        # ATR history for adaptive SL (symbol -> deque of ATR pct values)
        self._atr_history: Dict[str, List[float]] = {}

        # Symbol-level consecutive-loss counter (resets on win)
        self._symbol_loss_streak: Dict[str, int] = {}
        # Symbol-level ban expiry timestamp (unix epoch); 0 = not banned
        self._symbol_ban_until: Dict[str, float] = {}
        # Hard re-entry block: unix timestamp of last stop-loss close per symbol
        self._last_stop_loss_ts: Dict[str, float] = {}

        # Executors
        crypto_exec = CryptoExecution() if CONFIG["PROJECT"]["CRYPTO_ENABLED"] else None
        forex_exec  = ForexExecution()  if CONFIG["PROJECT"]["FOREX_ENABLED"]  else None
        stock_exec  = StockExecution()  if CONFIG["PROJECT"]["STOCKS_ENABLED"] else None
        self.router = ExecutionRouter(crypto_exec, forex_exec, stock_exec)

        self.risk_manager    = RiskManager(balance=0.0)
        self.portfolio_manager = PortfolioManager()
        self.reporter        = Reporter()
        self.telegram        = TelegramService()
        self.risk_manager.set_drawdown_breach_callback(self.telegram.notify_drawdown_breach)

        # Funding rate + Fear & Greed service
        self.funding_service = _FundingRateService() if _FUNDING_SERVICE_AVAILABLE else None

        # Report generator
        log_dir = CONFIG.get("REPORTING", {}).get("LOG_DIR", os.path.join(os.getcwd(), "logs"))
        self.report_generator = _ReportGenerator(log_dir) if _REPORT_GENERATOR_AVAILABLE else None

        # Sentiment overlay (news + social)
        self.sentiment_service = _SentimentService() if _SENTIMENT_AVAILABLE else None

        # Event calendar (FOMC, CPI, futures expiry)
        self.event_calendar = _EventCalendar() if _EVENT_CALENDAR_AVAILABLE else None

        # Alpha decay tracker (per-strategy win-rate weight decay)
        self.alpha_decay = _AlphaDecayTracker() if _ALPHA_DECAY_AVAILABLE else None

        # BTC 4H price reference for surprise-move detection (event calendar)
        self._btc_4h_ref_price: float = 0.0

        self.models    = load_all_models()
        self.predictor = AIPredictor(models=self.models)

        # ── Startup model health check ────────────────────────────────
        # Fail-fast: log CRITICAL for any symbol without a model file so the
        # operator knows to run AegisQuantTrainer before the first cycle fires.
        _missing_models = [
            f"{sec}/{sym}"
            for sec, syms in self.models.items()
            for sym, m in syms.items()
            if m is None
        ]
        if _missing_models:
            self.logger.critical(
                "NO MODEL FILES for %d symbol(s): %s — these will produce "
                "FEATURE_ALIGNMENT_FAILURE every cycle. "
                "Run Data/Trainer/AegisQuantTrainer.py first.",
                len(_missing_models), _missing_models,
            )

        # ── ModelHealthMonitor — live anomaly detection ───────────────
        if CONFIG.get("MODEL_HEALTH", {}).get("ENABLED", True):
            try:
                from Execution.ModelHealthMonitor import ModelHealthMonitor as _MHM
                _mh_cfg = CONFIG.get("MODEL_HEALTH", {})
                self.model_health = _MHM(
                    min_signal_rate=int(_mh_cfg.get("MIN_SIGNAL_RATE", 5)),
                    signal_rate_window=int(_mh_cfg.get("SIGNAL_RATE_WINDOW", 100)),
                    prob_compression_threshold=float(_mh_cfg.get("PROB_COMPRESSION_THRESHOLD", 0.05)),
                    max_meta_rejection_rate=float(_mh_cfg.get("MAX_META_REJECTION_RATE", 0.80)),
                )
            except Exception:
                self.model_health = None
        else:
            self.model_health = None

        strategies = [
            RSIStrategy(), MACDStrategy(), BollingerStrategy(),
            EMACrossStrategy(), VWAPATRStrategy(),
        ]
        self.strategy = EnsembleStrategy(strategies)

        self._markets: Dict[str, Any] = {}
        if CONFIG["PROJECT"]["CRYPTO_ENABLED"]:
            from Markets.Crypto import CryptoMarket
            self._markets["CRYPTO"] = CryptoMarket()
        if CONFIG["PROJECT"]["FOREX_ENABLED"]:
            from Markets.Forex import ForexMarket
            self._markets["FOREX"] = ForexMarket()
        if CONFIG["PROJECT"]["STOCKS_ENABLED"]:
            from Markets.Stocks import StockMarket
            self._markets["STOCKS"] = StockMarket()

        # Register Telegram command handlers
        self._register_telegram_commands()

    # ──────────────────────────────────────────────────────────────────
    # Network safety helper
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    async def _tt(func, *args, timeout: float = 15.0, **kwargs):
        """
        _tt = timed-thread: run a synchronous CCXT / network call in a
        thread pool with a hard timeout.  Raises asyncio.TimeoutError if
        the call doesn't return within `timeout` seconds so callers never
        hang silently.  Usage:
            candles = await self._tt(market.fetch_ohlcv, symbol, "5m", limit=200)
        """
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=timeout,
        )

    # ──────────────────────────────────────────────────────────────────
    # Telegram interactive commands
    # ──────────────────────────────────────────────────────────────────

    def _register_telegram_commands(self) -> None:
        """Register /command handlers that operate the engine remotely."""
        try:
            self.telegram.register_command_handler("status",     self._cmd_status)
            self.telegram.register_command_handler("pause",      self._cmd_pause)
            self.telegram.register_command_handler("resume",     self._cmd_resume)
            self.telegram.register_command_handler("close",      self._cmd_close)
            self.telegram.register_command_handler("report",     self._cmd_report)
            self.telegram.register_command_handler("models",     self._cmd_models)
            self.telegram.register_command_handler("benchmark",  self._cmd_benchmark)
            self.telegram.register_command_handler("unban",      self._cmd_unban)
            self.telegram.register_command_handler("sentiment",  self._cmd_sentiment)
        except Exception as e:
            self.logger.warning("Telegram command registration failed: %s", e)

    def _cmd_status(self, args: List[str]) -> str:
        balance = self.router.get_balance() or 0.0
        open_pos = self.router.get_open_positions() or []
        open_n   = len(open_pos) if isinstance(open_pos, list) else 0
        growth   = CONFIG.get("GROWTH_TARGET", {})
        idr_rate = growth.get("IDR_RATE", 16000)
        target   = growth.get("TARGET_CAPITAL_IDR", 10_000_000)
        start    = growth.get("STARTING_CAPITAL_IDR", 300_000)
        idr      = balance * idr_rate
        pct      = min(100, max(0, (idr - start) / (target - start) * 100))
        paused   = "⏸ PAUSED" if self._telegram_paused else "▶ RUNNING"
        return (
            f"📊 *AegisQuant Status*\n"
            f"Engine: {paused}\n"
            f"Balance: ${balance:.4f} USDT\n"
            f"IDR: Rp {idr:,.0f}\n"
            f"Open Positions: {open_n}\n"
            f"Growth Progress: {pct:.1f}%\n"
            f"_{datetime.now(timezone.utc).strftime('%H:%M UTC')}_"
        )

    def _cmd_pause(self, args: List[str]) -> str:
        self._telegram_paused = True
        return "⏸ *Trading PAUSED* — engine will hold all new signals. Send /resume to restart."

    def _cmd_resume(self, args: List[str]) -> str:
        self._telegram_paused = False
        return "▶ *Trading RESUMED* — engine back to normal operation."

    def _cmd_close(self, args: List[str]) -> str:
        if not args:
            return "Usage: /close BTCUSDT"
        symbol_raw = args[0].upper()
        # Normalise: BTCUSDT → BTC/USDT
        symbol = symbol_raw if "/" in symbol_raw else (symbol_raw[:-4] + "/" + symbol_raw[-4:])
        try:
            result = self.router.close_position("CRYPTO", symbol)
            if result.get("status") in ["filled", "closed"]:
                self.portfolio_manager.record_close(symbol, sector="CRYPTO")
                return f"✅ *Closed position*: `{symbol}` — manually triggered via Telegram."
            return f"⚠️ Close attempt for `{symbol}` returned: `{result.get('status', 'unknown')}`"
        except Exception as e:
            return f"❌ Close failed for `{symbol}`: {e}"

    def _cmd_report(self, args: List[str]) -> str:
        if self.report_generator is None:
            return "⚠️ Report generator not available (fpdf2 missing)."
        balance = self.router.get_balance() or 0.0
        path = self.report_generator.generate_weekly_report(balance)
        if path:
            sent = self.telegram.send_document(path, caption="📊 AegisQuant Weekly Report")
            return "📊 Weekly report sent." if sent else f"📊 Report saved to: {path}"
        return "❌ Report generation failed."

    def _cmd_models(self, args: List[str]) -> str:
        model_dir = CONFIG["AI"]["MODEL_PATH"]
        lines = ["🤖 *Model Health*"]
        for sector, syms in CONFIG["SYMBOLS"].items():
            if not CONFIG["MARKETS"].get(sector.upper()):
                continue
            for sym in syms:
                base = f"RandomForest_{sector}_{sym.replace('/', '')}"
                meta_path = os.path.join(model_dir, f"{base}_metadata.json")
                has_lstm  = os.path.exists(os.path.join(model_dir, f"{base}_LSTM.pt"))
                has_xgb   = os.path.exists(os.path.join(model_dir, f"{base}_XGB.joblib"))
                has_lgb   = os.path.exists(os.path.join(model_dir, f"{base}_LGB.joblib"))
                member_str = " [" + "+".join(filter(None, [
                    "RF", "XGB" if has_xgb else "", "LGB" if has_lgb else "",
                    "LSTM" if has_lstm else "",
                ])) + "]"
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path) as f:
                            m = json.load(f)
                        acc = m.get("global_test_accuracy", 0)
                        n   = m.get("n_train", 0)
                        wf  = m.get("walk_forward", {})
                        wf_str = f" WF:{wf.get('mean_accuracy', 0):.2f}" if wf.get("mean_accuracy") else ""
                        status = "✅" if acc >= 0.55 and n >= 100 else "⚠️"
                        # Symbol ban status
                        ban_until = self._symbol_ban_until.get(sym, 0)
                        streak    = self._symbol_loss_streak.get(sym, 0)
                        ban_str   = f" 🚫{int((ban_until - time.time())/60)}m" if time.time() < ban_until else (
                            f" L{streak}" if streak > 0 else ""
                        )
                        lines.append(f"{status} `{sym}` acc={acc:.2f} n={n}{wf_str}{member_str}{ban_str}")
                    except Exception:
                        lines.append(f"❓ `{sym}` — metadata unreadable{member_str}")
                else:
                    lines.append(f"❌ `{sym}` — no model file{member_str}")

        # Alpha decay summary
        if self.alpha_decay:
            lines.append("\n📉 *Strategy Alpha Decay*")
            for name, stats in self.alpha_decay.summary().items():
                wr  = stats.get("win_rate", "N/A")
                wt  = stats.get("weight", 0)
                n_t = stats.get("trades", 0)
                wr_str = f"{wr:.0%}" if isinstance(wr, float) else wr
                lines.append(f"  `{name}` WR={wr_str} n={n_t} w={wt:.2f}")

        return "\n".join(lines)

    def _cmd_benchmark(self, args: List[str]) -> str:
        if self._btc_benchmark_price is None:
            return "⚠️ Benchmark not initialized (engine hasn't fetched BTC price yet)."
        balance = self.router.get_balance() or 0.0
        init_bal = self._btc_benchmark_balance or balance
        strategy_return_pct = (balance - init_bal) / init_bal * 100 if init_bal > 0 else 0.0
        # Estimate BTC current price
        try:
            mkt = self._markets.get("CRYPTO")
            if mkt:
                ticker = mkt.exchange.fetch_ticker("BTC/USDT")
                btc_now = float(ticker.get("last", self._btc_benchmark_price))
            else:
                btc_now = self._btc_benchmark_price
        except Exception:
            btc_now = self._btc_benchmark_price
        btc_return_pct = (btc_now - self._btc_benchmark_price) / self._btc_benchmark_price * 100
        alpha = strategy_return_pct - btc_return_pct
        return (
            f"📊 *Benchmark Comparison*\n"
            f"Since engine start:\n"
            f"AegisQuant strategy: {strategy_return_pct:+.2f}%\n"
            f"BTC buy-and-hold:     {btc_return_pct:+.2f}%\n"
            f"Alpha:               {alpha:+.2f}%\n"
            f"BTC start: ${self._btc_benchmark_price:,.2f} → now: ${btc_now:,.2f}"
        )

    def _cmd_unban(self, args: List[str]) -> str:
        """Manual /unban BTCUSDT — lifts a symbol ban early."""
        if not args:
            # Show all active bans
            bans = {
                sym: int((ts - time.time()) / 60)
                for sym, ts in self._symbol_ban_until.items()
                if time.time() < ts
            }
            if not bans:
                return "✅ No active symbol bans."
            lines = ["🚫 *Active Symbol Bans*"]
            for sym, mins in bans.items():
                lines.append(f"  `{sym}` — {mins}m remaining")
            return "\n".join(lines)
        raw = args[0].upper().replace("USDT", "/USDT").replace("//", "/")
        if raw in self._symbol_ban_until:
            self._symbol_ban_until[raw]    = 0
            self._symbol_loss_streak[raw]  = 0
            self._last_stop_loss_ts[raw]   = 0
            return f"✅ Ban lifted for `{raw}` — normal trading resumed."
        return f"⚠️ No active ban found for `{raw}`."

    def _cmd_sentiment(self, args: List[str]) -> str:
        """Manual /sentiment BTCUSDT — check current sentiment state."""
        sym = (args[0].upper().replace("USDT", "/USDT") if args else "BTC/USDT").replace("//", "/")
        if not self.sentiment_service:
            return "⚠️ Sentiment service not available."
        try:
            fg   = self.sentiment_service.get_fear_greed_index()
            mood = "Extreme Fear" if fg < 20 else "Fear" if fg < 40 else \
                   "Neutral" if fg < 60 else "Greed" if fg < 80 else "Extreme Greed"
            panic, headline = self.sentiment_service._check_panic_news(sym)
            panic_str = f"\n🚨 Panic headline: _{headline[:80]}_" if panic else "\n✅ No panic news"
            return (
                f"📊 *Sentiment — {sym}*\n"
                f"Fear & Greed: {fg}/100 ({mood}){panic_str}"
            )
        except Exception as e:
            return f"❌ Sentiment check failed: {e}"

    # ──────────────────────────────────────────────────────────────────
    # Standard helpers
    # ──────────────────────────────────────────────────────────────────

    async def _get_balance(self) -> Optional[float]:
        # ── Fast path: serve from TTL cache (no lock, no I/O) ────────────
        now = time.time()
        if (
            self._balance_cache is not None
            and (now - self._balance_cache_ts) < self._balance_cache_ttl
        ):
            return self._balance_cache

        # ── Slow path: fetch from exchange ────────────────────────────────
        # IMPORTANT: _capital_lock must NEVER be held during I/O.
        # Holding an asyncio.Lock across an `await` still blocks every other
        # coroutine that tries to acquire the same lock, stalling the entire
        # engine for the duration of the network call (up to 62 s on retries).
        # Instead: do the network call outside the lock with a hard timeout,
        # then take a brief lock window only to write the in-memory cache.
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.router.get_balance),
                timeout=15.0,
            )
        except (asyncio.TimeoutError, Exception):
            result = None

        if result is not None:
            # Minimal lock window — pure in-memory write, no I/O.
            async with self._capital_lock:
                self._balance_cache    = result
                self._balance_cache_ts = time.time()

        return result

    async def _get_open_positions_count(self) -> int:
        # router.get_open_positions() returns [] for SPOT mode (Binance spot has no
        # /positions endpoint).  Use portfolio_manager which tracks every fill
        # internally and is always accurate for both spot and futures.
        return self.portfolio_manager.get_open_count()

    def _get_value_for_position(self, asset_class: str, p: Any) -> float:
        if asset_class == "CRYPTO":
            qty   = float(p.get("contracts", 0) or p.get("contractSize", 0) or 0)
            price = float(p.get("markPrice") or p.get("entryPrice") or p.get("averagePrice") or 0)
            return qty * price if price else 0
        if asset_class == "FOREX":
            long_units  = float(p.get("long",  {}).get("units", 0) or 0)
            short_units = float(p.get("short", {}).get("units", 0) or 0)
            long_price  = float(p.get("long",  {}).get("averagePrice", 0) or 0)
            short_price = float(p.get("short", {}).get("averagePrice", 0) or 0)
            return abs(long_units) * long_price + abs(short_units) * short_price
        if asset_class == "STOCKS":
            qty   = float(p.get("qty", 0) or 0)
            price = float(p.get("avg_entry_price") or p.get("current_price") or 0)
            return qty * price if price else 0
        return 0

    def _get_symbol_for_position(self, asset_class: str, p: Any) -> str:
        if asset_class == "FOREX":
            return p.get("instrument", "")
        return p.get("symbol", "") or ""

    async def _recovery_on_startup(self) -> None:
        self.logger.info("Recovery: fetching open positions...")
        total_recovered = 0
        for asset_class in ("CRYPTO", "FOREX", "STOCKS"):
            try:
                assert_asset_enabled(asset_class)
            except RuntimeError:
                continue
            try:
                positions = self.router.get_open_positions(asset_class)
                if positions:
                    get_sym = lambda p, ac=asset_class: self._get_symbol_for_position(ac, p)
                    get_val = lambda p, ac=asset_class: self._get_value_for_position(ac, p)
                    self.portfolio_manager.update_from_positions(asset_class, positions, get_symbol=get_sym, get_value=get_val)
                    total_recovered += len(positions)
                    self.logger.info("Recovery: %s positions loaded: %s", asset_class, len(positions))
            except Exception as e:
                self.logger.warning("Recovery %s failed: %s", asset_class, e)
        balance = await self._get_balance()
        if balance is not None:
            total_equity = balance + self.portfolio_manager.get_total_exposure()
            self.risk_manager.update_balance(total_equity)
            msg = f"Restart complete. Balance: {balance:.2f}. Positions reconciled."
        else:
            msg = "Restart complete. Balance UNKNOWN. Positions reconciled."
        self.telegram.notify_system_restart(msg, total_recovered)

    def _log_trade_decision(self, symbol, signal, conf, votes, open_trades,
                            balance, slot_capital, notional, sl_status,
                            entry_status, block) -> None:
        alignment = "N/A"
        if votes:
            b = votes.get("BUY", 0)
            s = votes.get("SELL", 0)
            t = max(1, votes.get("TOTAL", 1))
            alignment = f"{b}/{t}" if signal == "BUY" else f"{s}/{t}" if signal == "SELL" else f"{max(b,s)}/{t}"
        msg = (
            f"\nTRADE_DECISION:\nSymbol: {symbol}\nSignal: {signal}\n"
            f"AI_Confidence: {conf*100:.2f}%\nOpen_Trades: {open_trades}\n"
            f"Free_Balance: {balance:.2f}\nSlot_Capital: {slot_capital:.2f}\n"
            f"Order_Notional: {notional:.2f}\nSL_Status: {sl_status}\n"
            f"Entry_Status: {entry_status}\nBlock_Reason: {block}\n"
        )
        self.logger.warning(msg) if block else self.logger.info(msg)

    def _broadcast_probabilities(self) -> None:
        """Deprecated: acts as no-op."""
        pass

    def _rank_symbols(self, scores: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        top_n     = CONFIG.get("RANKING", {}).get("TOP_N", 5)
        threshold = CONFIG.get("RANKING", {}).get("CONFIDENCE_THRESHOLD", 0.70)
        sorted_scores = sorted(scores, key=lambda x: -abs(x[3]))
        out = []
        for item in sorted_scores:
            if len(out) >= top_n:
                break
            signal, p_long, p_short = item[2], item[4], item[5]
            conf = p_long if signal == "BUY" else p_short
            if conf < threshold:
                continue
            out.append(item)
        return out

    # ──────────────────────────────────────────────────────────────────
    # Core signal processing
    # ──────────────────────────────────────────────────────────────────

    async def _process_symbol(
        self,
        asset_class: str,
        symbol: str,
        timeframe: str,
        cycle_trace_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch candles, run strategy + ML, evaluate execution gates."""
        if not hasattr(self, "_consecutive_losses"):
            self._consecutive_losses = {}
        if not hasattr(self, "_cooldown_until"):
            self._cooldown_until = {}

        import time
        current_time = time.time()

        trace = {
            "trace_id":        f"{cycle_trace_id}-{symbol.replace('/', '')}",
            "asset_class":     asset_class,
            "symbol":          symbol,
            "signal":          "HOLD",
            "confidence":      0.0,
            "edge_score":      0.0,
            "market_regime":   "UNKNOWN",
            "atr_5m_pct":      0.0010,
            "terminal_state":  "SKIPPED",
            "blocking_reason": "No actionable signal",
            "price":           0.0,
            "candle_ts":       0.0,
            "usable_capital":  0.0,
            "allocation_per_trade": 0.0,
            "is_valid_candidate": False,
        }

        if getattr(self, "_emergency_trading_halt", False):
            trace["terminal_state"] = "SKIPPED"
            trace["blocking_reason"] = "GLOBAL HALT ACTIVE"
            return trace

        if getattr(self, "_telegram_paused", False):
            trace["terminal_state"] = "SKIPPED"
            trace["blocking_reason"] = "ENGINE PAUSED (Telegram /pause)"
            return trace

        if self.risk_manager.cooldown_active():
            trace["terminal_state"] = "REJECTED"
            trace["blocking_reason"] = "Blocked: Cooldown Active"
            return trace

        # ── Symbol ban — consecutive-loss protection ──────────────────
        ban_cfg = CONFIG.get("SYMBOL_BAN", {})
        if ban_cfg.get("ENABLED", True):
            ban_until = self._symbol_ban_until.get(symbol, 0)
            if current_time < ban_until:
                remaining_min = int((ban_until - current_time) / 60)
                trace["terminal_state"] = "REJECTED"
                trace["blocking_reason"] = (
                    f"SYMBOL_BAN: {symbol} banned for {remaining_min}m more "
                    f"(consecutive losses ≥ {ban_cfg.get('LOSS_THRESHOLD', 3)})"
                )
                return trace

        # ── Hard re-entry block — post stop-loss protection ──────────
        hard_block_sec = CONFIG.get("EXECUTION", {}).get("HARD_REENTRY_BLOCK_SEC", 2700)
        last_sl_ts = self._last_stop_loss_ts.get(symbol, 0)
        if last_sl_ts > 0 and (current_time - last_sl_ts) < hard_block_sec:
            remaining_min = int((hard_block_sec - (current_time - last_sl_ts)) / 60)
            trace["terminal_state"] = "REJECTED"
            trace["blocking_reason"] = (
                f"HARD_REENTRY_BLOCK: {symbol} blocked {remaining_min}m post stop-loss"
            )
            return trace

        # ── Session Filter — dead hours ───────────────────────────────
        session_cfg = CONFIG.get("TRADING_SESSIONS", {})
        session_filter_enabled = session_cfg.get("ENABLED", True)
        dead_hours = session_cfg.get("DEAD_HOURS_UTC", [23, 0, 1, 2])
        utc_hour = datetime.now(timezone.utc).hour
        in_dead_hours = session_filter_enabled and utc_hour in dead_hours
        # During dead hours a higher confidence bar is applied later
        dead_hour_threshold = session_cfg.get("DEAD_HOURS_CONFIDENCE_THRESHOLD", 0.78)
        trace["in_dead_hours"] = in_dead_hours

        market = self._markets.get(asset_class)
        if not market:
            return trace

        try:
            from Core.Indicator_Engine import IndicatorEngine

            candles = await self._tt(market.fetch_ohlcv, symbol, timeframe, limit=200, timeout=20.0)
            if not candles or len(candles) < 50:
                return trace

            ts_val = candles[-1].timestamp
            current_ts = float(ts_val.timestamp()) if hasattr(ts_val, "timestamp") else float(ts_val)
            trace["candle_ts"] = current_ts
            if current_ts <= self._last_traded_candle.get(symbol, 0):
                return trace
            # Mark candle seen so the same candle is never processed twice within one cycle
            self._last_traded_candle[symbol] = current_ts

            # Fetch balance via the TTL-cached helper.
            # The first coroutine in the cycle hits the network; the remaining 6
            # get the cached value immediately.  Cache is invalidated after every
            # real trade so execution sizing is always fresh.
            balance   = await self._get_balance() or 0.0
            # Use portfolio_manager for position count — router.get_open_positions()
            # returns [] for SPOT mode, making the concurrent-trade cap ineffective.
            current_positions = self.portfolio_manager.get_open_count()

            micro_mode   = balance < 100.0
            reserve_usdt = max(2.0, balance * 0.05)
            deployable   = balance - reserve_usdt

            # Align slot count with _run_cycle's max_concurrent_positions cap:
            #   < $50  → 1 trade max (grow mode) — full deployable per trade
            #   $50–99 → 2 trades allowed (diversified micro)
            #   ≥ $100 → config-driven (default 2)
            # Keeping this in sync prevents the engine allocating to N slots
            # while only ever executing 1, which left half the capital idle.
            if balance < 50.0:
                max_positions = 1
            elif micro_mode:      # $50 – $99
                max_positions = 2
            else:
                max_positions = CONFIG["RISK"].get("MAX_CONCURRENT_TRADES", 2)

            allocation_per_trade = deployable / max(max_positions, 1)
            min_safe_notional = 6.0

            if allocation_per_trade < min_safe_notional and current_positions == 0:
                allocation_per_trade = deployable * 0.90
                max_positions = 1
            if allocation_per_trade > deployable:
                allocation_per_trade = deployable

            trace["usable_capital"]      = deployable
            trace["allocation_per_trade"] = allocation_per_trade

            # ── Sentiment gate — news panic suppression ───────────────
            if self.sentiment_service:
                try:
                    sent_state = await self._tt(self.sentiment_service.evaluate, symbol, timeout=15.0)
                    trace["sentiment_score"] = sent_state.sentiment_score
                    trace["fg_raw"]          = sent_state.fg_index
                    if sent_state.is_blocked:
                        trace["terminal_state"] = "REJECTED"
                        trace["blocking_reason"] = sent_state.block_reason
                        return trace
                except Exception as sent_e:
                    self.logger.debug("Sentiment gate error for %s: %s", symbol, sent_e)

            # ── Event calendar gate ───────────────────────────────────
            _event_conf_boost = 0.0
            _event_size_scale = 1.0
            if self.event_calendar:
                try:
                    btc_4h_move = 0.0
                    if self._btc_4h_ref_price > 0 and symbol == "BTC/USDT":
                        current_p = 0.0
                        try:
                            candles_btc = await self._tt(market.fetch_ohlcv, "BTC/USDT", "4h", limit=2, timeout=15.0)
                            if candles_btc:
                                current_p = float(candles_btc[-1].close)
                        except Exception:
                            pass
                        if current_p > 0:
                            btc_4h_move = abs(current_p - self._btc_4h_ref_price) / self._btc_4h_ref_price
                            self._btc_4h_ref_price = current_p
                    evt = self.event_calendar.check(btc_4h_move)
                    if evt.in_event_window:
                        _event_conf_boost = evt.confidence_boost
                        _event_size_scale = evt.size_scale
                        trace["event_window"] = evt.event_name
                        self.logger.info(
                            "EVENT_WINDOW %s for %s — conf+%.2f size×%.2f",
                            evt.event_name, symbol, evt.confidence_boost, evt.size_scale,
                        )
                except Exception as evt_e:
                    self.logger.debug("Event calendar error for %s: %s", symbol, evt_e)

            # ── 1H Regime Detection ───────────────────────────────────
            candles_1h = await self._tt(market.fetch_ohlcv, symbol, "1h", limit=100, timeout=20.0)
            market_regime = "LOW_ACTIVITY"
            trend_strength = 0.0
            ema_alignment  = False
            htf_direction  = "NEUTRAL"   # for MTF consensus gate

            if candles_1h and len(candles_1h) >= 50:
                df_1h = IndicatorEngine().calculate_indicators(candles_1h)

                def classify_regime(row, p):
                    t_str  = row.get("adx", 0.0)
                    a_tr   = row.get("ATRr_14", 0.0)
                    vol    = a_tr / p if p > 0 else 0.0
                    e21    = row.get("EMA_21", 0)
                    e50    = row.get("EMA_50", 0)
                    e200   = row.get("EMA_200", 0)
                    ema_al = (e21 > e50 > e200) or (e21 < e50 < e200)
                    if t_str >= 25 and ema_al:
                        return "TRENDING", t_str, vol, ema_al
                    elif t_str < 20 and vol >= 0.002:
                        return "RANGING", t_str, vol, ema_al
                    return "LOW_ACTIVITY", t_str, vol, ema_al

                last_1h = df_1h.iloc[-1]
                market_regime, trend_strength, atr_1h_pct, ema_alignment = classify_regime(
                    last_1h, candles_1h[-1].close
                )
                # Extract 1H direction for MTF consensus check
                ema21_1h = float(last_1h.get("EMA_21", 0))
                ema50_1h = float(last_1h.get("EMA_50", 0))
                if ema21_1h > ema50_1h:
                    htf_direction = "BULLISH"
                elif ema21_1h < ema50_1h:
                    htf_direction = "BEARISH"
                else:
                    htf_direction = "NEUTRAL"
            else:
                atr_1h_pct = 0.002

            trace["market_regime"] = market_regime
            trace["htf_direction"] = htf_direction

            # ── Regime Change Alert ───────────────────────────────────
            prev_regime = self._last_regime_per_symbol.get(symbol)
            if prev_regime and prev_regime != market_regime:
                self.telegram.notify_regime_change(symbol, prev_regime, market_regime)
                self.logger.info("REGIME_CHANGE: %s  %s → %s", symbol, prev_regime, market_regime)
            self._last_regime_per_symbol[symbol] = market_regime

            # ── 4H Trend Confirmation ─────────────────────────────────
            trend_4h_aligned = True
            try:
                candles_4h = await self._tt(market.fetch_ohlcv, symbol, "4h", limit=55, timeout=20.0)
                if candles_4h and len(candles_4h) >= 50:
                    df_4h = IndicatorEngine().calculate_indicators(candles_4h)
                    row_4h  = df_4h.iloc[-1]
                    ema21_4 = row_4h.get("EMA_21", 0)
                    ema50_4 = row_4h.get("EMA_50", 0)
                    adx_4h  = row_4h.get("adx", 0)
                    trend_4h_aligned = False
                    if ema21_4 > ema50_4 and adx_4h > 18:
                        trend_4h_aligned = True
                    elif ema21_4 < ema50_4 and adx_4h > 18:
                        trend_4h_aligned = True
            except Exception as e4h:
                self.logger.debug("4H trend filter fetch failed for %s: %s", symbol, e4h)
            trace["trend_4h_aligned"] = trend_4h_aligned

            # ── Strategy + ML Prediction ──────────────────────────────
            analysis    = self.strategy.analyze(candles)
            df          = self.strategy.prepare_data(candles)
            base_signal = analysis.get("signal", "HOLD")
            # Capture per-strategy results for alpha decay tracking in _run_cycle
            trace["strategy_results"] = analysis.get("strategy_results", [])

            try:
                import pandas as pd
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                    df = df.set_index("timestamp")
                df = add_order_flow_features(df, cvd_mode="rolling", cvd_window=96, normalization_window=96)
                df = add_mtf_features(df)
                df = df.ffill().fillna(0.0)
            except Exception as feat_e:
                self.logger.warning("Feature enrichment failed for %s: %s", symbol, feat_e)

            p_long, p_short = self.predictor.get_probabilities(df, sector=asset_class, symbol=symbol)
            trace["p_long"]  = p_long
            trace["p_short"] = p_short

            ai_signal     = "BUY" if p_long > p_short else "SELL"
            ai_confidence = p_long if ai_signal == "BUY" else p_short

            # ── Neutral gate: model is undecided — treat as HOLD ─────────
            # For 3-class {-1,0,1} models the bulk of probability mass can
            # sit in the neutral class.  If long + short together is < 50%
            # the model has no real conviction in either direction.
            if (p_long + p_short) < 0.50:
                ai_signal     = "HOLD"
                ai_confidence = 0.0

            votes         = analysis.get("votes", {})
            bull_indicators = votes.get("BUY", 0)
            bear_indicators = votes.get("SELL", 0)
            trace["bull_indicators"] = bull_indicators
            trace["bear_indicators"] = bear_indicators

            # ── TA vs AI directional agreement ───────────────────────
            if base_signal != "HOLD" and ai_signal != "HOLD" and base_signal != ai_signal:
                trace["terminal_state"] = "HOLD"
                trace["blocking_reason"] = f"Signal conflict: TA={base_signal} vs AI={ai_signal}"
                return trace

            if base_signal == "HOLD" and ai_confidence < 0.78:
                signal = "HOLD"
            elif base_signal != "HOLD":
                signal = base_signal
            else:
                signal = ai_signal

            # ── Multi-Timeframe Consensus (1H direction) ──────────────
            gates_cfg = CONFIG.get("SIGNAL_GATES", {})
            mtf_required     = gates_cfg.get("MTF_CONSENSUS_REQUIRED", True)
            mtf_override_conf = gates_cfg.get("MTF_CONSENSUS_OVERRIDE_CONF", 0.82)
            if mtf_required and signal != "HOLD" and htf_direction != "NEUTRAL":
                htf_agrees = (
                    (signal == "BUY"  and htf_direction == "BULLISH") or
                    (signal == "SELL" and htf_direction == "BEARISH")
                )
                if not htf_agrees and ai_confidence < mtf_override_conf:
                    trace["terminal_state"]  = "HOLD"
                    trace["blocking_reason"] = f"MTF conflict: {signal} vs 1H={htf_direction}"
                    return trace

            indicator_count = bull_indicators if signal == "BUY" else bear_indicators
            trace["signal"] = signal

            signal = enforce_account_constraints(signal, symbol, self.portfolio_manager)
            trace["signal"]     = signal
            trace["confidence"] = ai_confidence

            # ── Feed ModelHealthMonitor ───────────────────────────────
            if self.model_health:
                try:
                    _regime_int = {"TRENDING": 1, "RANGING": 0, "LOW_ACTIVITY": -1}.get(market_regime, 0)
                    self.model_health.ingest_tick(
                        is_raw_cusum=(base_signal != "HOLD"),
                        meta_prob=ai_confidence,
                        meta_approved=(signal != "HOLD"),
                        signal_direction=(1 if signal == "BUY" else -1 if signal == "SELL" else 0),
                        current_regime=_regime_int,
                    )
                except Exception:
                    pass

            if signal == "HOLD":
                trace["terminal_state"] = "HOLD"
                trace["blocking_reason"] = "No actionable signal"
                return trace

            # ── Volume + momentum scores ──────────────────────────────
            avg_vol       = sum(c.volume for c in candles[-21:-1]) / 20.0 if len(candles) > 20 else 1.0
            current_vol   = candles[-1].volume
            volume_ratio  = current_vol / avg_vol if avg_vol > 0 else 1.0

            price = float(candles[-1].close) if candles[-1].close else 0.0
            if price <= 0.0:
                trace["terminal_state"] = "REJECTED"
                trace["blocking_reason"] = "Invalid market data (price <= 0)"
                return trace
            trace["price"] = price

            last_df_row = df.iloc[-1]
            ema20 = last_df_row.get("EMA_21", price)
            rsi   = last_df_row.get("RSI_14", 50.0)

            momentum_checks_passed = sum([
                (price > ema20 if signal == "BUY" else price < ema20),
                (rsi > 50      if signal == "BUY" else rsi < 50),
                (volume_ratio >= 1.2),
            ])

            df_5m     = IndicatorEngine().calculate_indicators(candles)
            atr_5m_pct = (
                df_5m["ATRr_14"].iloc[-1] / price
                if not df_5m.empty and "ATRr_14" in df_5m.columns
                else 0.0
            )
            trace["atr_5m_pct"] = atr_5m_pct

            # Track ATR history for adaptive SL
            if symbol not in self._atr_history:
                self._atr_history[symbol] = []
            self._atr_history[symbol].append(atr_5m_pct)
            if len(self._atr_history[symbol]) > 90:
                self._atr_history[symbol] = self._atr_history[symbol][-90:]

            ind_norm  = min(indicator_count / 5.0, 1.0)
            mom_str   = min(momentum_checks_passed / 3.0, 1.0)
            vol_norm  = min(volume_ratio / 2.0, 1.0)

            edge_score = (ai_confidence * 0.65) + (ind_norm * 0.15) + (mom_str * 0.10) + (vol_norm * 0.10)

            # ── Re-entry penalty ──────────────────────────────────────
            import time as _time
            last_trade_ts  = self._last_trade_executed_ts.get(symbol, 0)
            reentry_window = CONFIG.get("EXECUTION", {}).get("SYMBOL_REENTRY_COOLDOWN_SEC", 1800)
            elapsed        = _time.time() - last_trade_ts
            if elapsed < reentry_window and last_trade_ts > 0:
                penalty = 0.15 * (1.0 - elapsed / reentry_window)
                edge_score -= penalty
                trace["reentry_penalty"] = round(penalty, 4)

            # ── Fear & Greed gate ─────────────────────────────────────
            fg_value = 50  # neutral default
            if self.funding_service and gates_cfg.get("FEAR_GREED_ENABLED", True):
                try:
                    fg_value = await self._tt(self.funding_service.get_fear_greed_index, timeout=8.0)
                    trace["fear_greed"] = fg_value
                    fg_extreme_fear  = gates_cfg.get("FEAR_GREED_EXTREME_FEAR", 20)
                    fg_extreme_greed = gates_cfg.get("FEAR_GREED_EXTREME_GREED", 80)
                    # Extreme greed → raise BUY bar (market likely overbought)
                    if fg_value > fg_extreme_greed and signal == "BUY":
                        edge_score *= 0.92   # 8% edge reduction
                        trace["fg_adjustment"] = "greed_penalty"
                    # Extreme fear → lower BUY bar (contrarian buy environment)
                    elif fg_value < fg_extreme_fear and signal == "BUY":
                        edge_score = min(1.0, edge_score * 1.06)  # 6% edge bonus
                        trace["fg_adjustment"] = "fear_bonus"
                except Exception as fg_e:
                    self.logger.debug("Fear & Greed gate failed: %s", fg_e)

            # ── Funding Rate gate ─────────────────────────────────────
            if self.funding_service and gates_cfg.get("FUNDING_RATE_ENABLED", True):
                try:
                    funding_rate = await self._tt(
                        self.funding_service.get_funding_rate, symbol, timeout=8.0
                    )
                    if funding_rate is not None:
                        trace["funding_rate"] = funding_rate
                        fr_long  = gates_cfg.get("FUNDING_RATE_EXTREME_LONG", 0.001)
                        fr_short = gates_cfg.get("FUNDING_RATE_EXTREME_SHORT", -0.0005)
                        if funding_rate > fr_long and signal == "BUY":
                            edge_score *= 0.70   # crowded longs → fade penalty
                            trace["funding_penalty"] = "crowded_longs"
                        elif funding_rate < fr_short and signal == "SELL":
                            edge_score *= 0.70   # crowded shorts → fade penalty
                            trace["funding_penalty"] = "crowded_shorts"
                except Exception as fr_e:
                    self.logger.debug("Funding rate gate failed for %s: %s", symbol, fr_e)

            # ── Volume Profile POC ────────────────────────────────────
            poc_price = 0.0
            if gates_cfg.get("VOLUME_PROFILE_ENABLED", True) and len(candles) >= 20:
                poc_price = _compute_volume_poc(candles[-100:] if len(candles) >= 100 else candles)
                trace["poc_price"] = poc_price
                if poc_price > 0:
                    poc_proximity = abs(price - poc_price) / poc_price
                    trace["poc_proximity_pct"] = poc_proximity
                    if poc_proximity <= gates_cfg.get("VOLUME_PROFILE_PROXIMITY_PCT", 0.005):
                        trace["near_poc"] = True   # SL will be widened in _run_cycle

            trace["edge_score"] = edge_score

            # ── Apply event-calendar adjustments ─────────────────────
            if _event_conf_boost > 0:
                trace["event_conf_boost"] = round(_event_conf_boost, 3)
                trace["event_size_scale"] = round(_event_size_scale, 3)

            # ── Dead-hours confidence gate ────────────────────────────
            if in_dead_hours and ai_confidence < dead_hour_threshold:
                trace["terminal_state"] = "REJECTED"
                trace["blocking_reason"] = (
                    f"Dead-hour filter (UTC={utc_hour:02d}:xx): "
                    f"conf {ai_confidence:.0%} < {dead_hour_threshold:.0%}"
                )
                return trace

            # ── Per-symbol confidence threshold + dynamic streak gate ────
            # Read the per-symbol confidence override from SYMBOL_OVERRIDES;
            # fall back to the global RANKING threshold (0.70).
            sym_override  = CONFIG.get("SYMBOL_OVERRIDES", {}).get(symbol, {})
            base_conf_thr = float(sym_override.get(
                "confidence",
                CONFIG.get("RANKING", {}).get("CONFIDENCE_THRESHOLD", 0.70),
            ))
            # Dynamic streak penalty: +5pp per loss after the 3rd consecutive loss
            dyn_cfg   = CONFIG.get("DYNAMIC_CONFIDENCE", {})
            dyn_boost = 0.0
            if dyn_cfg.get("ENABLED", False):
                streak  = getattr(self, "_global_loss_streak", 0)
                n_raise = max(0, streak - dyn_cfg.get("STREAK_RAISE_THRESHOLD", 3) + 1)
                if n_raise > 0:
                    dyn_boost = min(
                        n_raise * dyn_cfg.get("CONFIDENCE_BOOST", 0.05),
                        dyn_cfg.get("MAX_BOOST", 0.15),
                    )
            # Live-accuracy boost stacks on top
            dyn_boost     = min(dyn_boost + getattr(self, "_live_accuracy_boost", 0.0), 0.20)
            base_conf_thr = min(base_conf_thr + dyn_boost, 0.95)
            if dyn_boost > 0:
                trace["dyn_conf_boost"]    = round(dyn_boost, 3)
            trace["effective_conf_thr"] = round(base_conf_thr, 3)

            # ── Apply event-calendar confidence boost + size scale ────
            base_conf_thr        = min(base_conf_thr + _event_conf_boost, 0.97)
            allocation_per_trade = allocation_per_trade * _event_size_scale
            # ── Per-symbol size scale (from SYMBOL_OVERRIDES) ────────
            sym_size_scale       = float(sym_override.get("size_scale", 1.0))
            allocation_per_trade = allocation_per_trade * sym_size_scale

            # ── Pre-execution gates ───────────────────────────────────
            blocking_reason    = ""
            execution_allowed  = True

            if deployable < 5.0:
                execution_allowed, blocking_reason = False, "Balance below minimum tradable capital"
            elif current_positions >= max_positions:
                execution_allowed, blocking_reason = False, "Position limit reached"
            elif allocation_per_trade < min_safe_notional and current_positions > 0:
                execution_allowed, blocking_reason = False, "Insufficient capital for additional position"
            elif edge_score < 0.40 and ai_confidence < 0.71:
                execution_allowed, blocking_reason = (
                    False, f"Signal quality floor missed (Edge={edge_score:.2f} < 0.40)"
                )
            elif atr_5m_pct < 0.0010:
                execution_allowed, blocking_reason = False, "Volatility/regime mismatch (ATR < 0.10%)"

            # ── Correlation gate — no two r>0.75 positions simultaneously ─
            corr_cfg = CONFIG.get("CORRELATION_GATE", {})
            if corr_cfg.get("ENABLED", False) and execution_allowed:
                corr_pairs = corr_cfg.get("PAIRS", {})
                max_corr   = corr_cfg.get("MAX_CORRELATION", 0.75)
                open_syms  = {
                    s for s, v in self.portfolio_manager._exposure_by_symbol.items()
                    if v > 0
                }
                for open_sym in open_syms:
                    corr_val = (
                        corr_pairs.get((symbol, open_sym))
                        or corr_pairs.get((open_sym, symbol))
                    )
                    if corr_val and corr_val > max_corr:
                        execution_allowed = False
                        blocking_reason   = (
                            f"Correlation gate: {symbol} r={corr_val:.2f} with open {open_sym}"
                        )
                        trace["correlation_blocked"] = f"{open_sym}:{corr_val:.2f}"
                        break

            # ── Order book: spread + liquidity + imbalance ───────────
            if execution_allowed:
                try:
                    order_book = await self._tt(
                        market.exchange.fetch_order_book, symbol, limit=5, timeout=10.0
                    )
                    bids = order_book.get("bids", [])
                    asks = order_book.get("asks", [])

                    if not bids or not asks:
                        execution_allowed, blocking_reason = False, "SKIPPED: MISSING ORDER BOOK DATA"
                    else:
                        best_bid = float(bids[0][0])
                        best_ask = float(asks[0][0])
                        mid_price = (best_ask + best_bid) / 2.0

                        # Spread check
                        spread_pct = (best_ask - best_bid) / mid_price if mid_price > 0 else 0.0
                        trace["entry_spread_pct"] = spread_pct
                        if spread_pct > 0.005:
                            execution_allowed, blocking_reason = (
                                False, "SKIPPED: SPREAD EXTREME (>0.5%) — MICRO-CAP PROTECTION"
                            )
                        else:
                            # Order book imbalance → edge bonus
                            if gates_cfg.get("ORDER_BOOK_IMBALANCE_ENABLED", True):
                                bid_vol = sum(float(b[1]) for b in bids)
                                ask_vol = sum(float(a[1]) for a in asks)
                                total_vol = bid_vol + ask_vol
                                if total_vol > 0:
                                    imbalance = (bid_vol - ask_vol) / total_vol
                                    trace["ob_imbalance"] = round(imbalance, 4)
                                    imb_threshold = gates_cfg.get("ORDER_BOOK_IMBALANCE_THRESHOLD", 0.25)
                                    imb_boost     = gates_cfg.get("ORDER_BOOK_IMBALANCE_BOOST", 0.04)
                                    # Confirms BUY → bids dominate (positive imbalance)
                                    if signal == "BUY" and imbalance > imb_threshold:
                                        edge_score = min(1.0, edge_score + imb_boost)
                                        trace["imbalance_boost"] = True
                                    # Confirms SELL → asks dominate (negative imbalance)
                                    elif signal == "SELL" and imbalance < -imb_threshold:
                                        edge_score = min(1.0, edge_score + imb_boost)
                                        trace["imbalance_boost"] = True
                                    trace["edge_score"] = edge_score

                            # Precision-safe position size
                            raw_qty = allocation_per_trade / mid_price if mid_price > 0 else 0.0
                            try:
                                precise_qty = float(market.exchange.amount_to_precision(symbol, raw_qty))
                            except Exception:
                                precise_qty = raw_qty

                            if precise_qty <= 0:
                                execution_allowed, blocking_reason = False, "SKIPPED: SIZE BELOW PRECISION LIMIT"
                            else:
                                order_size = precise_qty
                                levels     = asks if signal == "BUY" else bids
                                rem_qty    = order_size
                                w_price_sum = 0.0
                                filled_qty  = 0.0

                                for px, sz in levels:
                                    if rem_qty <= 0:
                                        break
                                    px, sz  = float(px), float(sz)
                                    take_sz = min(sz, rem_qty)
                                    w_price_sum += px * take_sz
                                    filled_qty  += take_sz
                                    rem_qty     -= take_sz

                                if filled_qty < order_size:
                                    execution_allowed, blocking_reason = (
                                        False, "SKIPPED: INSUFFICIENT TOP-OF-BOOK LIQUIDITY"
                                    )
                                else:
                                    expected_fill_price = w_price_sum / filled_qty
                                    slippage_pct = abs(expected_fill_price - mid_price) / mid_price if mid_price > 0 else 0.0

                                    if slippage_pct > 0.01:
                                        execution_allowed, blocking_reason = (
                                            False, "SKIPPED: ABNORMAL SLIPPAGE ESTIMATE"
                                        )
                                    else:
                                        fee_rt      = 0.002
                                        spread_c    = spread_pct * 2
                                        total_fric  = fee_rt + spread_c + slippage_pct
                                        noise_ratio = atr_5m_pct / atr_1h_pct if atr_1h_pct > 0 else 0.0
                                        if noise_ratio > 1.8:
                                            execution_allowed, blocking_reason = (
                                                False, "SKIPPED: VOLATILITY WHIPSAW RISK"
                                            )
                                        elif atr_5m_pct <= total_fric * 1.5:
                                            # ATR is the real expected-move proxy; if ATR < 1.5× friction,
                                            # the move is too small to cover fees + spread + slippage
                                            execution_allowed, blocking_reason = (
                                                False, "SKIPPED: INSUFFICIENT EDGE — FRICTION DOMINANT"
                                            )
                except Exception as e:
                    execution_allowed, blocking_reason = (
                        False, f"SKIPPED: ORDER BOOK ANALYSIS FAILED ({e})"
                    )

            if execution_allowed:
                bull_ind = trace.get("bull_indicators", 0)
                bear_ind = trace.get("bear_indicators", 0)
                # base_conf_thr already incorporates per-symbol override + dynamic streak boost
                if ai_confidence < base_conf_thr and bull_ind < 2:
                    execution_allowed, blocking_reason = (
                        False,
                        f"Signal quality floor missed (Conf={ai_confidence:.0%} < {base_conf_thr:.0%}, Bull={bull_ind}/5)",
                    )
                elif ai_confidence >= base_conf_thr + 0.05:
                    pass   # strong signal — skip regime check
                elif market_regime == "LOW_ACTIVITY":
                    if ai_confidence < base_conf_thr:
                        execution_allowed, blocking_reason = (
                            False, "Volatility/regime mismatch (Low Activity)"
                        )
                elif market_regime == "RANGING":
                    if momentum_checks_passed < 1 or ai_confidence < max(base_conf_thr - 0.05, 0.55):
                        execution_allowed, blocking_reason = (
                            False, "Expected move mismatch (Ranging momentum failed)"
                        )
                elif market_regime == "TRENDING":
                    if ai_confidence < max(base_conf_thr - 0.10, 0.55):
                        execution_allowed, blocking_reason = (
                            False, "Signal expired / Trending threshold missed"
                        )

            if execution_allowed and not trace.get("trend_4h_aligned", True):
                if ai_confidence < 0.80:
                    execution_allowed, blocking_reason = False, "SKIP_TRADE_4H_TREND_MISALIGNED"

            if not execution_allowed:
                trace["terminal_state"] = "REJECTED"
                trace["blocking_reason"] = blocking_reason
            else:
                trace["is_valid_candidate"] = True
                trace["terminal_state"]     = "CANDIDATE"
                trace["blocking_reason"]    = "Valid Edge Score"

            return trace

        except ValueError as ve:
            # Feature alignment failures raise ValueError from Predictor._align_features.
            # These are NOT benign — they indicate a model/feature mismatch that
            # will persist every cycle.  Log at CRITICAL so it's immediately visible.
            err_str = str(ve)
            is_feat_err = any(
                k in err_str for k in ("Feature alignment", "Missing critical features", "feature_names_in_")
            )
            if is_feat_err:
                self.logger.critical(
                    "FEATURE_ALIGNMENT_FAILURE for %s — model/feature mismatch will recur: %s",
                    symbol, ve,
                )
            else:
                self.logger.error("_process_symbol ValueError for %s: %s", symbol, ve)
            trace["terminal_state"] = "FAILED"
            trace["blocking_reason"] = f"Feature/Model Error: {ve}"
            return trace

        except Exception as e:
            trace["terminal_state"] = "FAILED"
            trace["blocking_reason"] = f"Internal Exception: {e}"
            return trace

    # ──────────────────────────────────────────────────────────────────
    # Order execution
    # ──────────────────────────────────────────────────────────────────

    async def _execute_one(
        self,
        trace: dict,
        sl_price: float,
        tp_price: float,
    ) -> tuple:
        """Execute order. Returns (TerminalStatus, Reason, ExecutedPrice)."""

        # ── Paper trading mode ────────────────────────────────────────
        paper_cfg = CONFIG.get("PAPER_TRADING", {})
        is_paper  = (
            paper_cfg.get("ENABLED", False) or
            CONFIG["PROJECT"].get("MODE", "LIVE") == "PAPER"
        )
        if is_paper:
            price         = trace["price"]
            slip_bps      = paper_cfg.get("SIMULATE_SLIPPAGE_BPS", 5.0)
            simulated_fill = price * (1 + slip_bps / 10000)
            symbol        = trace["symbol"]
            paper_entry   = {
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "symbol":     symbol,
                "signal":     trace["signal"],
                "price":      price,
                "fill_price": simulated_fill,
                "sl":         sl_price,
                "tp":         tp_price,
                "edge_score": trace.get("edge_score", 0.0),
                "confidence": trace.get("confidence", 0.0),
            }
            self._paper_trades.append(paper_entry)
            # Write to CSV if configured
            if paper_cfg.get("LOG_SIMULATED_FILLS", True):
                try:
                    import csv
                    log_dir   = CONFIG.get("REPORTING", {}).get("LOG_DIR", "logs")
                    pt_file   = os.path.join(log_dir, "paper_trades.csv")
                    write_hdr = not os.path.exists(pt_file)
                    with open(pt_file, "a", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=paper_entry.keys())
                        if write_hdr:
                            writer.writeheader()
                        writer.writerow(paper_entry)
                except Exception as pt_e:
                    self.logger.warning("Paper trade log write failed: %s", pt_e)
            import time as _time
            self._last_trade_executed_ts[symbol] = _time.time()
            return ("EXECUTED", f"[PAPER] Simulated fill @ {simulated_fill:.5f}", simulated_fill)

        # ── Live execution ────────────────────────────────────────────
        try:
            open_count = await self._get_open_positions_count()

            asset_class          = trace["asset_class"]
            symbol               = trace["symbol"]
            signal               = trace["signal"]
            price                = trace["price"]
            allocation_per_trade = trace["allocation_per_trade"]

            if price <= 0:
                return ("REJECTED", "Price <= 0 violation pre-sizing", 0.0)

            # Fetch balance and executor reference OUTSIDE the lock — get_balance()
            # is a network call and must not hold _capital_lock, which serialises
            # all concurrent _execute_one coroutines when held during I/O.
            # Use _tt() to run the synchronous CCXT call in a thread pool worker.
            balance     = await self._tt(self.router.get_balance, timeout=15.0)
            crypto_exec = self.router._executors.get("CRYPTO")

            if crypto_exec and getattr(crypto_exec, "_trading_suspended", False):
                return ("BLOCKED", "Execution lock active (Time sync suspension)", 0.0)
            if balance is None or (crypto_exec and getattr(crypto_exec, "_balance_unknown", False)):
                return ("BLOCKED", "Portfolio allocation unavailable (Balance Unknown)", 0.0)

            balance = balance or 0.0

            async with self._capital_lock:

                total_equity = balance + self.portfolio_manager.get_total_exposure()
                self.risk_manager.update_balance(total_equity)
                ex_market  = (
                    crypto_exec.exchange.market(symbol)
                    if crypto_exec and crypto_exec.exchange and hasattr(crypto_exec.exchange, "markets")
                    and crypto_exec.exchange.markets else {}
                )
                min_notional = ex_market.get("limits", {}).get("cost", {}).get("min", 5.0)
                min_qty      = ex_market.get("limits", {}).get("amount", {}).get("min", 0.0)

                qty_raw = allocation_per_trade / price
                rounded_quantity = qty_raw

                try:
                    rounded_quantity = float(crypto_exec.exchange.amount_to_precision(symbol, qty_raw))
                    if rounded_quantity > qty_raw:
                        prec_val = ex_market.get("precision", {}).get("amount")
                        step = (
                            float(prec_val) if isinstance(prec_val, str)
                            else (10 ** -int(prec_val) if prec_val else 0.00001)
                        )
                        rounded_quantity -= step
                        rounded_quantity = float(crypto_exec.exchange.amount_to_precision(symbol, rounded_quantity))
                except Exception:
                    rounded_quantity = round(qty_raw - 0.000000005, 8)

                if rounded_quantity <= 0 or rounded_quantity < min_qty:
                    return ("REJECTED", "Precision rounding resulted in zero quantity", 0.0)

                notional = rounded_quantity * price
                if notional < min_notional:
                    return ("REJECTED", f"Below Binance min notional (Notional {notional:.2f} < Min {min_notional})", 0.0)

                if not self.risk_manager.check_drawdown(total_equity):
                    return ("BLOCKED", "Risk budget exceeded (Max Drawdown)", 0.0)

                max_trades = 1 if balance < 50 else 2
                if self.portfolio_manager.get_exposure_for_symbol(symbol) > 0:
                    return ("BLOCKED", "Existing position conflict", 0.0)
                if not self.risk_manager.can_open_new_trade(open_count) or open_count >= max_trades:
                    return ("BLOCKED", "Max positions reached", 0.0)

            import functools
            func   = functools.partial(self.router.open_position, asset_class, symbol, rounded_quantity, signal, sl=sl_price, tp=tp_price)
            result = await self._tt(func, timeout=30.0)
            self.logger.info("Exchange API Response [%s]: %s", symbol, json.dumps(result))

            status     = (result.get("status") or "").lower()
            filled_qty = float(result.get("filled_qty") or result.get("filled") or result.get("amount") or 0.0)

            if status not in ["open", "partial", "closed", "filled"] or filled_qty <= 0:
                err_msg = result.get("info", "Zero filled quantity")
                return ("FAILED", f"Exchange rejected order (Code: {status} - {err_msg})", 0.0)

            avg_price = float(result.get("avg_price") or result.get("price") or price)
            if avg_price <= 0:
                avg_price = price

            async with self._capital_lock:
                self.portfolio_manager.record_open(asset_class, symbol, filled_qty * avg_price, entry_price=avg_price)
                # Invalidate balance cache — capital has moved into a position.
                # Next _get_balance() call will fetch fresh from exchange.
                self._balance_cache_ts = 0.0

            try:
                self.reporter.log_trade(
                    symbol=symbol, side=signal, qty=filled_qty, price=avg_price,
                    result="OPEN", confidence=trace.get("confidence", 0.0),
                    edge_score=trace.get("edge_score", 0.0),
                    reason="TDA Native AI Execution",
                    risk_tier="MICRO-CAP" if trace.get("usable_capital", 0.0) < 100 else "STANDARD",
                )
            except Exception as e:
                self.logger.error("Reporter log_trade failed: %s", e)

            # SL verification
            try:
                has_sl = False
                for _ in range(3):
                    open_orders = await self._tt(
                        self.router.get_executor(asset_class).exchange.fetch_open_orders, symbol,
                        timeout=10.0,
                    )
                    for o in open_orders:
                        o_type   = o.get("type", "").lower()
                        params   = o.get("info", {})
                        is_reduce = o.get("reduceOnly", False) or params.get("reduceOnly", False) or params.get("closePosition", False)
                        is_stop   = o_type in ["stop_market", "stop", "stop_loss", "stop_loss_limit"]
                        if is_reduce or is_stop or o.get("stopPrice"):
                            has_sl = True
                            break
                    if has_sl:
                        break
                    await asyncio.sleep(0.4)

                if not has_sl:
                    self.logger.critical("SL VERIFICATION FAILED FOR %s! Emergency exit.", symbol)
                    close_res = await self._tt(self.router.close_position, asset_class, symbol, timeout=30.0)
                    if close_res.get("status") == "filled":
                        async with self._capital_lock:
                            self.portfolio_manager.record_close(symbol, sector=asset_class)
                        return ("FAILED", "SL UNPROTECTED — POSITION FORCE-CLOSED", 0.0)
                    else:
                        self.telegram.send_message(f"🚨 GHOST POSITION RISK: Failed to emergency-close {symbol}", severity="CRITICAL")
                        return ("FAILED", "SL UNPROTECTED — EMERGENCY CLOSE FAILED (GHOST RISK)", 0.0)
            except Exception as sl_e:
                self.logger.critical("SL VERIFICATION CRASHED: %s", sl_e)
                try:
                    close_res = await self._tt(self.router.close_position, asset_class, symbol, timeout=30.0)
                    if close_res.get("status") == "filled":
                        async with self._capital_lock:
                            self.portfolio_manager.record_close(symbol, sector=asset_class)
                    else:
                        self.telegram.send_message(f"🚨 GHOST POSITION RISK: Crash during emergency-close {symbol}", severity="CRITICAL")
                except Exception as close_e:
                    self.logger.critical("Crash during emergency close for %s: %s", symbol, close_e)
                    self.telegram.send_message(f"🚨 GHOST POSITION RISK: Crash during emergency-close {symbol}: {close_e}", severity="CRITICAL")
                return ("FAILED", "SL UNPROTECTED (VERIFICATION CRASH) — POSITION GHOST/CLOSED", 0.0)

            import time as _time
            self._last_trade_executed_ts[symbol] = _time.time()
            return ("EXECUTED", f"Position opened at {avg_price:.5f} (Filled: {filled_qty})", avg_price)

        except Exception as e:
            import traceback
            tb = traceback.format_exc().replace("\\n", " ")
            self.logger.critical("CRITICAL ENGINE CRASH in _execute_one [%s]: %s | %s", trace.get("symbol"), e, tb)
            return ("FAILED", f"Internal Engine Exception: {repr(e)}", 0.0)

    # ──────────────────────────────────────────────────────────────────
    # Safe order fetch helper
    # ──────────────────────────────────────────────────────────────────

    async def _safe_fetch_open_orders(self, exchange, symbol: str):
        try:
            return await self._tt(exchange.fetch_open_orders, symbol, timeout=10.0)
        except Exception as e:
            err_str = str(e)
            if "Timestamp" in err_str or "recvWindow" in err_str:
                crypto_exec = self.router._executors.get("CRYPTO")
                if crypto_exec and hasattr(crypto_exec, "sync_time"):
                    await crypto_exec.sync_time()
                return await self._tt(exchange.fetch_open_orders, symbol, timeout=10.0)
            raise

    # ──────────────────────────────────────────────────────────────────
    # Reconciliation loop
    # ──────────────────────────────────────────────────────────────────

    async def _reconciliation_loop(self) -> None:
        import random
        await asyncio.sleep(10)
        self._api_failure_timestamps = []
        while getattr(self, "_running", True):
            try:
                crypto_exec = self.router._executors.get("CRYPTO")
                if not crypto_exec:
                    await asyncio.sleep(45)
                    continue

                is_spot = crypto_exec.exchange.options.get("defaultType") != "future"
                positions = []
                if not is_spot:
                    positions = await self._tt(crypto_exec.exchange.fetch_positions, timeout=15.0)
                else:
                    for sym, ival in list(self.portfolio_manager._exposure_by_symbol.items()):
                        if ival > 0:
                            try:
                                open_orders = await self._safe_fetch_open_orders(crypto_exec.exchange, sym)
                                if open_orders:
                                    qty_sum = max([float(o.get("amount") or 0.0) for o in open_orders] + [0.0])
                                    ticker  = await self._tt(crypto_exec.exchange.fetch_ticker, sym, timeout=10.0)
                                    price   = float(ticker.get("last", 0.0))
                                    positions.append({"symbol": sym, "contracts": qty_sum, "markPrice": price})
                            except Exception as e:
                                self.logger.error("Spot reconciliation order fetch failed for %s: %s", sym, e)

                async with self._capital_lock:
                    active_ext_symbols = []
                    for p in positions:
                        sym = p.get("symbol")
                        if not sym:
                            continue
                        qty = float(p.get("contracts", 0) or p.get("contractSize", 0) or 0)
                        if qty > 0:
                            active_ext_symbols.append(sym)
                            internal_val = self.portfolio_manager.get_exposure_for_symbol(sym)
                            price_p      = float(p.get("markPrice") or p.get("entryPrice") or 0)
                            ext_val      = qty * price_p

                            if internal_val <= 0:
                                open_orders = await self._safe_fetch_open_orders(crypto_exec.exchange, sym)
                                has_sl = any(
                                    (o.get("reduceOnly") or o.get("info", {}).get("reduceOnly") or
                                     o.get("type", "").lower() in ["stop_market", "stop", "stop_loss", "stop_loss_limit"] or
                                     o.get("stopPrice"))
                                    for o in open_orders
                                )
                                if not has_sl:
                                    self.logger.critical("NAKED EXPOSURE IMPORTED: %s has NO SL! Sweeping.", sym)
                                    self.telegram.send_message(f"NAKED EXPOSURE: {sym}. Initiating sweep...", severity="CRITICAL")
                                    try:
                                        sweep_res = await self._tt(self.router.close_position, "CRYPTO", sym, timeout=30.0)
                                        if sweep_res.get("status") == "filled":
                                            self.logger.info("SWEEP SUCCESS: %s liquidated.", sym)
                                        else:
                                            self.telegram.send_message(f"🚨 FATAL: Sweep failed for {sym}.", severity="CRITICAL")
                                    except Exception as sweep_e:
                                        self.telegram.send_message(f"🚨 FATAL: Sweep crashed for {sym}!", severity="CRITICAL")
                                    self._emergency_trading_halt = True
                                    self._halt_reason = "SWEEP_PROTOCOL"
                                    self.telegram.send_message("ENGINE HALTED — SWEEP PROTOCOL. Manual restart required.", severity="CRITICAL")
                                self.portfolio_manager.record_open("CRYPTO", sym, ext_val)
                                self.logger.warning("RECOVERED_EXTERNAL_POSITION: %s imported.", sym)
                            elif abs(internal_val - ext_val) > (ext_val * 0.1):
                                open_orders = await self._safe_fetch_open_orders(crypto_exec.exchange, sym)
                                sl_qty = next(
                                    (float(o.get("amount") or 0.0) for o in open_orders
                                     if o.get("reduceOnly") or o.get("stopPrice") or
                                     o.get("type", "").lower() in ["stop_market", "stop_loss"]),
                                    0.0
                                )
                                if sl_qty > 0 and abs(sl_qty - qty) > (qty * 0.05):
                                    self.logger.warning("SL SIZE MISMATCH: %s pos=%s sl=%s", sym, qty, sl_qty)

                    # Case B: internal exists, exchange missing
                    for isym, ival in list(self.portfolio_manager._exposure_by_symbol.items()):
                        if isym not in active_ext_symbols and ival > 0:
                            if CONFIG["PROJECT"].get("ACCOUNT_MODE", "SPOT") == "SPOT":
                                self.logger.info("OCO_EXIT_TRIGGERED: %s removed.", isym)
                            else:
                                self.logger.warning("POSITION_CLOSED_EXTERNALLY: %s removed.", isym)
                            try:
                                closed_orders = await self._tt(crypto_exec.exchange.fetch_closed_orders, isym, timeout=15.0)
                                if closed_orders:
                                    last_close    = closed_orders[-1]
                                    closing_price = float(last_close.get("average") or last_close.get("price") or 0.0)
                                    entry_price   = self.portfolio_manager.get_entry_price(isym)
                                    if entry_price and closing_price > 0:
                                        # Determine win/loss correctly for both long and short positions
                                        _trade_side = self._trade_context.get(isym, {}).get("signal", "BUY").upper()
                                        is_win       = (
                                            closing_price > entry_price if _trade_side != "SELL"
                                            else closing_price < entry_price
                                        )
                                        qty_derived      = ival / entry_price
                                        realized_pnl     = (closing_price - entry_price) * qty_derived
                                        if _trade_side == "SELL":
                                            realized_pnl = -realized_pnl  # short: profit when price falls
                                        self.risk_manager.record_realized_pnl(is_win)

                                        # ── Symbol ban: consecutive-loss tracker ─────────────
                                        ban_cfg = CONFIG.get("SYMBOL_BAN", {})
                                        if ban_cfg.get("ENABLED", True):
                                            if is_win:
                                                self._symbol_loss_streak[isym] = 0
                                                self._symbol_ban_until[isym] = 0
                                            else:
                                                # Mark hard re-entry block timestamp
                                                import time as _slt
                                                self._last_stop_loss_ts[isym] = _slt.time()
                                                streak = self._symbol_loss_streak.get(isym, 0) + 1
                                                self._symbol_loss_streak[isym] = streak
                                                threshold = ban_cfg.get("LOSS_THRESHOLD", 3)
                                                if streak >= threshold:
                                                    ban_sec = ban_cfg.get("BAN_DURATION_SEC", 14400)
                                                    self._symbol_ban_until[isym] = _slt.time() + ban_sec
                                                    self.logger.warning(
                                                        "SYMBOL_BAN: %s banned for %dh after %d consecutive losses.",
                                                        isym, ban_sec // 3600, streak,
                                                    )
                                                    self.telegram.send_message(
                                                        f"🚫 *Symbol Banned*\n"
                                                        f"{isym} — {streak} consecutive losses.\n"
                                                        f"New entries blocked for {ban_sec // 3600}h.",
                                                        severity="WARNING",
                                                    )

                                        # ── Dynamic confidence: update global loss streak ──────
                                        dyn_cfg = CONFIG.get("DYNAMIC_CONFIDENCE", {})
                                        if dyn_cfg.get("ENABLED", False):
                                            if is_win and dyn_cfg.get("RESET_ON_WIN", True):
                                                if self._global_loss_streak > 0:
                                                    self.logger.info(
                                                        "DYN_CONF: Win after %d-loss streak — threshold reset.",
                                                        self._global_loss_streak,
                                                    )
                                                self._global_loss_streak = 0
                                            elif not is_win:
                                                self._global_loss_streak += 1
                                                self.logger.info(
                                                    "DYN_CONF: Loss streak now %d (threshold raised at ≥%d).",
                                                    self._global_loss_streak,
                                                    dyn_cfg.get("STREAK_RAISE_THRESHOLD", 3),
                                                )

                                        # ── Live accuracy: rolling win-rate monitor ───────────
                                        acc_cfg = CONFIG.get("LIVE_ACCURACY", {})
                                        if acc_cfg.get("ENABLED", False):
                                            self._live_trade_results.append(is_win)
                                            window = acc_cfg.get("WINDOW", 20)
                                            if len(self._live_trade_results) > window:
                                                self._live_trade_results = self._live_trade_results[-window:]
                                            n_res = len(self._live_trade_results)
                                            if n_res >= 5:
                                                win_rate   = sum(self._live_trade_results) / n_res
                                                warn_thr   = acc_cfg.get("WARN_BELOW", 0.45)
                                                raise_thr  = acc_cfg.get("RAISE_THRESHOLD_BELOW", 0.40)
                                                boost_amt  = acc_cfg.get("THRESHOLD_BOOST", 0.05)
                                                if win_rate < raise_thr:
                                                    if self._live_accuracy_boost != boost_amt:
                                                        self._live_accuracy_boost = boost_amt
                                                        self.telegram.send_message(
                                                            f"⚠️ *ACCURACY ALERT*\n"
                                                            f"Last {n_res} trades WR={win_rate:.0%} < {raise_thr:.0%}\n"
                                                            f"Auto-raising confidence threshold +{boost_amt:.0%}.",
                                                            severity="WARNING",
                                                        )
                                                elif win_rate < warn_thr:
                                                    self.telegram.send_message(
                                                        f"⚠️ *Win-Rate Warning*\n"
                                                        f"Last {n_res} trades WR={win_rate:.0%} — monitor closely.",
                                                        severity="WARNING",
                                                    )
                                                else:
                                                    if self._live_accuracy_boost > 0.0:
                                                        self._live_accuracy_boost = 0.0
                                                        self.telegram.send_message(
                                                            f"✅ Win rate recovered: WR={win_rate:.0%} — "
                                                            f"confidence boost removed.",
                                                            severity="INFO",
                                                        )

                                        # ── Alpha decay: record outcome for each TA strategy ──
                                        if self.alpha_decay:
                                            ctx_strat = self._trade_context.get(isym, {})
                                            for sname in ctx_strat.get("strategies_voted", []):
                                                try:
                                                    self.alpha_decay.record_outcome(sname, is_win)
                                                except Exception:
                                                    pass

                                        self.logger.info("Realized PnL %s: Win=%s Entry=%.5f Close=%.5f PnL=$%.2f",
                                                         isym, is_win, entry_price, closing_price, realized_pnl)
                                        ctx = self._trade_context.pop(isym, {})
                                        self.logger.info("TRADE_DIAGNOSTICS: %s expected_RR=%.2f effective_RR=%.2f",
                                                         isym, ctx.get("expected_rr", 0), ctx.get("effective_rr", 0))
                                        try:
                                            self.reporter.log_trade(
                                                symbol=isym, side="SELL", qty=qty_derived,
                                                price=closing_price, result="CLOSE",
                                                pnl=realized_pnl, reason="Stop-Loss / Take-Profit (Spot OCO)",
                                            )
                                        except Exception as re:
                                            self.logger.error("Reporter close log failed: %s", re)
                            except Exception as e:
                                self.logger.error("Failed to extract close details for PnL: %s", e)
                            self.portfolio_manager.record_close(isym, sector="CRYPTO")

                # Age-out API failure timestamps naturally (do NOT wipe the list — that
                # would let a single successful cycle immediately lift a 5-failure halt)
                _now_rec = time.time()
                if not hasattr(self, "_api_failure_timestamps"):
                    self._api_failure_timestamps = []
                self._api_failure_timestamps = [
                    ts for ts in self._api_failure_timestamps if _now_rec - ts < 300
                ]

                if getattr(self, "_emergency_trading_halt", False):
                    halt_rsn = getattr(self, "_halt_reason", "")
                    if halt_rsn == "SWEEP_PROTOCOL":
                        self.logger.warning("HALT still active (SWEEP_PROTOCOL) — requires manual restart.")
                    elif halt_rsn == "API_FAILURE":
                        # Only lift the API_FAILURE halt once the 5-minute failure
                        # window has fully expired — a single successful cycle is
                        # not enough evidence that the exchange has stabilised.
                        if not self._api_failure_timestamps:
                            self._emergency_trading_halt = False
                            self._halt_reason = ""
                            self.telegram.send_message(
                                "✅ HALT CLEARED — API stability restored (failure window expired)",
                                severity="INFO",
                            )
                        else:
                            self.logger.warning(
                                "HALT active (API_FAILURE) — %d failures still within 5-min window",
                                len(self._api_failure_timestamps),
                            )
                    else:
                        self._emergency_trading_halt = False
                        self._halt_reason = ""
                        self.telegram.send_message("HALT CLEARED — NORMAL OPERATIONS RESUMED", severity="INFO")

            except Exception as e:
                import time as _t
                now = _t.time()
                if not hasattr(self, "_api_failure_timestamps"):
                    self._api_failure_timestamps = []
                self._api_failure_timestamps.append(now)
                self._api_failure_timestamps = [ts for ts in self._api_failure_timestamps if now - ts < 300]
                self.logger.error("Reconciliation daemon failure (%d/5 in 5m): %s",
                                  len(self._api_failure_timestamps), e)
                if len(self._api_failure_timestamps) >= 5 and not getattr(self, "_emergency_trading_halt", False):
                    self._emergency_trading_halt = True
                    self._halt_reason = "API_FAILURE"
                    self.telegram.send_message("GLOBAL HALT — EXCHANGE UNSTABLE", severity="CRITICAL")

            await asyncio.sleep(45 + random.randint(0, 15))

    # ──────────────────────────────────────────────────────────────────
    # Capital Rotation Engine
    # ──────────────────────────────────────────────────────────────────

    async def _attempt_capital_rotation(
        self,
        incoming_candidates: list,
        balance: float,
    ) -> int:
        """
        Capital Rotation Engine.

        Evaluates whether an existing open position should be closed early
        to free a slot for a significantly better new opportunity.

        Rules (all must pass):
          1. Rotation is enabled in CONFIG["CAPITAL_ROTATION"]
          2. All position slots are currently full
          3. The open position has been held for >= MIN_HOLD_BARS candles
          4. The open position is NOT in a loss deeper than MAX_LOSS_TO_ROTATE
             (let the SL handle deep losses — don't panic-close them early)
          5. The incoming signal's edge_score exceeds the open position's
             entry edge_score by >= MIN_EDGE_ADVANTAGE
          6. The incoming signal's edge_score is >= MIN_INCOMING_EDGE

        Returns the number of positions closed (0 or 1 per call).
        """
        rot_cfg = CONFIG.get("CAPITAL_ROTATION", {})
        if not rot_cfg.get("ENABLED", False):
            return 0

        min_hold_bars  = rot_cfg.get("MIN_HOLD_BARS",    24)
        min_edge_adv   = rot_cfg.get("MIN_EDGE_ADVANTAGE", 0.10)
        max_loss_pct   = rot_cfg.get("MAX_LOSS_TO_ROTATE",  0.003)
        min_incoming   = rot_cfg.get("MIN_INCOMING_EDGE",   0.72)

        try:
            crypto_exec = self.router._executors.get("CRYPTO")
            if not crypto_exec or not crypto_exec.exchange:
                return 0

            # Current open positions
            open_symbols: Dict[str, float] = {
                sym: val
                for sym, val in self.portfolio_manager._exposure_by_symbol.items()
                if val > 0
            }
            if not open_symbols:
                return 0

            # Only rotate when we're genuinely at capacity
            max_trades = 1 if balance < 50 else 2
            if len(open_symbols) < max_trades:
                return 0

            # ── Find incoming signals strong enough to trigger rotation ──
            strong_incoming = sorted(
                [t for t in incoming_candidates
                 if t.get("terminal_state") == "CANDIDATE"
                 and float(t.get("edge_score", 0)) >= min_incoming],
                key=lambda t: float(t.get("edge_score", 0)),
                reverse=True,
            )
            if not strong_incoming:
                return 0

            best_in       = strong_incoming[0]
            best_in_edge  = float(best_in.get("edge_score", 0))
            best_in_sym   = best_in.get("symbol", "")

            # ── Evaluate every open position as a rotation candidate ──
            now              = time.time()
            candle_sec       = 5 * 60           # 5-minute candles
            min_hold_sec     = min_hold_bars * candle_sec
            rotation_targets = []

            for sym, exposure_val in open_symbols.items():
                if sym == best_in_sym:
                    continue   # never rotate out of the same symbol we want in

                ctx         = getattr(self, "_trade_context", {}).get(sym, {})
                entry_time  = ctx.get("entry_time", now)      # default=now → won't qualify
                entry_edge  = float(ctx.get("entry_edge_score", 0.5))
                entry_price = self.portfolio_manager.get_entry_price(sym)

                # Must have held long enough
                if (now - entry_time) < min_hold_sec:
                    self.logger.debug(
                        "ROTATION_SKIP: %s held only %dm (min %dm)",
                        sym, int((now - entry_time) / 60), int(min_hold_sec / 60),
                    )
                    continue

                # Fetch live price to check current PnL
                pnl_pct = 0.0
                if entry_price and entry_price > 0:
                    try:
                        ticker = await self._tt(
                            crypto_exec.exchange.fetch_ticker, sym, timeout=10.0
                        )
                        cur_price = float(ticker.get("last", 0))
                        if cur_price > 0:
                            pnl_pct = (cur_price - entry_price) / entry_price
                    except Exception:
                        pass

                # Never rotate out of a position that is deeply under water —
                # the SL is already going to handle it; early closing would
                # realise a loss AND break the R:R discipline.
                if pnl_pct < -max_loss_pct:
                    self.logger.debug(
                        "ROTATION_SKIP: %s is %.2f%% down (threshold −%.2f%%)",
                        sym, pnl_pct * 100, max_loss_pct * 100,
                    )
                    continue

                # Check edge advantage
                edge_adv = best_in_edge - entry_edge
                if edge_adv < min_edge_adv:
                    self.logger.debug(
                        "ROTATION_SKIP: %s edge_adv=%.3f < min=%.3f",
                        sym, edge_adv, min_edge_adv,
                    )
                    continue

                rotation_targets.append({
                    "symbol":      sym,
                    "exposure":    exposure_val,
                    "entry_edge":  entry_edge,
                    "entry_price": entry_price or 0.0,
                    "pnl_pct":     pnl_pct,
                    "hold_min":    int((now - entry_time) / 60),
                    "edge_adv":    edge_adv,
                })

            if not rotation_targets:
                return 0

            # Pick the position with the largest edge advantage (clearest swap)
            rotation_targets.sort(key=lambda x: x["edge_adv"], reverse=True)
            target = rotation_targets[0]
            sym_close    = target["symbol"]
            closed_exp   = target["exposure"]
            entry_edge_c = target["entry_edge"]
            entry_price_c = target["entry_price"]
            hold_min_c   = target["hold_min"]
            pnl_pct_c    = target["pnl_pct"]
            edge_adv_c   = target["edge_adv"]

            self.logger.info(
                "CAPITAL_ROTATION: Replacing %s (edge=%.3f, held=%dm, pnl=%.2f%%) "
                "with %s (edge=%.3f, advantage=+%.3f)",
                sym_close, entry_edge_c, hold_min_c, pnl_pct_c * 100,
                best_in_sym, best_in_edge, edge_adv_c,
            )

            # ── Close the weaker position ──────────────────────────────
            close_res = await self._tt(
                self.router.close_position, "CRYPTO", sym_close, timeout=30.0
            )

            if close_res.get("status") not in ["filled", "closed"]:
                self.logger.warning(
                    "CAPITAL_ROTATION: Close of %s returned status=%s — aborting rotation",
                    sym_close, close_res.get("status"),
                )
                return 0

            # ── Record the close ───────────────────────────────────────
            async with self._capital_lock:
                closing_price = float(
                    close_res.get("average") or close_res.get("price") or entry_price_c
                )
                qty_approx = (closed_exp / entry_price_c) if entry_price_c > 0 else 0.0
                realized_pnl = (closing_price - entry_price_c) * qty_approx if qty_approx > 0 else 0.0
                is_win = realized_pnl >= 0

                self.risk_manager.record_realized_pnl(is_win)
                try:
                    self.reporter.log_trade(
                        symbol=sym_close, side="SELL",
                        qty=qty_approx, price=closing_price,
                        result="CLOSE", pnl=realized_pnl,
                        reason=(
                            f"Capital rotation → {best_in_sym} "
                            f"(edge +{edge_adv_c:.3f})"
                        ),
                    )
                except Exception as log_e:
                    self.logger.error("ROTATION: reporter log failed: %s", log_e)

                self.portfolio_manager.record_close(sym_close, sector="CRYPTO")
                self._trade_context.pop(sym_close, None)

                # Clean up trailing-stop tier
                if hasattr(self, "_trail_tiers") and sym_close in self._trail_tiers:
                    del self._trail_tiers[sym_close]

            # ── Telegram notification ──────────────────────────────────
            pnl_str  = f"${realized_pnl:+.4f}" if qty_approx > 0 else ""
            pnl_icon = "✅" if is_win else "🔻"
            self.telegram.send_message(
                f"🔄 *CAPITAL ROTATION*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔴 Closed:  *{sym_close}*\n"
                f"   Held {hold_min_c}m · Entry edge {entry_edge_c:.2f} · {pnl_icon} PnL {pnl_str}\n"
                f"\n"
                f"🟢 Opening: *{best_in_sym}*\n"
                f"   Edge {best_in_edge:.2f} · Advantage +{edge_adv_c:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Reason: Better signal detected — capital reallocated.",
                severity="INFO",
            )
            return 1

        except Exception as e:
            self.logger.error("CAPITAL_ROTATION: Unexpected error: %s", e)
            return 0

    # ──────────────────────────────────────────────────────────────────
    # Partial Take-Profit loop — scale-out at 1.5R, let rest ride
    # ──────────────────────────────────────────────────────────────────

    async def _partial_tp_loop(self) -> None:
        """
        Poll open positions; when price reaches TP1 (entry + 1.5R):
          1. Market-sell SCALE_OUT_PCT of the position to bank partial profit.
          2. Cancel existing OCO, re-place with SL=breakeven and TP=TP2 (3.5R).
        State is tracked in self._ptp_state {symbol: "WATCHING"|"TP1_HIT"}.
        """
        ptp_cfg = CONFIG.get("PARTIAL_TP", {})
        if not ptp_cfg.get("ENABLED", False):
            self.logger.info("PARTIAL_TP disabled — scale-out loop not started")
            return

        poll_sec = ptp_cfg.get("POLL_SEC",      60)
        tp1_r    = ptp_cfg.get("TP1_R_MULTIPLE", 1.5)
        tp2_r    = ptp_cfg.get("TP2_R_MULTIPLE", 3.5)
        scale    = ptp_cfg.get("SCALE_OUT_PCT",  0.50)
        move_sl  = ptp_cfg.get("MOVE_SL_TO_BE",  True)

        await asyncio.sleep(45)
        self.logger.info(
            "PARTIAL_TP_LOOP started | poll=%ds | TP1=%.1fR | TP2=%.1fR | scale=%.0f%%",
            poll_sec, tp1_r, tp2_r, scale * 100,
        )

        while getattr(self, "_running", True):
            try:
                crypto_exec = self.router._executors.get("CRYPTO")
                if not crypto_exec or not crypto_exec.exchange:
                    await asyncio.sleep(poll_sec)
                    continue

                tracked = {
                    sym: val
                    for sym, val in list(self.portfolio_manager._exposure_by_symbol.items())
                    if val > 0
                }

                # Clean up state for closed positions
                for sym in list(self._ptp_state.keys()):
                    if sym not in tracked:
                        del self._ptp_state[sym]

                for sym, exposure_val in tracked.items():
                    try:
                        # Already scaled out — trailing stop handles the rest
                        if self._ptp_state.get(sym) == "TP1_HIT":
                            continue

                        entry_price = self.portfolio_manager.get_entry_price(sym)
                        if not entry_price or entry_price <= 0:
                            continue

                        ctx          = getattr(self, "_trade_context", {}).get(sym, {})
                        sl_pct       = ctx.get("stop_loss_pct", 0.004)
                        initial_risk = entry_price * sl_pct       # 1R in price terms
                        if initial_risk <= 0:
                            continue

                        tp1_price = entry_price + (initial_risk * tp1_r)

                        ticker        = await self._tt(crypto_exec.exchange.fetch_ticker, sym, timeout=10.0)
                        current_price = float(ticker.get("last", 0))
                        if current_price <= 0 or current_price < tp1_price:
                            self._ptp_state.setdefault(sym, "WATCHING")
                            continue

                        # ── TP1 reached: scale out ─────────────────────────
                        self.logger.info(
                            "PARTIAL_TP: %s touched TP1=%.5f (cur=%.5f) — scaling out %.0f%%",
                            sym, tp1_price, current_price, scale * 100,
                        )

                        qty_full  = exposure_val / entry_price
                        qty_close = qty_full * scale
                        try:
                            qty_close = float(
                                crypto_exec.exchange.amount_to_precision(sym, qty_close)
                            )
                        except Exception:
                            qty_close = round(qty_close, 6)

                        if qty_close <= 0:
                            continue

                        sell_res    = await self._tt(
                            crypto_exec.exchange.create_market_sell_order, sym, qty_close,
                            timeout=25.0,
                        )
                        sell_price  = float(
                            sell_res.get("average") or sell_res.get("price") or current_price
                        )
                        partial_pnl = (sell_price - entry_price) * qty_close

                        self._ptp_state[sym] = "TP1_HIT"

                        # Update internal exposure (use actual sell_price, not entry_price)
                        async with self._capital_lock:
                            sold_notional = qty_close * sell_price
                            remaining_val = max(0.0, exposure_val - sold_notional)
                            if remaining_val > 0:
                                self.portfolio_manager._exposure_by_symbol[sym] = remaining_val
                            else:
                                self.portfolio_manager.record_close(sym, sector="CRYPTO")
                                self._ptp_state.pop(sym, None)
                                remaining_val = 0.0

                        # ── Replace OCO: SL=breakeven, TP=TP2 ─────────────
                        if move_sl and remaining_val > 0:
                            try:
                                open_orders = await self._safe_fetch_open_orders(
                                    crypto_exec.exchange, sym
                                )
                                for o in open_orders:
                                    try:
                                        await self._tt(
                                            crypto_exec.exchange.cancel_order, o["id"], sym,
                                            timeout=10.0,
                                        )
                                    except Exception:
                                        pass

                                qty_rem = remaining_val / entry_price
                                try:
                                    qty_rem = float(
                                        crypto_exec.exchange.amount_to_precision(sym, qty_rem)
                                    )
                                except Exception:
                                    qty_rem = round(qty_rem, 6)

                                # SL = breakeven + 0.1% buffer so round-trip fees are covered
                                be_sl   = entry_price * 1.001
                                tp2_prc = entry_price + (initial_risk * tp2_r)

                                be_sl_str  = crypto_exec._price_to_precision(sym, be_sl)
                                tp2_str    = crypto_exec._price_to_precision(sym, tp2_prc)
                                qty_str    = crypto_exec._amount_to_precision(sym, qty_rem)

                                success = await self._tt(
                                    crypto_exec._place_sl_tp,
                                    sym, "buy", qty_str, be_sl_str, tp2_str,
                                    timeout=20.0,
                                )
                                if success:
                                    self.logger.info(
                                        "PARTIAL_TP: %s new OCO | SL=BE(%s) TP2=%s",
                                        sym, be_sl_str, tp2_str,
                                    )
                                else:
                                    self.logger.error("PARTIAL_TP: OCO placement failed for %s", sym)
                            except Exception as oco_e:
                                self.logger.error("PARTIAL_TP: OCO update failed for %s: %s", sym, oco_e)

                        # ── Telegram notification ──────────────────────────
                        self.telegram.send_message(
                            f"📊 *PARTIAL TP TRIGGERED*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"Symbol: *{sym}*\n"
                            f"Scaled out {scale:.0%} @ {sell_price:.5f}\n"
                            f"Partial PnL: ${partial_pnl:+.4f}\n"
                            + (f"🔐 SL moved to breakeven\n" if move_sl else "")
                            + f"Remainder riding to TP2 ({tp2_r:.1f}R)",
                            severity="INFO",
                        )

                        try:
                            self.reporter.log_trade(
                                symbol=sym, side="SELL",
                                qty=qty_close, price=sell_price,
                                result="PARTIAL_CLOSE",
                                pnl=partial_pnl,
                                reason=f"Partial TP1 scale-out ({scale:.0%} @ {tp1_r:.1f}R)",
                            )
                        except Exception as rlog_e:
                            self.logger.error("PARTIAL_TP: reporter log failed: %s", rlog_e)

                    except Exception as sym_e:
                        self.logger.debug("PARTIAL_TP: sym error [%s]: %s", sym, sym_e)

            except asyncio.CancelledError:
                break
            except Exception as loop_e:
                self.logger.error("PARTIAL_TP_LOOP error: %s", loop_e)

            await asyncio.sleep(poll_sec)

    # Trailing stop loop
    # ──────────────────────────────────────────────────────────────────

    async def _trailing_stop_loop(self) -> None:
        stops_cfg = CONFIG.get("STOPS", {})
        if not stops_cfg.get("TRAILING_STOP", False):
            self.logger.info("TRAILING_STOP disabled — trailing loop not started")
            return

        trail_poll_sec = stops_cfg.get("TRAIL_POLL_SEC", 45)
        trail_after_r  = stops_cfg.get("TRAIL_AFTER_R", 1.0)

        if not hasattr(self, "_trail_tiers"):
            self._trail_tiers = {}

        await asyncio.sleep(30)
        self.logger.info("TRAILING_STOP_LOOP started | poll=%ds | activation=%.1fR", trail_poll_sec, trail_after_r)

        while getattr(self, "_running", True):
            try:
                crypto_exec = self.router._executors.get("CRYPTO")
                if not crypto_exec or not crypto_exec.exchange:
                    await asyncio.sleep(trail_poll_sec)
                    continue

                tracked_symbols = {
                    sym: val for sym, val in
                    list(self.portfolio_manager._exposure_by_symbol.items())
                    if val > 0
                }

                for sym, exposure_val in tracked_symbols.items():
                    try:
                        entry = self.portfolio_manager.get_entry_price(sym)
                        if not entry or entry <= 0:
                            continue
                        ctx          = getattr(self, "_trade_context", {}).get(sym, {})
                        stop_loss_pct = ctx.get("stop_loss_pct", 0.004)
                        initial_risk  = entry * stop_loss_pct
                        if initial_risk <= 0:
                            continue

                        ticker        = await self._tt(crypto_exec.exchange.fetch_ticker, sym, timeout=10.0)
                        current_price = float(ticker.get("last", 0))
                        if current_price <= 0:
                            continue

                        profit    = current_price - entry
                        r_multiple = profit / initial_risk

                        new_tier, new_sl = 0, None
                        if r_multiple >= 3.0:
                            new_tier, new_sl = 3, entry + 2.0 * initial_risk
                        elif r_multiple >= 2.0:
                            new_tier, new_sl = 2, entry + 1.0 * initial_risk
                        elif r_multiple >= trail_after_r:
                            new_tier, new_sl = 1, entry

                        if new_sl is None or new_tier <= 0:
                            continue
                        if new_tier <= self._trail_tiers.get(sym, 0):
                            continue

                        open_orders = await self._safe_fetch_open_orders(crypto_exec.exchange, sym)
                        if not open_orders:
                            continue

                        for o in open_orders:
                            try:
                                await self._tt(crypto_exec.exchange.cancel_order, o["id"], sym, timeout=10.0)
                            except Exception:
                                pass

                        order_qty = max([float(o.get("amount") or 0) for o in open_orders] + [0.0])
                        if order_qty <= 0:
                            continue

                        tp_pct    = ctx.get("take_profit_pct", 0.012)
                        tp_price  = entry + (entry * tp_pct)
                        new_sl_pr = crypto_exec._price_to_precision(sym, new_sl)
                        tp_pr     = crypto_exec._price_to_precision(sym, tp_price)
                        qty_pr    = crypto_exec._amount_to_precision(sym, order_qty)

                        success = await self._tt(
                            crypto_exec._place_sl_tp, sym, "buy", qty_pr, new_sl_pr, tp_pr,
                            timeout=20.0,
                        )
                        if success:
                            self._trail_tiers[sym] = new_tier
                            self.logger.info("TRAIL_SL_MOVED | %s | tier=%d | new_sl=%.5f | R=%.2f",
                                             sym, new_tier, new_sl, r_multiple)
                        else:
                            self.logger.error("TRAIL_SL_PLACEMENT_FAILED | %s", sym)

                    except Exception as sym_e:
                        self.logger.debug("TRAIL: error %s: %s", sym, sym_e)

                closed = [s for s in self._trail_tiers if s not in tracked_symbols]
                for s in closed:
                    del self._trail_tiers[s]

            except asyncio.CancelledError:
                break
            except Exception as loop_e:
                self.logger.error("TRAILING_STOP_LOOP error: %s", loop_e)

            await asyncio.sleep(trail_poll_sec)

    # ──────────────────────────────────────────────────────────────────
    # Main cycle
    # ──────────────────────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        cycle_trace_id = f"TDA-{int(time.time())}"

        # Flush shared indicator cache at cycle start so every symbol gets
        # fresh candle data — prevents a rare stale-timestamp cache hit when
        # a symbol returns the same last-candle timestamp as the previous cycle.
        try:
            from Core.Indicator_Engine import _INDICATOR_CACHE
            _INDICATOR_CACHE.clear()
        except Exception:
            pass

        tasks = []
        for asset_class, symbols in CONFIG["SYMBOLS"].items():
            if asset_class not in self._markets:
                continue
            for symbol, timeframes in symbols.items():
                tf = timeframes[-1] if timeframes else "5m"
                tasks.append(self._process_symbol(asset_class, symbol, tf, cycle_trace_id))

        results    = await asyncio.gather(*tasks, return_exceptions=True)
        all_traces = [r for r in results if isinstance(r, dict)]

        valid_trades = [t for t in all_traces if t.get("is_valid_candidate")]
        valid_trades.sort(key=lambda x: x["edge_score"], reverse=True)

        # ── ModelHealthMonitor evaluation every cycle ─────────────────
        if self.model_health:
            try:
                health = self.model_health.evaluate_health()
                if health.get("is_safe_mode"):
                    anomalies = health.get("anomalies", [])
                    self.logger.warning("MODEL_HEALTH: SAFE MODE — %s", anomalies)
                    self.telegram.send_message(
                        "⚠️ *Model Health Alert*\n"
                        + "\n".join(f"• {a}" for a in anomalies),
                        severity="WARNING",
                    )
                if health.get("drift_retrain_recommended"):
                    self.logger.warning("MODEL_HEALTH: Probability degeneration — retrain recommended.")
            except Exception:
                pass

        balance = await self._get_balance()
        if balance is None:
            self.logger.error("_run_cycle: balance fetch failed — skipping execution phase this cycle")
            return
        max_concurrent_positions = 1 if balance < 50.0 else 2

        # ── Correlation filter ────────────────────────────────────────
        MEME_COINS = {"PEPE", "DOGE", "SHIB", "FLOKI", "BONK", "WIF"}
        MAJORS     = {"BTC", "ETH"}

        def _asset_tier(sym: str) -> str:
            base = sym.split("/")[0].upper()
            if base in MAJORS:
                return "MAJOR"
            if base in MEME_COINS:
                return "MEME"
            return "MID"

        open_tiers = {
            _asset_tier(sym)
            for sym, val in self.portfolio_manager._exposure_by_symbol.items()
            if val > 0
        }

        filtered_candidates: list = []
        selected_tiers: list      = []
        for t in valid_trades:
            if len(filtered_candidates) >= max_concurrent_positions:
                break
            tier = _asset_tier(t["symbol"])
            if tier in open_tiers or tier in selected_tiers:
                t["terminal_state"] = "SKIPPED"
                t["blocking_reason"] = f"Correlation filter: {tier} already represented"
                continue
            if 50.0 <= balance < 100.0 and len(filtered_candidates) == 1:
                existing_tier = selected_tiers[0] if selected_tiers else None
                if tier == "MEME" and existing_tier == "MEME":
                    t["terminal_state"] = "SKIPPED"
                    t["blocking_reason"] = "Growth mode: prefer MAJOR+MID over double MEME"
                    continue
            filtered_candidates.append(t)
            selected_tiers.append(tier)

        for t in valid_trades:
            if t not in filtered_candidates and t.get("terminal_state") == "CANDIDATE":
                t["terminal_state"]  = "SKIPPED"
                t["blocking_reason"] = "Lower priority than Top-N allocation"

        if not hasattr(self, "_trade_context"):
            self._trade_context = {}

        # ── Capital Rotation: free a slot for a better opportunity ────
        rot_cfg = CONFIG.get("CAPITAL_ROTATION", {})
        if rot_cfg.get("ENABLED", False) and filtered_candidates:
            rotated = await self._attempt_capital_rotation(filtered_candidates, balance)
            if rotated:
                self.logger.info("CAPITAL_ROTATION: %d position(s) freed this cycle", rotated)

        # ── Adaptive ATR history for SL percentile ────────────────────
        def _adaptive_sl_mult(symbol: str, base_mult: float) -> float:
            history = self._atr_history.get(symbol, [])
            if len(history) < 20:
                return base_mult
            q75 = float(np.percentile(history, 75))
            q25 = float(np.percentile(history, 25))
            current_atr = history[-1]
            if current_atr > q75:
                return base_mult * 1.30    # high-vol regime → wider SL
            if current_atr < q25:
                return base_mult * 0.85    # low-vol regime → tighter SL
            return base_mult

        for trade in filtered_candidates:
            atr_pct   = trade.get("atr_5m_pct", 0.0015)
            stops_cfg = CONFIG.get("STOPS", {})
            is_micro  = balance < 100.0

            sl_mult_base = stops_cfg.get("ATR_MULTIPLIER_SL_MICRO", 0.9) if is_micro else stops_cfg.get("ATR_MULTIPLIER_SL", 1.5)
            tp_mult      = stops_cfg.get("ATR_MULTIPLIER_TP_MICRO", 2.7) if is_micro else stops_cfg.get("ATR_MULTIPLIER_TP", 3.0)
            min_sl       = stops_cfg.get("MIN_SL_MICRO", 0.004) if is_micro else 0.004
            min_tp       = stops_cfg.get("MIN_TP_MICRO", 0.011) if is_micro else 0.006

            # Adaptive ATR-regime SL multiplier
            sl_mult = _adaptive_sl_mult(trade["symbol"], sl_mult_base)

            # Volume Profile POC proximity → widen SL guard slightly
            if trade.get("near_poc"):
                sl_mult = min(sl_mult * 1.20, sl_mult_base * 1.50)

            stop_loss_pct   = max(atr_pct * sl_mult, min_sl)
            take_profit_pct = max(atr_pct * tp_mult, min_tp)

            entry_spread_pct = trade.get("entry_spread_pct", 0.0)
            fee_per_side     = 0.001
            effective_rr     = calculate_effective_rr(stop_loss_pct, take_profit_pct, fee_per_side, entry_spread_pct)
            trade["expected_rr"] = round(take_profit_pct / stop_loss_pct, 2) if stop_loss_pct > 0 else 0.0
            trade["effective_rr"] = round(effective_rr, 2)

            if effective_rr < 1.8:
                trade["terminal_state"]  = "REJECTED"
                trade["blocking_reason"] = f"Effective RR {effective_rr:.2f} < 1.8 after fees+spread"
                continue

            risk_amount = trade["allocation_per_trade"] * stop_loss_pct
            if risk_amount > (balance * 0.05):
                trade["terminal_state"]  = "BLOCKED"
                trade["blocking_reason"] = "Risk budget exceeded (Stop Loss > 5% of balance)"
                continue

            sl_dist = trade["price"] * stop_loss_pct
            tp_dist = trade["price"] * take_profit_pct
            sl_price = trade["price"] - sl_dist if trade["signal"] == "BUY" else trade["price"] + sl_dist
            tp_price = trade["price"] + tp_dist if trade["signal"] == "BUY" else trade["price"] - tp_dist

            if sl_price <= 0 or tp_price <= 0:
                trade["terminal_state"]  = "BLOCKED"
                trade["blocking_reason"] = "Negative price bound computation"
                continue

            status, reason, filled_price = await self._execute_one(trade, sl_price, tp_price)
            trade["terminal_state"]  = status
            trade["blocking_reason"] = reason

            if status == "EXECUTED":
                self._trade_context[trade["symbol"]] = {
                    "signal":           trade.get("signal", "BUY"),   # BUY/SELL — used by reconciliation for win/loss
                    "expected_rr":      trade["expected_rr"],
                    "effective_rr":     trade["effective_rr"],
                    "entry_spread_pct": entry_spread_pct,
                    "stop_loss_pct":    stop_loss_pct,
                    "take_profit_pct":  take_profit_pct,
                    # Capital rotation metadata
                    "entry_time":       time.time(),
                    "entry_edge_score": trade.get("edge_score", 0.0),
                    "entry_price_snap": trade.get("price", 0.0),
                    # Alpha decay metadata — TA strategy names that voted for this trade
                    "strategies_voted": [
                        r["name"] for r in trade.get("strategy_results", [])
                        if r.get("signal") == trade.get("signal")
                    ],
                }

        # ── Cycle summary log ─────────────────────────────────────────
        executed_n  = sum(1 for t in all_traces if t.get("terminal_state") == "EXECUTED")
        candidate_n = sum(1 for t in all_traces if t.get("terminal_state") == "CANDIDATE")
        for t in all_traces:
            sym    = t.get("symbol", "?")
            state  = t.get("terminal_state", "SKIPPED")
            conf   = t.get("confidence", 0.0)
            reason = t.get("blocking_reason", "")
            self.logger.info(
                "[SCAN] %-12s | %-9s | conf=%.0f%% | %s",
                sym, state, conf * 100, reason[:60] if reason else "—",
            )
        # Count all configured symbols across every active market (not just CRYPTO)
        total_configured = sum(
            len(syms) for ac, syms in CONFIG["SYMBOLS"].items()
            if ac in self._markets
        )
        self.logger.info(
            "[SCAN] Done — %d/%d symbols processed | candidates=%d executed=%d",
            len(all_traces), total_configured,
            candidate_n, executed_n,
        )

        # ── Telegram cycle broadcast ──────────────────────────────────
        if not self.telegram.enabled:
            return

        trade_logs = [t for t in all_traces if t.get("terminal_state")]
        if not trade_logs:
            return

        priority_map = {"EXECUTED": 0, "FAILED": 1, "BLOCKED": 2, "REJECTED": 3, "SKIPPED": 4, "HOLD": 5}
        trade_logs.sort(key=lambda x: (priority_map.get(x["terminal_state"], 9), -x.get("confidence", 0.0)))

        # Growth progress
        growth_cfg = CONFIG.get("GROWTH_TARGET", {})
        growth_header = []
        if growth_cfg.get("ENABLED") and balance > 0:
            idr_rate   = growth_cfg.get("IDR_RATE", 16000)
            start_idr  = growth_cfg.get("STARTING_CAPITAL_IDR", 300_000)
            target_idr = growth_cfg.get("TARGET_CAPITAL_IDR", 10_000_000)
            curr_idr   = balance * idr_rate
            pct        = min(100.0, max(0, (curr_idr - start_idr) / (target_idr - start_idr) * 100))
            bars       = int(pct / 10)
            bar_str    = "█" * bars + "░" * (10 - bars)
            growth_header = [
                f"📈 *Growth: Rp {start_idr:,.0f} → Rp {target_idr:,.0f}*",
                f"💰 Rp {curr_idr:,.0f}  (~${balance:.2f} USDT)",
                f"🎯 [{bar_str}] {pct:.1f}%",
                f"📊 Remaining: Rp {max(0, target_idr - curr_idr):,.0f}",
                "",
            ]

        # BTC benchmark line
        benchmark_line = ""
        bm_cfg = CONFIG.get("BENCHMARK", {})
        if bm_cfg.get("ENABLED") and self._btc_benchmark_price and self._btc_benchmark_balance:
            init_bal = self._btc_benchmark_balance
            strat_ret = (balance - init_bal) / init_bal * 100 if init_bal > 0 else 0.0
            try:
                mkt = self._markets.get("CRYPTO")
                if mkt:
                    tck     = await asyncio.wait_for(
                        asyncio.to_thread(mkt.exchange.fetch_ticker, "BTC/USDT"),
                        timeout=8.0,
                    )
                    btc_now = float(tck.get("last", self._btc_benchmark_price))
                else:
                    btc_now = self._btc_benchmark_price
            except (asyncio.TimeoutError, Exception):
                btc_now = self._btc_benchmark_price
            btc_ret = (btc_now - self._btc_benchmark_price) / self._btc_benchmark_price * 100
            alpha   = strat_ret - btc_ret
            benchmark_line = f"⚡ Alpha vs BTC-hold: {alpha:+.2f}% (Strat {strat_ret:+.2f}% vs BTC {btc_ret:+.2f}%)"

        # Fear & Greed line
        fg_line = ""
        if self.funding_service:
            try:
                fg = await self._tt(self.funding_service.get_fear_greed_index, timeout=8.0)
                fg_line = f"😱 Fear & Greed: {fg}/100 ({self.funding_service.fear_greed_label(fg)})"
            except Exception:
                pass

        msg_lines = [
            "⚖️ *Trade Decision Authority (TDA)* — 15m Scan",
            f"🆔 {cycle_trace_id}",
            "",
            *growth_header,
        ]
        if benchmark_line:
            msg_lines += [benchmark_line, ""]
        if fg_line:
            msg_lines += [fg_line, ""]

        for t in trade_logs:
            sym    = t.get("symbol", "UNKNOWN")
            state  = t.get("terminal_state", "UNKNOWN")
            p_long = t.get("p_long", 0.0)
            p_short = t.get("p_short", 0.0)
            conf   = t.get("confidence", 0.0)
            bull_ind = t.get("bull_indicators", 0)
            bear_ind = t.get("bear_indicators", 0)
            reason = t.get("blocking_reason", "No actionable signal")
            sig    = t.get("signal", "HOLD")
            fr     = t.get("funding_rate")
            fg_v   = t.get("fear_greed")

            extras = []
            if fr is not None:
                extras.append(f"FR:{fr*100:+.3f}%")
            if fg_v is not None:
                extras.append(f"F&G:{fg_v}")
            if t.get("in_dead_hours"):
                extras.append("🌙dead-hour")
            extras_str = f" [{', '.join(extras)}]" if extras else ""

            if state in ["EXECUTED", "HIGH RISK ENTRY"]:
                msg_lines.append(f"🟢 {sym} — {sig} (L:{p_long*100:.0f}% S:{p_short*100:.0f}%){extras_str}")
                msg_lines.append(f"✅ EXECUTED — {reason}")
            else:
                emoji = "🔒" if state == "BLOCKED" else ("❌" if state == "REJECTED" else ("⚪" if state == "SKIPPED" else "💥"))
                msg_lines.append(f"{emoji} {sym} — {sig} (L:{p_long*100:.0f}% S:{p_short*100:.0f}%){extras_str}")
                risk_msg = "Low AI Score (<70%)" if "Signal quality floor missed" in reason else f"Risk Engine: {reason}"
                msg_lines.append(f" 🔒 BLOCKED — AI:{conf*100:.0f}% Bull:{bull_ind}/5 | {risk_msg}")
            msg_lines.append("")

        self.telegram.send_message("\n".join(msg_lines), severity="INFO")

    # ──────────────────────────────────────────────────────────────────
    # Time-based exit — recycle stale capital
    # ──────────────────────────────────────────────────────────────────

    async def _time_exit_loop(self) -> None:
        """
        Close positions that have been open longer than TIME_EXIT.MAX_HOLD_HOURS
        and have NOT reached TIME_EXIT.MIN_PROFIT_R × initial-risk in profit.
        Prevents capital from being tied up in drifting, directionless trades.
        """
        te_cfg = CONFIG.get("TIME_EXIT", {})
        if not te_cfg.get("ENABLED", True):
            self.logger.info("TIME_EXIT disabled — stale-position loop not started")
            return

        max_hold_sec = te_cfg.get("MAX_HOLD_HOURS", 24) * 3600
        min_profit_r = te_cfg.get("MIN_PROFIT_R", 0.3)
        poll_sec     = te_cfg.get("POLL_SEC", 300)

        await asyncio.sleep(60)   # give startup/reconciliation time to populate state
        self.logger.info(
            "TIME_EXIT_LOOP started | max_hold=%dh | min_R=%.1f | poll=%ds",
            max_hold_sec // 3600, min_profit_r, poll_sec,
        )

        while getattr(self, "_running", True):
            try:
                crypto_exec = self.router._executors.get("CRYPTO")
                if not crypto_exec or not crypto_exec.exchange:
                    await asyncio.sleep(poll_sec)
                    continue

                now = time.time()
                tracked = {
                    sym: val
                    for sym, val in list(self.portfolio_manager._exposure_by_symbol.items())
                    if val > 0
                }

                for sym, exposure_val in tracked.items():
                    try:
                        ctx         = getattr(self, "_trade_context", {}).get(sym, {})
                        entry_time  = ctx.get("entry_time", now)
                        held_sec    = now - entry_time

                        if held_sec < max_hold_sec:
                            continue  # not stale yet

                        entry_price = self.portfolio_manager.get_entry_price(sym)
                        if not entry_price or entry_price <= 0:
                            continue

                        # Fetch live price to check current profit in R-multiples
                        try:
                            ticker = await self._tt(
                                crypto_exec.exchange.fetch_ticker, sym, timeout=10.0
                            )
                            cur_price = float(ticker.get("last", 0))
                        except Exception:
                            continue

                        if cur_price <= 0:
                            continue

                        sl_pct       = ctx.get("stop_loss_pct", 0.004)
                        initial_risk = entry_price * sl_pct
                        profit_r     = (cur_price - entry_price) / initial_risk if initial_risk > 0 else 0.0

                        if profit_r >= min_profit_r:
                            continue  # trade is working — let SL/TP handle it

                        held_h = held_sec / 3600
                        self.logger.info(
                            "TIME_EXIT: %s held %.1fh > %dh max, profit=%.2fR < %.1fR — closing",
                            sym, held_h, max_hold_sec // 3600, profit_r, min_profit_r,
                        )

                        close_res = await self._tt(
                            self.router.close_position, "CRYPTO", sym, timeout=30.0
                        )
                        status = (close_res.get("status") or "").lower()

                        if status in ("filled", "closed"):
                            close_price = float(
                                close_res.get("average") or close_res.get("price") or cur_price
                            )
                            qty_approx  = (exposure_val / entry_price) if entry_price > 0 else 0.0
                            realized_pnl = (close_price - entry_price) * qty_approx
                            is_win = realized_pnl >= 0

                            async with self._capital_lock:
                                self.portfolio_manager.record_close(sym, sector="CRYPTO")
                                self._balance_cache_ts = 0.0  # invalidate balance cache

                            self.risk_manager.record_realized_pnl(is_win)
                            self._trade_context.pop(sym, None)

                            try:
                                self.reporter.log_trade(
                                    symbol=sym, side="SELL",
                                    qty=qty_approx, price=close_price,
                                    result="TIME_EXIT", pnl=realized_pnl,
                                    reason=f"Time exit: held {held_h:.1f}h, {profit_r:.2f}R (< {min_profit_r}R)",
                                )
                            except Exception:
                                pass

                            pnl_icon = "✅" if is_win else "🔻"
                            self.telegram.send_message(
                                f"⏱ *TIME EXIT*\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"Symbol: *{sym}*\n"
                                f"Held: {held_h:.1f}h (max {max_hold_sec // 3600}h)\n"
                                f"Profit: {profit_r:.2f}R at close\n"
                                f"{pnl_icon} PnL: ${realized_pnl:+.4f}\n"
                                f"Reason: No meaningful move — capital recycled.",
                                severity="INFO",
                            )
                        else:
                            self.logger.warning(
                                "TIME_EXIT: Close of %s returned status=%s", sym, status
                            )

                    except Exception as sym_e:
                        self.logger.debug("TIME_EXIT: error for %s: %s", sym, sym_e)

            except asyncio.CancelledError:
                break
            except Exception as loop_e:
                self.logger.error("TIME_EXIT_LOOP error: %s", loop_e)

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=poll_sec,
                )
                break
            except asyncio.TimeoutError:
                pass

    # ──────────────────────────────────────────────────────────────────
    # Scheduler
    # ──────────────────────────────────────────────────────────────────

    async def _scheduler_loop(self, interval_sec: float = 900) -> None:
        cycle_count = 0
        self._last_cycle_ts = time.time()   # health-watchdog heartbeat
        while self._running:
            try:
                # Time sync is handled at startup (run()) and by _time_sync_loop().
                # Calling sync_time() inline here blocked on Windows SelectorEventLoop
                # + Python 3.13 when asyncio.to_thread submitted work to a
                # ThreadPoolExecutor while the executor was contending for thread-pool
                # resources after a prior to_thread call in the same event loop tick.

                self.logger.info(
                    "[SCAN] Cycle #%d starting — %.0f min interval | MODE=%s",
                    cycle_count + 1, interval_sec / 60, CONFIG["PROJECT"]["MODE"],
                )
                await self._run_cycle()
                _now_ts = time.time()
                self._last_cycle_ts = _now_ts   # mark successful completion

                # Write a heartbeat file so the external WatchdogSupervisor can
                # detect a frozen-but-alive engine (asyncio stall).
                try:
                    _hb_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "engine_heartbeat.json",
                    )
                    with open(_hb_path, "w") as _hf:
                        import json as _json
                        _json.dump({"ts": _now_ts, "cycle": cycle_count + 1}, _hf)
                except Exception:
                    pass

                cycle_count += 1

                # Weekly report check (Monday = weekday 0)
                now_day = datetime.now(timezone.utc).weekday()
                now_ts  = time.time()
                if (
                    now_day == 0 and
                    self._weekly_report_day != now_day and
                    now_ts - self._weekly_report_last_ts > 3600
                ):
                    self._weekly_report_day    = now_day
                    self._weekly_report_last_ts = now_ts
                    if self.report_generator:
                        bal = await self._get_balance() or 0.0
                        path = await self._tt(self.report_generator.generate_weekly_report, bal, timeout=60.0)
                        if path:
                            sent = await self._tt(self.telegram.send_document, path, "📊 AegisQuant Weekly Report", timeout=60.0)
                            if sent:
                                self.logger.info("Weekly report sent via Telegram.")
                            else:
                                self.logger.info("Weekly report saved (Telegram send failed): %s", path)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Cycle error: %s", e)
                self.telegram.send_message(f"Engine cycle error: {e}")

            # ── Clean shutdown: wake immediately on stop signal, not after full sleep ──
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=interval_sec,
                )
                break   # shutdown event fired — exit the loop
            except asyncio.TimeoutError:
                pass    # normal: interval elapsed, run next cycle

    # ──────────────────────────────────────────────────────────────────
    # Background time sync (decoupled from scan cycle)
    # ──────────────────────────────────────────────────────────────────

    async def _time_sync_loop(self, interval_sec: float = 300) -> None:
        """Re-sync Binance clock offset every `interval_sec` seconds.

        Deliberately separated from _scheduler_loop so a slow/hung sync call
        never blocks the 15-minute scan from firing.  Runs in its own task.
        """
        await asyncio.sleep(interval_sec)   # startup sync already done in run()
        while self._running:
            try:
                crypto_exec = self.router._executors.get("CRYPTO")
                if crypto_exec and hasattr(crypto_exec, "sync_time"):
                    await asyncio.wait_for(crypto_exec.sync_time(), timeout=12.0)
            except Exception as ts_e:
                self.logger.debug("Background time sync failed (non-fatal): %s", ts_e)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=interval_sec,
                )
                break
            except asyncio.TimeoutError:
                pass

    # ──────────────────────────────────────────────────────────────────
    # Health watchdog
    # ──────────────────────────────────────────────────────────────────

    async def _health_watchdog_loop(self, stall_threshold_sec: float = 1800) -> None:
        """
        Fires a Telegram alert if no scan cycle has completed in
        stall_threshold_sec (default 30 min).  Does NOT restart the engine —
        that's the watchdog supervisor's job — but gives instant visibility
        so the problem can be diagnosed before capital is at risk.
        """
        await asyncio.sleep(stall_threshold_sec)   # give first cycle time to run
        while self._running:
            try:
                last = getattr(self, "_last_cycle_ts", time.time())
                stalled_sec = time.time() - last
                if stalled_sec > stall_threshold_sec:
                    msg = (
                        f"⚠️ ENGINE STALL DETECTED\n"
                        f"No scan cycle completed in {int(stalled_sec / 60)} min.\n"
                        f"Last cycle: {datetime.fromtimestamp(last, tz=timezone.utc).strftime('%H:%M UTC')}\n"
                        f"Check logs — engine may need restart."
                    )
                    self.logger.warning("HEALTH_WATCHDOG: stalled %d min", int(stalled_sec / 60))
                    self.telegram.send_message(msg, severity="CRITICAL")
            except Exception as hw_e:
                self.logger.debug("Health watchdog error: %s", hw_e)
            # check every 10 minutes
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=600,
                )
                break
            except asyncio.TimeoutError:
                pass

    # ──────────────────────────────────────────────────────────────────
    # Startup
    # ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True

        # Time sync
        crypto_exec = self.router._executors.get("CRYPTO")
        if crypto_exec and hasattr(crypto_exec, "sync_time"):
            await crypto_exec.sync_time()

        await self._recovery_on_startup()
        self.logger.info("AsyncEngine started | MODE=%s", CONFIG["PROJECT"]["MODE"])
        self.telegram.send_message(f"AegisQuant AsyncEngine started | {CONFIG['PROJECT']['MODE']}")

        # Start interactive Telegram command listener
        # TelegramService.start_command_listener() already logs on success —
        # no need to duplicate the message here (avoids double log entry).
        try:
            self.telegram.start_command_listener()
        except Exception as tg_e:
            self.logger.warning("Telegram command listener failed to start: %s", tg_e)

        self.telegram.start_periodic_alerts(
            alert_func=self._broadcast_probabilities,
            interval_sec=CONFIG["TELEGRAM"].get("PROBABILITY_ALERT_INTERVAL_SEC", 900),
        )
        # BTC benchmark price at startup
        # NOTE: wrapped in wait_for — a silent CCXT socket hang would otherwise
        # block run() forever, preventing _scheduler_loop from ever starting.
        bm_cfg = CONFIG.get("BENCHMARK", {})
        if bm_cfg.get("ENABLED"):
            try:
                mkt = self._markets.get("CRYPTO")
                if mkt:
                    tck = await asyncio.wait_for(
                        asyncio.to_thread(mkt.exchange.fetch_ticker, "BTC/USDT"),
                        timeout=10.0,
                    )
                    self._btc_benchmark_price   = float(tck.get("last", 0))
                    self._btc_benchmark_balance = await self._get_balance() or 0.0
                    self.logger.info("BTC benchmark set: $%.2f", self._btc_benchmark_price)
                    # Persist to disk
                    bm_file = bm_cfg.get("START_PRICE_FILE", "")
                    if bm_file:
                        os.makedirs(os.path.dirname(bm_file), exist_ok=True)
                        with open(bm_file, "w") as f:
                            json.dump({
                                "btc_price":     self._btc_benchmark_price,
                                "balance_usd":   self._btc_benchmark_balance,
                                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                            }, f, indent=2)
            except asyncio.TimeoutError:
                self.logger.warning("BTC benchmark init timed out (>10 s) — skipping; engine will continue")
            except Exception as bm_e:
                self.logger.warning("BTC benchmark init failed: %s", bm_e)

        try:
            recon_task    = asyncio.create_task(self._reconciliation_loop())
            trail_task    = asyncio.create_task(self._trailing_stop_loop())
            ptp_task      = asyncio.create_task(self._partial_tp_loop())
            watchdog_task = asyncio.create_task(self._health_watchdog_loop())
            tsync_task    = asyncio.create_task(self._time_sync_loop())
            texit_task    = asyncio.create_task(self._time_exit_loop())
            await self._scheduler_loop(interval_sec=900)
        finally:
            recon_task.cancel()
            trail_task.cancel()
            ptp_task.cancel()
            watchdog_task.cancel()
            tsync_task.cancel()
            texit_task.cancel()
            self._running = False
            self._shutdown_event.set()

    def stop(self) -> None:
        self._running = False
        self._shutdown_event.set()


def run_async_engine() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = AsyncEngine()

    def _shutdown(sig, frame):
        engine.stop()
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        loop.run_until_complete(engine.run())
    finally:
        loop.close()
