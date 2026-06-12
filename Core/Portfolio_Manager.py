"""
PortfolioManager — Position-based exposure (no additive-only state).
-------------------------------------------------------------------
- Exposure derived from actual open positions (injected each cycle).
- Per-symbol and per-sector exposure.
- Thread-safe: all state updated in one pass from positions snapshot.
"""

import threading
from typing import Dict, List, Any, Optional

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


class PortfolioManager:
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self._lock = threading.Lock()
        self.system_halted: bool = False
        self._halt_reason: str = ""
        max_sector = CONFIG.get("RISK", {}).get("MAX_SECTOR_EXPOSURE_PCT", 1.0)
        self._allocations = {
            "CRYPTO": max_sector if CONFIG["PROJECT"]["CRYPTO_ENABLED"] else 0.0,
            "FOREX": max_sector if CONFIG["PROJECT"]["FOREX_ENABLED"] else 0.0,
            "STOCKS": max_sector if CONFIG["PROJECT"]["STOCKS_ENABLED"] else 0.0,
        }
        self._exposure_by_symbol: Dict[str, float] = {}
        self._exposure_by_sector: Dict[str, float] = {"CRYPTO": 0.0, "FOREX": 0.0, "STOCKS": 0.0}
        self._entry_prices: Dict[str, float] = {}

    def emergency_halt(self, reason: str = "UNKNOWN") -> None:
        """Globally halt all trading. Called by KillSwitch, Reconciler, or operator."""
        with self._lock:
            self.system_halted = True
            self._halt_reason = reason
        self.logger.critical("EMERGENCY HALT — %s", reason)

    def resume_trading(self, reason: str = "RESOLVED") -> None:
        """Resume trading after halt condition is cleared."""
        with self._lock:
            self.system_halted = False
            self._halt_reason = ""
        self.logger.info("TRADING RESUMED — %s", reason)

    def get_supported_sectors(self) -> list:
        """Returns list of enabled sector keys for multi-sector reconciliation."""
        return [s for s, alloc in self._allocations.items() if alloc > 0.0]

    def get_entry_price(self, symbol: str) -> Optional[float]:
        return self._entry_prices.get(symbol)

    def update_from_positions(
        self,
        sector: str,
        positions: List[Any],
        get_symbol: Any = None,
        get_value: Any = None,
    ) -> None:
        """
        Recompute exposure for a sector from actual positions.
        positions: list of position dicts from executor.get_open_positions().
        get_symbol: callable(position) -> symbol str
        get_value: callable(position) -> notional value float
        """
        if get_symbol is None:
            get_symbol = lambda p: p.get("symbol") or p.get("symbol") or ""
        if get_value is None:
            def _value(p):
                qty = float(p.get("contracts", 0) or p.get("qty", 0) or 0)
                price = float(p.get("markPrice") or p.get("entryPrice") or p.get("currentPrice") or 0)
                return qty * price if price else 0
            get_value = _value
        with self._lock:
            sector_exposure = 0.0
            for p in positions:
                sym = get_symbol(p)
                if not sym:
                    continue
                val = get_value(p)
                self._exposure_by_symbol[sym] = val
                sector_exposure += val
            self._exposure_by_sector[sector] = sector_exposure

    def record_open(self, sector: str, symbol: str, value: float, entry_price: Optional[float] = None) -> None:
        """Record a new position (call after verified fill).

        Idempotent: if called twice for the same symbol (e.g. race between
        reconciliation loop and _execute_one), only the *delta* vs. the
        already-recorded value is applied to the sector total — preventing
        double-counting while still accepting legitimate position increases.
        """
        with self._lock:
            old_val = self._exposure_by_symbol.get(symbol, 0.0)
            delta   = value - old_val
            self._exposure_by_symbol[symbol] = value   # SET, not additive
            self._exposure_by_sector[sector] = max(
                0.0,
                self._exposure_by_sector.get(sector, 0.0) + delta
            )
            if entry_price is not None:
                self._entry_prices[symbol] = entry_price

    def record_close(self, symbol: str, sector: Optional[str] = None) -> None:
        """Record position closed; if sector given, decrement sector total."""
        with self._lock:
            val = self._exposure_by_symbol.pop(symbol, 0)
            self._entry_prices.pop(symbol, None)
            if sector and val:
                self._exposure_by_sector[sector] = max(0, self._exposure_by_sector.get(sector, 0) - val)
            elif val and not sector:
                self._recompute_sector_totals()

    def record_resize(self, sector: str, symbol: str, new_value: float) -> None:
        """Atomically resize a position and keep sector totals consistent."""
        with self._lock:
            old_value = self._exposure_by_symbol.get(symbol, 0.0)
            new_value = max(0.0, float(new_value))
            if new_value == 0.0:
                self._exposure_by_symbol.pop(symbol, None)
                self._entry_prices.pop(symbol, None)
            else:
                self._exposure_by_symbol[symbol] = new_value
            self._exposure_by_sector[sector] = max(
                0.0,
                self._exposure_by_sector.get(sector, 0.0) + new_value - old_value,
            )

    def _recompute_sector_totals(self) -> None:
        for sector in self._exposure_by_sector:
            self._exposure_by_sector[sector] = 0.0
        for sym, val in self._exposure_by_symbol.items():
            sector = "CRYPTO" if "USDT" in sym.upper() or "/" in sym else "STOCKS"
            if "EUR" in sym or "GBP" in sym or "JPY" in sym:
                sector = "FOREX"
            self._exposure_by_sector[sector] = self._exposure_by_sector.get(sector, 0) + val

    def get_open_count(self) -> int:
        """Number of symbols with positive exposure (works for spot + futures)."""
        with self._lock:
            return sum(1 for v in self._exposure_by_symbol.values() if v > 0)

    def get_exposure_for_symbol(self, symbol: str) -> float:
        with self._lock:
            return self._exposure_by_symbol.get(symbol, 0.0)

    def get_exposure_for_sector(self, sector: str) -> float:
        with self._lock:
            return self._exposure_by_sector.get(sector, 0.0)

    def get_total_exposure(self) -> float:
        with self._lock:
            return sum(self._exposure_by_sector.values())

    def can_open_trade(
        self,
        sector: str,
        trade_value: float,
        total_balance: float,
        positions_count: Optional[int] = None,
    ) -> bool:
        # C-3 FIX: Global halt enforcement — MUST be first check
        if self.system_halted:
            self.logger.warning(
                "Trade BLOCKED — system halted (reason: %s)", self._halt_reason
            )
            return False
        if total_balance <= 0:
            return False
        target_alloc = self._allocations.get(sector, 0.0)
        max_sector_value = total_balance * target_alloc
        current_sector = self.get_exposure_for_sector(sector)
        if current_sector + trade_value > max_sector_value:
            self.logger.warning(
                "Sector allocation limit: %s exposure %.2f + %.2f > %.2f",
                sector, current_sector, trade_value, max_sector_value,
            )
            return False
        max_exposure_pct = CONFIG.get("RISK", {}).get("MAX_PORTFOLIO_EXPOSURE", 0.10)
        total_exposure = self.get_total_exposure() + trade_value
        
        # Phase 11 Hardened Limit: 1.05x Maximum Total Leverage
        absolute_leverage_limit = 1.05
        if total_exposure > total_balance * absolute_leverage_limit:
            self.logger.critical(
                f"FATAL RISK: Leverage Clamp Triggered! Proposed ({total_exposure}) > Eq ({total_balance * absolute_leverage_limit})"
            )
            return False
            
        if total_exposure > total_balance * max_exposure_pct:
            self.logger.warning(
                "Max portfolio exposure: total exposure %.2f > %.0f%% of balance",
                total_exposure, max_exposure_pct * 100,
            )
            return False
            
        return True

    def get_allocation(self, sector: str) -> float:
        return self._allocations.get(sector, 0.0)
