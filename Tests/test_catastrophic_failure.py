"""
CATASTROPHIC FAILURE SIMULATION
================================
Simulates worst-case operational scenarios and verifies
the complete system shutdown cascade operates correctly.

Scenarios:
1) Exchange API total failure during open positions
2) Massive position drift (flash crash liquidation)
3) Kill switch full liquidation cascade
4) Cascading halt propagation across all modules
5) Double-fault: drift during kill switch execution
6) Safe Mode → Kill Switch escalation
7) Post-shutdown state: every gate locked
"""
import unittest
import sys
import os
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.PositionReconciler import PositionReconciler
from Execution.ExecutionKillSwitch import ExecutionKillSwitch
from Execution.ModelHealthMonitor import ModelHealthMonitor
from Execution.PortfolioRisk import RiskManager
import pandas as pd
import numpy as np


# ============================================================
# MOCK INFRASTRUCTURE
# ============================================================

class MockPortfolioManager:
    """Full-fidelity mock matching production PortfolioManager API."""
    def __init__(self):
        self.system_halted = False
        self._halt_reason = ""
        self.exposures = {}
        self._sector_state = {}
        self._halt_log = []  # Audit trail

    def emergency_halt(self, reason="UNKNOWN"):
        self.system_halted = True
        self._halt_reason = reason
        self._halt_log.append(("HALT", reason))

    def resume_trading(self, reason="RESOLVED"):
        self.system_halted = False
        self._halt_reason = ""
        self._halt_log.append(("RESUME", reason))

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
    """Exchange API mock with failure injection."""
    def __init__(self):
        self.live_positions = []
        self.canceled = False
        self.liquidated = []
        self._should_fail = False
        self._fail_on_cancel = False
        self._fail_on_close = False

    def inject_total_failure(self):
        """Simulate complete API outage."""
        self._should_fail = True

    def inject_partial_failure(self):
        """Simulate cancel works but liquidation fails."""
        self._fail_on_close = True

    def restore(self):
        self._should_fail = False
        self._fail_on_cancel = False
        self._fail_on_close = False

    def get_open_positions(self):
        if self._should_fail:
            raise ConnectionError("Exchange API UNREACHABLE")
        return self.live_positions

    def cancel_all_orders(self):
        if self._should_fail or self._fail_on_cancel:
            raise ConnectionError("Cancel orders failed")
        self.canceled = True

    def close_position(self, symbol, qty):
        if self._should_fail or self._fail_on_close:
            raise ConnectionError(f"Liquidation failed for {symbol}")
        self.liquidated.append((symbol, qty))


class MockHealthMonitor:
    def __init__(self):
        self._recent_signals = [1, -1, 1]
        self._recent_regimes = [0, 1]
        self._bars_since_last_signal = 50
        self._raw_cusum_count = 20
        self._meta_approved_count = 10
        self._meta_probabilities = [0.6, 0.7]


# ============================================================
# CATASTROPHIC FAILURE TESTS
# ============================================================

