import numpy as np
import pandas as pd
from typing import Optional, Tuple
import logging

# -------------------------------------------------------------------
# Phase 8: Performance Engineering Hardening
# Numba Compilation and Warm-Up
# -------------------------------------------------------------------
try:
    from numba import njit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    logging.warning("Numba not available. CUSUM filter will fall back to Python execution.")

if NUMBA_AVAILABLE:
    @njit(cache=True)
    def _cusum_numba_core(diffs: np.ndarray, h: float) -> np.ndarray:
        """
        C-level machine code core for Symmetric CUSUM.
        Thread-safe and deterministic.
        """
        s_pos = 0.0
        s_neg = 0.0
        n = len(diffs)
        
        # Pre-allocate worst-case array
        events = np.zeros(n, dtype=np.int64)
        event_count = 0
        
        for i in range(n):
            d = diffs[i]
            
            # Stability guards: clamp d to prevent overflow infinities
            if d > 1e6:
                d = 1e6
            elif d < -1e6:
                d = -1e6
                
            s_pos = max(0.0, s_pos + d)
            s_neg = min(0.0, s_neg + d)
            
            if s_pos > h:
                s_pos = 0.0
                events[event_count] = i
                event_count += 1
            elif s_neg < -h:
                s_neg = 0.0
                events[event_count] = i
                event_count += 1
                
        # Slice to actual events
        return events[:event_count]

else:
    def _cusum_numba_core(diffs: np.ndarray, h: float) -> np.ndarray:
        raise NotImplementedError("Numba not installed. Must use Python core.")

def _cusum_python_core(diffs: np.ndarray, h: float) -> np.ndarray:
    """
    Pure Python explicit fallback guaranteeing deterministic output parity.
    Used if Numba crashes or is uninstalled.
    """
    s_pos = 0.0
    s_neg = 0.0
    events = []
    
    for i, d in enumerate(diffs):
        # Stability guards
        if d > 1e6: d = 1e6
        if d < -1e6: d = -1e6
        
        s_pos = max(0.0, s_pos + d)
        s_neg = min(0.0, s_neg + d)
        
        if s_pos > h:
            s_pos = 0.0
            events.append(i)
        elif s_neg < -h:
            s_neg = 0.0
            events.append(i)
            
    return np.array(events, dtype=np.int64)

# Warm-up Latency Mitigation
_NUMBA_COMPILED = False
if NUMBA_AVAILABLE:
    try:
        # Pre-compilation with dummy arrays to destroy JIT latency before live ticks arrive
        _dummy_diffs = np.zeros(10, dtype=np.float64)
        _cusum_numba_core(_dummy_diffs, 1.0)
        _NUMBA_COMPILED = True
    except Exception as e:
        logging.error(f"Numba warmup failed during boot: {e}. Falling back to Python CUSUM.")
        _NUMBA_COMPILED = False
# -------------------------------------------------------------------

def compute_ewma_volatility(df: pd.DataFrame, span: int = 100) -> pd.Series:
    """
    Computes Exponentially Weighted Moving Average (EWMA) of True Range for local volatility.
    Strictly anti-leakage: Uses adjust=False and shift(1) so vol at t depends only on data < t.
    """
    if df.empty or 'high' not in df.columns or 'low' not in df.columns or 'close' not in df.columns:
        return pd.Series(dtype=float)
        
    # 1. True Range calculation
    prev_close = df['close'].shift(1)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - prev_close).abs()
    tr3 = (df['low'] - prev_close).abs()
    
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # 2. EWMA (adjust=False to prevent weighting future data via recursive formulation)
    # We shift by 1 to ensure the volatility measured at time T only uses data UP TO T-1.
    # This prevents the current candle's high/low from leaking into the barrier calculation.
    ewma_vol = true_range.ewm(span=span, adjust=False, min_periods=span).mean().shift(1)
    
    return ewma_vol

