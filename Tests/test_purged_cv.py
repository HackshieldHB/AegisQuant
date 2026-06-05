import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.PurgedCV import PurgedKFold

class TestPurgedCV(unittest.TestCase):
    def setUp(self):
        # Create a simple chronological index
        self.dates = pd.date_range('2026-01-01', periods=100, freq='15min')
        
        # Events DataFrame
        # Event path lasts for 3 distinct bars
        t_starts = self.dates
        t_ends = self.dates + pd.Timedelta(minutes=45) 
        
        self.events_df = pd.DataFrame({'t_start': t_starts, 't_end': t_ends}, index=self.dates)
        self.X = pd.DataFrame({'feat': np.arange(100)}, index=self.dates)
        
    def test_purging_and_embargo(self):
        """
        Prove that overlapping bars are explicitly removed to combat forward leakage,
        and post-validation bars are embargoed via fixed gaps.
        """
        pkf = PurgedKFold(n_splits=5, embargo_pct=0.02) # 2 bars embargo minimum size
        folds = list(pkf.split(self.X, events_df=self.events_df))
        
        # 100 samples / 5 splits = 20 test samples per fold (Indices 20 through 39 for fold 1)
        train_idx_1, test_idx_1 = folds[1] 
        
        self.assertEqual(len(test_idx_1), 20)
        self.assertEqual(test_idx_1[0], 20)
        self.assertEqual(test_idx_1[-1], 39)
        
        # Assert purging:
        # Index 17 ends exactly at 20 (overlap) -> Drop
        # Index 18 ends at 21 (overlap into test) -> Drop
        # Index 19 ends at 22 (overlap into test) -> Drop
        self.assertNotIn(17, train_idx_1)
        self.assertNotIn(18, train_idx_1)
        self.assertNotIn(19, train_idx_1)
        
        # Index 16 ends at 19, so it never touches the test set at 20 -> Keep
        self.assertIn(16, train_idx_1) 
        
        # Assert embargo:
        # Test ends at 39. Embargo length is 2 bars.
        # Indices 40 and 41 must be dropped.
        self.assertNotIn(40, train_idx_1)
        # Index 42 starts at the exact timestamp as max_test_end, so it is also purged!
        self.assertNotIn(42, train_idx_1)
        
        # Training resumes safely at 43
        self.assertIn(43, train_idx_1)

if __name__ == '__main__':
    unittest.main()
