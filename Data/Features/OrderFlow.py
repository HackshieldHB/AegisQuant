import pandas as pd
import numpy as np

def rolling_z_score(series: pd.Series, window: int) -> pd.Series:
    """
    Computes a strictly historical rolling Z-score for normalization.
    Returns:
        pd.Series normalized to past mean/std without lookahead leakage.
    """
    if window <= 1 or len(series) < window:
        return pd.Series(0.0, index=series.index)
        
    rolling_mean = series.rolling(window=window, min_periods=window).mean()
    # ddof=1 for sample standard deviation
    rolling_std = series.rolling(window=window, min_periods=window).std(ddof=1)
    
    # Avoid division by zero
    z_score = (series - rolling_mean) / rolling_std.replace(0, np.nan)
    
    # Forward-fill NaNs or fill with 0 (neutral) for early periods/zero-variance windows
    return z_score.fillna(0.0)

def compute_cumulative_volume_delta(
    imbalance: pd.Series, 
    mode: str = 'daily', 
    window: int = 24, 
    decay: float = 0.95,
    timestamp_index: pd.DatetimeIndex = None
) -> pd.Series:
    """
    Computes Cumulative Volume Delta (CVD) with strict bounds to prevent runaway stationarity drift.
    
    Args:
        imbalance: Series of Buy Vol - Sell Vol.
        mode: 'daily' (reset at UTC 00:00), 'rolling' (sum over N bars), 'decay' (exponential).
        window: N bars for 'rolling' mode.
        decay: Multiplier for 'decay' mode.
        timestamp_index: Required for 'daily' reset mode.
    """
    if mode == 'rolling':
        return imbalance.rolling(window=window, min_periods=1).sum()
        
    elif mode == 'decay':
        # Exponentially Weighted Moving Sum approximation via EWM
        # We use alpha = 1 - decay
        if decay >= 1.0 or decay <= 0.0:
            return imbalance # Fallback if invalid decay
        # adjust=False ensures strict anti-leakage
        return imbalance.ewm(alpha=(1.0 - decay), adjust=False).mean() / (1.0 - decay)
        
    elif mode == 'daily':
        if timestamp_index is None:
            # Fallback to rolling if no time index
            return imbalance.rolling(window=window, min_periods=1).sum()
            
        cvd = pd.Series(0.0, index=imbalance.index)
        # Groups by Year-Month-Day
        groups = imbalance.groupby(timestamp_index.date)
        for date, group in groups:
            cvd.loc[group.index] = group.cumsum()
        return cvd
        
    else:
        # Default safety fallback
        return imbalance.rolling(window=window, min_periods=1).sum()

