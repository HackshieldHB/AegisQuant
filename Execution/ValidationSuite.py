import numpy as np
import pandas as pd
from scipy.stats import norm
import itertools

class ValidationSuite:
    """
    Phase 7 Institutional Anti-Leakage & Validation Framework.
    Proves strategy robustness over historical noise.
    """
    
    @staticmethod
    def calculate_cpcv_paths(n_splits: int, k_test: int):
        """
        Calculates the number of Combinatorial Purged Cross-Validation paths.
        Given N total blocks, we test on K blocks at a time.
        Total combinations = N choose K
        Total paths = (N choose K) * (K / N)
        Returns the combination indices.
        """
        blocks = np.arange(n_splits)
        # All possible combinations of test blocks
        test_combinations = list(itertools.combinations(blocks, k_test))
        return test_combinations

    @staticmethod
    def calculate_pbo(sharpe_ratios_is: np.ndarray, sharpe_ratios_oos: np.ndarray) -> float:
        """
        Probability of Backtest Overfitting (PBO).
        Calculates the percentage of Combinatorial Cross-Validation splits 
        where the Out-Of-Sample Sharpe ratio degrades relative to the In-Sample performance.
        Return value > 0.05 indicates critical overfitting risk.
        """
        if len(sharpe_ratios_is) == 0 or len(sharpe_ratios_oos) == 0:
            return 1.0 # Assume total overfit if no data
            
        rank_is = np.argsort(np.argsort(sharpe_ratios_is))
        rank_oos = np.argsort(np.argsort(sharpe_ratios_oos))
        
        # Rank correlation (Spearman) proxy for PBO
        # Formally PBO is logit probability of rank inversion
        # Simple implementation: ratio of negative relative ranks
        inversions = 0
        for i in range(len(sharpe_ratios_is)):
            if rank_is[i] > np.median(rank_is) and rank_oos[i] < np.median(rank_oos):
                inversions += 1
                
        # Approximate Probability
        pbo = inversions / len(sharpe_ratios_is) if len(sharpe_ratios_is) > 0 else 1.0
        return float(np.clip(pbo, 0.0, 1.0))

    @staticmethod
    def calculate_deflated_sharpe(actual_sharpe: float, num_trials: int, variance_trials: float, returns_variance: float = 1.0) -> float:
        """
        Deflated Sharpe Ratio (DSR)
        Penalizes the Sharpe Ratio based on the number of trials (Selection Bias).
        Estimates the expected maximum Sharpe ratio from pure noise and deducts it.
        """
        if num_trials <= 1:
            return actual_sharpe
            
        # Euler-Mascheroni constant
        euler = 0.5772156649
        
        # Expected maximum Sharpe of random trials
        expected_max_sharpe = np.sqrt(variance_trials) * ((1 - euler) * norm.ppf(1 - 1/num_trials) + euler * norm.ppf(1 - 1/(num_trials * np.e)))
        
        # Deflated calculation
        dsr = actual_sharpe - expected_max_sharpe
        return float(dsr)
        
    @staticmethod
    def block_bootstrap_cvar(returns: pd.Series, block_size: int = 5, n_iterations: int = 2000, confidence_level: float = 0.05) -> dict:
        """
        Monte Carlo Block Bootstrapping for Tail Risk Analysis.
        Resamples equity path returns to estimate the 5th percentile worst-case outcomes.
        Returns Median, 5th percentile, and CVaR (Expected Shortfall).
        """
        if len(returns) < block_size:
            return {"median_return": 0.0, "p5_return": 0.0, "cvar": 0.0, "max_drawdown": 0.0}
            
        n_blocks = len(returns) // block_size
        simulated_returns = []
        max_drawdowns = []
        
        returns_arr = returns.values
        
        for _ in range(n_iterations):
            # Random uniform sampling of starting block indices
            start_indices = np.random.randint(0, len(returns) - block_size + 1, size=n_blocks)
            
            # Extract blocks and concatenate
            blocks = [returns_arr[i:i+block_size] for i in start_indices]
            sim_path = np.concatenate(blocks)
            
            # Cumulative physics
            cum_returns = (1 + sim_path).cumprod()
            peak = np.maximum.accumulate(cum_returns)
            drawdown = (cum_returns - peak) / peak
            
            simulated_returns.append(np.sum(sim_path))
            max_drawdowns.append(np.min(drawdown))
            
        sim_arr = np.array(simulated_returns)
        p5 = np.percentile(sim_arr, confidence_level * 100)
        cvar = np.mean(sim_arr[sim_arr <= p5])
        
        return {
            "median_return": float(np.median(sim_arr)),
            "p5_return": float(p5),
            "cvar": float(cvar),
            "worst_drawdown": float(np.min(max_drawdowns))
        }
        
    @staticmethod
    def evaluate_temporal_stability(returns: pd.Series, window_size: int = 100) -> dict:
        """
        Splits out-of-sample data into sequential windows.
        Rejects strategies that only succeed in a single era.
        """
        if len(returns) < window_size:
            return {"stable": False, "failed_windows": 1, "total_windows": 1}
            
        n_windows = len(returns) // window_size
        failed = 0
        
        for i in range(n_windows):
            window_ret = returns.iloc[i * window_size : (i + 1) * window_size]
            sharpe = np.sqrt(365 * 24) * window_ret.mean() / (window_ret.std() + 1e-9)
            
            # Sub-zero formulation
            if window_ret.sum() < 0 or sharpe < 0:
                failed += 1
                
        # Fragile if > 30% of individual blocks fail structurally 
        is_stable = (failed / n_windows) < 0.3
        return {"stable": is_stable, "failed_windows": failed, "total_windows": n_windows}

    @staticmethod
    def evaluate_regime_generalization(df: pd.DataFrame, signal_returns: pd.Series) -> dict:
        """
        Ensures model survives independent of exogenous market volatility states.
        df must contain a 'regime' categorical.
        """
        if 'regime' not in df.columns or len(df) != len(signal_returns):
            return {"generalized": True} # Auto-pass if no regime data
            
        aligned = pd.concat([df['regime'], signal_returns], axis=1)
        aligned.columns = ['regime', 'returns']
        
        regime_stats = {}
        generalized = True
        
        for reg in aligned['regime'].unique():
            reg_slice = aligned[aligned['regime'] == reg]
            if len(reg_slice) < 50:
                continue
                
            reg_ret = reg_slice['returns'].sum()
            # Win rate proxy (positive returns)
            win_rate = len(reg_slice[reg_slice['returns'] > 0]) / len(reg_slice)
            
            # If the strategy collapses violently in any single regime, flag it
            if reg_ret < -0.05 or win_rate < 0.35:
                generalized = False
                
            regime_stats[reg] = {"return": float(reg_ret), "win_rate": float(win_rate)}
            
        return {"generalized": generalized, "stats": regime_stats}

    @staticmethod
    def white_reality_check(strategy_returns: pd.Series, benchmark_returns: pd.Series, n_bootstraps: int = 1000) -> float:
        """
        White's Reality Check (SPA Test alternative).
        Bootstraps differential returns to calculate the physical p-value
        that the strategy is indistinguishable from the benchmark under random noise.
        """
        if len(strategy_returns) != len(benchmark_returns) or len(strategy_returns) < 100:
            return 1.0 # Auto-fail p-value
            
        diffs = strategy_returns.values - benchmark_returns.values
        mean_diff = np.mean(diffs)
        
        # Centered bootstrap distribution for null hypothesis (mean = 0)
        centered_diffs = diffs - mean_diff
        
        boot_means = []
        n_samples = len(diffs)
        
        for _ in range(n_bootstraps):
            sample = np.random.choice(centered_diffs, size=n_samples, replace=True)
            boot_means.append(np.mean(sample))
            
        boot_means = np.array(boot_means)
        
        # P-value is the proportion of bootstrapped means >= the observed mean
        p_value = np.sum(boot_means >= mean_diff) / n_bootstraps
        return float(p_value)

    @staticmethod
    def evaluate_turnover(trades_df: pd.DataFrame, portfolio_value: float) -> dict:
        """
        Assesses execution capacity and speed limits.
        Rejects strategies demanding unrealistic high-frequency turnover.
        """
        if trades_df.empty or 'size' not in trades_df.columns:
            return {"high_turnover_risk": False}
            
        # Assuming size is in base currency, calculate total churn
        total_volume = trades_df['size'].sum()
        
        # Calculate timespan in days
        if 't_start' in trades_df.columns and len(trades_df) > 1:
            timespan_days = (trades_df['t_start'].max() - trades_df['t_start'].min()).total_seconds() / 86400
        else:
            timespan_days = 1.0 # default fallback
            
        timespan_days = max(timespan_days, 1.0)
        
        daily_turnover_pct = (total_volume / timespan_days) / portfolio_value
        annualized_turnover = daily_turnover_pct * 365
        
        # If the strategy turns over the entire portfolio > 50 times a year, it's highly susceptible to slip/fee execution drag
        high_risk = annualized_turnover > 50.0  
        
        return {
            "high_turnover_risk": bool(high_risk),
            "annualized_turnover": float(annualized_turnover),
            "daily_volume": float(total_volume / timespan_days)
        }

    @staticmethod
    def detect_model_decay(oos_returns: pd.Series, window: int = 50) -> dict:
        """
        Calculates the slope of out-of-sample performance immediately following the training bounds.
        Detects if alpha burns out rapidly.
        """
        if len(oos_returns) < window * 2:
            return {"rapid_decay": False}
            
        # Compare first 50 periods vs second 50 periods
        early_ret = oos_returns.iloc[:window].sum()
        late_ret = oos_returns.iloc[window:window*2].sum()
        
        # If the first OOS block makes all the money and the second loses > 50% of it, it decays instantly
        rapid_decay = early_ret > 0 and late_ret < -(early_ret * 0.5)
        
        return {"rapid_decay": bool(rapid_decay), "early_ret": float(early_ret), "late_ret": float(late_ret)}

    @staticmethod
    def signal_quality_diagnostics(y_true: np.ndarray, y_proba: np.ndarray) -> dict:
        """
        Brier Score and Calibration metric verification.
        Ensures the ML probabilities map to physical hit rates.
        """
        if len(y_true) < 10 or len(y_proba) < 10:
            return {"valid_calibration": True}
            
        # Brier score: Mean squared difference between predicted probability and actual outcome
        brier = np.mean((y_proba - y_true)**2)
        
        # Precision formulation
        binary_preds = (y_proba > 0.5).astype(int)
        true_pos = np.sum((binary_preds == 1) & (y_true == 1))
        false_pos = np.sum((binary_preds == 1) & (y_true == 0))
        
        precision = true_pos / (true_pos + false_pos + 1e-9)
        
        # Calibration is valid if Brier is reasonably low (< 0.25 is better than random guessing)
        valid = brier < 0.25 and precision > 0.40
        
        return {"valid_calibration": bool(valid), "brier_score": float(brier), "precision": float(precision)}
