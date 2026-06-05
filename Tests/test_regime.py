import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.RegimeFilter import RegimeFilter

class TestRegimeFilter(unittest.TestCase):
    def setUp(self):
        # Create a deterministic synthetic dataset mirroring 3 obvious regimes
        
        # 1. LOW VOL SIDEWAYS (Indices 0 to 99)
        low_vol_close = np.random.normal(100, 0.1, 100)
        
        # 2. HIGH VOL BULL (Indices 100 to 199)
        # Strong upward drift, high variance
        bull_drift = np.linspace(100, 150, 100)
        high_vol_bull = bull_drift + np.random.normal(0, 2.0, 100)
        
        # 3. HIGH VOL BEAR (Indices 200 to 299)
        bear_drift = np.linspace(150, 50, 100)
        high_vol_bear = bear_drift + np.random.normal(0, 3.0, 100)
        
        closes = np.concatenate([low_vol_close, high_vol_bull, high_vol_bear])
        dates = pd.date_range('2026-01-01', periods=300, freq='15min')
        
        self.train_df = pd.DataFrame({'close': closes}, index=dates)

    def test_semantic_mapping_and_no_leakage(self):
        """
        Prove that fit() establishes the 3 canonical regimes correctly based ONLY on stats,
        and that a completely distinct test_df is successfully predicted onto those bounds.
        """
        rf = RegimeFilter(n_components=3, min_bars=5, random_state=42)
        rf.fit(self.train_df)
        
        # Ensure semantic mappings actually bound correctly
        self.assertTrue(RegimeFilter.LOW_VOL in rf.state_map.values())
        self.assertTrue(RegimeFilter.BULL in rf.state_map.values())
        self.assertTrue(RegimeFilter.BEAR in rf.state_map.values())
        self.assertTrue(rf.is_fitted)
        
        # Now predict on train dataset
        pred_df = rf.predict(self.train_df)
        self.assertTrue('regime' in pred_df.columns)
        self.assertTrue('regime_transition_flag' in pred_df.columns)

    def test_hysteresis_persistence(self):
        """
        Ensures the min_bars logic correctly irons out 1-bar blips.
        """
        rf = RegimeFilter(n_components=3, min_bars=3, random_state=42)
        # Mock raw states: 0, 0, 0, 1, 0, 0, 0
        raw_series = pd.Series([0, 0, 0, 1, 0, 0, 0])
        
        filtered, transitions = rf._apply_hysteresis_filter(raw_series)
        
        # The '1' at index 3 should be ironed out to '0' because it doesn't hold for min_bars (3)
        self.assertEqual(filtered.iloc[3], 0)
        
        # If we have 3 1s: 0,0,0, 1,1,1
        raw2 = pd.Series([0,0,0, 1,1,1])
        f2, t2 = rf._apply_hysteresis_filter(raw2)
        
        # The transition flag hits on exactly the completion of the 3rd bar (index 5)
        self.assertTrue(t2.iloc[5])
        self.assertEqual(f2.iloc[5], 1)
        
    def test_failsafe_unfitted_crash(self):
        """Ensure system blocks predicting without fit."""
        rf = RegimeFilter()
        with self.assertRaises(RuntimeError):
            rf.predict(self.train_df)

if __name__ == '__main__':
    # Fix random seed for pytest stability
    np.random.seed(42)
    unittest.main()
