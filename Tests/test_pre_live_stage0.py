import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.OrderFlow import add_order_flow_features
from Data.Features.MultiTimeframe import add_mtf_features

class TestStage0Remediation(unittest.TestCase):
    
    def test_zero_volume_shim_orderflow(self):
        """Asserts that exactly 0 volume does not throw ZeroDivisionError or Inf."""
        df = pd.DataFrame({
             'timestamp': pd.date_range("2025-01-01", periods=5, freq='1min', tz='UTC'),
             'open': [100, 101, 102, 103, 104],
             'high': [101, 102, 103, 104, 105],
             'low': [99, 100, 101, 102, 103],
             'close': [100, 101, 102, 103, 104],
             'volume': [0, 0, 0, 0, 0],
             'taker_buy_base': [0, 0, 0, 0, 0]
        })
        
        result = add_order_flow_features(df)
        
        # Verify no NaN or Inf is created due to zero division
        self.assertFalse(result.isin([np.inf, -np.inf]).any().any())
        # Taker buy / sum(taker buy + taker sell) => 0 / 1e-9 = 0
        self.assertEqual(result['flow_aggression'].iloc[-1], 0.0)

    def test_zero_volume_shim_multitimeframe(self):
         """Asserts VWAP calculation handles 0 volume arrays correctly."""
         df = pd.DataFrame({
             'timestamp': pd.date_range("2025-01-01", periods=10, freq='15min', tz='UTC'),
             'open': [100]*10,
             'high': [101]*10,
             'low': [99]*10,
             'close': [100]*10,
             'volume': [0]*10
         })
         result = add_mtf_features(df)
         
         self.assertFalse(result['1H_htf_vwap'].isna().all()) # Should fallback cleanly

    def test_multi_timeframe_backward_merge(self):
         """Asserts that 15m data does not look ahead into the future 1H candle."""
         # Create exactly one 1H boundary. 14:00 to 14:45.
         df = pd.DataFrame({
             'timestamp': pd.date_range("2025-01-01 14:00:00", periods=4, freq='15min', tz='UTC'),
             'open': [100, 101, 102, 103],
             'high': [101, 102, 103, 104],
             'low': [99, 100, 101, 102],
             'close': [101, 102, 103, 104],
             'volume': [10, 20, 30, 40]
         })
         
         result = add_mtf_features(df)
         
         # The 1H candle for 14:00 only "closes" at 15:00.
         # Because we shifted the 1H index to 15:00 and used `merge_asof(direction='backward')`,
         # the elements before 15:00 should NOT see the 14:00 1H bar metrics.
         # They should be NaN (or filled from the *previous* 13:00 bar if it existed).
         
         # Since this is the first bar, everything for 1H should be NaN to prevent lookahead!
         self.assertTrue(pd.isna(result['1H_htf_vwap'].iloc[-1]))
         
         # Now, add the 15:00 candle.
         df2 = pd.DataFrame({
             'timestamp': pd.date_range("2025-01-01 14:00:00", periods=5, freq='15min', tz='UTC'),
             'open': [100, 101, 102, 103, 104],
             'high': [101, 102, 103, 104, 105],
             'low': [99, 100, 101, 102, 103],
             'close': [101, 102, 103, 104, 105],
             'volume': [10, 20, 30, 40, 50]
         })
         
         result2 = add_mtf_features(df2)
         
         # At 15:00, the 14:00-14:59 1H bar is known.
         # The 15:00 row SHOULD have the 1H features populated.
         self.assertFalse(pd.isna(result2['1H_htf_vwap'].iloc[-1]))

    def test_utc_timezone_enforcement(self):
         """Asserts that naive datetimes are forcefully localized to UTC."""
         df_naive = pd.DataFrame({
             'timestamp': pd.date_range("2025-01-01", periods=5, freq='1min'), # Naive
             'open': [100]*5, 'high': [105]*5, 'low': [95]*5, 'close': [100]*5, 'volume': [10]*5,
             'taker_buy_base': [5]*5
         })
         
         result_of = add_order_flow_features(df_naive)
         self.assertEqual(str(result_of.index.tzinfo), 'UTC')

         df_naive_mtf = pd.DataFrame({
             'timestamp': pd.date_range("2025-01-01", periods=5, freq='15min'), # Naive
             'open': [100]*5, 'high': [105]*5, 'low': [95]*5, 'close': [100]*5, 'volume': [10]*5
         })
         result_mtf = add_mtf_features(df_naive_mtf)
         self.assertEqual(str(result_mtf.index.tzinfo), 'UTC')

if __name__ == '__main__':
    unittest.main()
