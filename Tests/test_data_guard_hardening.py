import unittest
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.DataGuard import DataGuard, QuarantineStatus

class TestDataGuardHardening(unittest.TestCase):
    
    def setUp(self):
        # Create a clean base dataframe of 100 prices
        dates = pd.date_range('2026-01-01', periods=100, freq='1min')
        np.random.seed(42)
        base = np.cumprod(1 + np.random.normal(0, 0.001, 100)) * 100
        
        self.df_clean = pd.DataFrame({
            'open': base,
            'high': base * 1.001,
            'low': base * 0.999,
            'close': base * 1.0005,
            'volume': np.random.uniform(10, 100, 100)
        }, index=dates)

    def test_ohlc_structural_integrity(self):
        """Asserts that inverted candles and negative volumes are flagged as Corrupted."""
        # 1. Clean data passes
        status, _ = DataGuard.validate_symbol(self.df_clean, "BTCUSDT")
        self.assertEqual(status, QuarantineStatus.SAFE)
        
        # 2. Inverted Low > High geometry
        df_inverted = self.df_clean.copy()
        df_inverted.loc[df_inverted.index[50], 'low'] = 99999.0
        status, _ = DataGuard.validate_symbol(df_inverted, "BTCUSDT")
        self.assertEqual(status, QuarantineStatus.CORRUPTED)
        
        # 3. Negative volume
        df_neg_vol = self.df_clean.copy()
        df_neg_vol.loc[df_neg_vol.index[50], 'volume'] = -50.0
        status, _ = DataGuard.validate_symbol(df_neg_vol, "BTCUSDT")
        self.assertEqual(status, QuarantineStatus.CORRUPTED)
        
    def test_latency_staleness(self):
        """Asserts that exactly duplicated candles flag as Degraded."""
        df_frozen = self.df_clean.copy()
        
        # Freeze all prices for 6 minutes (zero variance heartbeat failure)
        frozen_close = df_frozen['close'].iloc[20]
        df_frozen.loc[df_frozen.index[20:26], 'close'] = frozen_close
        df_frozen.loc[df_frozen.index[20:26], 'open'] = frozen_close
        df_frozen.loc[df_frozen.index[20:26], 'high'] = frozen_close
        df_frozen.loc[df_frozen.index[20:26], 'low'] = frozen_close
        
        status, _ = DataGuard.validate_symbol(df_frozen, "BTCUSDT")
        self.assertEqual(status, QuarantineStatus.DEGRADED)
        
    def test_flash_crash_isolation(self):
        """
        Asserts that an isolated 1-tick spike is safely scrubbed, but
        a persistent regime gap is kept exactly as is.
        """
        # --- TEST 1: ISOLATED FLASH CRASH ---
        df_flash = self.df_clean.copy()
        # Massive physical drop
        df_flash.loc[df_flash.index[50], 'close'] = 50.0
        df_flash.loc[df_flash.index[50], 'low'] = 45.0
        df_flash.loc[df_flash.index[50], 'open'] = 100.0
        df_flash.loc[df_flash.index[50], 'high'] = 100.5
        
        # Immediate recovery next tick
        df_flash.loc[df_flash.index[51], 'close'] = self.df_clean['close'].iloc[51]
        df_flash.loc[df_flash.index[51], 'low'] = self.df_clean['low'].iloc[51]
        df_flash.loc[df_flash.index[51], 'open'] = self.df_clean['open'].iloc[51]
        df_flash.loc[df_flash.index[51], 'high'] = self.df_clean['high'].iloc[51]
        
        # Should be scrubbed (revert wick, preserve gap)
        status, clean_df = DataGuard.validate_symbol(df_flash, "BTCUSDT")
        self.assertEqual(status, QuarantineStatus.SAFE) # SAFE because it was a minor isolate and successfully scrubbed
        # Check that high/low was clamped
        self.assertEqual(clean_df.loc[df_flash.index[50], 'high'], clean_df.loc[df_flash.index[50], 'open'])
        
        # --- TEST 2: MULTIPLE FLASH CRASHES (Too dangerous) ---
        df_malicious = self.df_clean.copy()
        # Spacing > 20 periods so rolling ATR isn't permanently poisoned by previous crashes
        for idx in [25, 50, 75, 95]:
            df_malicious.loc[df_malicious.index[idx], 'close'] = 10.0
            df_malicious.loc[df_malicious.index[idx], 'low'] = 5.0
            df_malicious.loc[df_malicious.index[idx], 'open'] = 100.0
            df_malicious.loc[df_malicious.index[idx], 'high'] = 100.0
            
            df_malicious.loc[df_malicious.index[idx+1], 'close'] = self.df_clean['close'].iloc[idx+1]
            df_malicious.loc[df_malicious.index[idx+1], 'low'] = self.df_clean['low'].iloc[idx+1]
            df_malicious.loc[df_malicious.index[idx+1], 'open'] = self.df_clean['open'].iloc[idx+1]
            df_malicious.loc[df_malicious.index[idx+1], 'high'] = self.df_clean['high'].iloc[idx+1]
            
        status, _ = DataGuard.validate_symbol(df_malicious, "BTCUSDT")
        self.assertEqual(status, QuarantineStatus.CORRUPTED)
        
        # --- TEST 3: REAL VOLATILITY BREAKOUT (Persistence) ---
        df_breakout = self.df_clean.copy()
        # Massive drop and then stays down
        df_breakout.loc[df_breakout.index[50:], 'close'] = df_breakout.loc[df_breakout.index[50:], 'close'] * 0.5
        df_breakout.loc[df_breakout.index[50:], 'low'] = df_breakout.loc[df_breakout.index[50:], 'low'] * 0.5
        df_breakout.loc[df_breakout.index[50:], 'open'] = df_breakout.loc[df_breakout.index[50:], 'open'] * 0.5
        df_breakout.loc[df_breakout.index[50:], 'high'] = df_breakout.loc[df_breakout.index[50:], 'high'] * 0.5
        
        status, clean_df = DataGuard.validate_symbol(df_breakout, "BTCUSDT")
        # Since it didn't snap back, it's considered a genuine market gap/crash
        self.assertEqual(status, QuarantineStatus.SAFE)
        # Verify the structure wasn't scrubbed unnecessarily 
        self.assertTrue((clean_df.loc[clean_df.index[50:], 'close'] < 60).all())

if __name__ == '__main__':
    unittest.main()
