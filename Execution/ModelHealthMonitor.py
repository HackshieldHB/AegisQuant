import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
import logging

class ModelHealthMonitor:
    """
    Phase 10 Final Extension: Silent Failure & Health Detection.
    Monitors structural execution anomalies like signal collapse,
    directional bias strings, and meta-model probability degeneration.
    """
    
    def __init__(self,
                 min_signal_rate: int = 5,
                 signal_rate_window: int = 100,
                 max_directional_imbalance: float = 0.90,
                 prob_compression_threshold: float = 0.05,
                 max_regime_stickiness_bars: int = 2016, # ~14 days at 10m bars
                 max_meta_rejection_rate: float = 0.80):
                     
        self.min_signal_rate = min_signal_rate
        self.signal_rate_window = signal_rate_window
        self.max_directional_imbalance = max_directional_imbalance
        self.prob_compression_threshold = prob_compression_threshold
        self.max_regime_stickiness_bars = max_regime_stickiness_bars
        self.max_meta_rejection_rate = max_meta_rejection_rate
        
        # Internal state metrics
        self._recent_signals: List[int] = [] # 1 or -1
        self._meta_probabilities: List[float] = [] # 0.0 to 1.0
        self._recent_regimes: List[int] = [] 
        self._raw_cusum_count: int = 0
        self._meta_approved_count: int = 0
        self._bars_since_last_signal: int = 0
        
    def _check_signal_collapse(self) -> Tuple[bool, str]:
        """Flags if the system stops generating approved trades."""
        if self._bars_since_last_signal > self.signal_rate_window and len(self._recent_signals) < self.min_signal_rate:
             return True, f"Signal Collapse: Only {len(self._recent_signals)} signals in {self._bars_since_last_signal} bars"
        return False, ""
        
    def _check_directional_bias(self) -> Tuple[bool, str]:
        """Flags > 90% long or short signal strings."""
        if len(self._recent_signals) < 10:
            return False, "" # Need a minimum sample
            
        longs = sum(1 for s in self._recent_signals if s > 0)
        shorts = sum(1 for s in self._recent_signals if s < 0)
        total = longs + shorts
        
        if total == 0:
             return False, ""
             
        imbalance = max(longs, shorts) / total
        if imbalance > self.max_directional_imbalance:
             return True, f"Directional Bias Anomaly: {imbalance:.0%} one-sided trades."
        return False, ""

    def _check_confidence_degeneration(self) -> Tuple[bool, str]:
        """Flags variance compression (stuck at 0.5) or overconfidence (stuck at 1.0)."""
        if len(self._meta_probabilities) < 50:
             return False, ""
             
        probs = np.array(self._meta_probabilities[-50:])
        variance = np.var(probs)
        mean_prob = np.mean(probs)
        
        if variance < self.prob_compression_threshold:
             if 0.45 < mean_prob < 0.55:
                 return True, f"Probability Indecision Compression (Variance: {variance:.4f}, Mean: {mean_prob:.2f})"
             elif mean_prob > 0.95 or mean_prob < 0.05:
                 return True, f"Probability Overconfidence Degeneration (Variance: {variance:.4f}, Mean: {mean_prob:.2f})"
                 
        return False, ""

    def _check_regime_stickiness(self) -> Tuple[bool, str]:
        """Flags classifiers frozen in one state for abnormal physical durations."""
        if len(self._recent_regimes) < self.max_regime_stickiness_bars:
             return False, ""
             
        tail = self._recent_regimes[-self.max_regime_stickiness_bars:]
        # If all items in tail are exactly the same
        if len(set(tail)) == 1:
            return True, f"Regime Classifier frozen in State {tail[0]} for [{self.max_regime_stickiness_bars}] bars."
            
        return False, ""

    def _check_meta_rejection_spike(self) -> Tuple[bool, str]:
        """Flags if Meta-Model throws out > X% of all primary signals."""
        if self._raw_cusum_count < 20:
             return False, ""
             
        rejection_rate = 1.0 - (self._meta_approved_count / float(self._raw_cusum_count))
        if rejection_rate > self.max_meta_rejection_rate:
             return True, f"Meta-Filter Execution Freeze: Rejecting {rejection_rate:.0%} of primary flow."
             
        return False, ""

    def ingest_tick(self, is_raw_cusum: bool, meta_prob: float = None, meta_approved: bool = False, signal_direction: int = None, current_regime: int = None):
        """Streaming update of internal monitor state."""
        self._bars_since_last_signal += 1
        
        if current_regime is not None:
             self._recent_regimes.append(current_regime)
             # Memory curb
             if len(self._recent_regimes) > self.max_regime_stickiness_bars * 1.5:
                 self._recent_regimes = self._recent_regimes[-self.max_regime_stickiness_bars:]
                 
        if is_raw_cusum:
             self._raw_cusum_count += 1
             
        if meta_prob is not None:
             self._meta_probabilities.append(meta_prob)
             if len(self._meta_probabilities) > 500:
                  self._meta_probabilities = self._meta_probabilities[-500:]
                  
        if meta_approved and signal_direction is not None:
             self._bars_since_last_signal = 0
             self._meta_approved_count += 1
             self._recent_signals.append(signal_direction)
             if len(self._recent_signals) > self.signal_rate_window:
                 self._recent_signals = self._recent_signals[-self.signal_rate_window:]
                 
    def evaluate_health(self) -> Dict[str, any]:
        """
        Runs all 5 detection arrays and coordinates Soft Safe Mode tracking.
        """
        flags = []
        is_safe_mode = False
        
        c1, m1 = self._check_signal_collapse()
        if c1: flags.append(m1)
        
        c2, m2 = self._check_directional_bias()
        if c2: flags.append(m2)
        
        c3, m3 = self._check_confidence_degeneration()
        if c3: flags.append(m3)
        
        c4, m4 = self._check_regime_stickiness()
        if c4: flags.append(m4)
        
        c5, m5 = self._check_meta_rejection_spike()
        if c5: flags.append(m5)
        
        if len(flags) > 0:
            is_safe_mode = True
            logging.critical(f"[ModelHealthMonitor] SOFT SAFE MODE INITIATED: {len(flags)} Anomalies Detected.")
            for f in flags:
                logging.error(f"  -> {f}")
                
        return {
             "is_safe_mode":       is_safe_mode,
             "anomalies":          flags,
             "sizing_multiplier":  0.25 if is_safe_mode else 1.0,
             "retraining_locked":  is_safe_mode,   # Cannot auto-heal structural software bugs
             "drift_retrain_recommended": c3,       # probability degeneration → trigger retrain
        }
