import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.MultiTimeframe import _resample_to_htf, add_mtf_features

class TestMultiTimeframe(unittest.TestCase):
    def setUp(self):
        # 16 candles of 15m data = exactly four 1-Hour candles.
        # Starts 14:00, ends 17:45.
        dates = pd.date_range('2026-01-01 14:00:00', periods=16, freq='15min')
        
        self.df = pd.DataFrame({
            'open': [100.0] * 16,
            'high': [105.0] * 16,
            'low': [95.0] * 16,
            'close': [100.0, 101.0, 102.0, 103.0,  # 14:xx -> Close = 103
                      100.0, 99.0,  98.0,  97.0,   # 15:xx -> Close = 97
                       90.0, 95.0, 100.0, 105.0,   # 16:xx -> Close = 105
                      105.0, 106.0, 107.0, 108.0], # 17:xx -> Close = 108
            'volume': [10.0] * 16
        }, index=dates)

    def test_resampling_aggregation(self):
        """Testing exchange-consistent HTF OHLCV construction."""
        # 1-Hour resample:
        htf = _resample_to_htf(self.df, '1h')
        
        # Expect 4 candles
        self.assertEqual(len(htf), 4)
        
        # Check properties of 14:00 candle
        # Last close in the 14:xx bucket is 14:45 close = 103.0
        self.assertEqual(htf.loc['2026-01-01 14:00:00']['close'], 103.0)
        self.assertEqual(htf.loc['2026-01-01 14:00:00']['volume'], 40.0) # 4 * 10
        
        # 15:00 candle close = 97.0
        self.assertEqual(htf.loc['2026-01-01 15:00:00']['close'], 97.0)


    def test_strict_anti_leakage_projection(self):
        """
        CRITICAL TEST: Ensures 1H features do NOT leak into intra-bar 15m.
        The 14:00 (1H) candle closes at precisely 15:00:00.
        Therefore, at 14:15, 14:30, 14:45 -> The 14:00 feature must be NaN (or filled from 13:00, but here NaN).
        The feature must FIRST arrive at 15:00:00.
        """
        features = add_mtf_features(self.df)
        
        # The 1H_htf_trend should exist
        self.assertTrue('1H_htf_trend' in features.columns)
        
        row_1415 = features.loc['2026-01-01 14:15:00']
        row_1430 = features.loc['2026-01-01 14:30:00']
        row_1445 = features.loc['2026-01-01 14:45:00']
        row_1500 = features.loc['2026-01-01 15:00:00']
        row_1515 = features.loc['2026-01-01 15:15:00']
        
        # At 14:15 through 14:45, the 1H candle hasn't closed yet. It MUST be NaN.
        self.assertTrue(pd.isna(row_1415['1H_htf_trend']))
        self.assertTrue(pd.isna(row_1445['1H_htf_trend']))
        
        # EXACTLY at 15:00:00, the 14:00 candle has closed, and the feature is mapped onto the 15:00 bar.
        # This occurs because we shifted the 14:00 HTF calculation by "1h" -> forcing it to map to the 15:00 index.
        self.assertFalse(pd.isna(row_1500['1H_htf_trend']))
        
        # At 15:15, it should be forward-filled from the 15:00 injection point.
        self.assertFalse(pd.isna(row_1515['1H_htf_trend']))
        self.assertEqual(row_1515['1H_htf_trend'], row_1500['1H_htf_trend'])

    def test_regime_sanity(self):
        """Verify the regime distance ratios do not infinite loop or error out division by zero."""
        features = add_mtf_features(self.df)
        
        # In early rows (14:xx), the 4H feature will be NaN.
        # Ensure our clip/fill logic prevents a total system crash
        
        self.assertTrue('mtf_volatility_regime_ratio' in features.columns)
        ratio_col = features['mtf_volatility_regime_ratio'].fillna(0)
        
        self.assertFalse(np.isinf(ratio_col).any())
        
        # Alignment bounds
        align = features['mtf_trend_alignment'].dropna()
        self.assertTrue((align >= -1.0).all())
        self.assertTrue((align <= 1.0).all())


if __name__ == '__main__':
    unittest.main()
