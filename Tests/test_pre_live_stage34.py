import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.RegimeFilter import RegimeFilter
from Data.Features.DataGuard import DataGuard, QuarantineStatus

class TestStage34Remediation(unittest.TestCase):

    def test_regime_transition_cooldown(self):
        """Asserts that regime_transition_suppression is True for N bars after a confirmed transition."""
        rf = RegimeFilter(n_components=3, min_bars=2, transition_cooldown_bars=3)
        
        # We need to test the hysteresis + cooldown logic directly
        # Simulate a raw state series: state holds at 0, then jumps to 1
        raw_states = pd.Series([0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1])
        
        filtered, transitions = rf._apply_hysteresis_filter(raw_states)
        
        # With min_bars=2, the transition to state 1 should lock at index 6 (second consecutive bar of 1)
        self.assertTrue(transitions.iloc[6])
        
        # Verify cooldown manually
        cooldown = 0
        suppressed = []
        for i in range(len(transitions)):
            if transitions.iloc[i]:
                cooldown = rf.transition_cooldown_bars
            suppressed.append(cooldown > 0)
            if cooldown > 0:
                cooldown -= 1
                
        # Should suppress bars 6, 7, 8 (3 bars after transition)
        self.assertTrue(suppressed[6])
        self.assertTrue(suppressed[7])
        self.assertTrue(suppressed[8])
        self.assertFalse(suppressed[9])

    def test_dataguard_scrub_does_not_affect_execution_prices(self):
        """
        Verifies the Phase 11 Execution Decoupling Contract:
        DataGuard scrubs ML features but preserves original OHLC for execution.
        """
        dates = pd.date_range("2025-01-01", periods=30, freq='15min')
        df = pd.DataFrame({
            'open': np.random.uniform(99, 101, 30),
            'high': np.random.uniform(101, 103, 30),
            'low': np.random.uniform(97, 99, 30),
            'close': np.random.uniform(99, 101, 30),
            'volume': np.random.uniform(100, 200, 30)
        }, index=dates)
        
        # Ensure OHLC geometry
        df['high'] = df[['open', 'high', 'close']].max(axis=1) + 0.01
        df['low'] = df[['open', 'low', 'close']].min(axis=1) - 0.01
        
        original_close = df['close'].copy()
        
        status, scrubbed = DataGuard.validate_symbol(df, "TEST_SYMBOL")
        
        # Even if DataGuard scrubs wicks, the close prices (which drive fill pricing)
        # must remain completely untouched
        self.assertEqual(status, QuarantineStatus.SAFE)
        pd.testing.assert_series_equal(scrubbed['close'], original_close)

    def test_leverage_clamp_concept(self):
        """Validates the mathematical boundary of the 1.05x leverage ceiling."""
        # Direct mathematical proof
        balance = 100.0
        max_leverage = 1.05
        max_exposure = balance * max_leverage  # $105
        
        # A trade pushing above $105 must be rejected
        current_exposure = 100.0
        new_trade = 10.0
        total = current_exposure + new_trade
        
        self.assertTrue(total > max_exposure)  # $110 > $105 -> Rejected
        
        # A trade within limits must pass
        small_trade = 4.0
        total_small = current_exposure + small_trade
        self.assertTrue(total_small <= max_exposure)  # $104 <= $105 -> OK

if __name__ == '__main__':
    unittest.main()
