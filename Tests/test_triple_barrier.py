import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Labeling.TripleBarrier import apply_triple_barrier, compute_ewma_volatility

class TestTripleBarrier(unittest.TestCase):
    def setUp(self):
        # Create a deterministic OHLCV dataset
        dates = pd.date_range('2026-01-01', periods=10, freq='15min')
        self.df = pd.DataFrame({
            'open': [100.0] * 10,
            'high': [100.0] * 10,
            'low': [100.0] * 10,
            'close': [100.0] * 10,
            'volume': [1000] * 10
        }, index=dates)
        
        # Fixed volatility for testing (k_up=1, k_down=1 -> barriers at 101 and 99)
        self.vol = pd.Series([1.0] * 10, index=dates)
        
        # Testing events triggered at index 0 and 5
        self.events = pd.Series(1, index=[dates[0], dates[5]])

    def test_immediate_tp_hit_next_bar(self):
        """Upper barrier (TP) hit on the very next bar."""
        df = self.df.copy()
        
        # Bar 1 high touches 101.5 (Upper barrier is 101)
        df.iloc[1, df.columns.get_loc('high')] = 101.5 
        
        res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5)
        
        # Event 0: entry 100, tp hit on bar 1 (+1)
        row = res.iloc[0]
        self.assertEqual(row['label'], 1)
        self.assertEqual(row['t_end'], df.index[1])

    def test_immediate_sl_hit_next_bar(self):
        """Lower barrier (SL) hit on the very next bar."""
        df = self.df.copy()
        
        # Bar 1 low touches 98.5 (Lower barrier is 99)
        df.iloc[1, df.columns.get_loc('low')] = 98.5
        
        res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5)
        
        row = res.iloc[0]
        self.assertEqual(row['label'], -1)
        self.assertEqual(row['t_end'], df.index[1])

    def test_time_expiry(self):
        """No barrier hit before the horizon expires."""
        res = apply_triple_barrier(self.df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=3)
        
        row = res.iloc[0]
        self.assertEqual(row['label'], 0)
        # Ends exactly horizon_bars later
        self.assertEqual(row['t_end'], self.df.index[3])

    def test_simultaneous_tp_and_sl(self):
        """Simultaneous hit in the same candle resolves to -1."""
        df = self.df.copy()
        
        # Bar 2 is extremely volatile: hits both 102 and 98
        df.iloc[2, df.columns.get_loc('high')] = 102.0
        df.iloc[2, df.columns.get_loc('low')] = 98.0
        
        res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5)
        
        row = res.iloc[0]
        # Must resolve to worst-case (-1)
        self.assertEqual(row['label'], -1)
        self.assertEqual(row['t_end'], df.index[2])

    def test_flat_market(self):
         """Market does not move."""
         df = self.df.copy()
         res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5)
         
         row = res.iloc[0]
         self.assertEqual(row['label'], 0)

    def test_extreme_spike_behavior(self):
        """Spikes very high and then normal."""
        df = self.df.copy()
        
        # Bar 3 has a massive spike
        df.iloc[3, df.columns.get_loc('high')] = 200.0
        
        res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5)
        
        row = res.iloc[0]
        self.assertEqual(row['label'], 1)
        self.assertEqual(row['t_end'], df.index[3])
        
    def test_short_direction_tp(self):
        """Testing short direction where touching lower barrier is TP."""
        df = self.df.copy()
        
        # Bar 1 low touches 99 (TP for short)
        df.iloc[1, df.columns.get_loc('low')] = 98.5
        
        res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5, direction=-1)
        
        row = res.iloc[0]
        # Lower var touches = TP = +1
        self.assertEqual(row['label'], 1)
        self.assertEqual(row['t_end'], df.index[1])

    def test_short_direction_sl(self):
        """Testing short direction where touching upper barrier is SL."""
        df = self.df.copy()
        
        # Bar 1 high touches 101.5 (SL for short)
        df.iloc[1, df.columns.get_loc('high')] = 101.5
        
        res = apply_triple_barrier(df, self.vol, self.events, k_up=1.0, k_down=1.0, horizon_bars=5, direction=-1)
        
        row = res.iloc[0]
        # Upper var touches = SL = -1
        self.assertEqual(row['label'], -1)
        self.assertEqual(row['t_end'], df.index[1])


if __name__ == '__main__':
    unittest.main()
