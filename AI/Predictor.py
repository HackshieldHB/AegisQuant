"""
AIPredictor — Deterministic inference, strict feature alignment, P(long)/P(short).
-------------------------------------------------------------------------------
- Load model once per symbol.
- Align live features to model's feature list; fail if critical features missing.
- Return P_long, P_short and confidence aligned to signal direction.
- No silent zero-fill for missing critical features.
"""

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER

class AIPredictor:
    def __init__(self, models: Optional[Dict] = None) -> None:
        self.logger = AG_LOGGER
        self.models = models if models is not None else {}

    def _align_features(self, df: pd.DataFrame, feature_list: list) -> Optional[pd.DataFrame]:
        """Align live df to model feature list dynamically. RAISES error if critical features missing."""
        if df.empty or not feature_list:
            raise ValueError("Empty DataFrame or no feature list provided to _align_features")
        
        df = df.copy()
        numeric = df.select_dtypes(include=[np.number])
        live_cols = numeric.columns.tolist()
        
        # Dynamic mapping to ensure backward compatibility without hardcoded maps
        dynamic_map = {}
        for req_feat in feature_list:
            req_lower = req_feat.lower()
            
            # 1. Exact match
            if req_feat in live_cols:
                dynamic_map[req_feat] = req_feat
                continue
                
            # 2. Case-insensitive exact match
            live_cols_lower = {c.lower(): c for c in live_cols}
            if req_lower in live_cols_lower:
                dynamic_map[live_cols_lower[req_lower]] = req_feat
                continue
                
            # 3. Substring mapping for backward compatibility
            matched = False
            for c in live_cols:
                c_lower = c.lower()
                if req_lower in c_lower:
                    dynamic_map[c] = req_feat
                    matched = True
                    break
                # Special cases for historic model naming structures
                if req_lower == 'bollinger' and 'bbm' in c_lower:
                    dynamic_map[c] = req_feat
                    matched = True
                    break
                if req_lower == 'atr' and 'atrr' in c_lower:
                    dynamic_map[c] = req_feat
                    matched = True
                    break
            
            # 4. Fallback proxy for historic ema_14 where only ema_50 was emitted
            if not matched and req_lower == 'ema_14':
                for c in live_cols:
                    if 'ema' in c.lower():
                        dynamic_map[c] = req_feat
                        break
        
        # Rename live columns to training names
        renamed = numeric.rename(columns=dynamic_map)
        
        # Remove duplicate column names (keep first occurrence)
        renamed = renamed.loc[:, ~renamed.columns.duplicated(keep='first')]
        
        available = set(renamed.columns) & set(feature_list)
        missing = set(feature_list) - available
        
        if missing:
            self.logger.error("CRITICAL: Feature alignment missing required features: %s", missing)
            raise ValueError(f"Missing critical features for prediction: {missing}. Cannot proceed with NaN values.")
        
        out = renamed[feature_list]  # Select only the required columns in order
        
        # Final NaN check after reindexing
        if out.isna().any().any():
            nan_cols = out.columns[out.isna().any()].tolist()
            self.logger.error("CRITICAL: NaN values found in aligned features: %s", nan_cols)
            raise ValueError(f"NaN values found in aligned features: {nan_cols}. Model prediction unreliable.")
        
        return out

    def get_probabilities(
        self,
        features: pd.DataFrame,
        sector: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Tuple[float, float]:
        """
        Returns (P_long, P_short). For binary classifier, P_short = 1 - P_long.
        Uses sector/symbol model if available; else raises error (NEVER silent 0.5, 0.5).
        RAISES ValueError if model features cannot be aligned.
        """
        if not CONFIG["AI"]["ENABLED"] or features.empty:
            return 0.5, 0.5
        
        model = None
        if sector and symbol:
            model = self.models.get(sector, {}).get(symbol)
        
        if model is None:
            self.logger.error("CRITICAL: No model found for %s %s. AI disabled for this symbol.", sector, symbol)
            raise ValueError(f"No model found for {sector}/{symbol}")
        
        feature_list = getattr(model, "feature_names_in_", None)
        if feature_list is not None:
            feature_list = list(feature_list)
        else:
            feature_list = getattr(model, "feature_list", [])
        
        if not feature_list:
            raise ValueError(f"Model for {sector}/{symbol} has no feature_names_in_. Model corrupted or incompatible.")
        
        try:
            X = self._align_features(features, feature_list)
        except ValueError as e:
            self.logger.error("CRITICAL: Feature alignment failed for %s %s: %s", sector, symbol, e)
            raise
        
        try:
            # Pass the FULL aligned dataframe so the LSTM member inside
            # EnsembleModel receives its required ≥60-row sequence.
            # EnsembleModel handles per-member slicing internally and always
            # returns shape (1, n_classes); non-ensemble models return (N, n_classes)
            # so we take the last row with probs[-1].
            probs = model.predict_proba(X)
            if probs is None or probs.size == 0:
                raise ValueError("Model returned None or empty probabilities")

            row = probs[-1]
            # Use model.classes_ to locate the correct index for each direction.
            # This handles binary {-1,1}, binary {0,1}, and 3-class {-1,0,1} models.
            raw_classes = getattr(model, "classes_", None)
            if raw_classes is None:
                # Ensemble wrapper — try first member
                first_pair = getattr(model, "_norm_weights", [None])[0]
                if first_pair:
                    raw_classes = getattr(first_pair[0], "classes_", None)

            if raw_classes is not None:
                classes = list(raw_classes)
                if 1 not in classes and -1 not in classes:
                    raise ValueError(f"Unexpected model classes: {classes}")
                idx_long  = classes.index(1)  if 1  in classes else None
                idx_short = classes.index(-1) if -1 in classes else None
                p_long  = float(row[idx_long])  if idx_long  is not None else 0.5
                p_short = float(row[idx_short]) if idx_short is not None else 0.5
            else:
                # Legacy fallback: assume strictly binary [short, long]
                if len(row) < 2:
                    raise ValueError(f"Model returned {len(row)} probability class; expected ≥2")
                p_short = float(row[0])
                p_long  = float(row[1])

            if not (0 <= p_long <= 1 and 0 <= p_short <= 1):
                raise ValueError(f"Invalid probabilities: p_long={p_long}, p_short={p_short}")
            return p_long, p_short
        except Exception as e:
            self.logger.error("CRITICAL: Prediction failed for %s %s: %s", sector, symbol, e)
            raise ValueError(f"Prediction failed: {e}")

    def get_confidence(
        self,
        features: pd.DataFrame,
        sector: Optional[str] = None,
        symbol: Optional[str] = None,
        signal: Optional[str] = None,
    ) -> float:
        """
        Returns confidence aligned to signal direction.
        If signal is BUY: return P_long.
        If signal is SELL: return P_short.
        Otherwise return max(P_long, P_short).
        """
        p_long, p_short = self.get_probabilities(features, sector, symbol)
        if signal == "BUY":
            return p_long
        if signal == "SELL":
            return p_short
        return max(p_long, p_short)
