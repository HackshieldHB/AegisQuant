import numpy as np
import pandas as pd
import sys
import os
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.ValidationSuite import ValidationSuite
from Execution.TearsheetGenerator import TearsheetGenerator

class TestValidationHardening(unittest.TestCase):
    
    def test_overfitting_metrics(self):
        """
        Tests PBO, DSR, and SPA checks against synthetic noise.
        """
        # Synthetic data: IS does well, OOS fails entirely (Classic Overfit)
        sharpe_is = np.array([2.1, 1.8, 2.5, 3.0, 1.9])
        sharpe_oos = np.array([-1.2, -0.5, -2.0, -1.5, -0.8])
        
        pbo = ValidationSuite.calculate_pbo(sharpe_is, sharpe_oos)
        self.assertTrue(pbo >= 0.4) # Should be extremely high risk (40% rank inversion)
        
        # DSR should heavily penalize a Sharpe of 1.5 achieved after 100 trials
        dsr = ValidationSuite.calculate_deflated_sharpe(1.5, num_trials=100, variance_trials=1.0)
        self.assertTrue(dsr < 0.5) # The penalization crushed the reported Sharpe
        
        # SPA Test - Strategy identical to baseline benchmark (Null holds)
        strats = pd.Series(np.random.normal(0, 0.01, 200))      
        bench = strats + np.random.normal(0, 0.001, 200) # Negligible difference
        spa_p = ValidationSuite.white_reality_check(strats, bench, n_bootstraps=200)
        self.assertTrue(spa_p > 0.05) # Fails to reject the null hypothesis (It is noise)

    def test_fragility_and_tail_risk(self):
        """
        Tests Regime Generalization, Temporal block failures, and CVaR ruin paths.
        """
        # Temporal Collapse: First window wins big, next 4 lose
        returns = pd.Series([0.1]*100 + [-0.05]*100 + [-0.1]*100 + [-0.02]*100)
        temp_stability = ValidationSuite.evaluate_temporal_stability(returns, window_size=100)
        self.assertFalse(temp_stability["stable"])
        
        # Regime Fragility: Prints money in Bull, burns in Bear
        df = pd.DataFrame({'regime': ['BULL']*100 + ['BEAR']*100})
        signal_rets = pd.Series([0.05]*100 + [-0.10]*100)
        reg_gen = ValidationSuite.evaluate_regime_generalization(df, signal_rets)
        self.assertFalse(reg_gen["generalized"])
        
        # Monte Carlo 5th Percentile Ruin check
        path = pd.Series(np.random.normal(0.001, 0.05, 1000)) # High volatility, slight positive drift
        tail = ValidationSuite.block_bootstrap_cvar(path)
        self.assertTrue(tail["cvar"] < tail["median_return"]) # CVaR is strictly in the left tail

    def test_tearsheet_rejection_logic(self):
        """
        Asserts the Final Go/No-Go matrix triggers mathematical rejections
        based on rigid constraint breaches.
        """
        fake_metrics = {
            "pbo": 0.01,              # PASS
            "dsr": 2.5,               # PASS
            "spa_p_value": 0.01,      # PASS
            "temporal_stability": {"stable": True}, # PASS
            "regime_generalization": {"generalized": True}, # PASS
            "tail_risk": {"worst_drawdown": -0.35}, # FAIL: Fatal drawdown
            "model_decay": {"rapid_decay": False},  # PASS
            "signal_quality": {"valid_calibration": True}, # PASS
            "turnover": {"high_turnover_risk": False}, # PASS
            "cost_stress_return": 0.10  # PASS
        }
        
        conclusion = TearsheetGenerator.evaluate_go_no_go(fake_metrics)
        
        self.assertEqual(conclusion["status"], "REJECT \u2014 OVERFIT OR FRAGILE")
        self.assertEqual(len(conclusion["fail_reasons"]), 1)
        self.assertTrue("Lethal Drawdown" in conclusion["fail_reasons"][0])
        
        # Fix the tail risk to pass live
        fake_metrics["tail_risk"]["worst_drawdown"] = -0.10
        conclusion_safe = TearsheetGenerator.evaluate_go_no_go(fake_metrics)
        self.assertEqual(conclusion_safe["status"], "APPROVED FOR LIVE")

if __name__ == '__main__':
    unittest.main()
