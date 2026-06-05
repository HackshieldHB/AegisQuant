import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.OrderFlow import add_order_flow_features, compute_cumulative_volume_delta, rolling_z_score

class TestOrderFlow(unittest.TestCase):
    def setUp(self):
        dates = pd.date_range('2026-01-01', periods=10, freq='1h')
        self.df = pd.DataFrame({
            'open': [100.0] * 10,
            'high': [100.0] * 10,
            'low': [100.0] * 10,
            'close': [100.0, 101.0, 102.0, 101.0, 100.0, 99.0, 98.0, 99.0, 100.0, 101.0],
            'volume': [100.0, 100.0, 200.0, 0.0, 50.0, 100.0, 100.0, 500.0, 100.0, 100.0],
            'taker_buy_base': [60.0, 80.0, 200.0, 0.0, 10.0, 20.0, 10.0, 480.0, 50.0, 50.0]
        }, index=dates)

        # Baseline features
        self.features = add_order_flow_features(self.df, cvd_mode='rolling', cvd_window=3, normalization_window=5)

    def test_aggression_ratio_bounds_and_zero(self):
        """Verify aggression ratio stays in [0,1] and handles 0 volume gracefully."""
        # Row 3 (index 3) is 0 volume
        agg_zero = self.features.iloc[3]['flow_aggression']
        self.assertEqual(agg_zero, 0.0)
        
        # Row 2 (index 2) is 100% taker buy (200/200)
        agg_max = self.features.iloc[2]['flow_aggression']
        self.assertEqual(agg_max, 1.0)
        
        # All rows must be between 0 and 1
        self.assertTrue((self.features['flow_aggression'] >= 0.0).all())
        self.assertTrue((self.features['flow_aggression'] <= 1.0).all())

    def test_imbalance_calculation(self):
        """Verify buy and sell extraction works."""
        # Row 0: Vol 100, Taker Buy 60 -> Taker Sell 40 -> Imbalance 20
        imb_0 = self.features.iloc[0]['flow_imbalance']
        self.assertEqual(imb_0, 20.0)
        
        # Row 4: Vol 50, Taker Buy 10 -> Taker Sell 40 -> Imbalance -30
        imb_4 = self.features.iloc[4]['flow_imbalance']
        self.assertEqual(imb_4, -30.0)

    def test_rolling_cvd(self):
        """Verify bounded rolling CVD."""
        # Uses row 0, 1, 2
        # Imb 0: 20
        # Imb 1: (80 - 20) = 60
        # Imb 2: (200 - 0) = 200
        # Sum = 280
        cvd_2 = self.features.iloc[2]['cvd']
        self.assertEqual(cvd_2, 280.0)
        
        # Row 3: Vol 0 -> Imb 0
        # Sum of last 3 (idx 1, 2, 3) = 60 + 200 + 0 = 260
        cvd_3 = self.features.iloc[3]['cvd']
        self.assertEqual(cvd_3, 260.0)

    def test_daily_cvd_reset(self):
        """Verify CVD resets upon daily crossover."""
        dates = pd.date_range('2026-01-01 22:00:00', periods=4, freq='1h') 
        # spans across 00:00 (Row 2 starts new day)
        df_daily = pd.DataFrame({
            'volume': [100.0, 100.0, 100.0, 100.0],
            'taker_buy_base': [100.0, 100.0, 100.0, 100.0] # 100 imb each
        }, index=dates)
        
        feat = add_order_flow_features(df_daily, cvd_mode='daily')
        
        # Expected: 100, 200, (Reset!) 100, 200
        self.assertEqual(feat.iloc[1]['cvd'], 200.0)
        # New day starts here!
        self.assertEqual(feat.iloc[2]['cvd'], 100.0) 

    def test_z_score_normalization(self):
        """Verify z-score properties (no lookahead, scales correctly)."""
        z_scores = self.features['cvd_z']
        
        # First element has no std, should be 0.0 filled
        self.assertEqual(z_scores.iloc[0], 0.0)
        
        # Row 7 is a massive spike (480 taker buy out of 500)
        # Z-score should spike heavily positive
        self.assertTrue(z_scores.iloc[7] > 1.0)
        
    def test_extreme_spikes_and_stationarity(self):
        """Ensure no NaNs or Infs persist through computations."""
        # Row 7 is extreme spike
        self.assertFalse(self.features.isna().any().any())
        self.assertFalse(np.isinf(self.features.select_dtypes(include=np.number)).any().any())


if __name__ == '__main__':
    unittest.main()
