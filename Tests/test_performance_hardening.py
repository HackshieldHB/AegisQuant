import numpy as np
import pandas as pd
import time
import sys
import os
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Labeling.TripleBarrier import cusum_filter, _cusum_numba_core, _cusum_python_core, NUMBA_AVAILABLE

class TestPerformanceEngineering(unittest.TestCase):
    
    def test_deterministic_numerical_parity(self):
        """
        Asserts the C-level Numba implementation produces the EXACT same event
        sampling timeline as the reference Python implementation.
        """
        # Generate 100,000 ticks of synthetic price path
        np.random.seed(42)
        returns = np.random.normal(0, 0.001, 100000)
        price_path = np.exp(np.cumsum(returns)) * 100.0
        
        diffs = np.diff(price_path)
        diffs = np.insert(diffs, 0, 0.0) # Match length
        
        # Enforce memory layout matching production
        target_diffs = np.ascontiguousarray(diffs, dtype=np.float64)
        h = 0.5
        
        python_events = _cusum_python_core(target_diffs, h)
        
        if NUMBA_AVAILABLE:
            numba_events = _cusum_numba_core(target_diffs, h)
            
            # 1. Total Event Count Parity
            self.assertEqual(len(python_events), len(numba_events))
            
            # 2. Exact Index Match Parity (< 1e-12 difference, mathematically 0 since they are int64 indices)
            np.testing.assert_array_equal(python_events, numba_events)
            
    def test_fallback_degradation_path(self):
        """
        Tests the wrapper smoothly falls back to Python if forced.
        """
        dates = pd.date_range('2026-01-01', periods=1000, freq='1min')
        np.random.seed(77)
        price = np.cumsum(np.random.normal(0, 0.1, 1000)) + 100
        df = pd.DataFrame({'close': price}, index=dates)
        
        # Test 1: Numba Path (if available)
        res_numba = cusum_filter(df, h=0.5, use_numba=True)
        
        # Test 2: Forced Python Degradation
        res_python = cusum_filter(df, h=0.5, use_numba=False)
        
        self.assertEqual(len(res_numba), len(res_python))
        # Series indices must perfectly match
        self.assertTrue((res_numba.index == res_python.index).all())
        
    def test_stability_guards_overflow(self):
        """
        Asserts the CUSUM arrays do not explode if given infinite wicks.
        """
        diffs = np.array([0.0, 1e9, -1e9, 0.0, 0.1], dtype=np.float64)
        
        # Should clip internally and still execute without raising ValueError or OverflowError
        res_python = _cusum_python_core(diffs, 0.5)
        self.assertTrue(len(res_python) > 0)
        
        if NUMBA_AVAILABLE:
            res_numba = _cusum_numba_core(diffs, 0.5)
            self.assertTrue(len(res_numba) > 0)
            np.testing.assert_array_equal(res_python, res_numba)

if __name__ == '__main__':
    unittest.main()
