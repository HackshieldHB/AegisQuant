import unittest
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.ModelHealthMonitor import ModelHealthMonitor

class TestModelHealthMonitor(unittest.TestCase):
    
    def setUp(self):
        # 100 bar window, min 5 signals
        self.monitor = ModelHealthMonitor(min_signal_rate=5,
                                          signal_rate_window=100,
                                          max_directional_imbalance=0.90,
                                          prob_compression_threshold=0.01,
                                          max_regime_stickiness_bars=2016, 
                                          max_meta_rejection_rate=0.80)
                                          
    def test_signal_collapse(self):
        """Asserts that total silence for > window bars triggers Safe Mode."""
        # Feed 101 empty bars
        for _ in range(101):
            self.monitor.ingest_tick(is_raw_cusum=False)
            
        health = self.monitor.evaluate_health()
        self.assertTrue(health['is_safe_mode'])
        self.assertTrue(any("Signal Collapse" in a for a in health['anomalies']))

    def test_directional_bias(self):
        """Asserts that 95% long signals triggers Directional Bias Anomaly."""
        for _ in range(19):
             self.monitor.ingest_tick(is_raw_cusum=True, meta_prob=0.8, meta_approved=True, signal_direction=1)
        self.monitor.ingest_tick(is_raw_cusum=True, meta_prob=0.8, meta_approved=True, signal_direction=-1)
        
        # 19 Longs vs 1 Short = 95%
        health = self.monitor.evaluate_health()
        self.assertTrue(health['is_safe_mode'])
        self.assertTrue(any("Directional Bias" in a for a in health['anomalies']))

    def test_confidence_degeneration_compression(self):
        """Asserts that variance crashing exactly on ~0.5 triggers Indecision Compression."""
        for _ in range(60):
            # Give identically 0.505 probs (variance = 0)
            self.monitor.ingest_tick(is_raw_cusum=True, meta_prob=0.505)
            
        health = self.monitor.evaluate_health()
        self.assertTrue(health['is_safe_mode'])
        self.assertTrue(any("Probability Indecision Compression" in a for a in health['anomalies']))
        
    def test_confidence_degeneration_overconfidence(self):
        """Asserts that printing consistent 0.999 triggers Overconfidence Compression."""
        for _ in range(60):
            self.monitor.ingest_tick(is_raw_cusum=True, meta_prob=0.999)
            
        health = self.monitor.evaluate_health()
        self.assertTrue(health['is_safe_mode'])
        self.assertTrue(any("Probability Overconfidence" in a for a in health['anomalies']))

    def test_regime_stickiness(self):
        """Asserts that a regime locking up for physical days triggers an alarm."""
        for _ in range(2020):
            # Identical regime state [1] for 2000+ bars
            self.monitor.ingest_tick(is_raw_cusum=False, current_regime=1)
            
        health = self.monitor.evaluate_health()
        self.assertTrue(health['is_safe_mode'])
        self.assertTrue(any("frozen in State 1" in a for a in health['anomalies']))

    def test_meta_rejection_spike(self):
        """Asserts > 80% primary flow rejection flags the system."""
        for _ in range(25):
             # 25 primary events... only 1 approved
             app = True if _ == 0 else False
             self.monitor.ingest_tick(is_raw_cusum=True, meta_approved=app, signal_direction=1 if app else None)
             
        health = self.monitor.evaluate_health()
        self.assertTrue(health['is_safe_mode'])
        self.assertTrue(any("Meta-Filter Execution Freeze" in a for a in health['anomalies']))
        # Assert Sizing gets cut to 25% during Safe Mode
        self.assertEqual(health['sizing_multiplier'], 0.25)
        self.assertTrue(health['retraining_locked'])

    def test_healthy_state(self):
        """Asserts normal operation keeps the system unrestricted."""
        # 10 bars -> 5 approved longs, 5 approved shorts. Varied probs. 
        probs = [0.6, 0.4, 0.7, 0.3, 0.8, 0.2, 0.65, 0.35, 0.9, 0.1]
        dirs = [1, -1, 1, -1, 1, -1, 1, -1, 1, -1]
        
        for p, d in zip(probs, dirs):
             self.monitor.ingest_tick(is_raw_cusum=True, meta_prob=p, meta_approved=True, signal_direction=d, current_regime=d)
             
        health = self.monitor.evaluate_health()
        self.assertFalse(health['is_safe_mode'])
        self.assertEqual(health['sizing_multiplier'], 1.0)
        self.assertFalse(health['retraining_locked'])
        
if __name__ == '__main__':
    unittest.main()
