"""
ModelLoader
-----------
Loads sector/symbol-specific ML models trained by AegisQuantTrainer.
Aligned with AegisQuantConfig.py

Naming convention (newest trainer):
  {base}_GLOBAL.joblib   — calibrated global fallback model
  {base}_LOW_VOL.joblib  — regime-specialist
  {base}_HIGH_VOL_BULL.joblib
  {base}_HIGH_VOL_BEAR.joblib
  {base}_META.joblib     — meta-labeling filter

Legacy fallback (old trainer):
  {base}.joblib          — single plain model

ModelLoader tries GLOBAL first, then falls back to plain .joblib.
"""

import os
import json
import numpy as np
from joblib import load
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER

if CONFIG.get("LSTM_MODEL", {}).get("ENABLED", True):
    try:
        from AI.LSTMModel import AegisLSTM as _AegisLSTM
        _LSTM_AVAILABLE = True
    except ImportError:
        _LSTM_AVAILABLE = False
else:
    _AegisLSTM = None
    _LSTM_AVAILABLE = False

logger = AG_LOGGER
MODEL_DIR = CONFIG["AI"]["MODEL_PATH"]

def _model_admitted(sector: str, symbol: str) -> bool:
    admission = CONFIG.get("MODEL_ADMISSION", {})
    if not admission.get("ENABLED", True):
        return True

    base = f"RandomForest_{sector}_{symbol.replace('/', '')}"
    meta_path = os.path.join(MODEL_DIR, f"{base}_metadata.json")
    try:
        with open(meta_path) as f:
            metadata = json.load(f)
    except Exception as exc:
        logger.error(
            "Model admission rejected %s/%s: metadata unavailable (%s)",
            sector,
            symbol,
            exc,
        )
        return False

    test_accuracy = float(metadata.get("global_test_accuracy", 0.0) or 0.0)
    wf_accuracy = float(
        metadata.get("walk_forward", {}).get("mean_accuracy", 0.0) or 0.0
    )
    train_samples = int(metadata.get("n_train", 0) or 0)
    admitted = (
        test_accuracy >= float(admission.get("MIN_TEST_ACCURACY", 0.51))
        and wf_accuracy >= float(admission.get("MIN_WALK_FORWARD_ACCURACY", 0.51))
        and train_samples >= int(admission.get("MIN_TRAIN_SAMPLES", 1000))
    )
    if not admitted:
        logger.error(
            "MODEL_ADMISSION_REJECTED %s/%s test=%.3f wf=%.3f n=%d",
            sector,
            symbol,
            test_accuracy,
            wf_accuracy,
            train_samples,
        )
    return admitted


