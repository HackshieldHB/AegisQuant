import pandas as pd
import logging
from typing import Dict, List, Tuple
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Data.Features.DataGuard import QuarantineStatus

class RetrainingPipeline:
    """
    Phase 10: Retraining Capital Protection & Data Guard.
    Secures the automated retraining process against 
    suicidal adaptation during flash crashes or missing features.
    """
    
    @staticmethod
    def _check_capital_protection_lockout(
        portfolio_metrics: Dict[str, float], 
        volatility_metrics: Dict[str, float], 
        data_status: QuarantineStatus) -> Tuple[bool, str]:
        """
        Retraining is DISABLED during active drawdown periods, high vol spikes, 
        circuit breakers, or DEGRADED data state.
        """
        if data_status != QuarantineStatus.SAFE:
            return True, f"Data status is {data_status.value}. Safe training impossible."
            
        current_drawdown = portfolio_metrics.get("current_drawdown", 0.0)
        drawdown_limit = portfolio_metrics.get("max_drawdown_limit", -0.10)
        
        if current_drawdown <= drawdown_limit:
             return True, f"System in Severe Drawdown ({current_drawdown:.2%}). Priorities restricted to capital preservation."
             
        vol_ratio = volatility_metrics.get("current_vix", 15.0) / volatility_metrics.get("baseline_vix", 15.0)
        if vol_ratio > 3.0:
             return True, f"Exogenous Volatility Shock Detected ({vol_ratio:.1f}x baseline). Delaying adaptation."
             
        return False, "Capital and Markets Safe for Retraining."

    @staticmethod
    def _validate_training_data_quality(df_train: pd.DataFrame, min_samples: int = 500) -> Tuple[bool, str]:
        """
        Pre-flight checks on the newly extracted training payload.
        """
        if len(df_train) < min_samples:
             return False, f"Insufficient Samples ({len(df_train)} < {min_samples})"
             
        if df_train.isna().any().any():
             return False, "Raw NaNs detected in training block. Pipeline corrupted."
             
        if 'label' in df_train.columns:
            vc = df_train['label'].value_counts(normalize=True)
            if vc.min() < 0.05: # At least 5% representation per class
                 return False, f"Severe Class Imbalance detected. Minimum class represents {vc.min():.2%}."
                 
        if 'regime' in df_train.columns:
             unique_regimes = df_train['regime'].nunique()
             if unique_regimes < 2:
                 return False, f"Homogeneous Volatility. Only {unique_regimes} regime detected. Need cross-regime data."
                 
        return True, "Data Quality Passed."

    @staticmethod
    def _validate_feature_space_compatibility(
        champion_features: List[str], 
        challenger_df: pd.DataFrame) -> Tuple[bool, str]:
        """
        Prevent schema mismatches between Live feeds and Retrained models.
        """
        live_cols = list(challenger_df.columns)
        
        missing = [f for f in champion_features if f not in live_cols]
        if missing:
            return False, f"Schema mismatch. Missing mandatory features: {missing}"
            
        # Ensure exact order is technically present (extractable via subset)
        for i, f in enumerate(champion_features):
             if f not in live_cols:
                  return False, f"Feature index mismatch on {f}"
                  
        return True, "Feature space identical."
        
    @staticmethod
    def initiate_retraining(
        df_new: pd.DataFrame, 
        champion_features: List[str], 
        portfolio_metrics: Dict[str, float], 
        vol_metrics: Dict[str, float], 
        data_status: QuarantineStatus) -> Tuple[bool, str]:
        """
        Master gatekeeper. If ALL conditions pass, the system is cleared to 
        train a new CHALLENGER model.
        """
        is_locked, lock_reason = RetrainingPipeline._check_capital_protection_lockout(
             portfolio_metrics, vol_metrics, data_status)
             
        if is_locked:
            logging.error(f"[RetrainingPipeline] ABORTED: {lock_reason}")
            return False, lock_reason
            
        is_quality, qual_reason = RetrainingPipeline._validate_training_data_quality(df_new)
        if not is_quality:
            logging.error(f"[RetrainingPipeline] ABORTED: {qual_reason}")
            return False, qual_reason
            
        is_compatible, comp_reason = RetrainingPipeline._validate_feature_space_compatibility(
             champion_features, df_new)
             
        if not is_compatible:
            logging.error(f"[RetrainingPipeline] ABORTED: {comp_reason}")
            return False, comp_reason
            
        logging.info("[RetrainingPipeline] All Guards Passed. Initializing Candidate Architecture.")
        return True, "CLEARED FOR RETRAINING"
