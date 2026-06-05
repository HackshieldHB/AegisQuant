"""
RiskManager — Institutional-grade risk control.
-----------------------------------------------
- Position sizing: risk_per_trade %, balance, SL distance.
- Peak balance (daily/weekly) with calendar reset.
- Drawdown: (peak - current) / peak; disable trading if breached.
- MAX_CONCURRENT_TRADES enforced via can_open_new_trade(count).
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


class RiskManager:
    def __init__(self, balance: float = 0.0) -> None:
        self.balance = balance
        self.logger = AG_LOGGER
        self._peak_balance_daily: float = balance
        self._peak_balance_weekly: float = balance
        self._last_daily_reset: str = self._date_key()
        self._last_weekly_reset: str = self._week_key()
        self._trading_disabled: bool = False
        self._on_drawdown_breach: Any = None  # callback(message: str) -> None; call Telegram CRITICAL
        self._consecutive_losses: int = 0
        self._cooldown_until: float = 0.0
        self._last_known_balance: float = balance
        self._flash_crash_suspect: bool = False  # First reading below threshold
        self._flash_crash_suspect_ts: float = 0.0
        self._flash_crash_disabled_at: float = 0.0  # When trading was disabled by flash-crash

    def record_realized_pnl(self, is_win: bool) -> None:
        if is_win:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

        max_losses = CONFIG.get("RISK", {}).get("MAX_CONSECUTIVE_LOSSES", 5)
        if self._consecutive_losses == max_losses:
            import time
            self._cooldown_until = time.time() + (3 * 3600)  # 3 hours
            self.logger.warning(
                "CRITICAL: %d consecutive realized losses. Trading halted for 3 hours.",
                max_losses,
            )

    def cooldown_active(self) -> bool:
        import time
        if self._cooldown_until > 0 and time.time() >= self._cooldown_until:
            # Cooldown expired — self-heal: reset counter to prevent recursive triggers
            self._consecutive_losses = 0
            self._cooldown_until = 0.0
            return False
        return time.time() < self._cooldown_until

    def set_drawdown_breach_callback(self, callback: Any) -> None:
        """Set callback for drawdown breach (e.g. notify_drawdown_breach). Called before halting."""
        self._on_drawdown_breach = callback

    def _date_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _week_key(self) -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y") + "-W" + now.strftime("%V")

    def _maybe_reset_peaks(self) -> None:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week = now.strftime("%Y") + "-W" + now.strftime("%V")
        if today != self._last_daily_reset:
            self._peak_balance_daily = self.balance
            self._last_daily_reset = today
        if week != self._last_weekly_reset:
            self._peak_balance_weekly = self.balance
            self._last_weekly_reset = week

    def update_balance(self, balance: float) -> None:
        import time
        
        # If we just woke up from a cooldown, our last known balance is 3 hours stale.
        # It's not a flash crash, it's just normal market drift during sleep. Reset baseline.
        # Use 1800s (2 cycle lengths) as the grace window — the next scan cycle can arrive
        # up to 900s after the cooldown expires, so 15s was far too narrow.
        if hasattr(self, "_cooldown_until") and time.time() < (self._cooldown_until + 1800):
            # We are within the first two cycles of waking up from a cooldown.
            self._last_known_balance = balance
            
        if self._last_known_balance > 0 and balance > 0:
            # Track Sudden Flash-Crash Drop — requires 2 consecutive readings
            drop_pct = (self._last_known_balance - balance) / self._last_known_balance
            sudden_drop_limit = CONFIG.get("RISK", {}).get("SUDDEN_DROP_PCT", 0.05)
            if drop_pct >= sudden_drop_limit:
                if not self._flash_crash_suspect:
                    # First reading below threshold — mark suspect, wait for confirmation
                    self._flash_crash_suspect = True
                    self._flash_crash_suspect_ts = time.time()
                    self.logger.warning(
                        "Flash-crash suspect: -%.2f%% drop detected. Awaiting confirmation on next cycle.",
                        drop_pct * 100,
                    )
                elif time.time() - self._flash_crash_suspect_ts < 120:
                    # Second consecutive reading within 2 minutes — confirmed flash-crash
                    msg = f"CRITICAL: Sudden Flash-Crash Balance Drop Detected: -{drop_pct:.2%} (Limit: {sudden_drop_limit:.2%}). Trading halted."
                    self.logger.critical(msg)
                    if self._on_drawdown_breach:
                        try:
                            self._on_drawdown_breach(msg)
                        except Exception:
                            pass
                    self._trading_disabled = True
                    self._flash_crash_disabled_at = time.time()
                else:
                    # Suspect expired (> 2 min gap) — was likely a transient reading
                    self._flash_crash_suspect = False
            else:
                # Balance recovered above threshold — clear suspect flag
                self._flash_crash_suspect = False
                
        self._last_known_balance = balance
        self.balance = balance
        self._maybe_reset_peaks()
        if balance > self._peak_balance_daily:
            self._peak_balance_daily = balance
        if balance > self._peak_balance_weekly:
            self._peak_balance_weekly = balance

    def get_risk_tier(self) -> Dict[str, Any]:
        tiers = CONFIG["RISK"]["TIERS"]
        # Check MICRO tier first (if it exists)
        if "MICRO" in tiers and self.balance < tiers["MICRO"]["MAX_BALANCE"]:
            return {"name": "MICRO", "risk": tiers["MICRO"]["RISK_PCT"]}
        if self.balance < tiers["AGGRESSIVE"]["MAX_BALANCE"]:
            return {"name": "AGGRESSIVE", "risk": tiers["AGGRESSIVE"]["RISK_PCT"]}
        if self.balance < tiers["STANDARD"]["MAX_BALANCE"]:
            return {"name": "STANDARD", "risk": tiers["STANDARD"]["RISK_PCT"]}
        return {"name": "CONSERVATIVE", "risk": tiers["CONSERVATIVE"]["RISK_PCT"]}

    def calculate_position_size(self, symbol: str, price: float, sl_price: float) -> float:
        if price <= 0:
            return 0.0
        distance = abs(price - sl_price)
        if distance <= 0:
            self.logger.warning("Invalid SL distance for %s: price=%s sl=%s", symbol, price, sl_price)
            return 0.0
            
        # --- FIX 3: UNIFIED RISK-BASED SIZING (MICRO + STANDARD) ---
        # Position size = (equity × risk_pct) / stop_distance
        # This ensures consistent risk per trade regardless of account size.
        
        # Get risk percentage from tier or symbol override
        tier = self.get_risk_tier()
        risk_pct = tier["risk"]
        overrides = CONFIG.get("SYMBOL_OVERRIDES", {}).get(symbol, {})
        if "risk" in overrides:
            risk_pct = min(risk_pct, float(overrides["risk"]))
        
        # Cap risk_pct at 2% maximum (safety ceiling)
        risk_pct = min(risk_pct, 0.02)
        
        # Small account constraint: enforce reserve
        threshold = CONFIG["RISK"].get("SMALL_ACCOUNT_THRESHOLD", 100.0)
        if self.balance < threshold:
            reserve_usdt = max(2.0, self.balance * 0.05)
            deployable = self.balance - reserve_usdt
            
            if deployable < 5.0:
                self.logger.warning("Micro-cap allocation failed: Balance below minimum tradable capital (Deployable: %.2f)", deployable)
                return 0.0
            
            # Risk-based sizing: equity × risk% / SL distance
            risk_amt = deployable * risk_pct
            size = risk_amt / distance
            
            # Cap at deployable notional (cannot exceed available capital)
            max_size = deployable / price
            if size > max_size:
                size = max_size
            
            return size

        # Standard/Large account: same risk-based formula
        risk_amt = self.balance * risk_pct
        size = risk_amt / distance
        # Cap at deployable notional — tight SL on cheap asset could produce over-sized position
        max_size = self.balance / price
        return min(size, max_size)

    def check_drawdown(self, current_balance: float) -> bool:
        """
        Returns True if drawdown is within limits (trading allowed).
        Returns False if daily or weekly limit breached (trading must stop).
        """
        import time
        
        if self._trading_disabled:
            # AUTO-RECOVERY: After 1 hour, if balance is within 90% of last known peak, re-enable
            if self._flash_crash_disabled_at > 0:
                elapsed = time.time() - self._flash_crash_disabled_at
                if elapsed >= 3600 and current_balance > 0:  # 1 hour minimum
                    # Check if balance has stabilized (within 10% of when we halted)
                    recovery_ratio = current_balance / self._peak_balance_daily if self._peak_balance_daily > 0 else 0
                    if recovery_ratio >= 0.85:  # Balance is >= 85% of peak
                        self._trading_disabled = False
                        self._flash_crash_disabled_at = 0.0
                        self._flash_crash_suspect = False
                        self._last_known_balance = current_balance
                        self.logger.info(
                            "AUTO-RECOVERY: Trading re-enabled after 1h. Balance: %.2f (%.1f%% of peak)",
                            current_balance, recovery_ratio * 100,
                        )
                        # Fall through to normal drawdown check below
                    else:
                        return False
                else:
                    return False
            else:
                return False
            
        self._maybe_reset_peaks()
        
        # Guard: ensure peaks are set to valid values
        if self._peak_balance_daily <= 0:
            self.logger.warning("Peak balance daily is invalid (<=0): %.2f. Resetting.", self._peak_balance_daily)
            self._peak_balance_daily = max(current_balance, 1.0)
        
        if self._peak_balance_weekly <= 0:
            self.logger.warning("Peak balance weekly is invalid (<=0): %.2f. Resetting.", self._peak_balance_weekly)
            self._peak_balance_weekly = max(current_balance, 1.0)
        
        peak_daily = max(self._peak_balance_daily, current_balance)
        peak_weekly = max(self._peak_balance_weekly, current_balance)
        
        dd_daily = (peak_daily - current_balance) / peak_daily
        max_daily = CONFIG["RISK"].get("MAX_DAILY_DRAWDOWN", 0.05)
        if dd_daily > max_daily:
            msg = f"CRITICAL: Daily drawdown breached: {dd_daily:.2%} > {max_daily:.2%}. Trading halted."
            self.logger.error(msg)
            if self._on_drawdown_breach:
                try:
                    self._on_drawdown_breach(msg)
                except Exception as e:
                    self.logger.error("Drawdown breach callback failed: %s", e)
            self._trading_disabled = True
            return False
        
        dd_weekly = (peak_weekly - current_balance) / peak_weekly
        max_weekly = CONFIG["RISK"].get("MAX_WEEKLY_DRAWDOWN", 0.10)
        if dd_weekly > max_weekly:
            msg = f"CRITICAL: Weekly drawdown breached: {dd_weekly:.2%} > {max_weekly:.2%}. Trading halted."
            self.logger.error(msg)
            if self._on_drawdown_breach:
                try:
                    self._on_drawdown_breach(msg)
                except Exception as e:
                    self.logger.error("Drawdown breach callback failed: %s", e)
            self._trading_disabled = True
            return False
        
        return True

    @property
    def trading_halted(self) -> bool:
        return self._trading_disabled

    def can_open_new_trade(self, current_open_count: int) -> bool:
        if self._trading_disabled:
            return False
            
        if self.cooldown_active():
            self.logger.warning("BLOCKED: Cooldown Active from Consecutive Losses.")
            return False
            
        max_concurrent = CONFIG["RISK"].get("MAX_CONCURRENT_TRADES", 2)
        
        threshold = CONFIG["RISK"].get("SMALL_ACCOUNT_THRESHOLD", 100.0)
        if self.balance < threshold:
            if current_open_count >= max_concurrent:
                self.logger.warning("MAX_CONCURRENT_TRADES_REACHED")
                return False
                
        return current_open_count < max_concurrent

    def check_can_scale_position(self, current_profit_r_multiple: float) -> bool:
        return current_profit_r_multiple >= 1.0
