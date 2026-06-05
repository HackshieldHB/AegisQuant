import unittest
import pandas as pd
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.PortfolioRisk import RiskManager
from Execution.ExecutionRealism import calculate_nonlinear_slippage

class TestStage1Remediation(unittest.TestCase):
    
    def test_nonlinear_slippage_calculation(self):
        """Asserts slippage scales quadratically with order size / relative ADV."""
        base_bps = 2.0
        adv = 100.0
        
        # Tiny order (0 impact)
        # Ratio = 0.01 -> Sq = 0.0001
        slip_tiny = calculate_nonlinear_slippage(base_bps, 1.0, adv)
        self.assertAlmostEqual(slip_tiny, 2.0002, places=4)
        
        # Mid order (50% ADV)
        # Ratio = 0.5 -> Sq = 0.25
        slip_mid = calculate_nonlinear_slippage(base_bps, 50.0, adv)
        self.assertAlmostEqual(slip_mid, 2.5, places=4)
        
        # Whale order (100% ADV)
        # Ratio = 1.0 -> Sq = 1.0
        slip_whale = calculate_nonlinear_slippage(base_bps, 100.0, adv)
        self.assertAlmostEqual(slip_whale, 4.0, places=4)
        
        # Volatility multiplier
        slip_vol = calculate_nonlinear_slippage(base_bps, 100.0, adv, volatility_scalar=2.0)
        self.assertAlmostEqual(slip_vol, 8.0, places=4)
        
        # Limit order queue penalty (50% reduction)
        slip_limit = calculate_nonlinear_slippage(base_bps, 100.0, adv, is_market_order=False)
        self.assertAlmostEqual(slip_limit, 2.0, places=4)

    def test_minimum_notional_rejection(self):
        """Asserts RiskManager rejects orders falling below min notional limit."""
        manager = RiskManager(max_positions=5, max_session_exposure=1000.0)
        # By default, min_notional is 10.0
        
        dates = pd.date_range('2026-01-01', periods=3, freq='1h')
        signals = pd.DataFrame({
            't_start': [dates[0], dates[1], dates[2]],
            't_end': [dates[1], dates[2], dates[2]],
            'symbol': ['BTC', 'SHIB', 'ETH'],
            'proposed_size': [1.0, 1000.0, 0.001],
            'current_price': [50000.0, 0.00001, 3000.0]
        })
        
        res = manager.filter_signals(signals)
        
        # Row 0: 1 BTC @ 50k = $50,000 (Pass)
        self.assertEqual(res.iloc[0]['approved_size'], 1.0)
        
        # Row 1: 1000 SHIB @ 0.00001 = $0.01 (Fail)
        self.assertEqual(res.iloc[1]['approved_size'], 0.0)
        self.assertTrue('Minimum Notional' in res.iloc[1]['rejection_reason'])
        
        # Row 2: 0.001 ETH @ 3000 = $3.00 (Fail)
        self.assertEqual(res.iloc[2]['approved_size'], 0.0)
        self.assertTrue('Minimum Notional' in res.iloc[2]['rejection_reason'])

    def test_risk_manager_slippage_integration(self):
        """Asserts RiskManager properly assigns expected slippage to approved orders."""
        manager = RiskManager(max_session_exposure=1000.0, liquidity_threshold=1.0)
        
        dates = pd.date_range('2026-01-01', periods=1, freq='1h')
        signals = pd.DataFrame({
            't_start': [dates[0]],
            't_end': [dates[0]],
            'symbol': ['BTC'],
            'proposed_size': [50.0],
            'current_price': [1000.0],
            'adv_volume': [100.0],
            'volatility_scalar': [1.0],
            'pnl_pct': [0.01]
        })
        
        res = manager.filter_signals(signals)
        # 50 order size on 100 ADV -> 0.5 impact ratio -> 0.25 sq -> 1.25x * 2 bps = 2.5 bps
        self.assertAlmostEqual(res.iloc[0]['expected_slippage_bps'], 2.5, places=4)

if __name__ == '__main__':
    unittest.main()
