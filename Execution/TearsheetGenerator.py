import pandas as pd
import numpy as np
from typing import Dict, Any

class TearsheetGenerator:
    """
    Consumes executed portfolio ledgers and validation metrics
    to output the Final GO / NO-GO Decision Matrix.
    """
    
    @staticmethod
    def evaluate_go_no_go(metrics: Dict[str, Any]) -> dict:
        """
        The absolute ultimate safety net. 
        If ANY of these conditions fail, the strategy is mathematically 
        rejected from live deployment.
        """
        rejections = []
        
        # 1. PBO (Probability of Backtest Overfitting)
        if metrics.get('pbo', 1.0) > 0.05:
            rejections.append(f"PBO Exceeds 5% ({metrics.get('pbo'):.3f})")
            
        # 2. Deflated Sharpe Ratio
        if metrics.get('dsr', 0.0) < 1.0:
            rejections.append(f"Deflated Sharpe too low ({metrics.get('dsr'):.2f})")
            
        # 3. SPA / Reality Check
        if metrics.get('spa_p_value', 1.0) > 0.05:
            rejections.append(f"SPA Reality Check Failed (p={metrics.get('spa_p_value'):.3f})")
            
        # 4. Temporal Stability
        if not metrics.get('temporal_stability', {}).get('stable', False):
            rejections.append("Unstable across multiple time windows")
            
        # 5. Regime Generalization
        if not metrics.get('regime_generalization', {}).get('generalized', False):
            rejections.append("Fails in adverse volatility regimes")
            
        # 6. Tail Risk Survival
        if metrics.get('tail_risk', {}).get('worst_drawdown', -1.0) < -0.20:
            rejections.append(f"Lethal Drawdown Risk ({metrics.get('tail_risk', {}).get('worst_drawdown'):.2f})")
            
        # 7. Model Decay Rate
        if metrics.get('model_decay', {}).get('rapid_decay', True):
            rejections.append("Alpha decays immediately post-training")
            
        # 8. Calibration
        if not metrics.get('signal_quality', {}).get('valid_calibration', False):
            rejections.append("Probabilities do not map to physical outcomes (Poor Calibration)")
            
        # 9. Turnover Toxicity
        if metrics.get('turnover', {}).get('high_turnover_risk', True):
            rejections.append("Unrealistic capacity demands (Toxic High Frequency)")
            
        # 10. Cost-Stress Resilience
        if metrics.get('cost_stress_return', 0.0) <= 0.0:
            rejections.append("Strategy bankrupt under stressed execution frictions")
            
        is_approved = len(rejections) == 0
        
        return {
            "status": "APPROVED FOR LIVE" if is_approved else "REJECT — OVERFIT OR FRAGILE",
            "fail_reasons": rejections
        }
    
    @staticmethod
    def generate_tearsheet(metrics: Dict[str, Any]) -> str:
        """
        Generates a human-readable string Tearsheet detailing the strict 
        Phase 7 institutional metrics.
        """
        decision_matrix = TearsheetGenerator.evaluate_go_no_go(metrics)
        status = decision_matrix["status"]
        
        report = []
        report.append("="*50)
        report.append(f" OMEGA ∞ VALIDATION TEARSHEET ")
        report.append("="*50)
        report.append(f"FINAL DECISION: {status}")
        
        if len(decision_matrix["fail_reasons"]) > 0:
            report.append("-" * 30)
            report.append("REJECTION REASONS:")
            for reason in decision_matrix["fail_reasons"]:
                report.append(f" [!] {reason}")
            report.append("-" * 30)
        
        report.append("\n[1] ANTI-OVERFITTING METRICS")
        report.append(f" PBO (Probability of Overfit) : {metrics.get('pbo', 1.0):.4f}")
        report.append(f" DSR (Deflated Sharpe Ratio)  : {metrics.get('dsr', 0.0):.2f}")
        report.append(f" SPA Reality Check (p-value)  : {metrics.get('spa_p_value', 1.0):.4f}")
        
        report.append("\n[2] RESILIENCE & TAIL RISK")
        tail = metrics.get('tail_risk', {})
        report.append(f" 5th Pct Bootstrap Return     : {tail.get('p5_return', 0.0):.2%}")
        report.append(f" Expected Shortfall (CVaR)    : {tail.get('cvar', 0.0):.2%}")
        report.append(f" Worst Simulated Drawdown     : {tail.get('worst_drawdown', 0.0):.2%}")
        report.append(f" Cost-Stress (2x Frictions)   : {metrics.get('cost_stress_return', 0.0):.2%}")
        
        report.append("\n[3] FRAGILITY & CAPACITY")
        report.append(f" Temporal Stability Passed    : {metrics.get('temporal_stability', {}).get('stable', False)}")
        report.append(f" Regime Generality Passed     : {metrics.get('regime_generalization', {}).get('generalized', False)}")
        report.append(f" Post-Train Burnout Detected  : {metrics.get('model_decay', {}).get('rapid_decay', True)}")
        report.append(f" Annualized Turnover          : {metrics.get('turnover', {}).get('annualized_turnover', 0.0):.1f}x")
        
        report.append("\n[4] SIGNAL QUALITY")
        report.append(f" Brier Score                  : {metrics.get('signal_quality', {}).get('brier_score', 1.0):.3f}")
        report.append(f" OOS Precision                : {metrics.get('signal_quality', {}).get('precision', 0.0):.2%}")
        
        report.append("="*50)
        
        return "\n".join(report)
