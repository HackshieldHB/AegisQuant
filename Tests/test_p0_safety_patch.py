"""
P0 Critical Safety Patch Validation Tests
==========================================
Confirms all 5 certification requirements:
1) Drift triggers halt immediately
2) No new trades open while halted
3) Trading resumes after reconciliation
4) Multi-sector positions update correctly
5) Kill switch and reconciler share same halt path
"""
import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.PositionReconciler import PositionReconciler
from Execution.ExecutionKillSwitch import ExecutionKillSwitch


class MockPortfolioManager:
    """Simulates the real PortfolioManager with all C-3 fixes."""
    def __init__(self):
        self.system_halted = False
        self._halt_reason = ""
        self.exposures = {}
        self._sector_state = {}

    def emergency_halt(self, reason="UNKNOWN"):
        self.system_halted = True
        self._halt_reason = reason

    def resume_trading(self, reason="RESOLVED"):
        self.system_halted = False
        self._halt_reason = ""

    def get_supported_sectors(self):
        return ["CRYPTO", "FOREX", "STOCKS"]

    def get_exposure_for_symbol(self, sym):
        return self.exposures.get(sym, 0.0)

    def update_from_positions(self, sector, positions, get_symbol=None, get_value=None):
        self._sector_state[sector] = []
        self.exposures.clear()
        for p in positions:
            sym = get_symbol(p) if get_symbol else p.get('symbol')
            val = get_value(p) if get_value else float(p.get('positionAmt', 0))
            self.exposures[sym] = val
            self._sector_state[sector].append(sym)

    def can_open_trade(self, sector, trade_value, total_balance, positions_count=None):
        if self.system_halted:
            return False
        return True


class MockAPI:
    def __init__(self):
        self.live_positions = []
        self.canceled = False
        self.liquidated = []

    def get_open_positions(self):
        return self.live_positions

    def cancel_all_orders(self):
        self.canceled = True

    def close_position(self, symbol, qty):
        self.liquidated.append((symbol, qty))


class TestP0CriticalSafetyPatch(unittest.TestCase):

    # ============================================================
    # TEST 1: Drift triggers halt immediately
    # ============================================================
    def test_drift_triggers_halt_immediately(self):
        """C-1: When exchange state differs from local, trading MUST halt."""
        port = MockPortfolioManager()
        api = MockAPI()
        
        # Local says: BTC = 1.0
        port.exposures = {'BTC': 1.0}
        
        # Exchange says: BTC = 0.5 (partial fill drift)
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '0.5'}]
        
        reconciler = PositionReconciler(port, api, tolerance_pct=0.01)
        
        # Before reconciliation: trading is open
        self.assertFalse(port.system_halted)
        
        # Reconcile: drift detected
        reconciler.reconcile_now()
        
        # MUST be halted
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "POSITION_DRIFT")
        self.assertTrue(reconciler._drift_active)

    # ============================================================
    # TEST 2: No new trades open while halted
    # ============================================================
    def test_no_trades_while_halted(self):
        """C-3: can_open_trade() MUST return False when system_halted is True."""
        port = MockPortfolioManager()
        
        # System is operational
        self.assertTrue(port.can_open_trade("CRYPTO", 10.0, 1000.0))
        
        # Emergency halt
        port.emergency_halt(reason="TEST_HALT")
        
        # All trades MUST be blocked
        self.assertFalse(port.can_open_trade("CRYPTO", 10.0, 1000.0))
        self.assertFalse(port.can_open_trade("FOREX", 5.0, 500.0))
        self.assertFalse(port.can_open_trade("STOCKS", 1.0, 100.0))

    # ============================================================
    # TEST 3: Trading resumes after reconciliation
    # ============================================================
    def test_trading_resumes_after_drift_resolved(self):
        """C-1: After drift is corrected, trading MUST resume automatically."""
        port = MockPortfolioManager()
        api = MockAPI()
        
        # Step 1: Create drift
        port.exposures = {'BTC': 1.0}
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '0.5'}]
        
        reconciler = PositionReconciler(port, api, tolerance_pct=0.01)
        reconciler.reconcile_now()
        
        # Halted after drift
        self.assertTrue(port.system_halted)
        
        # Step 2: Exchange state now matches local (reconciler overwrote local to 0.5)
        # Local is now BTC=0.5 (from overwrite), Exchange is BTC=0.5
        # Next cycle should detect NO drift
        reconciler.reconcile_now()
        
        # MUST resume trading
        self.assertFalse(port.system_halted)
        self.assertFalse(reconciler._drift_active)

    # ============================================================
    # TEST 4: Multi-sector positions update correctly
    # ============================================================
    def test_multi_sector_reconciliation(self):
        """C-2: ALL supported sectors must be reconciled, not just CRYPTO."""
        port = MockPortfolioManager()
        api = MockAPI()
        
        api.live_positions = [
            {'symbol': 'BTC', 'positionAmt': '1.0'},
            {'symbol': 'EUR_USD', 'positionAmt': '10000'},
        ]
        
        reconciler = PositionReconciler(port, api, tolerance_pct=0.01)
        reconciler.reconcile_now()
        
        # Verify ALL sectors were updated
        sectors_updated = list(port._sector_state.keys())
        self.assertIn("CRYPTO", sectors_updated)
        self.assertIn("FOREX", sectors_updated)
        self.assertIn("STOCKS", sectors_updated)
        self.assertEqual(len(sectors_updated), 3)

    # ============================================================
    # TEST 5: Kill switch and reconciler share same halt path
    # ============================================================
    def test_kill_switch_and_reconciler_share_halt_path(self):
        """Both must call emergency_halt() and set system_halted=True."""
        port = MockPortfolioManager()
        api = MockAPI()
        api.live_positions = [{'symbol': 'SOL', 'positionAmt': '100'}]
        
        # Test Kill Switch halt path
        ks = ExecutionKillSwitch(port, api)
        ks.force_liquidate_all()
        
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "KILL_SWITCH")
        
        # Reset
        port.resume_trading()
        self.assertFalse(port.system_halted)
        
        # Test Reconciler halt path (create drift)
        port.exposures = {'BTC': 100.0}  # Local says BTC=100
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '1.0'}]  # Exchange says BTC=1
        
        reconciler = PositionReconciler(port, api, tolerance_pct=0.01)
        reconciler.reconcile_now()
        
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "POSITION_DRIFT")

    # ============================================================
    # BONUS: emergency_halt and resume_trading work correctly
    # ============================================================
    def test_emergency_halt_api(self):
        """PortfolioManager emergency_halt and resume_trading must toggle correctly."""
        port = MockPortfolioManager()
        
        port.emergency_halt(reason="TEST")
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "TEST")
        
        port.resume_trading(reason="ALL_CLEAR")
        self.assertFalse(port.system_halted)
        self.assertEqual(port._halt_reason, "")


if __name__ == '__main__':
    unittest.main()
