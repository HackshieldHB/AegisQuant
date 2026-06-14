"""
TelegramService — PRODUCTION Async Alerts & Notifications
========================================================
- Non-blocking background queue + worker thread
- Retry with exponential backoff
- Comprehensive notification types
- Startup system status
- Real-time trade alerts
- Periodic probability updates (configurable interval)
- Never blocks trading engine
"""

import queue
import hmac
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable, List, Set

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER

try:
    import requests
except ImportError:
    requests = None


class TelegramService:
    """Non-blocking Telegram notification service with priority queue."""
    
    MAX_QUEUE_SIZE = 500
    RETRY_ATTEMPTS = 3
    RETRY_BASE_DELAY = 1.0
    TIMEOUT = 5
    
    # Priority levels: higher = sent first
    PRIORITY_CRITICAL = 0  # CRITICAL alerts (never drop)
    PRIORITY_HIGH = 1      # Trade alerts, errors
    PRIORITY_NORMAL = 2    # Info, updates
    PRIORITY_LOW = 3       # Debug, verbose

    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self.enabled = (
            CONFIG["TELEGRAM"]["ENABLED"]
            and bool(CONFIG["TELEGRAM"].get("TOKEN"))
            and bool(CONFIG["TELEGRAM"].get("CHAT_ID"))
        )
        self.token = CONFIG["TELEGRAM"].get("TOKEN") or ""
        self.chat_id = CONFIG["TELEGRAM"].get("CHAT_ID") or ""
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage" if self.token else ""
        
        # Priority queue: lower number = higher priority = sent first
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=self.MAX_QUEUE_SIZE)
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        self._alert_interval = CONFIG["TELEGRAM"].get("PROBABILITY_ALERT_INTERVAL_SEC", 900)
        self._alert_thread: Optional[threading.Thread] = None
        self._stop_alert = threading.Event()
        
        if self.enabled:
            self.logger.info("Telegram Service initialized (chat_id=%s)", self.chat_id[:10] + "...")
        else:
            self.logger.info("Telegram Service disabled")

    def _send(self, text: str) -> bool:
        """Send message to Telegram with retry."""
        if not self.enabled or not requests:
            return False
        
        for attempt in range(self.RETRY_ATTEMPTS):
            try:
                r = requests.post(
                    self.base_url,
                    data={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown"
                    },
                    timeout=self.TIMEOUT,
                )
                if r.status_code == 200:
                    return True
                self.logger.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            except Exception as e:
                self.logger.debug("Telegram send attempt %s failed: %s", attempt + 1, e)
            
            if attempt < self.RETRY_ATTEMPTS - 1:
                time.sleep(self.RETRY_BASE_DELAY * (2 ** attempt))
        
        return False

    def _run_worker(self) -> None:
        """Background worker thread; never blocks main execution."""
        while True:
            try:
                # Get highest priority message (lowest number first)
                priority, msg = self._queue.get(timeout=1.0)
                if msg is None:
                    break
                
                # Send message with retries
                success = self._send(msg)
                if not success and priority == self.PRIORITY_CRITICAL:
                    # Re-queue critical messages that failed
                    self.logger.warning("Failed to send CRITICAL message, re-queueing")
                    try:
                        self._queue.put_nowait((priority, msg))
                    except queue.Full:
                        self.logger.error("Queue full; CRITICAL message lost: %s", msg[:100])
                        
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error("Telegram worker error: %s", e)

    def _enqueue(self, text: str, priority: int = PRIORITY_NORMAL) -> None:
        """Add message to priority queue."""
        try:
            self._queue.put_nowait((priority, text))
        except queue.Full:
            if priority == self.PRIORITY_CRITICAL:
                # CRITICAL messages: drop oldest low-priority message to make room
                try:
                    self._queue.get_nowait()  # Remove one message
                    self._queue.put_nowait((priority, text))
                    self.logger.warning("Queue full; dropped low-priority message to make room for CRITICAL")
                except queue.Empty:
                    self.logger.error("Queue full and empty; cannot queue CRITICAL message")
            else:
                self.logger.warning("Telegram queue full, dropping %s priority message", 
                                  self._priority_name(priority))

    def send_message(self, message: str, severity: str = "INFO") -> None:
        """
        Generic message (non-blocking).
        Severity levels: CRITICAL, ERROR, WARNING, INFO, DEBUG
        """
        priority_map = {
            "CRITICAL": self.PRIORITY_CRITICAL,
            "ERROR": self.PRIORITY_HIGH,
            "WARNING": self.PRIORITY_HIGH,
            "INFO": self.PRIORITY_NORMAL,
            "DEBUG": self.PRIORITY_LOW,
        }
        priority = priority_map.get(severity.upper(), self.PRIORITY_NORMAL)
        self._enqueue(message, priority=priority)

    def _priority_name(self, priority: int) -> str:
        """Convert priority int to name."""
        names = {
            self.PRIORITY_CRITICAL: "CRITICAL",
            self.PRIORITY_HIGH: "HIGH",
            self.PRIORITY_NORMAL: "NORMAL",
            self.PRIORITY_LOW: "LOW",
        }
        return names.get(priority, f"UNKNOWN({priority})")

    # ===== STARTUP / SYSTEM MESSAGES =====
    
    def notify_startup(
        self,
        mode: str,
        balance: float,
        symbols_count: int,
        models_ok: bool,
        stream_connected: bool,
        watchdog_active: bool,
        dashboard_url: Optional[str] = None,
    ) -> None:
        """System startup notification."""
        emoji = "🟢" if all([models_ok, stream_connected, watchdog_active]) else "🟡"
        text = (
            f"{emoji} *AegisQuant ONLINE*\n"
            f"Mode: {mode}\n"
            f"Balance: ${balance:.2f}\n"
            f"Symbols: {symbols_count}\n"
            f"Models: {'✓' if models_ok else '✗'}\n"
            f"Stream: {'✓' if stream_connected else '✗'}\n"
            f"Watchdog: {'✓' if watchdog_active else '✗'}\n"
        )
        if dashboard_url:
            text += f"Dashboard: [Open]({dashboard_url})\n"
        text += f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def notify_shutdown(self, reason: str = "Manual shutdown") -> None:
        """System shutdown notification."""
        text = (
            f"⛔ *AegisQuant OFFLINE*\n"
            f"Reason: {reason}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def notify_system_error(self, error_type: str, message: str, severity: str = "ERROR") -> None:
        """System error/anomaly notification."""
        emoji = "🔴" if severity == "CRITICAL" else "🟠"
        text = (
            f"{emoji} *{severity}* System Error\n"
            f"Type: {error_type}\n"
            f"Message: {message}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        priority = self.PRIORITY_CRITICAL if severity == "CRITICAL" else self.PRIORITY_HIGH
        self._enqueue(text, priority=priority)

    # ===== TRADE NOTIFICATIONS =====
    
    def notify_trade_open(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        position_size: float,
        sl: float,
        tp: float,
        risk_pct: float,
        model_probability: float,
        balance_after: float,
        exchange_name: str = "Binance",
        risk_tier: str = "STANDARD",
        asset_class: str = "CRYPTO",
    ) -> None:
        """Trade opened notification."""
        emoji_side = "🟢 BUY" if side.upper() == "BUY" else "🔴 SELL"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = (
            f"{emoji_side} {asset_class} {symbol}\n"
            f"Entry: ${entry_price:.8f}\n"
            f"Size: {position_size:.8f}\n"
            f"SL: ${sl:.8f} | TP: ${tp:.8f}\n"
            f"Risk: {risk_pct:.2%} | Model: {model_probability:.1%}\n"
            f"Balance: ${balance_after:.2f}\n"
            f"Exchange: {exchange_name} | Tier: {risk_tier}\n"
            f"_{ts}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def notify_trade_close(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        pnl_abs: float,
        pnl_pct: float,
        duration_str: str,
        close_reason: str,
        balance_after: float,
        asset_class: str = "CRYPTO",
    ) -> None:
        """Trade closed notification."""
        emoji = "✅" if pnl_abs >= 0 else "❌"
        pnl_sign = "+" if pnl_abs >= 0 else ""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = (
            f"{emoji} *CLOSE* {asset_class} {symbol}\n"
            f"Entry: ${entry_price:.8f} → Exit: ${exit_price:.8f}\n"
            f"PnL: {pnl_sign}${pnl_abs:.2f} ({pnl_sign}{pnl_pct:.2f}%)\n"
            f"Duration: {duration_str}\n"
            f"Reason: {close_reason}\n"
            f"Balance: ${balance_after:.2f}\n"
            f"_{ts}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def notify_partial_fill(self, symbol: str, filled_pct: float) -> None:
        """Partial fill notification."""
        text = (
            f"⚠️  Partial Fill: {symbol}\n"
            f"Filled: {filled_pct:.1%}\n"
            f"_{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    # ===== RISK/DRAWDOWN ALERTS =====
    
    def notify_drawdown_breach(self, message: str) -> None:
        """CRITICAL: Drawdown limit breached, trading halted."""
        text = (
            f"🔴 *CRITICAL ALERT*\n"
            f"Drawdown Breach - Trading Halted\n"
            f"{message}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_CRITICAL)

    def notify_portfolio_concentration_warning(self, message: str) -> None:
        """Warning: Portfolio too concentrated."""
        text = (
            f"⚠️  *Portfolio Concentration*\n"
            f"{message}\n"
            f"_{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def notify_exposure_limit_reached(self, sector: str, exposure_pct: float) -> None:
        """Warning: Sector exposure limit reached."""
        text = (
            f"⚠️  *Exposure Limit*\n"
            f"Sector: {sector}\n"
            f"Exposure: {exposure_pct:.1%}\n"
            f"No new {sector} trades until exposure decreases\n"
            f"_{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    # ===== RECOVERY / RESTART ALERTS =====
    
    def notify_system_restart(self, reason: str, recovered_positions: int) -> None:
        """System recovered from restart."""
        text = (
            f"🔄 *System Restart*\n"
            f"Reason: {reason}\n"
            f"Recovered Positions: {recovered_positions}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def notify_reconciliation_complete(self, discrepancies: int) -> None:
        """Exchange reconciliation complete."""
        emoji = "✅" if discrepancies == 0 else "⚠️"
        text = (
            f"{emoji} *Reconciliation Complete*\n"
            f"Discrepancies Found: {discrepancies}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_NORMAL)

    # ===== PROBABILITY / SIGNALS REPORTS =====
    
    def notify_probability_update(
        self,
        symbol: str,
        price: float,
        p_long: float,
        p_short: float,
        position_status: str,
        unrealized_pnl: Optional[float] = None,
        signal: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        """Periodic probability update (15-min intervals)."""
        signal_str = f" | Signal: {signal}" if signal else ""
        conf_str = f" | Conf: {confidence:.1%}" if confidence else ""
        pnl_str = f" | PnL: ${unrealized_pnl:.2f}" if unrealized_pnl is not None else ""
        
        text = (
            f"📊 {symbol} @ ${price:.8f}\n"
            f"P(Long): {p_long:.1%} | P(Short): {p_short:.1%}\n"
            f"Position: {position_status}{signal_str}{conf_str}{pnl_str}\n"
            f"_{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_LOW)

    def notify_daily_summary(self, summary: Dict[str, Any]) -> None:
        """Daily performance summary."""
        trades = summary.get("trades_closed", 0)
        wins = summary.get("wins", 0)
        daily_pnl = summary.get("daily_pnl", 0.0)
        daily_return = summary.get("daily_return_pct", 0.0)
        balance = summary.get("balance", 0.0)
        
        emoji = "📈" if daily_pnl >= 0 else "📉"
        return_sign = "+" if daily_return >= 0 else ""
        
        text = (
            f"{emoji} *Daily Summary*\n"
            f"Trades: {trades} ({wins}W/{trades-wins}L)\n"
            f"PnL: ${daily_pnl:.2f} ({return_sign}{daily_return:.2f}%)\n"
            f"Balance: ${balance:.2f}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        )
        self._enqueue(text, priority=self.PRIORITY_NORMAL)

    def notify_weekly_summary(self, summary: Dict[str, Any]) -> None:
        """Weekly performance summary."""
        trades = summary.get("trades_closed", 0)
        wins = summary.get("wins", 0)
        weekly_pnl = summary.get("weekly_pnl", 0.0)
        weekly_return = summary.get("weekly_return_pct", 0.0)
        balance = summary.get("balance", 0.0)
        
        emoji = "📈" if weekly_pnl >= 0 else "📉"
        return_sign = "+" if weekly_return >= 0 else ""
        win_rate = (wins / trades * 100) if trades > 0 else 0
        
        text = (
            f"{emoji} *Weekly Summary*\n"
            f"Trades: {trades} | Win Rate: {win_rate:.1f}%\n"
            f"PnL: ${weekly_pnl:.2f} ({return_sign}{weekly_return:.2f}%)\n"
            f"Balance: ${balance:.2f}\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d Week %W')}_"
        )
        self._enqueue(text)

    # ===== SCHEDULED ALERTS =====
    
    def start_periodic_alerts(
        self,
        alert_func: Callable[[], None],
        interval_sec: float = 900,  # 15 minutes default
    ) -> None:
        """Start periodic alert thread."""
        def _loop() -> None:
            while not self._stop_alert.wait(timeout=interval_sec):
                try:
                    alert_func()
                except Exception as e:
                    self.logger.error("Periodic alert error: %s", e)
        
        if self._alert_thread is not None:
            return
        
        self._alert_thread = threading.Thread(target=_loop, daemon=True)
        self._alert_thread.start()
        self.logger.info("Telegram periodic alerts started (interval=%.0fs)", interval_sec)

    def stop_periodic_alerts(self) -> None:
        """Stop periodic alert thread."""
        self._stop_alert.set()

    # ===== INTERACTIVE COMMAND LISTENER =====

    def register_command_handler(self, command: str, callback: Callable[..., Optional[str]]) -> None:
        """
        Register a callback for a bot command (e.g. '/status').
        The callback receives a list of argument strings and should return
        a reply string (or None to send no reply).
        """
        if not hasattr(self, "_command_handlers"):
            self._command_handlers: Dict[str, Callable] = {}
        self._command_handlers[command.lower().lstrip("/")] = callback

    def start_command_listener(self) -> None:
        """
        Start a background thread that polls getUpdates and dispatches commands.
        Safe to call multiple times — only one listener runs at a time.
        """
        if not hasattr(self, "_command_handlers"):
            self._command_handlers = {}
        if not self.enabled:
            self.logger.info("Telegram disabled — command listener not started")
            return
        if getattr(self, "_listener_thread", None) and self._listener_thread.is_alive():
            return

        self._stop_listener = threading.Event()
        self._listener_thread = threading.Thread(
            target=self._command_poll_loop, daemon=True, name="TG-CommandListener"
        )
        self._listener_thread.start()
        self.logger.info("Telegram command listener started")

    def stop_command_listener(self) -> None:
        if hasattr(self, "_stop_listener"):
            self._stop_listener.set()

    def _command_poll_loop(self) -> None:
        """Long-poll getUpdates, dispatch registered commands."""
        if not requests:
            return
        get_url   = f"https://api.telegram.org/bot{self.token}/getUpdates"
        # Use latest offset to skip historic messages
        offset: int = self._fetch_initial_offset(get_url)

        while not getattr(self, "_stop_listener", threading.Event()).is_set():
            try:
                params = {"timeout": 20, "offset": offset, "allowed_updates": ["message"]}
                resp = requests.get(get_url, params=params, timeout=25)
                if resp.status_code != 200:
                    time.sleep(5)
                    continue
                updates = resp.json().get("result", [])
                for upd in updates:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    if not self._is_authorized_message(msg):
                        sender = msg.get("chat", {}).get("id")
                        self.logger.warning(
                            "Rejected Telegram command from unauthorized chat_id=%s",
                            sender,
                        )
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    parts = text.split()
                    cmd   = parts[0].lstrip("/").split("@")[0].lower()
                    args  = parts[1:]
                    handler = getattr(self, "_command_handlers", {}).get(cmd)
                    if handler:
                        try:
                            reply = handler(args)
                            if reply:
                                self.send_message(reply)
                        except Exception as he:
                            self.logger.error("Command /%s handler error: %s", cmd, he)
                    else:
                        self.send_message(
                            f"❓ Unknown command: `/{cmd}`\n"
                            f"Available: /status /pause /resume /close /report /models"
                        )
            except Exception as pe:
                self.logger.debug("Telegram poll error: %s", pe)
                time.sleep(10)

    def _is_authorized_message(self, message: Dict[str, Any]) -> bool:
        """Accept control commands only from the configured owner chat."""
        supplied = str(message.get("chat", {}).get("id", ""))
        expected = str(self.chat_id)
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    def _fetch_initial_offset(self, get_url: str) -> int:
        """Skip all pending updates; start fresh from the next one."""
        if not requests:
            return 0
        try:
            resp = requests.get(get_url, params={"limit": 100}, timeout=5)
            updates = resp.json().get("result", [])
            if updates:
                return updates[-1]["update_id"] + 1
        except Exception:
            pass
        return 0

    def notify_regime_change(self, symbol: str, old_regime: str, new_regime: str) -> None:
        """Alert: HMM regime transition detected for a symbol."""
        emoji_map = {
            "TRENDING": "📈", "RANGING": "↔️", "LOW_ACTIVITY": "💤",
            "LOW_VOL": "💤", "HIGH_VOL_BULL": "🐂", "HIGH_VOL_BEAR": "🐻",
        }
        old_e = emoji_map.get(old_regime, "")
        new_e = emoji_map.get(new_regime, "🔄")
        # Escape underscores in regime names — Markdown V1 treats _ as italic
        # delimiter, so e.g. "LOW_ACTIVITY" creates an unclosed entity (HTTP 400).
        old_r = old_regime.replace("_", "\\_")
        new_r = new_regime.replace("_", "\\_")
        text = (
            f"🔄 *Regime Change Detected*\n"
            f"Symbol: `{symbol}`\n"
            f"{old_e} {old_r}  →  {new_e} {new_r}\n"
            f"Adjusting thresholds accordingly\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        self._enqueue(text, priority=self.PRIORITY_HIGH)

    def send_document(self, file_path: str, caption: str = "") -> bool:
        """Send a file (e.g. PDF) to the Telegram chat."""
        if not self.enabled or not requests:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendDocument"
        try:
            with open(file_path, "rb") as fh:
                resp = requests.post(
                    url,
                    data={"chat_id": self.chat_id, "caption": caption, "parse_mode": "Markdown"},
                    files={"document": fh},
                    timeout=30,
                )
            return resp.status_code == 200
        except Exception as e:
            self.logger.error("send_document failed: %s", e)
            return False


# Alias
TelegramBot = TelegramService
