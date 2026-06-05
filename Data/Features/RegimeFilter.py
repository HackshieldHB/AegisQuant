import pandas as pd
import numpy as np
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler
import joblib

class RegimeFilter:
    """
    Institutional-Grade Regime Detection Module.
    Enforces strict anti-lookahead rules: StandardScaler and GaussianHMM 
    are ONLY fitted on training OOS data, and applied via .predict() in production.
    """
    
    # Canonical string mappings
    LOW_VOL = "LOW_VOL"
    BULL = "HIGH_VOL_BULL"
    BEAR = "HIGH_VOL_BEAR"

    def __init__(self, n_components=3, min_bars=5, transition_cooldown_bars=3, random_state=42):
        self.n_components = n_components
        self.min_bars = min_bars
        self.transition_cooldown_bars = transition_cooldown_bars
        self.random_state = random_state
        
        # ML artifacts
        self.model = GaussianHMM(
            n_components=self.n_components, 
            covariance_type="full", 
            n_iter=1000, 
            random_state=self.random_state
        )
        self.scaler = StandardScaler()
        self.state_map = {} # Maps integer states (0, 1, 2) to Canonical Strings
        
        self.is_fitted = False

    def _prepare_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Creates the standardized features for the HMM: Log Returns & Realized Volatility.
        """
        feats = pd.DataFrame(index=df.index)
        
        # 1. Log Returns
        # We use shifted close to prevent immediate execution-time leakage.
        # Actually, for regime state of the *current* closed bar, using its close vs prev_close is fine.
        prev_close = df['close'].shift(1)
        feats['log_return'] = np.log(df['close'] / prev_close)
        
        # 2. Realized Volatility (Rolling 20-period StdDev of returns)
        # Using min_periods=2 to get early variance, replace 0 variance with minimum float
        feats['realized_vol'] = feats['log_return'].rolling(window=20, min_periods=2).std(ddof=1)
        
        # Clean up NaNs / Infs safely
        feats.replace([np.inf, -np.inf], np.nan, inplace=True)
        # Fill early data with neutral zeros
        feats = feats.fillna(0.0)
        
        return feats

    def fit(self, train_df: pd.DataFrame):
        """
        Strictly fits the Scaler and the HMM onto the Training dataset only.
        Maps the arbitrary integer states to canonical labels based on mathematical topology.
        """
        if len(train_df) < 100:
            raise ValueError("Insufficient data to fit HMM.")
            
        feats = self._prepare_features(train_df)
        X = feats[['log_return', 'realized_vol']].values
        
        # 1. Fit standard scaler to training data
        X_scaled = self.scaler.fit_transform(X)
        
        # 2. Fit HMM
        self.model.fit(X_scaled)
        
        # 3. Predict the training states to compute their semantic mapping
        states = self.model.predict(X_scaled)
        feats['state'] = states
        
        # 4. Semantic Mapping Logic
        # Calculate mean return and mean volatility for each unique state
        state_stats = feats.groupby('state').agg({
            'log_return': 'mean',
            'realized_vol': 'mean'
        }).to_dict(orient='index')
        
        # Identify the lowest volatility state
        low_vol_state = min(state_stats, key=lambda s: state_stats[s]['realized_vol'])
        
        # Identify Bull and Bear from the remaining two states
        remaining_states = [s for s in state_stats.keys() if s != low_vol_state]
        
        if len(remaining_states) == 2:
            s1, s2 = remaining_states
            # Highest mean return goes to BULL
            if state_stats[s1]['log_return'] > state_stats[s2]['log_return']:
                bull_state, bear_state = s1, s2
            else:
                bull_state, bear_state = s2, s1
        elif len(remaining_states) == 1:
            # Degenerate case, force it
            s1 = remaining_states[0]
            if state_stats[s1]['log_return'] > 0:
                bull_state, bear_state = s1, -1
            else:
                bear_state, bull_state = s1, -1
        else:
            # Failsafe if everything collapsed into 1 state
            bull_state, bear_state = -1, -2
        
        self.state_map = {
            low_vol_state: self.LOW_VOL,
            bull_state: self.BULL,
            bear_state: self.BEAR
        }
        
        self.is_fitted = True
        return self

    def _apply_hysteresis_filter(self, raw_states: pd.Series) -> tuple[pd.Series, pd.Series]:
        """
        Requires a state to persist for `min_bars` before locking it in.
        Returns the filtered state series and a boolean transition_flag series.
        """
        filtered_states = raw_states.copy()
        transitions = pd.Series(False, index=raw_states.index)
        
        current_state = raw_states.iloc[0]
        consecutive_count = 1
        
        for i in range(1, len(raw_states)):
            candidate_state = raw_states.iloc[i]
            
            if candidate_state == current_state:
                consecutive_count += 1
            else:
                consecutive_count = 1 # Reset counter on change
                current_state = candidate_state
                
            # If we haven't hit the threshold, keep the previous filtered state
            if consecutive_count < self.min_bars:
                filtered_states.iloc[i] = filtered_states.iloc[i-1]
                
            # Flag a transition on the exact bar the filter finally accepts the new state
            if consecutive_count == self.min_bars:
                transitions.iloc[i] = True
                
        return filtered_states, transitions

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Applies the pre-fitted Scaler and HMM to the dataset.
        Applies the Hysteresis filter and appends standard regime columns.
        """
        if not self.is_fitted:
            raise RuntimeError("RegimeFilter must be .fit() before .predict()")
            
        result = df.copy()
        
        # 1. Prepare and scale features without leaking
        feats = self._prepare_features(df)
        X = feats[['log_return', 'realized_vol']].values
        X_scaled = self.scaler.transform(X)
        
        # 2. Predict raw states
        raw_states = self.model.predict(X_scaled)
        raw_series = pd.Series(raw_states, index=df.index)
        
        # 3. Apply smoothing / hysteresis filter
        filtered_states, transition_flags = self._apply_hysteresis_filter(raw_series)
        
        # 4. Map integers to Semantic Strings
        # Map unknown states to LOW_VOL as a baseline safety
        mapped_states = filtered_states.map(lambda x: self.state_map.get(x, self.LOW_VOL))
        
        # 5. Append diagnostic and routing columns
        result['hmm_raw_state'] = raw_series
        result['regime'] = mapped_states
        result['regime_transition_flag'] = transition_flags
        
        # Calculate state probabilities safely
        try:
            probs = self.model.predict_proba(X_scaled)
            result['regime_confidence'] = np.max(probs, axis=1)
        except Exception:
            result['regime_confidence'] = 1.0 # Failsafe
        # 6. Transition Cooldown: suppress trading for N bars after any confirmed regime change
        regime_suppression = pd.Series(False, index=df.index)
        cooldown_remaining = 0
        for i in range(len(transition_flags)):
            if transition_flags.iloc[i]:
                cooldown_remaining = self.transition_cooldown_bars
            if cooldown_remaining > 0:
                regime_suppression.iloc[i] = True
                cooldown_remaining -= 1
        result['regime_transition_suppression'] = regime_suppression
            
        return result

    def save(self, model_dir: str, prefix: str):
        """Saves artifact pipeline to disk for live loading."""
        import os
        os.makedirs(model_dir, exist_ok=True)
        joblib.dump(self.model, os.path.join(model_dir, f"{prefix}_HMM.joblib"))
        joblib.dump(self.scaler, os.path.join(model_dir, f"{prefix}_Scaler.joblib"))
        import json
        with open(os.path.join(model_dir, f"{prefix}_SemanticMap.json"), "w") as f:
            # JSON keys must be strings
            str_map = {str(k): v for k, v in self.state_map.items()}
            json.dump(str_map, f, indent=2)

    @classmethod
    def load(cls, model_dir: str, prefix: str):
        """Loads fitted artifact pipeline from disk."""
        import os
        import json
        rf = cls()
        rf.model = joblib.load(os.path.join(model_dir, f"{prefix}_HMM.joblib"))
        rf.scaler = joblib.load(os.path.join(model_dir, f"{prefix}_Scaler.joblib"))
        with open(os.path.join(model_dir, f"{prefix}_SemanticMap.json"), "r") as f:
            str_map = json.load(f)
            rf.state_map = {int(k): v for k, v in str_map.items()}
        rf.is_fitted = True
        return rf