def apply_triple_barrier(
    df: pd.DataFrame, 
    volatility: pd.Series,
    events: pd.Series, 
    k_up: float = 1.0, 
    k_down: float = 1.0, 
    horizon_bars: int = 24,
    fees_bps: float = 4.0,     # Default 4bps Binance Taker
    spread_bps: float = 0.0,   
    slippage_bps: float = 2.0, # Default entry scale penalty
    direction: int = 1  # 1 for Long, -1 for Short
) -> pd.DataFrame:
    """
    Applies Triple-Barrier Method to evaluate price paths.
    
    Returns:
        pd.DataFrame containing ['t_start', 't_end', 'label', 'entry_price', 'upper_barrier', 'lower_barrier', 'effective_cost_pct', 'vol_at_entry']
    """
    if df.empty or events.empty:
        return pd.DataFrame(columns=['t_start', 't_end', 'label', 'entry_price', 'upper_barrier', 'lower_barrier', 'effective_cost_pct', 'vol_at_entry'])

    # Ensure datetime index
    if 'timestamp' in df.columns:
        df = df.set_index('timestamp')
        
    # Extract only the event timestamps
    event_times = events.index
    
    # Pre-allocate output dictionary for speed
    results = {
        't_start': [],
        't_end': [],
        'label': [],
        'entry_price': [],
        'upper_barrier': [],
        'lower_barrier': [],
        'effective_cost_pct': [],
        'vol_at_entry': []
    }
    
    # We must iterate over events, but the inner path-search is vectorized
    for t_start in event_times:
        if t_start not in df.index:
            continue
            
        start_idx = df.index.get_loc(t_start)
        
        # Calculate boundaries for this path
        end_idx = min(start_idx + horizon_bars + 1, len(df))
        path = df.iloc[start_idx : end_idx]
        
        if path.empty:
            continue
            
        entry_price = path.iloc[0]['close']
        vol = volatility.loc[t_start]
        
        if pd.isna(vol) or entry_price <= 0:
            continue
            
        # 1. Structural Barrier distances derived from volatility targeting
        tp_pct_raw = (k_up * vol) / entry_price
        sl_pct_raw = (k_down * vol) / entry_price
        
        # 2. Dynamic Basis Points Execution Constraints 
        # Fees incur twice (entry/exit round trip). 
        total_cost_pct = ((fees_bps * 2) + spread_bps + slippage_bps) / 10000.0
        
        # 3. Geometric Boundary Application (Adverse to Expected Value)
        if direction == 1: # LONG
            upper_barrier = entry_price * (1 + tp_pct_raw - total_cost_pct)
            lower_barrier = entry_price * (1 - sl_pct_raw - total_cost_pct)
        else: # SHORT
            upper_barrier = entry_price * (1 + sl_pct_raw + total_cost_pct)
            lower_barrier = entry_price * (1 - tp_pct_raw + total_cost_pct)
            
        # Physical impossibility failsafe (costs overwhelm range)
        if direction == 1 and upper_barrier <= lower_barrier:
            continue
        if direction == -1 and lower_barrier >= upper_barrier:
            continue
            
        # Path evaluation (Vectorized check within the window)
        # We start checking from step 1 (the bar AFTER entry)
        if len(path) > 1:
            eval_path = path.iloc[1:]
            
            # Find hits (High for upper, Low for lower)
            upper_hits = eval_path[eval_path['high'] >= upper_barrier].index
            lower_hits = eval_path[eval_path['low'] <= lower_barrier].index
            
            # Use an index-type appropriate "infinity" fallback
            if pd.api.types.is_datetime64_any_dtype(df.index):
                if getattr(df.index, 'tzinfo', None) is not None:
                    fallback_max = pd.Timestamp.max.tz_localize('UTC')
                else:
                    fallback_max = pd.Timestamp.max
            else:
                fallback_max = float('inf')
            
            first_upper = upper_hits[0] if len(upper_hits) > 0 else fallback_max
            first_lower = lower_hits[0] if len(lower_hits) > 0 else fallback_max
            
            # Resolution
            t_end = None
            label = 0 # Default: Time expiry
            
            if first_upper < first_lower:
                t_end = first_upper
                label = 1 if direction == 1 else -1 
                # Gap Risk: Short SL gaps up
                if direction == -1 and 'open' in eval_path.columns:
                    exit_bar = eval_path.loc[t_end]
                    if exit_bar['open'] > upper_barrier:
                        upper_barrier = exit_bar['open']
                        
            elif first_lower < first_upper:
                t_end = first_lower
                label = -1 if direction == 1 else 1 
                # Gap Risk: Long SL gaps down
                if direction == 1 and 'open' in eval_path.columns:
                    exit_bar = eval_path.loc[t_end]
                    if exit_bar['open'] < lower_barrier:
                        lower_barrier = exit_bar['open']
                        
            elif first_upper == first_lower and first_upper != fallback_max:
                # Simultaneous hit in the same candle: Worst-case assumption
                t_end = first_upper
                label = -1
                if 'open' in eval_path.columns:
                    exit_bar = eval_path.loc[t_end]
                    if direction == 1 and exit_bar['open'] < lower_barrier:
                        lower_barrier = exit_bar['open']
                    if direction == -1 and exit_bar['open'] > upper_barrier:
                        upper_barrier = exit_bar['open']
            else:
                # Time barrier
                t_end = path.index[-1]
                label = 0
                
        else:
             # Hit end of data
             t_end = path.index[-1]
             label = 0
             
        results['t_start'].append(t_start)
        results['t_end'].append(t_end)
        results['label'].append(label)
        results['entry_price'].append(entry_price)
        results['upper_barrier'].append(upper_barrier)
        results['lower_barrier'].append(lower_barrier)
        results['effective_cost_pct'].append(total_cost_pct)
        results['vol_at_entry'].append(vol)
        
    return pd.DataFrame(results)