# ─────────────────────────────────────────────────────────────────────────────
# Ensemble wrapper: soft-voting blend of RF + XGB + LGB
# ─────────────────────────────────────────────────────────────────────────────
class EnsembleModel:
    """
    Soft-voting ensemble that wraps multiple classifier models.
    Implements predict_proba() / predict() / score() so it's a drop-in
    replacement for any single sklearn-compatible model.

    weights: list of floats that sum to 1.0 (normalised internally)
    """

    def __init__(self, models_weights: list, feature_names_in_) -> None:
        """
        models_weights: [(model, weight), ...]
        feature_names_in_: numpy array of feature names (from the base model)
        """
        self._models_weights = models_weights
        total = sum(w for _, w in models_weights)
        self._norm_weights = [(m, w / total) for m, w in models_weights]
        self.feature_names_in_ = feature_names_in_

        # ── Cross-validate that all ensemble members share the same features ──
        # Mismatches usually mean a stale model from a previous training run was
        # loaded.  We log loudly but keep the ensemble rather than crashing —
        # the Predictor's _align_features will handle column selection at
        # inference time.
        ref_features = set(feature_names_in_) if feature_names_in_ is not None else set()
        for m, _w in models_weights:
            m_feats = getattr(m, "feature_names_in_", None)
            if m_feats is None:
                continue  # LSTM or legacy model without feature list
            m_set = set(m_feats)
            extra   = m_set - ref_features
            missing = ref_features - m_set
            if extra or missing:
                logger.warning(
                    "EnsembleModel feature mismatch for %s — "
                    "extra: %s  missing: %s  (model will use its own feature set at inference)",
                    type(m).__name__, sorted(extra)[:10], sorted(missing)[:10],
                )

    def predict_proba(self, X):
        """
        Blend all ensemble members into a single (1, 3) probability row.

        Shape contract — every member is normalised to (1, 3) = [P(-1), P(0), P(1)]:
          • RF / XGB / LGB  — 3-class output already; we pass X.tail(1) for efficiency.
          • LSTM            — binary [P_short, P_long]; we pass the FULL dataframe so
                              the sequence model gets ≥60 rows, then pad a neutral
                              column at index 1 to match the 3-class layout.

        Failed members are skipped (weight is redistributed implicitly).
        """
        blended   = None
        total_w   = 0.0

        for model, w in self._norm_weights:
            try:
                from AI.LSTMModel import AegisLSTM as _AL
                if isinstance(model, _AL):
                    # LSTM requires the full sequence history (≥60 rows).
                    X_arr = X.values if hasattr(X, "values") else np.asarray(X)
                    proba = model.predict_proba(X_arr)   # (N-59, 2)
                    proba = proba[-1:, :]                 # (1, 2): [P_short, P_long]
                else:
                    # RF / XGB / LGB only need the last bar for inference.
                    X_last = X.tail(1) if hasattr(X, "tail") else X[-1:]
                    proba  = model.predict_proba(X_last)  # (1, 3)
            except Exception as _e:
                logger.debug(
                    "EnsembleModel: member %s skipped (%s)",
                    type(model).__name__, _e,
                )
                continue   # skip member; weight redistributed naturally below

            # Normalise binary LSTM output (1, 2) → (1, 3) by inserting a zero
            # neutral column at index 1, giving [P(-1), 0, P(1)] = [-1, 0, +1].
            if proba.shape[1] == 2:
                proba = np.concatenate(
                    [proba[:, :1], np.zeros((proba.shape[0], 1)), proba[:, 1:]],
                    axis=1,
                )

            blended  = proba * w if blended is None else blended + proba * w
            total_w += w

        if blended is None or total_w == 0:
            return np.array([[0.333, 0.334, 0.333]])   # neutral fallback

        # Re-normalise if any member was skipped (weights no longer sum to 1)
        if abs(total_w - 1.0) > 0.01:
            blended = blended / total_w

        return blended

    def predict(self, X):
        proba = self.predict_proba(X)
        classes = getattr(self._norm_weights[0][0], "classes_", None)
        if classes is not None:
            return np.array([classes[np.argmax(p)] for p in proba])
        return (proba[:, 1] >= 0.5).astype(int)

    def score(self, X, y):
        return (self.predict(X) == np.array(y)).mean()


def _attach_feature_list_from_metadata(model, sector: str, symbol: str):
    """
    If a model lacks feature_names_in_ (old sklearn or plain joblib), try to inject
    the feature list from the companion metadata JSON so the predictor can align columns.
    """
    if hasattr(model, "feature_names_in_") and model.feature_names_in_ is not None:
        return model
    base = f"RandomForest_{sector}_{symbol.replace('/', '')}"
    meta_path = os.path.join(MODEL_DIR, f"{base}_metadata.json")
    if not os.path.exists(meta_path):
        return model
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        fl = meta.get("feature_list", [])
        if fl:
            import numpy as np
            model.feature_names_in_ = np.array(fl, dtype=object)
            logger.info("Injected feature_list from metadata for %s/%s (%d features)", sector, symbol, len(fl))
    except Exception as e:
        logger.warning("Could not inject feature_list from metadata for %s/%s: %s", sector, symbol, e)
    return model


def _validate_model(model, sector: str, symbol: str):
    """Return model if it has a non-empty feature list; else return None."""
    feature_names = getattr(model, "feature_names_in_", None)
    if feature_names is None or len(feature_names) == 0:
        logger.error("Model %s/%s has no feature_names_in_ after metadata injection.", sector, symbol)
        return None
    return model


