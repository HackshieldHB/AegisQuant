import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.MetaLabeler import MetaLabeler

class TestMetaLabeler(unittest.TestCase):
    
    def test_meta_target_generation(self):
        """
        Prove that the Meta-Target correctly binds to a logical binary comparison 
        of the Primary Model vs Reality.
        """
        y_true = pd.Series([1, -1, 1, -1])
        y_pred = pd.Series([1, 1, -1, -1])
        
        targets = MetaLabeler.generate_meta_labels(y_true, y_pred)
        
        self.assertEqual(targets.iloc[0], 1) # Hit TP logic mapped correctly
        self.assertEqual(targets.iloc[1], 0) # Missed logic flagged 0
        self.assertEqual(targets.iloc[2], 0) 
        self.assertEqual(targets.iloc[3], 1) # Short TP hit 
        
    def test_kelly_sizing(self):
        """
        Verify the mathematical boundaries of the Institutional Kelly constraints.
        f_full = p - ((1-p)/1.5)
        Then * kelly_fraction, then clip bounds.
        """
        p = pd.Series([0.1, 0.4, 0.6, 0.9])
        
        # Testing Hard Risk Caps
        sizes = MetaLabeler.compute_kelly_size(p, payoff_ratio=1.5, kelly_fraction=0.5, max_risk_cap=0.02)
        
        # 10% and 40% win rates at 1.5R produce negative EVs -> 0 allocate
        self.assertEqual(sizes.iloc[0], 0.0)
        self.assertEqual(sizes.iloc[1], 0.0)
        
        # 60% and 90% produce positive EVs, but get clipped by the strict 2% account limit
        self.assertEqual(sizes.iloc[2], 0.02)
        self.assertEqual(sizes.iloc[3], 0.02)
        
        # Testing absolute scale to prove equation holds without artificially clipping early
        sizes_uncap = MetaLabeler.compute_kelly_size(p, payoff_ratio=1.5, kelly_fraction=0.5, max_risk_cap=1.0)
        self.assertAlmostEqual(sizes_uncap.iloc[2], 0.333333333333 * 0.5, places=5)

if __name__ == '__main__':
    unittest.main()
