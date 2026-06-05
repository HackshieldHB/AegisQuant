import pandas as pd
import numpy as np
from enum import Enum
import logging
from typing import Dict, Tuple

class ModelVersionState(Enum):
    TRAINING = "TRAINING"
    SHADOW = "SHADOW"
    CANDIDATE = "CANDIDATE"
    CHAMPION = "CHAMPION"
    ARCHIVED = "ARCHIVED"
    BLACKLISTED = "BLACKLISTED"

class ShadowValidation:
    """
    Phase 10: Champion vs Challenger Validation Pipeline.
    Models cannot be deployed directly; they must defeat the Champion
    in simulated Side-by-Side shadow tracking.
    """
    
    @staticmethod
    def evaluate_shadow_performance(
        champion_metrics: Dict[str, float], 
        challenger_metrics: Dict[str, float], 
        min_eval_duration_days: int = 7) -> Tuple[bool, str]:
        """
        Determines if a Challenger in SHADOW state has earned the right 
        to advance to CANDIDATE/CHAMPION.
        """
        eval_days = challenger_metrics.get("eval_duration_days", 0)
        
        if eval_days < min_eval_duration_days:
            return False, f"InSufficient Shadow Duration ({eval_days}/{min_eval_duration_days} days)"
            
        champ_sharpe = champion_metrics.get("realized_sharpe", 0.0)
        chall_sharpe = challenger_metrics.get("realized_sharpe", 0.0)
        
        champ_precision = champion_metrics.get("realized_precision", 0.0)
        chall_precision = challenger_metrics.get("realized_precision", 0.0)
        
        champ_cvar = champion_metrics.get("worst_drawdown", 0.0)
        chall_cvar = challenger_metrics.get("worst_drawdown", 0.0)
        
        # 1. Negative Constraint (Must not introduce lethal risk)
        if chall_cvar < -0.15 or chall_sharpe < 0:
            return False, f"Unacceptable Risk Metrics in Shadow (CVaR: {chall_cvar:.2%})"
            
        # 2. Positive Constraint (Must strictly outperform the Champion)
        # If the Champion is already losing money, the challenger only has to be positive.
        # If the Champion is making money, the Challenger must be objectively better.
        outperforms = False
        if champ_sharpe <= 0 and chall_sharpe > 0:
            outperforms = True
        elif chall_sharpe > champ_sharpe * 1.05 and chall_precision > champ_precision:
            outperforms = True
            
        if not outperforms:
            return False, f"Failed to significantly outperform Champion (Champ SR: {champ_sharpe:.2f}, Chall SR: {chall_sharpe:.2f})"
            
        return True, "Challenger Defeated Champion. Safe to Promote."

    @staticmethod
    def enforce_rollback_safety_window(
        champion_metrics: Dict[str, float],
        max_loss_tolerance: float = -0.05,
        max_days_for_rollback: int = 14) -> Tuple[bool, str]:
        """
        Monitors a newly promoted Champion. If it instantly prints a violent 
        drawdown, it is blacklisted and the system rollbacks to earlier weights.
        """
        active_days = champion_metrics.get("active_days", 0)
        realized_pnl = champion_metrics.get("realized_pnl", 0.0)
        
        if active_days > max_days_for_rollback:
            return False, "Past Rollback Window" # Too old to be considered a false launch
            
        if realized_pnl <= max_loss_tolerance:
            logging.critical(f"[ShadowValidation] ROllBACK TRIGGERED: New Champion hit rapid loss boundary ({realized_pnl:.2%}) in {active_days} days.")
            return True, "Violent Rapid Drawdown"
            
        return False, "Champion stable"
        
    @staticmethod
    def promote_model(current_state: ModelVersionState, shadow_passed: bool) -> ModelVersionState:
        """
        Enforces unidirectional state transition tracking.
        """
        if current_state == ModelVersionState.TRAINING:
             return ModelVersionState.SHADOW
        elif current_state == ModelVersionState.SHADOW and shadow_passed:
             return ModelVersionState.CANDIDATE
        elif current_state == ModelVersionState.CANDIDATE: # Manual or automated sync
             return ModelVersionState.CHAMPION
             
        return current_state
