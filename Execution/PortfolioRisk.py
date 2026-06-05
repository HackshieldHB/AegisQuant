import pandas as pd
import numpy as np
import sys
import os

try:
    from Execution.ExecutionRealism import calculate_nonlinear_slippage
except ImportError:
    calculate_nonlinear_slippage = None

class RiskManager:
    """
    Final Gatekeeper for Portfolio Risk Constraints.
    Simulates a chronological execution walk over Meta-Labeler approved signals 
    to enforce live capital fragmentation and correlation protections.
    """
    def __init__(self, 
                 max_positions: int = 5,
                 correlation_threshold: float = 0.7,
                 penalty_factor: float = 0.5,
                 drawdown_limit: float = -0.05,
                 max_drift_bps: float = 10.0,
                 max_session_exposure: float = 0.50,
                 liquidity_threshold: float = 0.05):
        
        self.max_positions = max_positions
        self.correlation_threshold = correlation_threshold
        self.penalty_factor = penalty_factor
        self.drawdown_limit = drawdown_limit
        self.max_drift_bps = max_drift_bps
        self.max_session_exposure = max_session_exposure
        self.liquidity_threshold = liquidity_threshold
        self.min_notional = 10.0 # Standard exchange minimum (e.g. $10)
        self.base_execution_bps = 2.0
        self.system_halted = False

    def emergency_halt(self, halt: bool = True):
        self.system_halted = halt

    def filter_signals(self, signals: pd.DataFrame, returns_df: pd.DataFrame = None) -> pd.DataFrame:
        """
        Chronological execution walk to apply Path-Dependent portfolio limits.
        
        Expected columns in `signals`: 
            ['t_start', 't_end', 'symbol', 'proposed_size', 'pnl_pct']
            
        Returns a copy of the dataframe with an `approved_size` column indicating 
        the ultimate constrained allocation, alongside rejection reasoning.
        """
        if signals.empty:
            result = signals.copy()
            result['approved_size'] = 0.0
            result['rejection_reason'] = 'Empty'
            return result
            
        result = signals.copy().sort_values('t_start')
        result['approved_size'] = 0.0
        result['rejection_reason'] = ''
        result['expected_slippage_bps'] = 0.0
        
        active_trades = []
        current_daily_pnl = 0.0
        last_day = None
        circuit_breaker = False
        
        # Kill switch pre-check
        if self.system_halted:
            result['rejection_reason'] = 'Global Kill Switch Active'
            return result
        
        # Precompute static correlation matrix flag
        has_returns = returns_df is not None and not returns_df.empty
            
        for idx, row in result.iterrows():
            t_start = row['t_start']
            t_end = row['t_end']
            proposed_size = row.get('proposed_size', 0.0)
            symbol = row.get('symbol', 'UNKNOWN')
            
            # Day reset for circuit breakers
            current_day = t_start.date() if hasattr(t_start, 'date') else None
            if current_day != last_day:
                current_daily_pnl = 0.0
                circuit_breaker = False
                last_day = current_day
                
            # Clear expired trades from active margin pool
            active_trades = [t for t in active_trades if t['t_end'] > t_start]
            
            if proposed_size <= 0:
                result.loc[idx, 'rejection_reason'] = 'Negative EV Primary'
                continue
                
            # 1. Daily Drawdown Circuit Breaker
            if circuit_breaker:
                result.loc[idx, 'rejection_reason'] = 'Circuit Breaker Lockout'
                continue
                
            # 2. Max Concurrent Position Capital Fragmentation Lock
            if len(active_trades) >= self.max_positions:
                result.loc[idx, 'rejection_reason'] = 'Max Positions Exceeded'
                continue
                
            # 3. Latency & Price Drift Protection
            signal_price = row.get('signal_price', None)
            current_price = row.get('current_price', None)
            if signal_price is not None and current_price is not None and signal_price > 0:
                drift_bps = abs(current_price - signal_price) / signal_price * 10000
                if drift_bps > self.max_drift_bps:
                    result.loc[idx, 'rejection_reason'] = f'Price Drift Exceeded ({drift_bps:.1f} bps)'
                    continue
                    
            # 4. Session-Level Risk Normalization (Total Notional)
            current_exposure = sum(t['size'] for t in active_trades)
            if current_exposure + proposed_size > self.max_session_exposure:
                # Scale down strictly to what is available
                available = max(0.0, self.max_session_exposure - current_exposure)
                if available <= 0:
                    result.loc[idx, 'rejection_reason'] = 'Max Session Exposure Exceeded'
                    continue
                proposed_size = available
                result.loc[idx, 'rejection_reason'] = 'Scaled (Session Exposure)'

            # 5. Liquidity & Market Impact Check
            adv_proxy = row.get('adv_volume', None)
            if adv_proxy is not None and adv_proxy > 0:
                max_liquidity_size = adv_proxy * self.liquidity_threshold
                if proposed_size > max_liquidity_size:
                    proposed_size = max_liquidity_size
                    result.loc[idx, 'rejection_reason'] = 'Scaled (Liquidity Check)'
                    
            # 5b. Exchange Minimum Notional Limit
            # Only apply when an explicit current_price is provided. proposed_size can be either
            # a fractional weight (0.02 = 2% of capital) or an absolute quantity depending on caller.
            # Without a price we cannot compute notional, so skip the check rather than use a
            # meaningless fallback of 1.0 that would reject all fraction-based sizes.
            current_price_explicit = row.get('current_price', None)
            if current_price_explicit is not None and current_price_explicit > 0:
                if proposed_size * current_price_explicit < self.min_notional:
                    result.loc[idx, 'rejection_reason'] = f'Below Minimum Notional (Val: {proposed_size * current_price_explicit:.2f})'
                    proposed_size = 0.0
                    continue

            # 6. Correlated Asset Penalties (Rolling lookback constraint)
            penalty = 1.0
            correlated_with = None
            if has_returns and symbol in returns_df.columns:
                try:
                    # Locate t_start in returns_df. Guard against dtype mismatch (e.g.,
                    # integer index vs datetime t_start) by catching TypeError.
                    t_idx = -1
                    try:
                        if t_start in returns_df.index:
                            t_idx = returns_df.index.get_loc(t_start)
                        else:
                            idx_arr = returns_df.index.get_indexer([t_start], method='ffill')
                            t_idx = int(idx_arr[0]) if len(idx_arr) > 0 else -1
                    except (TypeError, KeyError):
                        # Index dtype incompatible — use all available rows as the window
                        t_idx = len(returns_df)

                    if t_idx >= 1:  # Need at least 1 row for correlation
                        window_df = returns_df.iloc[max(0, t_idx - 30):t_idx]
                        for active in active_trades:
                            active_sym = active['symbol']
                            if active_sym in returns_df.columns and len(window_df) >= 2:
                                corr = window_df[symbol].corr(window_df[active_sym])
                                if pd.notna(corr) and corr > self.correlation_threshold:
                                    penalty = self.penalty_factor
                                    correlated_with = active_sym
                                    break  # Apply penalty once based on worst correlation
                except (KeyError, IndexError):
                    pass
                            
            final_size = proposed_size * penalty
            
            # Commit Approved Trade to ledger
            if final_size > 0:
                result.loc[idx, 'approved_size'] = final_size
                if penalty < 1.0:
                    # Append correlation penalty to any existing scale messages
                    existing = result.loc[idx, 'rejection_reason']
                    if 'Scaled' in existing:
                        result.loc[idx, 'rejection_reason'] = existing + f' & Correlated ({correlated_with})'
                    else:
                        result.loc[idx, 'rejection_reason'] = f'Correlated Size Penalty ({correlated_with})'
                else:
                    if result.loc[idx, 'rejection_reason'] == '':
                        result.loc[idx, 'rejection_reason'] = 'Approved'
                    
                active_trades.append({
                    't_end': t_end,
                    'symbol': symbol,
                    'size': final_size,
                    'pnl_pct': row.get('pnl_pct', 0.0)
                })
                
                # 7. Apply Execution Realism (Nonlinear Slippage Model)
                adv_proxy = row.get('adv_volume', None)
                vol_scalar = row.get('volatility_scalar', 1.0)
                dynamic_slip = self.base_execution_bps
                
                if calculate_nonlinear_slippage is not None and adv_proxy is not None and adv_proxy > 0:
                     dynamic_slip = calculate_nonlinear_slippage(
                          base_bps=self.base_execution_bps,
                          order_size=final_size,
                          adv_volume=adv_proxy,
                          volatility_scalar=vol_scalar,
                          is_market_order=True
                     )
                result.loc[idx, 'expected_slippage_bps'] = dynamic_slip
                
                # Add expected PnL impact strictly for intraday tracking
                # Apply instantaneous expected hit including absolute execution friction
                expected_pnl_hit = row.get('pnl_pct', 0.0) - (dynamic_slip / 10000.0)
                current_daily_pnl += (final_size * expected_pnl_hit)
                
                if current_daily_pnl <= self.drawdown_limit:
                    circuit_breaker = True
                    
        return result