def cusum_filter(df: pd.DataFrame, h: float, use_numba: bool = True) -> pd.Series:
    """
    Symmetric CUSUM Filter for event sampling.
    Accelerated via Numba with explicit NumPy/Python degradation paths.
    """
    if df.empty or 'close' not in df.columns:
        return pd.Series(dtype=int)

    # Calculate price diffs and scrub NaNs strictly
    diff_series = df['close'].diff().fillna(0.0)
    
    # Memory Layout Optimization: Enforce contiguous float64 for C-level safety
    diffs = np.ascontiguousarray(diff_series.values, dtype=np.float64)
    h_float = float(h)
    
    # Fail-Safe Degradation Path
    if use_numba and NUMBA_AVAILABLE and _NUMBA_COMPILED:
        try:
            event_indices = _cusum_numba_core(diffs, h_float)
        except Exception as e:
            logging.error(f"Numba CUSUM failed during execution: {e}. Degrading to pure Python.")
            event_indices = _cusum_python_core(diffs, h_float)
    else:
        event_indices = _cusum_python_core(diffs, h_float)
        
    # Map array indices back to absolute DataFrame index timestamps
    events_idx = df.index[event_indices]
    
    # Return as pandas Series matching index format
    return pd.Series(1, index=events_idx)

def print_class_distribution(events_df: pd.DataFrame):
     """
     Sanity checks the label distribution
     """
     if events_df.empty:
         print("WARNING: Empty events dataframe.")
         return
         
     vc = events_df['label'].value_counts(normalize=True) * 100
     print("\n=== Label Distribution Check ===")
     for label in [-1, 0, 1]:
         pct = vc.get(label, 0.0)
         print(f"Label {label:>2}: {pct:>5.1f}%")
         
     if vc.get(0, 0) > 50.0:
         print("WARNING: Time-expiry labels (0) dominate (>50%). Adjust horizon or k-multipliers.")
     
     if vc.get(1, 0) > 0 and vc.get(-1, 0) > 0:    
         ratio = vc.get(1, 0) / vc.get(-1, 0)
         if ratio > 3.0 or ratio < 0.33:
             print(f"WARNING: Severe class imbalance detected (Ratio TP/SL: {ratio:.2f}).")
