import pandas as pd
import numpy as np
from typing import Dict, List, Any
from Data.Models import Candle
from Core.Logger import AG_LOGGER

# Module-level cache: (n_candles, last_ts) → DataFrame
# Shared across all IndicatorEngine instances so the same candle set is
# computed only once per scan cycle regardless of how many strategies request it.
_INDICATOR_CACHE: dict = {}
_CACHE_MAX_ENTRIES = 14  # 7 symbols × 2 timeframes (5m + 1h) with headroom


class IndicatorEngine:
    """
    Centralized engine for calculating technical indicators using pure pandas.
    Replacing pandas-ta due to compatibility issues with Python 3.14.
    """
    
    def __init__(self):
        self.logger = AG_LOGGER

    def calculate_indicators(self, candles: List[Candle]) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame()

        # Return cached result for this candle set (same length + last timestamp)
        # eliminates the 5× redundant recomputation inside EnsembleStrategy.
        try:
            last_ts = float(candles[-1].timestamp.timestamp()
                            if hasattr(candles[-1].timestamp, "timestamp")
                            else candles[-1].timestamp)
            cache_key = (len(candles), last_ts)
            if cache_key in _INDICATOR_CACHE:
                return _INDICATOR_CACHE[cache_key].copy()
        except Exception:
            cache_key = None

        # Convert candles to DataFrame
        data = [vars(c) for c in candles]
        df = pd.DataFrame(data)
        
        # Ensure correct types
        cols = ['open', 'high', 'low', 'close', 'volume']
        for col in cols:
            df[col] = df[col].astype(float)

        try:
            # 1. Moving Averages
            # EMA
            df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
            df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()
            df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()
            
            # SMA
            df['SMA_14'] = df['close'].rolling(window=14).mean()  # ADD FOR AI MODELS
            df['SMA_50'] = df['close'].rolling(window=50).mean()
            df['SMA_200'] = df['close'].rolling(window=200).mean()
            
            # Fill NaN values in SMA with forward/backward fill
            df['SMA_14'] = df['SMA_14'].bfill().ffill().fillna(df['close'].mean() if len(df['close']) > 0 else 0)
            df['SMA_50'] = df['SMA_50'].bfill().ffill().fillna(df['close'].mean() if len(df['close']) > 0 else 0)
            df['SMA_200'] = df['SMA_200'].bfill().ffill().fillna(df['close'].mean() if len(df['close']) > 0 else 0)

            # 2. MACD (12, 26, 9)
            exp1 = df['close'].ewm(span=12, adjust=False).mean()
            exp2 = df['close'].ewm(span=26, adjust=False).mean()
            df['MACD_12_26_9'] = exp1 - exp2
            df['MACDs_12_26_9'] = df['MACD_12_26_9'].ewm(span=9, adjust=False).mean()
            df['MACDh_12_26_9'] = df['MACD_12_26_9'] - df['MACDs_12_26_9']

            # 3. RSI (14)
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            
            # Prevent division by zero with small epsilon
            rs = gain / (loss.replace(0, 1e-8))
            df['RSI_14'] = 100 - (100 / (1 + rs))
            # Fill NaN for initial values with forward/backward fill
            df['RSI_14'] = df['RSI_14'].bfill().fillna(50.0)
            df['RSI'] = df['RSI_14'] # Alias

            # 4. Bollinger Bands (20, 2)
            df['BBM_20_2.0'] = df['close'].rolling(window=20).mean() # Middle
            std = df['close'].rolling(window=20).std()
            df['BBU_20_2.0'] = df['BBM_20_2.0'] + (std * 2) # Upper
            df['BBL_20_2.0'] = df['BBM_20_2.0'] - (std * 2) # Lower
            
            # Fill NaN values in Bollinger Bands
            df['BBM_20_2.0'] = df['BBM_20_2.0'].bfill().ffill().fillna(df['close'].mean() if len(df['close']) > 0 else 0)
            df['BBU_20_2.0'] = df['BBU_20_2.0'].bfill().ffill().fillna(df['close'].max() if len(df['close']) > 0 else 1.0)
            df['BBL_20_2.0'] = df['BBL_20_2.0'].bfill().ffill().fillna(df['close'].min() if len(df['close']) > 0 else 0.1)

            # 5. ATR (14)
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['ATRr_14'] = true_range.rolling(window=14).mean()
            # Fill NaN with backfill and forward fill
            df['ATRr_14'] = df['ATRr_14'].bfill().ffill().fillna(0.01)

            # 6. Stochastic (14, 3, 3)
            low_min = df['low'].rolling(window=14).min()
            high_max = df['high'].rolling(window=14).max()
            df['stoch'] = 100 * ((df['close'] - low_min) / (high_max - low_min + 1e-8))
            df['stoch'] = df['stoch'].bfill().ffill().fillna(50.0)

            # 7. ADX (Average Directional Index) - Simplified
            plus_dm = df['high'].diff()
            minus_dm = -df['low'].diff()
            tr1 = df['high'] - df['low']
            tr = pd.concat([tr1, np.abs(df['high'] - df['close'].shift()), np.abs(df['low'] - df['close'].shift())], axis=1).max(axis=1)
            atr = tr.rolling(window=14).mean()
            plus_di = 100 * (plus_dm.rolling(window=14).mean() / (atr + 1e-8))
            minus_di = 100 * (minus_dm.rolling(window=14).mean() / (atr + 1e-8))
            df['adx'] = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-8)
            df['adx'] = df['adx'].bfill().ffill().fillna(25.0)

            # 8. Returns (log returns for AI models)
            df['return'] = np.log(df['close'] / df['close'].shift()).fillna(0.0)

            # ===== SELECT ONLY ESSENTIAL COLUMNS FOR PREDICTION =====
            # Return a clean dataframe with just the columns needed for predictions
            # This avoids duplicate column issues
            essential_cols = [
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'EMA_9', 'EMA_21', 'EMA_50', 'EMA_200',
                'SMA_14', 'SMA_50', 'SMA_200',
                'MACD_12_26_9', 'MACDs_12_26_9', 'MACDh_12_26_9',
                'RSI_14', 'RSI',
                'BBM_20_2.0', 'BBU_20_2.0', 'BBL_20_2.0',
                'ATRr_14',
                'stoch', 'adx', 'return'
            ]
            
            # Only keep columns that exist
            available_cols = [col for col in essential_cols if col in df.columns]
            df = df[available_cols]

            # Store in shared cache (evict oldest entry when full)
            if cache_key is not None:
                if len(_INDICATOR_CACHE) >= _CACHE_MAX_ENTRIES:
                    _INDICATOR_CACHE.pop(next(iter(_INDICATOR_CACHE)))
                _INDICATOR_CACHE[cache_key] = df

            return df

        except Exception as e:
            self.logger.error(f"Error calculating indicators: {e}")
            return pd.DataFrame()
