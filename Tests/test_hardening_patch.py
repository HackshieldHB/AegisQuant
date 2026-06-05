import pandas as pd
import numpy as np
import sys
import os
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Labeling.TripleBarrier import apply_triple_barrier
from Execution.PortfolioRisk import RiskManager
from Data.Features.MetaLabeler import MetaLabeler
from Execution.SystemState import Reconciler

class TestHardeningPatch(unittest.TestCase):
    
    def test_gap_risk_handling(self):
        """
        Tests if a massive gap down instantly overrides the theoretical stop limit.
        """
        dates = pd.date_range('2026-01-01', periods=3, freq='1h')
        
        # Candle 0: Entry.
        # Candle 1: Gap down past our stop loss.
        # Candle 2: Expiry
        df = pd.DataFrame({
            'open': [100.0, 50.0, 40.0],  # Open on Candle 1 is way below the stop limit
            'high': [100.0, 50.0, 40.0],
            'low': [100.0, 40.0, 30.0],
            'close': [100.0, 45.0, 35.0]
        }, index=dates)
        
        events = pd.Series([1], index=[dates[0]])
        vol = pd.Series([5.0], index=[dates[0]]) # Target TP at 105, SL at 95
        
        # Test Long (1)
        res = apply_triple_barrier(
            df, vol, events, k_up=1.0, k_down=1.0,
            fees_bps=0.0, spread_bps=0.0, slippage_bps=0.0, direction=1
        )
        
        # Because we gapped down on the open of Candle 1, our exit price should be 50.0
        # and NOT the magical theoretical 95.0 barrier
        self.assertEqual(res.iloc[0]['label'], -1)
        self.assertEqual(res.iloc[0]['lower_barrier'], 50.0)
        
    def test_liquidity_and_kill_switches(self):
        """
        Tests ADV proxy liquidity clipping, drift bounds, and emergency halt.
        """
        manager = RiskManager(liquidity_threshold=0.10, max_drift_bps=50.0, max_session_exposure=1000.0)
        dates = pd.date_range('2026-01-01', periods=4, freq='1h')
        
        signals = pd.DataFrame({
            't_start': dates,
            't_end': [d + pd.Timedelta(hours=2) for d in dates],
            'symbol': ['A', 'B', 'C', 'D'],
            'proposed_size': [100.0, 100.0, 100.0, 100.0],
            'adv_volume': [500.0, 2000.0, 2000.0, 2000.0], # A has low liquidity
            'signal_price': [10.0, 10.0, 10.0, 10.0],
            'current_price': [10.0, 10.0, 15.0, 10.0], # C has massive price drift
        })
        
        res_initial = manager.filter_signals(signals)
        
        # A: Limited by Liquidity (10% of 500 = 50.0)
        self.assertEqual(res_initial.iloc[0]['approved_size'], 50.0)
        self.assertIn('Liquidity', res_initial.iloc[0]['rejection_reason'])
        
        # B: Plentiful Liquidity, No Drift
        self.assertEqual(res_initial.iloc[1]['approved_size'], 100.0)
        
        # C: Massive latency price drift (50% drift > 50bps max)
        self.assertEqual(res_initial.iloc[2]['approved_size'], 0.0)
        self.assertIn('Price Drift', res_initial.iloc[2]['rejection_reason'])
        
        # Test Emergency Halt
        manager.emergency_halt(True)
        res_halted = manager.filter_signals(signals)
        self.assertEqual(res_halted.iloc[3]['approved_size'], 0.0)
        self.assertIn('Kill Switch', res_halted.iloc[3]['rejection_reason'])
        
    def test_oco_and_funding(self):
        """
        Tests exact inversion detection in OCO generation and carry cost deductions.
        """
        # 1. Test Funding / Carry
        # 80% Win probability with 1.5 Payoff -> 0.8 - (0.2/1.5) = 0.666
        base_kelly = MetaLabeler.compute_kelly_size(
            pd.Series([0.8]), payoff_ratio=1.5, kelly_fraction=1.0, max_risk_cap=1.0, funding_rate_bps_per_hour=0.0
        ).iloc[0]
        
        # 5 bps an hour funding charge = 120bps per day = roughly 0.012 penalty
        heavy_cost_kelly = MetaLabeler.compute_kelly_size(
            pd.Series([0.8]), payoff_ratio=1.5, kelly_fraction=1.0, max_risk_cap=1.0, funding_rate_bps_per_hour=5.0
        ).iloc[0]
        
        self.assertTrue(heavy_cost_kelly < base_kelly)
        
        # 2. Test OCO Validation geometry 
        df = pd.DataFrame({
            'symbol': ['BTC', 'ETH'],
            'primary_signal': [1, -1],
            'approved_size': [0.1, 0.1],
            'entry_price': [10.0, 10.0],
            # BTC Long: Stop Loss mathematically above entry -> INVALID REJECTION 
            # ETH Short: Stop Loss (upper barrier) mathematically below entry -> INVALID REJECTION
            'lower_barrier': [15.0, 15.0], 
            'upper_barrier': [5.0, 5.0]
        })
        
        payloads = MetaLabeler.generate_oco_payloads(df)
        self.assertEqual(len(payloads), 0) # Bot correctly detects geometrical impossibility and refuses API shipment
        
    def test_reconciler(self):
        """
        Tests the local state checker against the mock exchange.
        """
        ledger = Reconciler()
        ledger.update_strategy_state("BTC", 0.5)
        ledger.update_exchange_state("BTC", 0.5)
        
        ledger.update_strategy_state("SOL", 1.0)
        ledger.update_exchange_state("SOL", 0.0) # Exchange misfired the order 
        
        is_safe = ledger.reconcile()
        
        self.assertFalse(is_safe)
        self.assertEqual(len(ledger.mismatches), 1)
        self.assertEqual(ledger.mismatches[0]['symbol'], "SOL")

if __name__ == '__main__':
    unittest.main()
