import pandas as pd

class Reconciler:
    """
    Enforces state consistency between the ML Strategy module 
    and the physical Exchange Positions.
    
    If latency or API drops cause the exchange to un-sync from 
    the internal ledger, this module triggers an emergency flag.
    """
    def __init__(self):
        self.strategy_positions = {}
        self.exchange_positions = {}
        self.mismatches = []
        
    def update_strategy_state(self, symbol: str, expected_size: float):
        """Update what the ML model thinks is active."""
        self.strategy_positions[symbol] = expected_size
        
    def update_exchange_state(self, symbol: str, actual_size: float):
        """Update based on live websocket/REST exchange pulls."""
        self.exchange_positions[symbol] = actual_size
        
    def reconcile(self) -> bool:
        """
        Cross-validates internal ML accounting against physical Exchange reality.
        Returns False (Halt) if critical de-syncs are detected.
        """
        self.mismatches = []
        all_symbols = set(self.strategy_positions.keys()).union(set(self.exchange_positions.keys()))
        
        is_safe = True
        for sym in all_symbols:
            expected = self.strategy_positions.get(sym, 0.0)
            actual = self.exchange_positions.get(sym, 0.0)
            
            # Use a tiny float tolerance for fractional sizing arithmetic
            if abs(expected - actual) > 1e-4:
                self.mismatches.append({
                    "symbol": sym, 
                    "expected": expected, 
                    "actual": actual, 
                    "diff": expected - actual
                })
                is_safe = False
                
        return is_safe
