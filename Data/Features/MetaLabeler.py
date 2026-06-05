import pandas as pd
import numpy as np

class MetaLabeler:
    """
    Stateless functional module for Phase 5 Meta-Labeling and Sizing evaluation.
    Converts Primary Model predictions into Execution-Gating logic.
    """

    @staticmethod
    def generate_meta_labels(y_true: pd.Series, y_pred: pd.Series) -> pd.Series:
        """
        Generates Binary Meta-Target:
        1 = True Positive (Primary was right, trade succeeds)
        0 = False Positive (Primary was wrong, trade hits SL)
        """
        aligned = pd.concat([y_true, y_pred], axis=1).dropna()
        aligned.columns = ['y_true', 'y_pred']
        
        meta_target = (aligned['y_true'] == aligned['y_pred']).astype(int)
        return pd.Series(meta_target, index=aligned.index)

    @staticmethod
    def build_meta_features(df: pd.DataFrame, primary_signal: pd.Series, primary_proba: pd.Series, events_df: pd.DataFrame) -> pd.DataFrame:
        """
        Appends strictly out-of-sample execution-time context: 
        Regime, Volatility, Order Flow, and structural Barrier Distances.
        """
        meta = pd.DataFrame(index=primary_signal.index)
        meta['primary_signal'] = primary_signal
        meta['primary_proba'] = primary_proba
        
        # Pull Categorical Regime safely
        if 'regime' in df.columns:
            dummies = pd.get_dummies(df['regime'].loc[meta.index], prefix='regime', dtype=float)
            meta = pd.concat([meta, dummies], axis=1)
            
        # Contextual Market States
        if 'atr' in df.columns:
            meta['volatility_atr'] = df['atr'].loc[meta.index]
        
        if 'cvd_z' in df.columns:
            meta['cvd_z'] = df['cvd_z'].loc[meta.index]
            
        if 'flow_aggression_z' in df.columns:
            meta['flow_aggression_z'] = df['flow_aggression_z'].loc[meta.index]
            
        if '1H_htf_trend' in df.columns:
            meta['1H_htf_trend'] = df['1H_htf_trend'].loc[meta.index]
            
        # Distances to barriers from structural pricing events
        if all(col in events_df.columns for col in ['entry_price', 'upper_barrier', 'lower_barrier']):
            # Events structure operates on the same index keys
            e_df = events_df.loc[meta.index]
            
            # Mathematical distance dynamically tracks the physical constraint shape
            meta['dist_upper_pct'] = (e_df['upper_barrier'] - e_df['entry_price']).abs() / e_df['entry_price']
            meta['dist_lower_pct'] = (e_df['entry_price'] - e_df['lower_barrier']).abs() / e_df['entry_price']
            
        return meta.fillna(0)

    @staticmethod
    def compute_kelly_size(
        p_success: pd.Series, 
        payoff_ratio: float, 
        kelly_fraction: float = 0.5, 
        max_risk_cap: float = 0.02,
        funding_rate_bps_per_hour: float = 0.0
    ) -> pd.Series:
        """
        Computes Fractional Kelly position sizing safely bounded by total capital destruction protections.
        Formulas:
        f_full = p_success - ((1.0 - p_success) / payoff_ratio)
        f_deploy = max(0, min(f_full * kelly_fraction, max_risk_cap))
        """
        f_full = p_success - ((1.0 - p_success) / payoff_ratio)
        
        # Funding rate & carry cost penalty
        if funding_rate_bps_per_hour != 0.0:
            # Approximate 24-hour carry penalty 
            carry_penalty = (funding_rate_bps_per_hour * 24.0) / 10000.0
            f_full = f_full - carry_penalty
            
        f = (f_full * kelly_fraction).round(10)
        
        # Hard limits on output boundary
        f = f.clip(lower=0.0, upper=max_risk_cap)
        return f

    @staticmethod
    def generate_oco_payloads(df: pd.DataFrame, max_entry_drift_bps: float = 10.0) -> list:
        """
        Creates immediately executable OCO order dictionaries for exchange APIs
        based on approved execution sizes and structural barrier bounds.
        
        Expected columns:
        ['symbol', 'primary_signal', 'approved_size', 'entry_price', 'lower_barrier', 'upper_barrier']
        """
        payloads = []
        for idx, row in df.iterrows():
            if row.get('approved_size', 0.0) <= 0:
                continue
                
            direction = row.get('primary_signal', 1)
            side = "LONG" if direction == 1 else "SHORT"
            
            entry_price = round(float(row['entry_price']), 6)
            stop_loss = round(float(row['lower_barrier']), 6)
            take_profit = round(float(row['upper_barrier']), 6)
            
            # OCO Validation before dispatch
            if direction == 1: # LONG validation
                if not (stop_loss < entry_price < take_profit):
                    continue
            else: # SHORT validation
                # In TripleBarrier short convention, lower_barrier is the lower price (TP) and upper_barrier is higher price (SL)
                # Ensure geometry is valid: TP < Entry < SL
                if not (stop_loss < entry_price < take_profit): 
                    continue
            
            payload = {
                "symbol": row.get('symbol', 'UNKNOWN'),
                "side": side,
                "position_size": round(float(row['approved_size']), 6),
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "entry_tif": "IOC", # Immediate Or Cancel to prevent stale entries
                "oco_tif": "GTC",   # Good Till Cancel for the stop legs
                "max_drift_bps": max_entry_drift_bps,
                "signal_timestamp": str(idx)
            }
            payloads.append(payload)
            
        return payloads
