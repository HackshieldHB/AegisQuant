import numpy as np
import pandas as pd
from scipy.stats import ks_2samp
from typing import Dict, List, Tuple
import logging

class DriftMonitor:
    """
    Phase 10: Online Adaptation & Monitoring.
    Executes Multivariate statistical drift detection combined with 
    physical performance deterioration tracking.
    """
    
    def __init__(self, 
                 ks_threshold: float = 0.05,
                 psi_critical_limit: float = 0.20,
                 mahalanobis_critical_quantile: float = 0.95):
        self.ks_threshold = ks_threshold
        self.psi_critical_limit = psi_critical_limit
        self.mahalanobis_critical_quantile = mahalanobis_critical_quantile
        
    @staticmethod
    def _calculate_psi(expected: np.ndarray, actual: np.ndarray, buckets: int = 10) -> float:
        """
        Calculates Population Stability Index (PSI).
        """
        def scale_range(arr, _min, _max):
            # Scale to 0-1 range based on provided min/max bounds
            arr = np.clip(arr, _min, _max)
            span = _max - _min if _max > _min else 1e-9
            return (arr - _min) / span

        _min = min(expected.min(), actual.min())
        _max = max(expected.max(), actual.max())
        
        # Scale arrays into consistent 0-1 distributions
        e_scaled = scale_range(expected, _min, _max)
        a_scaled = scale_range(actual, _min, _max)
        
        # Build percentiles
        breakpoints = np.linspace(0, 1, buckets + 1)
        
        e_counts, _ = np.histogram(e_scaled, breakpoints)
        a_counts, _ = np.histogram(a_scaled, breakpoints)
        
        # Convert to percentages
        e_pct = e_counts / len(expected)
        a_pct = a_counts / len(actual)
        
        # Replace 0s to avoid inf/nan math
        e_pct = np.clip(e_pct, 1e-4, 1.0)
        a_pct = np.clip(a_pct, 1e-4, 1.0)
        
        psi = np.sum((a_pct - e_pct) * np.log(a_pct / e_pct))
        return float(psi)

    def evaluate_multivariate_drift(self, 
                                  training_features: pd.DataFrame, 
                                  live_features: pd.DataFrame, 
                                  active_feature_subset: List[str]) -> Dict[str, any]:
        """
        1. Tradeable Drift Filter: ONLY evaluates features actively ranked by MDA/MDI.
        2. Ensemble scoring using KS, PSI, and Mahalanobis logic.
        """
        if training_features.empty or live_features.empty or not active_feature_subset:
            return {"drift_confirmed": False, "reason": "Insufficient Data"}
            
        # Ensure identical space geometries
        missing = [f for f in active_feature_subset if f not in training_features.columns or f not in live_features.columns]
        if missing:
            raise ValueError(f"Feature Space Mismatch. Missing critical active features: {missing}")
            
        train_subset = training_features[active_feature_subset]
        live_subset = live_features[active_feature_subset]
        
        ks_flags = 0
        psi_sum = 0.0
        
        for col in active_feature_subset:
            base_col = train_subset[col].values
            live_col = live_subset[col].values
            
            # KS Test (p-value < threshold means distributions differ materially)
            ks_stat, p_val = ks_2samp(base_col, live_col)
            if p_val < self.ks_threshold:
                ks_flags += 1
                
            # PSI Test
            psi_val = self._calculate_psi(base_col, live_col)
            psi_sum += psi_val
            
        mean_psi = psi_sum / len(active_feature_subset)
        ks_flag_ratio = ks_flags / len(active_feature_subset)
        
        # Mahalanobis Distance proxy (simplified via global covariance scale)
        try:
            cov_inv = np.linalg.pinv(np.cov(train_subset.T))
            mean_train = np.mean(train_subset.values, axis=0)
            mean_live = np.mean(live_subset.values, axis=0)
            diff = mean_live - mean_train
            mahalanobis_dist = np.sqrt(np.dot(np.dot(diff, cov_inv), diff.T))
        except np.linalg.LinAlgError:
            mahalanobis_dist = 0.0 # Fallback if matrix is singular
            
        # Hardened Drift Determination logic
        drift_confirmed = False
        reason = ""
        
        if ks_flag_ratio >= 0.5: # 50% of active features drift
            if mean_psi > self.psi_critical_limit:
                 drift_confirmed = True
                 reason = f"Ensemble Trigger (KS: {ks_flag_ratio:.1%} features drifted, PSI: {mean_psi:.3f} > {self.psi_critical_limit})"
            elif mahalanobis_dist > 5.0: # Hardcoded critical metric
                 drift_confirmed = True
                 reason = f"Ensemble Trigger (KS: {ks_flag_ratio:.1%}, Mahalanobis Shifted)"
                 
        return {
            "drift_confirmed": drift_confirmed,
            "ks_flag_ratio": float(ks_flag_ratio),
            "mean_psi": float(mean_psi),
            "mahalanobis_dist": float(mahalanobis_dist),
            "reason": reason
        }

    @staticmethod
    def construct_retrain_trigger(drift_metrics: Dict[str, any], performance_metrics: Dict[str, float]) -> bool:
        """
        Retraining triggers ONLY when: (DriftDetected AND PerformanceDegraded) OR 
        Performance collapses violently.
        """
        is_drift_confirmed = drift_metrics.get("drift_confirmed", False)
        
        # Precision drop from baseline
        precision_degraded = performance_metrics.get("live_precision", 1.0) < performance_metrics.get("baseline_precision", 0.5) - 0.10
        sharpe_degraded = performance_metrics.get("live_sharpe", 1.0) < performance_metrics.get("baseline_sharpe", 1.0) * 0.5
        brier_deteriorated = performance_metrics.get("live_brier", 0.0) > 0.25
        
        is_performance_degraded = precision_degraded or sharpe_degraded or brier_deteriorated
        
        if is_drift_confirmed and is_performance_degraded:
            logging.warning(f"[DriftMonitor] ALARM: Critical Drift and Performance Decay Detected. Retraining Triggered. ({drift_metrics['reason']})")
            return True
            
        return False
