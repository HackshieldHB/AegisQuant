import logging
import threading
import time
from typing import Dict, Any, Callable, List

class PositionReconciler:
    """
    Phase 11 Pre-Live Remediation: Execution Anchor.
    Establishes the Exchange REST API as the absolute 
    Source of Truth over the local latency cache.
    
    C-1 FIX: Freezes trading immediately on detected drift.
    C-2 FIX: Reconciles ALL supported sectors, not just CRYPTO.
    """
    
    def __init__(self, 
                 portfolio_manager: Any,
                 exchange_api_client: Any,
                 interval_seconds: int = 60,
                 tolerance_pct: float = 0.01):
        """
        Args:
            portfolio_manager: The local PortfolioManager instance tracking state.
            exchange_api_client: An interface capable of calling `get_open_positions()`.
            interval_seconds: How often to ping the REST endpoint (default 60s).
            tolerance_pct: Allowed deviation before a hard overwrite (e.g., 1% for tiny partial fills).
        """
        self.portfolio = portfolio_manager
        self.api = exchange_api_client
        self.interval = interval_seconds
        self.tolerance = tolerance_pct
        
        self._stop_event = threading.Event()
        self._thread = None
        self.reconciliation_count = 0
        self.mismatches_found = 0
        self._drift_active = False

    def start(self):
        """Spawns the background reconciliation daemon."""
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._reconcile_loop, daemon=True, name="PositionReconciler")
            self._thread.start()
            logging.info("[PositionReconciler] Daemon started.")

    def stop(self):
        """Halts the daemon."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            logging.info("[PositionReconciler] Daemon stopped.")

    def _reconcile_loop(self):
        while not self._stop_event.is_set():
            try:
                self.reconcile_now()
            except Exception as e:
                logging.error(f"[PositionReconciler] Network/API Error during sync: {e}")
                
            # Sleep in chunks to remain responsive to stop events
            for _ in range(self.interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def reconcile_now(self):
        """
        Actively pulls REST positions, compares against local memory,
        and forcefully overwrites local constraints if reality has decoupled.
        
        C-1: FREEZES TRADING on any mismatch. Resumes only when clean.
        C-2: Iterates ALL supported sectors from PortfolioManager.
        """
        self.reconciliation_count += 1
        
        # 1. Pull Ground Truth from Exchange
        try:
            live_positions = self.api.get_open_positions()
        except Exception as e:
            logging.warning(f"[PositionReconciler] Failed to fetch REST truth. Skipping cycle. {e}")
            return

        # Build Exchange State Map — with partial fill detail tracking
        exchange_state = {}
        for pos in live_positions:
            sym = pos.get('symbol')
            val = float(pos.get('notionalValue', pos.get('positionAmt', 0.0)))
            if sym and abs(val) > 0:
                exchange_state[sym] = {
                    'qty': abs(val),
                    'avg_price': float(pos.get('entryPrice', pos.get('avgPrice', 0.0))),
                    'side': 'LONG' if val > 0 else 'SHORT',
                }

        # 2. Drift Detection — check for mismatches
        cycle_has_drift = False
        
        # Check exchange positions against local
        for sym, ex_info in exchange_state.items():
            ex_qty = ex_info['qty']
            local_val = self.portfolio.get_exposure_for_symbol(sym)
            if local_val == 0.0 and ex_qty > 0.0:
                 cycle_has_drift = True
                 self.mismatches_found += 1
                 logging.critical(
                     f"[PositionReconciler] ALARM: Exchange holds {sym} "
                     f"(qty={ex_qty}, avg_price={ex_info['avg_price']:.4f}, side={ex_info['side']}), "
                     f"but Local Cache is EMPTY!"
                 )
            elif local_val > 0.0:
                 diff_pct = abs(local_val - ex_qty) / local_val
                 if diff_pct > self.tolerance:
                     cycle_has_drift = True
                     self.mismatches_found += 1
                     logging.critical(
                         f"[PositionReconciler] ALARM: {sym} Drift! "
                         f"Local={local_val}, Exchange={ex_qty} (partial_fill={diff_pct:.1%} deviation)."
                     )

        # 3. C-1 FIX: FREEZE TRADING on drift
        if cycle_has_drift:
            self._drift_active = True
            if hasattr(self.portfolio, 'emergency_halt'):
                self.portfolio.emergency_halt(reason="POSITION_DRIFT")
            else:
                self.portfolio.system_halted = True
            logging.critical("*" * 60)
            logging.critical("POSITION DRIFT DETECTED — TRADING HALTED")
            logging.critical("*" * 60)

        # 4. Hard State Overwrite — push REST truth into local
        def get_sym(p): return p.get('symbol')
        def get_val(p): return abs(float(p.get('notionalValue', p.get('positionAmt', 0.0))))
        
        # C-2 FIX: Reconcile ALL supported sectors
        if hasattr(self.portfolio, 'get_supported_sectors'):
            sectors = self.portfolio.get_supported_sectors()
        else:
            sectors = ["CRYPTO"]
            
        for sector in sectors:
            self.portfolio.update_from_positions(sector, live_positions, get_symbol=get_sym, get_value=get_val)
        
        # 5. C-1 FIX: RESUME TRADING if drift is resolved
        if self._drift_active and not cycle_has_drift:
            self._drift_active = False
            if hasattr(self.portfolio, 'resume_trading'):
                self.portfolio.resume_trading(reason="DRIFT_RESOLVED")
            else:
                self.portfolio.system_halted = False
            logging.info("*" * 60)
            logging.info("DRIFT RESOLVED — TRADING RESUMED")
            logging.info("*" * 60)
        
        logging.debug("[PositionReconciler] Sync Complete.")

