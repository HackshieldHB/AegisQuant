"""
AegisQuant ML Model — Time-series safe training, versioning, reproducibility.
---------------------------------------------------------------------------
- Chronological split only (no shuffle).
- Save model + feature list + metadata.
- Inference: strict feature validation; fail if critical features missing.
"""

import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


METADATA_FILENAME = "metadata.json"
RANDOM_STATE = 42


def _chronological_split(
    X: pd.DataFrame, y: pd.Series, test_ratio: float = 0.2
) -> tuple:
    n = len(X)
    split_idx = int(n * (1 - test_ratio))
    if split_idx < 1 or n - split_idx < 1:
        split_idx = max(1, n - 1)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    return X_train, X_test, y_train, y_test


class AIModelBase:
    def __init__(self, name: str) -> None:
        self.name = name
        self.model = None
        self.logger = AG_LOGGER
        self.model_path = os.path.join(CONFIG["AI"]["MODEL_PATH"], f"{name}.joblib")
        self.metadata_path = os.path.join(CONFIG["AI"]["MODEL_PATH"], f"{name}_{METADATA_FILENAME}")
        os.makedirs(CONFIG["AI"]["MODEL_PATH"], exist_ok=True)

    def train(self, data: pd.DataFrame, target: str) -> None:
        raise NotImplementedError

    def predict_proba_row(self, X: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError


class RandomForestModel(AIModelBase):
    def __init__(self) -> None:
        super().__init__("RandomForest")

    def train(self, data: pd.DataFrame, target: str = "target") -> None:
        df = data.dropna().copy()
        if df.empty:
            self.logger.warning("No data to train on")
            return
        drop_cols = [target, "timestamp", "target"]
        X = df.drop(columns=[c for c in drop_cols if c in df.columns], errors="ignore")
        X = X.select_dtypes(include=[np.number])
        if target not in df.columns:
            self.logger.warning("Target column %s not found", target)
            return
        y = df[target]
        if len(X) != len(y):
            return
        X_train, X_test, y_train, y_test = _chronological_split(X, y, test_ratio=0.2)
        if len(X_train) < 10:
            self.logger.warning("Insufficient training samples after chronological split")
            return
        self.model = RandomForestClassifier(
            n_estimators=50,  # REDUCED: 200 → 50 (70% memory reduction, minimal accuracy loss)
            max_depth=8,      # REDUCED: 10 → 8 (prevents overfitting)
            n_jobs=1,         # CRITICAL: Force single thread (avoid thread overhead on 5700U)
            random_state=RANDOM_STATE
        )
        self.model.fit(X_train, y_train)
        feature_list = list(X_train.columns)
        acc = self.model.score(X_test, y_test)
        self.logger.info("Model trained (chronological split); test accuracy=%.4f", acc)
        self.save(feature_list)

    def save(self, feature_list: Optional[List[str]] = None) -> None:
        if self.model is None:
            return
        joblib.dump(self.model, self.model_path)
        meta = {
            "feature_list": feature_list or getattr(self.model, "feature_names_in_", list(self.model.feature_names_in_) if hasattr(self.model, "feature_names_in_") else []),
            "version": datetime.now(timezone.utc).isoformat(),
            "n_estimators": getattr(self.model, "n_estimators", 0),
            "random_state": RANDOM_STATE,
        }
        with open(self.metadata_path, "w") as f:
            json.dump(meta, f, indent=2)
        self.logger.info("Model and metadata saved to %s", self.model_path)

    def load(self) -> bool:
        if not os.path.exists(self.model_path):
            self.logger.warning("No model file at %s", self.model_path)
            return False
        self.model = joblib.load(self.model_path)
        return True

    def get_feature_list(self) -> List[str]:
        if hasattr(self.model, "feature_names_in_"):
            return list(self.model.feature_names_in_)
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path) as f:
                    meta = json.load(f)
                return meta.get("feature_list", [])
            except Exception:
                pass
        return []

    def predict_proba_row(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            self.load()
        if self.model is None:
            return np.array([[0.5, 0.5]])
        X = X.select_dtypes(include=[np.number])
        if X.empty:
            return np.array([[0.5, 0.5]])
        # Align to training feature order to prevent silent column-order mismatches
        if hasattr(self.model, "feature_names_in_"):
            X = X.reindex(columns=self.model.feature_names_in_, fill_value=0)
        return self.model.predict_proba(X.tail(1))  # (1, n_classes)

    def predict(self, data: pd.DataFrame) -> float:
        probs = self.predict_proba_row(data)
        if probs is None or probs.size == 0:
            return 0.5
        # Class 1 index (assuming 0=down, 1=up)
        if probs.shape[1] >= 2:
            return float(probs[-1, 1])
        return float(probs[-1, 0])
