import pandas as pd
import numpy as np

def _resample_to_htf(ltf_df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Deterministically resamples lower timeframe (LTF) to higher timeframe (HTF)
    matching Binance exchange construction rules.
    """
    if ltf_df.empty:
        return pd.DataFrame()
        
    # Ensure datetime index
    if not isinstance(ltf_df.index, pd.DatetimeIndex):
        raise ValueError("LTF DataFrame must have a DatetimeIndex for resampling.")
        
    # Mandatory aggregation schema
    agg_dict = {
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
    }
    
    # Add volume if it exists
    if 'volume' in ltf_df.columns:
        agg_dict['volume'] = 'sum'
        
    # Resample using "left" label and "left" closed to match typical crypto boundaries
    # e.g., 14:00 to 14:59:59 becomes the 14:00 candle.
    htf_df = ltf_df.resample(rule, label='left', closed='left').agg(agg_dict)
    
    # Drop incomplete/NaN rows resulting from missing data gaps
    htf_df = htf_df.dropna(subset=['close'])
    return htf_df

def _calculate_htf_features(htf_df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates macro trend, VWAP, and ATR on the HTF dataframe natively.
    """
    if htf_df.empty:
        return pd.DataFrame()
        
    feats = pd.DataFrame(index=htf_df.index)
    
    # 1. EMA Slope (Normalized)
    fast_ema = htf_df['close'].ewm(span=9, adjust=False).mean()
    slow_ema = htf_df['close'].ewm(span=21, adjust=False).mean()
    
    # Trend alignment: +1 for Bullish, -1 for Bearish, else 0
    feats['htf_trend'] = np.where(fast_ema > slow_ema, 1.0, np.where(fast_ema < slow_ema, -1.0, 0.0))
    
    # 2. HTF ATR calculation
    prev_close = htf_df['close'].shift(1)
    tr1 = htf_df['high'] - htf_df['low']
    tr2 = (htf_df['high'] - prev_close).abs()
    tr3 = (htf_df['low'] - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    feats['htf_atr'] = true_range.ewm(span=14, adjust=False).mean()
    
    # 3. Rolling VWAP (Typical Price)
    typical_price = (htf_df['high'] + htf_df['low'] + htf_df['close']) / 3.0
    
    if 'volume' in htf_df.columns:
        vol = htf_df['volume']
        vol_safe = np.where(vol == 0, 1e-9, vol)
        vol_safe_series = pd.Series(vol_safe, index=vol.index)
        
        # 20-period rolling 
        rolling_tp_vol = (typical_price * vol).rolling(window=20, min_periods=1).sum()
        rolling_vol = vol_safe_series.rolling(window=20, min_periods=1).sum()
        feats['htf_vwap'] = rolling_tp_vol / rolling_vol
    else:
        # Fallback to rolling mean of TP if no volume
        feats['htf_vwap'] = typical_price.rolling(window=20, min_periods=1).mean()
        
    return feats

def add_mtf_features(ltf_df: pd.DataFrame) -> pd.DataFrame:
    """
    Main entry point. Ingests 15m Base data, generates 1H and 4H context,
    and safely forward-fills them down to the LTF execution rows without lookahead leakage.
    """
    if ltf_df.empty:
        return ltf_df
        
    result = ltf_df.copy()
    
    if not isinstance(result.index, pd.DatetimeIndex):
        if 'timestamp' in result.columns:
            result['timestamp'] = pd.to_datetime(result['timestamp'], utc=True)
            result = result.set_index('timestamp')
        elif 'open_time' in result.columns:
            # Assumed ms timestamps from Binance
            result['open_time'] = pd.to_datetime(result['open_time'], unit='ms', utc=True)
            result = result.set_index('open_time')
        else:
            raise ValueError("Dataframe must have a DatetimeIndex or 'timestamp'/'open_time' column for MTF aggregation.")
    else:
        if result.index.tzinfo is None:
             result.index = result.index.tz_localize('UTC')
            
    # Quick sanity check on time monotonicity
    if not result.index.is_monotonic_increasing:
        result = result.sort_index()

    # Define our anchoring rules
    htf_rules = {'1H': '1h', '4H': '4h'}
    
    for prefix, rule in htf_rules.items():
        # Step A: Resample to exact HTF
        htf_df = _resample_to_htf(result, rule)
        
        # Step B: Compute the indicators ON the HTF dataset natively
        htf_feats = _calculate_htf_features(htf_df)
        
        # Step C: STRICT ANTI-LEAKAGE ALIGNMENT
        # For a 1H candle labeled "14:00" (which covers 14:00 to 14:59:59), 
        # its data is ONLY fully known exactly at 15:00.
        # So we shift the index forward by exactly 1 HTF period before merging!
        
        # Add the offset to the index (e.g. 1h or 4h)
        offset = pd.to_timedelta(rule)
        htf_feats.index = htf_feats.index + offset
        
        # Prefix the columns so they don't collide
        htf_feats.columns = [f"{prefix}_{c}" for c in htf_feats.columns]
        
        # Step D: Merge onto the LTF index.
        # We use merge_asof with backward direction to ensure absolute mathematical causality.
        # The shifted HTF closing time will match the LTF timestamp exactly, or fall back 
        # to the last known complete HTF bar.
        ltf_temp = result.reset_index()
        htf_temp = htf_feats.reset_index()
        
        # Determine the name of the time column, usually 'index' if it was named None
        time_col_ltf = 'index' if ltf_temp.columns[0] == 'index' else ltf_temp.columns[0]
        time_col_htf = 'index' if htf_temp.columns[0] == 'index' else htf_temp.columns[0]

        # Normalize datetime resolution on BOTH merge keys. pandas 2.x treats
        # datetime64[ms]/[us]/[ns] as incompatible merge keys and raises
        # "incompatible merge keys ... must be the same type" — which silently
        # broke every retrain. Force both keys to ns (UTC) before merge_asof.
        for _df, _col in ((ltf_temp, time_col_ltf), (htf_temp, time_col_htf)):
            if pd.api.types.is_datetime64_any_dtype(_df[_col]):
                _df[_col] = pd.to_datetime(_df[_col], utc=True).astype("datetime64[ns, UTC]")

        merged = pd.merge_asof(
             ltf_temp.sort_values(time_col_ltf), 
             htf_temp.sort_values(time_col_htf), 
             left_on=time_col_ltf, 
             right_on=time_col_htf, 
             direction='backward'
        )
        
        result = merged.set_index(time_col_ltf)
        result.index.name = ltf_df.index.name # Restore original name
        
    # Step F: Cross-Timeframe Derived Features (LTF vs HTF)
    
    # Volatility Structure Regime: ATR_15m / ATR_4H
    # 1. Compute LTF ATR first
    prev_close = result['close'].shift(1)
    tr1 = result['high'] - result['low']
    tr2 = (result['high'] - prev_close).abs()
    tr3 = (result['low'] - prev_close).abs()
    ltf_tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    result['ltf_atr_15m'] = ltf_tr.ewm(span=14, adjust=False).mean()
    
    # 2. Ratio & Regime Smoothing (prevent zero div)
    epsilon = 1e-9
    denom = np.maximum(result['4H_htf_atr'].fillna(0), epsilon)
    atr_ratio = result['ltf_atr_15m'] / denom
    
    # Smooth it and log transform for stability
    # log1p safely handles small numbers
    result['mtf_volatility_regime_ratio'] = np.log1p(atr_ratio.ewm(span=5, adjust=False).mean())
    
    # Anchored Macro Distance: Normalized distance to 4H VWAP using 4H ATR
    vwap_diff = result['close'] - result['4H_htf_vwap']
    result['mtf_macro_vwap_distance'] = vwap_diff / denom
    
    # Trend Alignment Aggregation
    result['mtf_trend_alignment'] = result['1H_htf_trend'].fillna(0) + result['4H_htf_trend'].fillna(0)
    # Clip to -1, 1 (e.g. +2 becomes +1 indicating unified trend)
    result['mtf_trend_alignment'] = result['mtf_trend_alignment'].clip(-1.0, 1.0)
    
    # Step G: Final Safety Cleanup
    # Replace inf with nan, then ffill, then fillna(0) for leading edges
    result.replace([np.inf, -np.inf], np.nan, inplace=True)
    
    # Clean up intermediate redundant columns if desired, but we keep core ones for training
    
    return result