class TestCatastrophicFailureSimulation(unittest.TestCase):

    # ──────────────────────────────────────────────────────
    # SCENARIO 1: Exchange API total failure with open positions
    # ──────────────────────────────────────────────────────
    def test_scenario_1_api_failure_during_open_positions(self):
        """
        Exchange goes completely offline while we hold BTC + ETH.
        Reconciler must NOT crash. System must remain in last known state.
        Kill switch must attempt liquidation and log failures.
        """
        port = MockPortfolioManager()
        api = MockAPI()

        # System holds positions
        port.exposures = {'BTC': 1.0, 'ETH': 50.0}
        api.live_positions = [
            {'symbol': 'BTC', 'positionAmt': '1.0'},
            {'symbol': 'ETH', 'positionAmt': '50.0'},
        ]

        reconciler = PositionReconciler(port, api)

        # Exchange goes down
        api.inject_total_failure()

        # Reconciler must survive gracefully (skip cycle, not crash)
        reconciler.reconcile_now()  # Should log warning, not raise
        self.assertFalse(port.system_halted)  # No false alarm

        # Kill switch attempted during outage
        ks = ExecutionKillSwitch(port, api)
        result = ks.force_liquidate_all()

        # Kill switch MUST set halt even if liquidation fails
        self.assertTrue(port.system_halted)
        self.assertEqual(len(api.liquidated), 0)  # Couldn't actually liquidate

    # ──────────────────────────────────────────────────────
    # SCENARIO 2: Flash crash causes massive position drift
    # ──────────────────────────────────────────────────────
    def test_scenario_2_flash_crash_massive_drift(self):
        """
        Local says BTC=10.0, exchange says BTC=0.001 (99.99% liquidated).
        System must immediately halt and NOT open any new trades.
        """
        port = MockPortfolioManager()
        api = MockAPI()

        port.exposures = {'BTC': 10.0}
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '0.001'}]

        reconciler = PositionReconciler(port, api)
        reconciler.reconcile_now()

        # MUST halt immediately
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "POSITION_DRIFT")

        # Every trade gate MUST be locked
        self.assertFalse(port.can_open_trade("CRYPTO", 1.0, 1000.0))
        self.assertFalse(port.can_open_trade("FOREX", 1.0, 1000.0))
        self.assertFalse(port.can_open_trade("STOCKS", 1.0, 1000.0))

    # ──────────────────────────────────────────────────────
    # SCENARIO 3: Full kill switch liquidation cascade
    # ──────────────────────────────────────────────────────
    def test_scenario_3_kill_switch_full_cascade(self):
        """
        Kill switch must: halt → cancel orders → liquidate all → log CRITICAL.
        After completion, system MUST be in terminal halted state.
        """
        port = MockPortfolioManager()
        api = MockAPI()
        api.live_positions = [
            {'symbol': 'BTC', 'positionAmt': '1.5'},
            {'symbol': 'ETH', 'positionAmt': '100.0'},
            {'symbol': 'SOL', 'positionAmt': '-50.0'},  # Short position
        ]

        ks = ExecutionKillSwitch(port, api)
        result = ks.force_liquidate_all()

        # Verify cascade
        self.assertTrue(result)
        self.assertTrue(port.system_halted)
        self.assertTrue(api.canceled)
        self.assertEqual(len(api.liquidated), 3)

        # Verify terminal state — NO trades possible
        self.assertFalse(port.can_open_trade("CRYPTO", 0.01, 10000.0))

    # ──────────────────────────────────────────────────────
    # SCENARIO 4: Cascading halt across all modules
    # ──────────────────────────────────────────────────────
    def test_scenario_4_halt_propagation_across_all_gates(self):
        """
        When emergency_halt fires, every single trade entry point
        must be blocked simultaneously.
        """
        port = MockPortfolioManager()
        risk = RiskManager()

        # Emergency halt issued
        port.emergency_halt(reason="CATASTROPHIC_TEST")

        # PortfolioManager gate
        self.assertFalse(port.can_open_trade("CRYPTO", 10.0, 1000.0))
        self.assertFalse(port.can_open_trade("FOREX", 10.0, 1000.0))
        self.assertFalse(port.can_open_trade("STOCKS", 10.0, 1000.0))

        # RiskManager gate (PortfolioRisk checks system_halted)
        risk.system_halted = True
        signals = pd.DataFrame({
            't_start': [pd.Timestamp.now(tz='UTC')],
            't_end': [pd.Timestamp.now(tz='UTC')],
            'symbol': ['BTC'],
            'proposed_size': [1.0],
            'pnl_pct': [0.05],
        })
        result = risk.filter_signals(signals)
        self.assertTrue((result['rejection_reason'] == 'Global Kill Switch Active').all())

    # ──────────────────────────────────────────────────────
    # SCENARIO 5: Double-fault — drift DURING kill switch
    # ──────────────────────────────────────────────────────
    def test_scenario_5_double_fault_drift_plus_killswitch(self):
        """
        Kill switch fires. During liquidation, reconciler also detects drift.
        System must remain halted — no conflicting state.
        """
        port = MockPortfolioManager()
        api = MockAPI()
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '5.0'}]

        # Kill switch fires first
        ks = ExecutionKillSwitch(port, api)
        ks.force_liquidate_all()
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "KILL_SWITCH")

        # Reconciler detects drift on next cycle
        port.exposures = {'BTC': 100.0}  # Stale local state
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '0.1'}]

        reconciler = PositionReconciler(port, api)
        reconciler.reconcile_now()

        # Must STILL be halted (drift overwrites reason but halt persists)
        self.assertTrue(port.system_halted)

        # Trades still blocked
        self.assertFalse(port.can_open_trade("CRYPTO", 1.0, 10000.0))

    # ──────────────────────────────────────────────────────
    # SCENARIO 6: Safe Mode → Kill Switch escalation
    # ──────────────────────────────────────────────────────
    def test_scenario_6_safe_mode_to_kill_switch_escalation(self):
        """
        ModelHealth enters Safe Mode (0.25x sizing).
        Operator sees conditions worsen.
        Kill switch is escalated.
        System must transition cleanly to full halt.
        """
        # Step 1: Model health degrades
        health = ModelHealthMonitor(min_signal_rate=5, signal_rate_window=10)
        # Simulate signal collapse (>100 bars, <5 signals)
        health._bars_since_last_signal = 200
        health._recent_signals = [1, -1]

        result = health.evaluate_health()
        self.assertTrue(result["is_safe_mode"])
        self.assertEqual(result["sizing_multiplier"], 0.25)
        self.assertTrue(result["retraining_locked"])

        # Step 2: Operator decides to escalate to kill switch
        port = MockPortfolioManager()
        api = MockAPI()
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '2.0'}]

        ks = ExecutionKillSwitch(port, api, health_monitor=health)
        ks.force_liquidate_all()

        # Step 3: Verify full shutdown
        self.assertTrue(port.system_halted)
        self.assertFalse(port.can_open_trade("CRYPTO", 0.01, 100.0))

        # Step 4: Safe mode reset is available for recovery
        ks.safe_mode_reset()
        self.assertEqual(len(health._recent_signals), 0)
        self.assertEqual(health._bars_since_last_signal, 0)

    # ──────────────────────────────────────────────────────
    # SCENARIO 7: Post-shutdown — every gate is locked
    # ──────────────────────────────────────────────────────
    def test_scenario_7_post_shutdown_total_lockout(self):
        """
        After kill switch, EVERY possible entry point must refuse trades.
        This is the terminal state verification.
        """
        port = MockPortfolioManager()
        api = MockAPI()
        api.live_positions = [{'symbol': 'BTC', 'positionAmt': '1.0'}]
        risk = RiskManager()

        # Kill switch
        ks = ExecutionKillSwitch(port, api)
        ks.force_liquidate_all()

        # ── Gate 1: PortfolioManager.can_open_trade ──
        for sector in ["CRYPTO", "FOREX", "STOCKS"]:
            self.assertFalse(
                port.can_open_trade(sector, 0.001, 1_000_000.0),
                f"Gate 1 FAILED: {sector} trade allowed after kill switch"
            )

        # ── Gate 2: RiskManager.filter_signals ──
        risk.system_halted = True
        signals = pd.DataFrame({
            't_start': [pd.Timestamp.now(tz='UTC')],
            't_end': [pd.Timestamp.now(tz='UTC')],
            'symbol': ['BTC'],
            'proposed_size': [1.0],
            'pnl_pct': [0.05],
        })
        filtered = risk.filter_signals(signals)
        self.assertTrue(
            (filtered['approved_size'] == 0.0).all(),
            "Gate 2 FAILED: RiskManager approved trade after kill switch"
        )

        # ── Gate 3: Halt audit trail ──
        self.assertIn(("HALT", "KILL_SWITCH"), port._halt_log)

        # ── Gate 4: System requires manual restart ──
        self.assertTrue(port.system_halted)
        self.assertEqual(port._halt_reason, "KILL_SWITCH")

    # ──────────────────────────────────────────────────────
    # SCENARIO 8: Partial API failure during liquidation
    # ──────────────────────────────────────────────────────
    def test_scenario_8_partial_liquidation_failure(self):
        """
        Kill switch fires but exchange rejects some close orders.
        System must remain halted. Unliquidated positions are logged.
        """
        port = MockPortfolioManager()
        api = MockAPI()
        api.live_positions = [
            {'symbol': 'BTC', 'positionAmt': '1.0'},
            {'symbol': 'ETH', 'positionAmt': '50.0'},
        ]

        # Close orders will fail
        api.inject_partial_failure()

        ks = ExecutionKillSwitch(port, api)
        result = ks.force_liquidate_all()

        # Must STILL be halted even though liquidation failed
        self.assertTrue(port.system_halted)
        # No positions actually closed
        self.assertEqual(len(api.liquidated), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
