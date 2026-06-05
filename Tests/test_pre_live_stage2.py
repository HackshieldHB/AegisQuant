import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.PositionReconciler import PositionReconciler
from Execution.ExecutionKillSwitch import ExecutionKillSwitch

class MockPortfolioManager:
    def __init__(self):
        self.exposures = {'BTC': 1.0, 'ETH': 100.0}
        self.system_halted = False
        
    def get_exposure_for_symbol(self, sym):
        return self.exposures.get(sym, 0.0)
        
    def update_from_positions(self, sector, positions, get_symbol, get_value):
        # Full flush and rebuild based on the physical truth
        self.exposures.clear()
        for p in positions:
            sym = get_symbol(p)
            val = get_value(p)
            self.exposures[sym] = val

    def emergency_halt(self, reason="UNKNOWN"):
        self.system_halted = True
        self._halt_reason = reason

    def resume_trading(self, reason="RESOLVED"):
        self.system_halted = False

    def get_supported_sectors(self):
        return ["CRYPTO"]

class MockAPI:
    def __init__(self):
        self.live_positions = [
            {'symbol': 'BTC', 'positionAmt': '0.5'}, # Missed a partial fill
            {'symbol': 'SOL', 'positionAmt': '500.0'} # Manual intervention
        ]
        self.canceled = False
        self.liquidated_assets = []
        
    def get_open_positions(self):
        return self.live_positions
        
    def cancel_all_orders(self):
        self.canceled = True
        
    def close_position(self, symbol, qty):
        self.liquidated_assets.append((symbol, qty))

class MockHealthMonitor:
    def __init__(self):
        self._recent_signals = [1, 2, 3]
        self._recent_regimes = ['BULL', 'BEAR']
        self._bars_since_last_signal = 100
        self._raw_cusum_count = 50
        self._meta_approved_count = 5

class TestStage2Remediation(unittest.TestCase):
    
    def test_position_reconciler_overwrite(self):
        """Asserts the Reconciler correctly identifies mismatches and forces an overwrite."""
        port = MockPortfolioManager()
        api = MockAPI()
        
        # Local state: BTC=1.0, ETH=100.0 (Ghost position!)
        # REST truth: BTC=0.5, SOL=500.0 (Unregistered manual position!)
        
        reconciler = PositionReconciler(port, api, tolerance_pct=0.01)
        reconciler.reconcile_now()
        
        self.assertTrue(reconciler.mismatches_found > 0)
        
        # Local portfolio MUST now strictly match the REST truth
        self.assertEqual(port.get_exposure_for_symbol('BTC'), 0.5)
        self.assertEqual(port.get_exposure_for_symbol('ETH'), 0.0) # Flushed ghost
        self.assertEqual(port.get_exposure_for_symbol('SOL'), 500.0) # Acquired physical
        
    def test_execution_kill_switch(self):
        """Asserts the Kill switch halts the daemon, sweeps orders, and market executes."""
        port = MockPortfolioManager()
        api = MockAPI()
        
        ks = ExecutionKillSwitch(port, api)
        res = ks.force_liquidate_all()
        
        self.assertTrue(res)
        self.assertTrue(port.system_halted)
        self.assertTrue(api.canceled)
        
        # Swept REST state: BTC=0.5, SOL=500.0
        self.assertEqual(len(api.liquidated_assets), 2)
        self.assertIn(('BTC', 0.5), api.liquidated_assets)
        self.assertIn(('SOL', 500.0), api.liquidated_assets)
        
    def test_safe_mode_reset(self):
        """Asserts Safe Mode Reset correctly flushes Model Health queues."""
        hm = MockHealthMonitor()
        ks = ExecutionKillSwitch(None, None, health_monitor=hm)
        
        res = ks.safe_mode_reset()
        
        self.assertTrue(res)
        self.assertEqual(len(hm._recent_signals), 0)
        self.assertEqual(hm._bars_since_last_signal, 0)
        self.assertEqual(hm._raw_cusum_count, 0)

if __name__ == '__main__':
    unittest.main()