def load_model(sector: str, symbol: str):
    """
    Load the best available model for a given sector + symbol.

    Search order:
      1. Ensemble (RF + XGB + LGB) — if all three _GLOBAL files exist
      2. {base}_GLOBAL.joblib       — single new-pipeline model
      3. {base}.joblib              — legacy plain model

    Returns None if nothing loadable is found.
    """
    if not _model_admitted(sector, symbol):
        return None

    base = f"RandomForest_{sector}_{symbol.replace('/', '')}"

    ensemble_cfg = CONFIG.get("ENSEMBLE", {})
    ensemble_enabled = ensemble_cfg.get("ENABLED", True)

    # ── Attempt ensemble load ─────────────────────────────────────────
    if ensemble_enabled:
        rf_path  = os.path.join(MODEL_DIR, f"{base}_GLOBAL.joblib")
        xgb_path = os.path.join(MODEL_DIR, f"{base}_XGB.joblib")
        lgb_path = os.path.join(MODEL_DIR, f"{base}_LGB.joblib")

        models_to_blend = []
        if os.path.exists(rf_path):
            try:
                m = load(rf_path)
                m = _attach_feature_list_from_metadata(m, sector, symbol)
                if _validate_model(m, sector, symbol):
                    models_to_blend.append((m, ensemble_cfg.get("RF_WEIGHT", 0.40)))
            except Exception as e:
                logger.warning("Ensemble: RF load failed %s: %s", rf_path, e)

        if os.path.exists(xgb_path):
            try:
                m = load(xgb_path)
                m = _attach_feature_list_from_metadata(m, sector, symbol)
                if _validate_model(m, sector, symbol):
                    models_to_blend.append((m, ensemble_cfg.get("XGB_WEIGHT", 0.35)))
            except Exception as e:
                logger.warning("Ensemble: XGB load failed %s: %s", xgb_path, e)

        if os.path.exists(lgb_path):
            try:
                m = load(lgb_path)
                m = _attach_feature_list_from_metadata(m, sector, symbol)
                if _validate_model(m, sector, symbol):
                    models_to_blend.append((m, ensemble_cfg.get("LGB_WEIGHT", 0.25)))
            except Exception as e:
                logger.warning("Ensemble: LGB load failed %s: %s", lgb_path, e)

        # ── Attempt LSTM load ─────────────────────────────────────────
        lstm_cfg = CONFIG.get("LSTM_MODEL", {})
        if _LSTM_AVAILABLE and lstm_cfg.get("ENABLED", True):
            lstm_model = _AegisLSTM.load(MODEL_DIR, base)
            if lstm_model is not None:
                lstm_weight = lstm_cfg.get("ENSEMBLE_WEIGHT", 0.20)
                # Re-normalise classical weights to leave room for LSTM
                classical_sum = sum(w for _, w in models_to_blend)
                scaled = [(m, w * (1.0 - lstm_weight) / max(classical_sum, 1e-9))
                          for m, w in models_to_blend]
                scaled.append((lstm_model, lstm_weight))
                models_to_blend = scaled
                logger.info("LSTM model added to ensemble for %s/%s (weight=%.2f)", sector, symbol, lstm_weight)

        if len(models_to_blend) >= 2:
            base_model = models_to_blend[0][0]
            ensemble = EnsembleModel(models_to_blend, base_model.feature_names_in_)
            names = [type(m).__name__ for m, _ in models_to_blend]
            logger.info(
                "Ensemble model loaded for %s/%s — %d members: %s",
                sector, symbol, len(models_to_blend), names,
            )
            return ensemble

    # ── Single-model fallback ──────────────────────────────────────────
    preferred_type = str(ensemble_cfg.get("SINGLE_MODEL_TYPE", "RF")).upper()
    preferred_suffix = {
        "RF": "_GLOBAL.joblib",
        "XGB": "_XGB.joblib",
        "LGB": "_LGB.joblib",
    }.get(preferred_type, "_GLOBAL.joblib")
    candidate_suffixes = [
        preferred_suffix,
        "_GLOBAL.joblib",
        ".joblib",
    ]
    candidates = []
    for suffix in candidate_suffixes:
        path = os.path.join(MODEL_DIR, f"{base}{suffix}")
        if path not in candidates:
            candidates.append(path)

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            model = load(path)
            model = _attach_feature_list_from_metadata(model, sector, symbol)
            validated = _validate_model(model, sector, symbol)
            if validated is None:
                continue
            fl = list(validated.feature_names_in_)
            logger.info(
                "Loaded single model for %s/%s from %s | features=%d",
                sector, symbol, os.path.basename(path), len(fl),
            )
            return validated
        except Exception as e:
            logger.error("Failed to load model %s: %s", path, e)

    logger.warning("No usable model found for %s/%s. Symbol will be skipped by predictor.", sector, symbol)
    return None


def load_all_models():
    """
    Iterate through CONFIG["SYMBOLS"] and load all available models.
    Returns a dict: {sector: {symbol: model | None}}
    """
    models = {}
    live_symbols = set(CONFIG["PROJECT"].get("LIVE_SYMBOLS", []))
    for sector, symbols in CONFIG["SYMBOLS"].items():
        if CONFIG["MARKETS"][sector.upper()]:
            models[sector] = {}
            for symbol in symbols.keys():
                if (
                    CONFIG["PROJECT"]["MODE"] == "LIVE"
                    and sector.upper() == "CRYPTO"
                    and live_symbols
                    and symbol.upper() not in live_symbols
                ):
                    models[sector][symbol] = None
                    continue
                models[sector][symbol] = load_model(sector, symbol)
    return models
