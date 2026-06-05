import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.DriftMonitor import DriftMonitor
from Execution.ShadowValidation import ShadowValidation, ModelVersionState
from Execution.RetrainingPipeline import RetrainingPipeline
from Data.Features.DataGuard import QuarantineStatus

class TestOnlineAdaptationHardening(unittest.TestCase):
    
    def test_multivariate_drift_detection(self):
        """
        Asserts the Ensemble (KS + PSI) correctly flags mathematically drifted
        distributions while ignoring minor noise.
        """
        monitor = DriftMonitor(ks_threshold=0.05, psi_critical_limit=0.20)
        
        # 1. Baseline Training Distribution (Normal 0, 1)
        np.random.seed(42)
        base_f1 = np.random.normal(0, 1, 1000)
        base_f2 = np.random.normal(5, 2, 1000)
        train_df = pd.DataFrame({'f1': base_f1, 'f2': base_f2, 'f3': np.random.normal(0,1,1000)})
        
        # 2. Live Distribution (Identical) - Should NOT drift
        live_safe = pd.DataFrame({'f1': base_f1 + np.random.normal(0,0.01,1000), 
                                 'f2': base_f2 + np.random.normal(0,0.01,1000), 
                                 'f3': np.random.normal(0,1,1000)})
                                 
        safe_res = monitor.evaluate_multivariate_drift(train_df, live_safe, ['f1', 'f2'])
        self.assertFalse(safe_res['drift_confirmed'])
        self.assertTrue(safe_res['mean_psi'] < 0.20)
        
        # 3. Live Distribution (Massive Regime Shift on F1)
        live_drift = live_safe.copy()
        live_drift['f1'] = np.random.normal(3, 1.5, 1000) # Shifted mean by 3 stds
        
        drift_res = monitor.evaluate_multivariate_drift(train_df, live_drift, ['f1', 'f2'])
        # 50% of the active features (f1) drifted via KS, and the PSI should cross threshold
        self.assertTrue(drift_res['ks_flag_ratio'] >= 0.5)
        self.assertTrue(drift_res['mean_psi'] > 0.20)
        self.assertTrue(drift_res['drift_confirmed'])

    def test_performance_drift_confirmation(self):
        """
        Asserts retraining only triggers if statistical drift is explicitly
        coupled with physical performance collapses.
        """
        drift_flagged = {"drift_confirmed": True, "reason": "KS Shift"}
        good_perf = {"live_precision": 0.55, "baseline_precision": 0.52} # Precision actually went up
        bad_perf = {"live_precision": 0.35, "baseline_precision": 0.55}  # Precision collapsed
        
        # Statistical drift but making money = Ignore
        self.assertFalse(DriftMonitor.construct_retrain_trigger(drift_flagged, good_perf))
        
        # Statistical drift AND losing money = Retrain
        self.assertTrue(DriftMonitor.construct_retrain_trigger(drift_flagged, bad_perf))

    def test_shadow_validation_pipeline(self):
        """
        Asserts Challengers must mathematically defeat Champions without
        causing lethal tail risks.
        """
        champ = {"realized_sharpe": 1.5, "realized_precision": 0.55, "worst_drawdown": -0.05}
        chall_fail = {"realized_sharpe": 1.2, "realized_precision": 0.50, "worst_drawdown": -0.05, "eval_duration_days": 10}
        chall_win = {"realized_sharpe": 2.1, "realized_precision": 0.60, "worst_drawdown": -0.04, "eval_duration_days": 10}
        chall_suicide = {"realized_sharpe": 3.0, "realized_precision": 0.70, "worst_drawdown": -0.40, "eval_duration_days": 10}
        
        # 1. Challenger underperforms (Reject)
        passed, _ = ShadowValidation.evaluate_shadow_performance(champ, chall_fail)
        self.assertFalse(passed)
        
        # 2. Challenger objectively wins (Promote)
        passed, _ = ShadowValidation.evaluate_shadow_performance(champ, chall_win)
        self.assertTrue(passed)
        
        # 3. Challenger wins Sharpe but introduces absolute destructive tail risk (Reject)
        passed, _ = ShadowValidation.evaluate_shadow_performance(champ, chall_suicide)
        self.assertFalse(passed)

    def test_capital_protection_lockout(self):
        """
        Asserts the system refuses to retrain if the portfolio is actively burning.
        """
        safe_ports = {"current_drawdown": -0.02, "max_drawdown_limit": -0.10}
        danger_ports = {"current_drawdown": -0.25, "max_drawdown_limit": -0.10}
        vol_safe = {"current_vix": 15.0, "baseline_vix": 15.0} # Normal markets
        
        # 1. Safe markets, Safe Data -> Cleared
        locked, _ = RetrainingPipeline._check_capital_protection_lockout(safe_ports, vol_safe, QuarantineStatus.SAFE)
        self.assertFalse(locked)
        
        # 2. Safe markets, BUT corrupted data feeds -> Locked
        locked, _ = RetrainingPipeline._check_capital_protection_lockout(safe_ports, vol_safe, QuarantineStatus.DEGRADED)
        self.assertTrue(locked)
        
        # 3. Safe Data, BUT portfolio in severe drawdown -> Locked (Preserve capital, don't adapt dangerously)
        locked, _ = RetrainingPipeline._check_capital_protection_lockout(danger_ports, vol_safe, QuarantineStatus.SAFE)
        self.assertTrue(locked)

if __name__ == '__main__':
    unittest.main()
