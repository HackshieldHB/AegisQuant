import pandas as pd
import numpy as np
import sys
import os
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Labeling.TripleBarrier import apply_triple_barrier
from Execution.PortfolioRisk import RiskManager

class TestExecutionRealism(unittest.TestCase):
    
    def test_dynamic_costs(self):
        """
        Verify that dynamic constraints scale perfectly across BTC (100k) and DOGE (10 cents),
        and appropriately compress the theoretical profit margin while widening loss risk.
        """
        dates = pd.date_range('2026-01-01', periods=10, freq='15min')
        
        df_high = pd.DataFrame({
            'close': [100000.0] * 10,
            'high': [106000.0] * 10,
            'low': [94000.0] * 10
        }, index=dates)
        
        df_low = pd.DataFrame({
            'close': [0.10] * 10,
            'high': [0.106] * 10,
            'low': [0.094] * 10
        }, index=dates)
        
        events = pd.Series([1], index=[dates[0]])
        vol_high = pd.Series([5000.0], index=[dates[0]]) # 5% relative vol
        vol_low = pd.Series([0.005], index=[dates[0]])   # 5% relative vol
        
        res_high = apply_triple_barrier(
            df_high, vol_high, events, k_up=1.0, k_down=1.0,
            fees_bps=4.0, spread_bps=2.0, slippage_bps=2.0, direction=1
        )
        
        res_low = apply_triple_barrier(
            df_low, vol_low, events, k_up=1.0, k_down=1.0,
            fees_bps=4.0, spread_bps=2.0, slippage_bps=2.0, direction=1
        )
        
        # total_bps = 4(entry) + 4(exit) + 2(spread) + 2(slippage) = 12 bps = 0.0012
        # Expected TP threshold gets reduced by costs (target moves closer but represents lower net)
        # Expected SL threshold gets reduced (hits earlier, compounding downside loss probability)
        
        expected_upper_high = 100000.0 * (1.05 - 0.0012)
        expected_lower_high = 100000.0 * (0.95 - 0.0012)
        
        self.assertAlmostEqual(res_high.iloc[0]['upper_barrier'], expected_upper_high, places=2)
        self.assertAlmostEqual(res_high.iloc[0]['lower_barrier'], expected_lower_high, places=2)
        self.assertAlmostEqual(res_high.iloc[0]['effective_cost_pct'], 0.0012, places=5)
        
        expected_upper_low = 0.10 * (1.05 - 0.0012)
        expected_lower_low = 0.10 * (0.95 - 0.0012)
        
        self.assertAlmostEqual(res_low.iloc[0]['upper_barrier'], expected_upper_low, places=5)
        self.assertAlmostEqual(res_low.iloc[0]['lower_barrier'], expected_lower_low, places=5)
        
    def test_portfolio_risk(self):
        """
        Verify portfolio manager gates limits on Max Positions, Correlation, and Drawdown.
        """
        manager = RiskManager(max_positions=2, correlation_threshold=0.7, drawdown_limit=-5.0)
        
        dates = pd.date_range('2026-01-01', periods=5, freq='1h')
        
        # Simulate simultaneous ML approvals
        signals = pd.DataFrame({
            't_start': [dates[0], dates[0], dates[0], dates[3], dates[4]],
            't_end': [dates[2], dates[2], dates[2], dates[4], dates[4]],
            'symbol': ['BTC', 'ETH', 'SOL', 'BTC', 'DOGE'],
            'proposed_size': [0.02, 0.02, 0.02, 0.02, 0.02],
            'pnl_pct': [0.0, 0.0, 0.0, -1000.0, 1.0] # Massive drawdown on row 3
        })
        
        returns_df = pd.DataFrame({
            'BTC': [0.01, 0.02, -0.01],
            'ETH': [0.01, 0.02, -0.01], # 1.0 Correlation with BTC
            'SOL': [0.0, -0.01, 0.0]
        })
        
        res = manager.filter_signals(signals, returns_df)
        
        # Row 0: Approved cleanly
        self.assertEqual(res.iloc[0]['approved_size'], 0.02)
        
        # Row 1: Penalized (Correlated to BTC)
        self.assertEqual(res.iloc[1]['approved_size'], 0.01)
        self.assertIn('Correlated', res.iloc[1]['rejection_reason'])
        
        # Row 2: Rejected (Max Positions 2)
        self.assertEqual(res.iloc[2]['approved_size'], 0.0)
        self.assertEqual(res.iloc[2]['rejection_reason'], 'Max Positions Exceeded')
        
        # Row 3: Opens new trade as previous trades have expired. This trade destroys the account.
        self.assertEqual(res.iloc[3]['approved_size'], 0.02)
        
        # Row 4: Rejected (Circuit Breaker Tripped)
        self.assertEqual(res.iloc[4]['approved_size'], 0.0)
        self.assertEqual(res.iloc[4]['rejection_reason'], 'Circuit Breaker Lockout')

if __name__ == '__main__':
    unittest.main()
