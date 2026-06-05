import logging
import threading
from typing import List, Any

class ExecutionKillSwitch:
    """
    Phase 11 Pre-Live Remediation: Execution Anchor.
    Centralized emergency control system.
    """
    
    def __init__(self, 
                 portfolio_manager: Any,
                 exchange_api_client: Any,
                 health_monitor: Any = None):
                     
        self.portfolio = portfolio_manager
        self.api = exchange_api_client
        self.health_monitor = health_monitor
        self.is_flashing = False
        self._lock = threading.Lock()

    def force_liquidate_all(self):
        """
        THE HARD KILL SWITCH.
        Cancels all open orders. Dumps all open positions at market.
        Halts the PortfolioManager permanently until manual reset.
        """
        with self._lock:
            if self.is_flashing:
                return # Already running
            self.is_flashing = True
            
        logging.critical("**************************************************")
        logging.critical("CRITICAL: FORCE LIQUIDATE ALL (KILL SWITCH) INITIATED")
        logging.critical("**************************************************")
        
        # 1. Permanently halt the Risk Manager from accepting any new signals
        try:
             if hasattr(self.portfolio, 'emergency_halt'):
                 self.portfolio.emergency_halt(reason="KILL_SWITCH")
             else:
                 self.portfolio.system_halted = True
        except Exception as e:
            logging.error(f"[KillSwitch] Failed to lock RiskManager: {e}")

        # 2. Cancel all outstanding limit/stop orders
        try:
             # Abstracted call to API generic cancel all
             if hasattr(self.api, 'cancel_all_orders'):
                  self.api.cancel_all_orders()
             logging.info("[KillSwitch] Swept existing resting orders.")
        except Exception as e:
             logging.error(f"[KillSwitch] REST API Cancel All failed: {e}")

        # 3. Pull Ground Truth Positions directly from REST
        live_positions = []
        try:
            live_positions = self.api.get_open_positions()
        except Exception as e:
            logging.critical(f"[KillSwitch] FATAL: Cannot pull REST positions to liquidate: {e}")
            
        # 4. Iteratively execute Market Close Orders
        closed_count = 0
        for pos in live_positions:
             sym = pos.get('symbol')
             qty = float(pos.get('positionAmt', pos.get('notionalValue', 0.0)))
             # If quantity is not 0
             if sym and abs(qty) > 0:
                 side = "SELL" if qty > 0 else "BUY" # Closing physical direction
                 try:
                     # Abstracted close position call
                     if hasattr(self.api, 'close_position'):
                          self.api.close_position(symbol=sym, qty=abs(qty))
                          closed_count += 1
                          logging.critical(f"   -> LIQUIDATED: {sym} (Qty: {qty}) via MARKET {side}")
                 except Exception as e:
                     logging.error(f"[KillSwitch] Failed to liquidate {sym}: {e}")

        logging.critical("**************************************************")
        logging.critical(f"KILL SWITCH COMPLETE. Liquidated {closed_count} assets.")
        logging.critical("TRADING HALTED. SYSTEM REQUIRES PHYSICAL RESTART.")
        logging.critical("**************************************************")
        
        self.is_flashing = False
        return True

    def safe_mode_reset(self) -> bool:
        """
        Allows a human operator to clear a Phase 10 Model Health anomaly lock
        after verifying the system health.
        """
        if self.health_monitor is not None:
             logging.warning("[ExecutionKillSwitch] Operator initiating manual Safe Mode Reset.")
             
             # Flush internal anomaly queues
             self.health_monitor._recent_signals.clear()
             self.health_monitor._recent_regimes.clear()
             self.health_monitor._bars_since_last_signal = 0
             self.health_monitor._raw_cusum_count = 0
             self.health_monitor._meta_approved_count = 0
             
             logging.info("[ExecutionKillSwitch] Internal Model Health Queues Flushed. Safe Mode disengaged.")
             return True
             
        logging.error("[ExecutionKillSwitch] No Health Monitor registered to reset.")
        return False