def add_order_flow_features(
    df: pd.DataFrame, 
    cvd_mode: str = 'daily',
    cvd_window: int = 96, # 96 * 15m = 24h
    cvd_decay: float = 0.95,
    normalization_window: int = 96
) -> pd.DataFrame:
    """
    Ingests OHLCV + Taker metrics to build Phase 2 Flow Features.
    Handles Klines or AggTrades data structures.
    """
    result = df.copy()
    
    # Ensure index is datetime for daily grouping if present
    ts_idx = None
    if isinstance(result.index, pd.DatetimeIndex):
         ts_idx = result.index
         if ts_idx.tzinfo is None:
              ts_idx = ts_idx.tz_localize('UTC')
              result.index = ts_idx
    elif 'timestamp' in result.columns:
         result['timestamp'] = pd.to_datetime(result['timestamp'], utc=True)
         result = result.set_index('timestamp')
         ts_idx = result.index
    elif 'open_time' in result.columns:
         result['open_time'] = pd.to_datetime(result['open_time'], unit='ms', utc=True)
         # Do not set index to maintain compatibility, but pass it for daily reset
         ts_idx = pd.DatetimeIndex(result['open_time'])
    elif 'time' in result.columns:
         result['time'] = pd.to_datetime(result['time'], utc=True)
         ts_idx = pd.DatetimeIndex(result['time'])

    # 1. Identify Data Source Schema
    taker_buy_col = None
    total_vol_col = 'volume'
    
    if 'taker_buy_base' in result.columns: # Binance Klines
        taker_buy_col = 'taker_buy_base'
    elif 'taker_buy_volume' in result.columns: # Generic Schema
        taker_buy_col = 'taker_buy_volume'

    # Fallback if no taker data is present (e.g. OANDA / Yahoo Fin / ccxt OHLCV)
    if taker_buy_col is None or total_vol_col not in result.columns:
        # We fill neutral values to prevent downstream pipeline crashes.
        # IMPORTANT: ALL features that the predictor models were trained on must be
        # present here — including vpin and vpin_z.  Previously these two were
        # omitted, causing FEATURE_ALIGNMENT_FAILURE for every live scan cycle.
        result['taker_buy'] = 0.0
        result['taker_sell'] = 0.0
        result['flow_imbalance'] = 0.0
        result['flow_aggression'] = 0.5  # Neutral
        result['cvd'] = 0.0
        result['cvd_z'] = 0.0
        result['flow_imbalance_z'] = 0.0
        result['flow_aggression_z'] = 0.0
        result['cvd_slope'] = 0.0
        result['cvd_slope_z'] = 0.0
        result['flow_divergence_dist'] = 0.0
        result['price_cvd_correlation'] = 0.0
        result['imbalance_trend'] = 0.0
        result['imbalance_acceleration'] = 0.0
        result['delta_cvd'] = 0.0
        # vpin=0.5 means neutral toxicity (no informed-trading signal either way).
        result['vpin'] = 0.5
        result['vpin_z'] = 0.0
        return result

    # 2. Base Core Features
    # Zero/low-volume edge cases handles implicitly by subtraction, safe from division yet
    result['taker_buy'] = result[taker_buy_col].fillna(0.0)
    result['taker_sell'] = (result[total_vol_col] - result['taker_buy']).clip(lower=0.0).fillna(0.0)
    
    result['flow_imbalance'] = result['taker_buy'] - result['taker_sell']
    
    # 3. Aggression Ratio (with Epsilon safeguard)
    epsilon = 1e-9
    total_taker = result['taker_buy'] + result['taker_sell']
    total_taker_safe = np.where(total_taker == 0, epsilon, total_taker)
    
    # Buy / max(Buy + Sell, epsilon)
    result['flow_aggression'] = result['taker_buy'] / total_taker_safe
    # Bound explicitly to [0, 1] for safety
    result['flow_aggression'] = result['flow_aggression'].clip(0.0, 1.0)
    
    # 4. Cumulative Volume Delta (CVD)
    result['cvd'] = compute_cumulative_volume_delta(
        imbalance=result['flow_imbalance'],
        mode=cvd_mode,
        window=cvd_window,
        decay=cvd_decay,
        timestamp_index=ts_idx
    )
    
    # 5. Flow Momentum Features (Acceleration)
    result['delta_cvd'] = result['cvd'].diff().fillna(0.0)
    result['cvd_slope'] = result['delta_cvd'].rolling(window=5, min_periods=1).mean()
    result['imbalance_trend'] = result['flow_imbalance'].ewm(span=14, adjust=False).mean()
    
    short_term_imb = result['flow_imbalance'].ewm(span=5, adjust=False).mean()
    long_term_imb = result['flow_imbalance'].ewm(span=21, adjust=False).mean()
    result['imbalance_acceleration'] = short_term_imb - long_term_imb
    
    # 6. Divergence Detection (Continuous Signals)
    # Price vs CVD rolling correlation is bounded [-1, 1]. +1 = aligned, -1 = divergent
    if 'close' in result.columns:
        # Min periods avoids NaNs at very start
        price_diff = result['close'].diff().fillna(0.0)
        cvd_diff = result['delta_cvd']
        
        corr_window = 24
        # Calculate rolling correlation between price diffs and CVD diffs
        rolling_corr = price_diff.rolling(window=corr_window, min_periods=2).corr(cvd_diff)
        result['price_cvd_correlation'] = rolling_corr.fillna(0.0)
        
        # Euclidean distance metric between Normalized Price and Normalized CVD (Trend divergence)
        norm_price = rolling_z_score(result['close'], window=normalization_window)
        norm_cvd = rolling_z_score(result['cvd'], window=normalization_window)
        result['flow_divergence_dist'] = norm_price - norm_cvd
    else:
        result['price_cvd_correlation'] = 0.0
        result['flow_divergence_dist'] = 0.0

    # 7. Universal Feature Normalization (Strict Anti-Leakage Z-Scores)
    result['cvd_z'] = rolling_z_score(result['cvd'], window=normalization_window)
    result['flow_imbalance_z'] = rolling_z_score(result['flow_imbalance'], window=normalization_window)
    result['flow_aggression_z'] = rolling_z_score(result['flow_aggression'], window=normalization_window)
    result['cvd_slope_z'] = rolling_z_score(result['cvd_slope'], window=normalization_window)

    # 8. VPIN — Volume-Synchronized Probability of Informed Trading
    # Measures the probability that counterparties are informed traders.
    # High VPIN → market is toxic → avoid entry (informed traders ahead of us).
    result['vpin'] = _compute_vpin(result['taker_buy'], result['taker_sell'], window=50)
    result['vpin_z'] = rolling_z_score(result['vpin'], window=normalization_window)

    return result


def _compute_vpin(buy_vol: pd.Series, sell_vol: pd.Series, window: int = 50) -> pd.Series:
    """
    Simplified VPIN (Easley et al. 2012):
      VPIN = |buy_vol - sell_vol| / (buy_vol + sell_vol)
    Rolling average over `window` bars approximates the signed order-flow fraction.
    Values near 1.0 → heavily one-sided → high informed-trading probability.
    """
    total = buy_vol + sell_vol
    signed_imbalance = (buy_vol - sell_vol).abs()
    epsilon = 1e-9
    raw_vpin = signed_imbalance / (total + epsilon)
    return raw_vpin.rolling(window=window, min_periods=1).mean().fillna(0.5)
