"""
AegisQuantTrainer
-----------------
Retrains sector-aware RandomForest models using 1 year of historical data
from Binance (Crypto), OANDA (Forex), and Yahoo Finance (Stocks).

Aligned with AegisQuantConfig.py for:
- Symbols
- Markets enable switches
- Broker credentials
- AI model path
- Indicators (dynamic feature selection)
"""

import os
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, timezone
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from joblib import dump
import ta  # pip install ta

# Optional ensemble learners — gracefully skip if not installed
try:
    from xgboost import XGBClassifier
    _XGB_AVAILABLE = True
except ImportError:
    _XGB_AVAILABLE = False
    print("[WARN] xgboost not installed. Ensemble will use RF-only. pip install xgboost")

try:
    from lightgbm import LGBMClassifier
    _LGB_AVAILABLE = True
except ImportError:
    _LGB_AVAILABLE = False
    print("[WARN] lightgbm not installed. Ensemble will use RF-only. pip install lightgbm")

# Import unified config
from AegisQuantConfig import CONFIG

# Import new labeling and features
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from Data.Labeling.TripleBarrier import compute_ewma_volatility, apply_triple_barrier, cusum_filter, print_class_distribution

try:
    from AI.LSTMModel import AegisLSTM as _AegisLSTM
    _LSTM_TRAIN_AVAILABLE = True
except ImportError:
    _LSTM_TRAIN_AVAILABLE = False
    print("[WARN] PyTorch not installed. LSTM model will not be trained. pip install torch")
from Data.Features.OrderFlow import add_order_flow_features
from Data.Features.MultiTimeframe import add_mtf_features
from Data.Features.RegimeFilter import RegimeFilter
from Data.Features.PurgedCV import cross_val_predict_purged
from Data.Features.MetaLabeler import MetaLabeler
from sklearn.calibration import CalibratedClassifierCV

MODEL_DIR = CONFIG["AI"]["MODEL_PATH"]
os.makedirs(MODEL_DIR, exist_ok=True)


def _calibration_method(y_train: pd.Series) -> str:
    configured = CONFIG.get("MODEL_ADMISSION", {}).get("CALIBRATION_METHOD", "auto")
    if configured in {"sigmoid", "isotonic"}:
        return configured
    min_class = int(y_train.value_counts().min()) if len(y_train) else 0
    return "isotonic" if len(y_train) >= 3000 and min_class >= 100 else "sigmoid"


def _calibration_metrics(model, X_test: pd.DataFrame, y_test: pd.Series) -> dict:
    probabilities = model.predict_proba(X_test)
    classes = list(model.classes_)
    class_to_index = {value: index for index, value in enumerate(classes)}
    one_hot = np.zeros_like(probabilities, dtype=float)
    for row_index, label in enumerate(y_test):
        if label in class_to_index:
            one_hot[row_index, class_to_index[label]] = 1.0
    multiclass_brier = float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1)))
    confidence = probabilities.max(axis=1)
    predicted = np.asarray(classes)[probabilities.argmax(axis=1)]
    correctness = (predicted == np.asarray(y_test)).astype(float)
    bins = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    for lower, upper in zip(bins[:-1], bins[1:]):
        mask = (confidence >= lower) & (
            confidence <= upper if upper == 1.0 else confidence < upper
        )
        if mask.any():
            ece += float(mask.mean()) * abs(
                float(correctness[mask].mean()) - float(confidence[mask].mean())
            )
    return {
        "method": _calibration_method(y_test),
        "multiclass_brier": multiclass_brier,
        "expected_calibration_error": float(ece),
        "mean_confidence": float(confidence.mean()),
        "max_confidence": float(confidence.max()),
    }

# --- CRYPTO (Binance REST API — paginated for 12 months of history) ---
def fetch_crypto(symbol, interval="15m", months=12):
    """
    Fetch up to `months` months of Binance kline data by paginating the API.
    Binance returns max 1000 bars per request; at 15m that covers ~10.4 days.
    We page backward from now until we have enough history.
    """
    symbol_fmt = symbol.replace("/", "")
    url = "https://api.binance.com/api/v3/klines"

    # Interval → milliseconds per bar
    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }.get(interval, 900_000)

    target_bars = int((months * 30 * 24 * 60 * 60 * 1000) / interval_ms)
    all_rows = []
    end_time = None  # start from now, page backward

    print(f"[INFO] Fetching {target_bars} bars ({months}m history) for {symbol} @ {interval} ...")
    while len(all_rows) < target_bars:
        params = {"symbol": symbol_fmt, "interval": interval, "limit": 1000}
        if end_time is not None:
            params["endTime"] = end_time

        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"[WARN] Binance fetch error for {symbol}: {e}. Stopping pagination.")
            break

        if not data or not isinstance(data, list) or len(data) == 0:
            break

        all_rows = data + all_rows  # prepend older data
        end_time = int(data[0][0]) - 1  # page further back

        if len(data) < 1000:
            break  # reached the start of available history

    if not all_rows:
        raise ValueError(f"No data returned for {symbol} {interval}")

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("close", "high", "low", "open", "volume", "taker_buy_base", "taker_buy_quote"):
        df[col] = df[col].astype(float)
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    print(f"[INFO] Fetched {len(df)} bars for {symbol} {interval}")
    return df

# --- FOREX (OANDA REST API) ---
def fetch_forex(symbol, granularity="M15"):
    headers = {"Authorization": f"Bearer {CONFIG['BROKERS']['OANDA']['API_KEY']}"}
    url = f"https://api-fxpractice.oanda.com/v3/instruments/{symbol}/candles"
    params = {"granularity": granularity, "count": 1000, "price": "M"}
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json()["candles"]
    df = pd.DataFrame([
        {"time":c["time"], "open":c["mid"]["o"], "high":c["mid"]["h"],
         "low":c["mid"]["l"], "close":c["mid"]["c"], "volume":0}
        for c in data if c["complete"]
    ])
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    return df

# --- STOCKS (Yahoo Finance) ---
def fetch_stock(symbol, interval="15m"):
    df = yf.download(symbol, period="60d", interval=interval)
    df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
    return df

# --- Indicator Builder (dynamic from CONFIG) ---
def add_indicators(df):
    indicators = CONFIG["INDICATORS"]
    if "TREND" in indicators:
        if "EMA" in indicators["TREND"]:
            df["ema_14"] = ta.trend.EMAIndicator(df["close"], window=14).ema_indicator()
        if "SMA" in indicators["TREND"]:
            df["sma_14"] = ta.trend.SMAIndicator(df["close"], window=14).sma_indicator()
        if "MACD" in indicators["TREND"]:
            df["macd"] = ta.trend.MACD(df["close"]).macd()
        if "ADX" in indicators["TREND"]:
            df["adx"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"]).adx()

    if "VOLATILITY" in indicators:
        if "ATR" in indicators["VOLATILITY"]:
            df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"]).average_true_range()
        if "BOLLINGER" in indicators["VOLATILITY"]:
            df["bollinger"] = ta.volatility.BollingerBands(df["close"]).bollinger_hband_indicator()

    if "MOMENTUM" in indicators:
        if "RSI" in indicators["MOMENTUM"]:
            df["rsi"] = ta.momentum.RSIIndicator(df["close"]).rsi()
        if "STOCH" in indicators["MOMENTUM"]:
            df["stoch"] = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"]).stoch()

    return df

# --- Chronological split (no shuffle, no leakage) ---
def _chronological_split(X: pd.DataFrame, y: pd.Series, test_ratio: float = 0.2):
    n = len(X)
    split_idx = int(n * (1 - test_ratio))
    if split_idx < 1 or n - split_idx < 1:
        split_idx = max(1, n - 1)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    return X_train, X_test, y_train, y_test


# --- Training Function (chronological only, save feature list + version) ---
def train_random_forest(sector, symbol, df):
    # Ensure chronological order (no shuffle)
    if "open_time" in df.columns:
        df = df.sort_values("open_time").reset_index(drop=True)
    elif "time" in df.columns:
        df = df.sort_values("time").reset_index(drop=True)
    else:
        df = df.sort_index().reset_index(drop=True)

    df = add_indicators(df)
    
    # NEW: Add Multi-Timeframe Strategy Context
    df = add_mtf_features(df)
    
    # NEW: Add Order Flow Features (Phase 2)
    df = add_order_flow_features(df, cvd_mode='rolling', cvd_window=96, normalization_window=96)
    
    # 1. Compute Volatility
    volatility = compute_ewma_volatility(df, span=100)
    
    # 2. Sample Events using CUSUM filter
    # Assuming average daily vol of ~2% for crypto, h=0.02 * expected price roughly
    # For a robust normalized approach, we use a rolling multiple of standard deviation
    # Here we just use a simplified absolute threshold for the example based on average close
    avg_price = df['close'].mean()
    h_thresh = avg_price * 0.01 # 1% moves
    events = cusum_filter(df, h=h_thresh)
    
    # Ensure event alignment
    events = events[events.index.isin(df.index)]
    
    if len(events) < 200:
         print(f"[WARN] Not enough CUSUM events for {symbol} (got {len(events)}, need 200). Increase history or lower h_thresh.")
         return
         
    # 3. Apply Triple-Barrier Labeling (Long perspective for now as base signals)
    events_df = apply_triple_barrier(
         df=df,
         volatility=volatility,
         events=events,
         k_up=1.5,      # Take profit multiplier
         k_down=1.0,    # Stop loss multiplier (tighter)
         horizon_bars=24, # 24 hours max hold
         fees_bps=8.0,  # 8bps round-trip (4bps taker x2)
         spread_bps=1.0,
         slippage_bps=2.0,
         direction=1
    )
    
    # NOTE: We intentionally KEEP label=0 (time-barrier / no strong outcome) events.
    # Dropping them creates survivorship bias — the model only sees strong moves.
    # Instead, we use class_weight='balanced' in the classifier to handle imbalance.
    # This teaches the model to distinguish "strong momentum" from "noise".

    if len(events_df) < 150:
         print(f"[WARN] Not enough labeled events for {sector} model for {symbol} (got {len(events_df)}, need 150).")
         return
         
    print_class_distribution(events_df)

    # 4. Align features with the labeled events (at t_start)
    features = []
    for group in CONFIG["INDICATORS"].values():
        for ind in group:
            features.append(ind.lower())
            
    feature_map = {
        "ema": "ema_14", "sma": "sma_14", "macd": "macd", "adx": "adx",
        "atr": "atr", "bollinger": "bollinger", "rsi": "rsi", "stoch": "stoch"
    }
    extracted_features = [feature_map.get(f.lower(), f) for f in features if feature_map.get(f.lower(), f) in df.columns]
    
    # Explicitly register Phase 2 Order Flow features (including VPIN)
    flow_features = [
        'cvd_z', 'flow_imbalance_z', 'flow_aggression_z', 'cvd_slope_z',
        'imbalance_trend', 'imbalance_acceleration', 'price_cvd_correlation',
        'flow_divergence_dist', 'vpin', 'vpin_z',
    ]
    for ff in flow_features:
        if ff in df.columns:
            extracted_features.append(ff)
            
    # Explicitly register Phase 3 MTF features
    mtf_features = [
        '1H_htf_trend', '1H_htf_atr', '1H_htf_vwap',
        '4H_htf_trend', '4H_htf_atr', '4H_htf_vwap',
        'mtf_volatility_regime_ratio', 'mtf_macro_vwap_distance', 'mtf_trend_alignment'
    ]
    for mf in mtf_features:
        if mf in df.columns:
            extracted_features.append(mf)

    # Deduplicate while preserving insertion order — a feature could appear in
    # both feature_map and a flow/mtf list if names overlap.
    extracted_features = list(dict.fromkeys(extracted_features))

    # Merge features carefully at the exact event start time point
    # We use index matching since cusum returns indices
    X = df.loc[events_df['t_start'], extracted_features].copy()
    y = events_df['label'].values  # 1 or -1
    
    # Drop any NaN feature rows
    valid_idx = ~X.isna().any(axis=1)
    X = X[valid_idx]
    y = y[valid_idx]

    if len(X) < 50:
        print(f"[WARN] Not enough valid features to train {sector} model for {symbol}")
        return

    # Ensure `y` maintains the identical index structure as `X` for future boolean masking
    y_series = pd.Series(y, index=X.index)
    X_train, X_test, y_train, y_test = _chronological_split(X, y_series, test_ratio=0.2)
    if len(X_train) < 100:
        print(f"[WARN] Insufficient train samples after chronological split for {sector} {symbol} (got {len(X_train)}, need 100).")
        return

    # 4.5. Fit HMM strictly on Training Data
    # To prevent leakage, we pass the original df corresponding to the training timestamps
    train_end_time = X_train.index[-1]
    df_train = df.loc[:train_end_time]
    
    regime_filter = RegimeFilter(n_components=3, min_bars=5)
    try:
        regime_filter.fit(df_train)
    except Exception as e:
        print(f"[WARN] Failed to fit HMM for {symbol}: {e}")
        return
        
    # Predict over entire dataset (prediction handles scaling safely using standard fit from train)
    df_with_regimes = regime_filter.predict(df)
    
    # Map the generated regimes back onto the aligned feature sets
    # Prevent SettingWithCopy by using direct assignment on the underlying dataframe subset
    X_train = X_train.copy()
    X_test = X_test.copy()
    
    # Merge the 'regime' column directly via index
    X_train['regime'] = df_with_regimes['regime'].reindex(X_train.index)
    X_test['regime'] = df_with_regimes['regime'].reindex(X_test.index)
    
    # Create a perfectly aligned events_df for OOS purging
    aligned_events = events_df.set_index('t_start', drop=False)
    events_train = aligned_events.loc[X_train.index]

    # Pre-allocate unified OOS prediction vectors for the Meta-Model training set
    meta_oos_proba = pd.Series(np.nan, index=X_train.index)
    meta_oos_signal = pd.Series(np.nan, index=X_train.index)
    
    # Pre-allocate Test subset prediction vectors
    meta_test_proba = pd.Series(np.nan, index=X_test.index)
    meta_test_signal = pd.Series(np.nan, index=X_test.index)

    # 5. Build the Specialist Ensembles
    # Create the global fallback model — class_weight='balanced' handles label imbalance
    # without dropping any class, preserving the full distribution the model sees in live trading.
    global_model = RandomForestClassifier(
        n_estimators=200, max_depth=10, n_jobs=-1,
        class_weight='balanced', random_state=42,
    )
    _global_min_class = int(y_train.value_counts().min())
    _global_cv = max(2, min(5, _global_min_class))
    global_calibration_method = _calibration_method(y_train)
    calibrated_global = CalibratedClassifierCV(
        global_model, method=global_calibration_method, cv=_global_cv
    )
    calibrated_global.fit(X_train.drop(columns=['regime']), y_train)
    global_acc = calibrated_global.score(X_test.drop(columns=['regime']), y_test)
    calibration_metrics = _calibration_metrics(
        calibrated_global,
        X_test.drop(columns=["regime"]),
        y_test,
    )
    calibration_metrics["method"] = global_calibration_method

    # Generate OOS predictions for the global model as a potential fallback
    global_oos_proba, global_oos_signal = cross_val_predict_purged(
        global_model, X_train.drop(columns=['regime']), y_train,
        events_df=events_train, n_splits=_global_cv, embargo_pct=0.01
    )
    
    # Save artifacts
    base_name = f"RandomForest_{sector}_{symbol.replace('/', '')}"
    
    # Persist the HMM pipeline
    regime_filter.save(MODEL_DIR, base_name)
    
    # Save RF Global Model — two paths:
    # 1. {base}_GLOBAL.joblib  — primary (ModelLoader prefers this)
    # 2. {base}.joblib          — legacy fallback for backward compatibility
    dump(calibrated_global, os.path.join(MODEL_DIR, f"{base_name}_GLOBAL.joblib"))
    dump(calibrated_global, os.path.join(MODEL_DIR, f"{base_name}.joblib"))

    # ── XGBoost training ──────────────────────────────────────────────
    if _XGB_AVAILABLE:
        try:
            xgb_model = XGBClassifier(
                n_estimators=300,
                max_depth=6,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                eval_metric="logloss",
                n_jobs=-1,
                random_state=42,
            )
            # XGBoost requires non-negative integer labels; remap {-1,0,1} -> {0,1,2}
            _xgb_map = {-1: 0, 0: 1, 1: 2}
            y_train_xgb = y_train.map(_xgb_map)
            y_test_xgb  = y_test.map(_xgb_map)
            calibrated_xgb = CalibratedClassifierCV(
                xgb_model, method=_calibration_method(y_train), cv=_global_cv
            )
            calibrated_xgb.fit(X_train.drop(columns=["regime"]), y_train_xgb)
            xgb_acc = calibrated_xgb.score(X_test.drop(columns=["regime"]), y_test_xgb)
            # Restore original class labels so Predictor.py can look up P_long/P_short by class value
            calibrated_xgb.classes_ = np.array([-1, 0, 1])
            dump(calibrated_xgb, os.path.join(MODEL_DIR, f"{base_name}_XGB.joblib"))
            print(f"[INFO] XGBoost model saved for {symbol} (test_acc={xgb_acc:.4f})")
        except Exception as xgb_e:
            print(f"[WARN] XGBoost training failed for {symbol}: {xgb_e}")

    # ── LightGBM training ─────────────────────────────────────────────
    if _LGB_AVAILABLE:
        try:
            lgb_model = LGBMClassifier(
                n_estimators=400,
                max_depth=6,
                learning_rate=0.04,
                num_leaves=63,
                subsample=0.8,
                colsample_bytree=0.8,
                class_weight="balanced",
                n_jobs=-1,
                random_state=42,
                verbosity=-1,
            )
            calibrated_lgb = CalibratedClassifierCV(
                lgb_model, method=_calibration_method(y_train), cv=5
            )
            calibrated_lgb.fit(X_train.drop(columns=["regime"]), y_train)
            lgb_acc = calibrated_lgb.score(X_test.drop(columns=["regime"]), y_test)
            dump(calibrated_lgb, os.path.join(MODEL_DIR, f"{base_name}_LGB.joblib"))
            print(f"[INFO] LightGBM model saved for {symbol} (test_acc={lgb_acc:.4f})")
        except Exception as lgb_e:
            print(f"[WARN] LightGBM training failed for {symbol}: {lgb_e}")
    
    # ── LSTM sequential model ─────────────────────────────────────────
    lstm_cfg = CONFIG.get("LSTM_MODEL", {})
    if _LSTM_TRAIN_AVAILABLE and lstm_cfg.get("ENABLED", True):
        try:
            seq_len   = lstm_cfg.get("SEQUENCE_LEN", 60)
            # Build full-chronology feature matrix (no regime column)
            X_lstm = df.loc[events_df['t_start'], extracted_features].copy().dropna()
            y_lstm_raw = events_df.set_index('t_start').loc[X_lstm.index, 'label'].values
            if len(X_lstm) >= seq_len + 20:
                lstm_model = _AegisLSTM(
                    sequence_len=seq_len,
                    hidden=lstm_cfg.get("HIDDEN_UNITS", 128),
                    dropout=lstm_cfg.get("DROPOUT", 0.3),
                    epochs=lstm_cfg.get("EPOCHS", 50),
                    batch_size=lstm_cfg.get("BATCH_SIZE", 64),
                    feature_names=list(X_lstm.columns),
                )
                lstm_model.fit(X_lstm.values, y_lstm_raw)
                lstm_model.save(MODEL_DIR, base_name)
                print(f"[INFO] LSTM model saved for {symbol}")
            else:
                print(f"[WARN] Insufficient data for LSTM on {symbol} ({len(X_lstm)} < {seq_len + 20})")
        except Exception as lstm_e:
            print(f"[WARN] LSTM training failed for {symbol}: {lstm_e}")

    regimes = [RegimeFilter.LOW_VOL, RegimeFilter.BULL, RegimeFilter.BEAR]
    
    for state in regimes:
        # state_mask is now an aligned boolean Series with the exact same index length and order
        state_mask_train = (X_train['regime'] == state)
        state_mask_test = (X_test['regime'] == state)
        
        X_train_r = X_train.loc[state_mask_train].drop(columns=['regime'])
        y_train_r = y_train.loc[state_mask_train]
        X_test_r = X_test.loc[state_mask_test].drop(columns=['regime'])
        y_test_r = y_test.loc[state_mask_test]
        
        # Sufficiency Check
        if len(X_train_r) < 50:
            print(f"[WARN] Insufficient data for {state} model on {symbol}. Relying on GLOBAL fallback.")
            # Map fallback OOS directly into the overarching Meta vector
            meta_oos_proba.loc[state_mask_train] = global_oos_proba.loc[state_mask_train]
            meta_oos_signal.loc[state_mask_train] = global_oos_signal.loc[state_mask_train]
            
            if len(X_test_r) > 0:
                # Use the index of class +1 (long) — for 3-class {-1,0,1} models
                # this is NOT necessarily column 1; locate it explicitly.
                _global_long_idx = list(calibrated_global.classes_).index(1)
                meta_test_proba.loc[state_mask_test] = calibrated_global.predict_proba(X_test_r)[:, _global_long_idx]
                meta_test_signal.loc[state_mask_test] = calibrated_global.predict(X_test_r)
            continue
            
        specialist = RandomForestClassifier(
            n_estimators=200, max_depth=10, n_jobs=-1,
            class_weight='balanced', random_state=42,
        )
        # Reduce CV folds when any class has fewer than 5 samples to avoid CV error
        _spec_min_class = int(y_train_r.value_counts().min())
        _spec_cv = max(2, min(5, _spec_min_class))
        calibrated_specialist = CalibratedClassifierCV(
            specialist, method=_calibration_method(y_train_r), cv=_spec_cv
        )
        calibrated_specialist.fit(X_train_r, y_train_r)

        # OOS Predictions exclusively for this Regime Segment
        events_train_r = events_train.loc[X_train_r.index]
        oos_proba_r, oos_signal_r = cross_val_predict_purged(
            specialist, X_train_r, y_train_r,
            events_df=events_train_r, n_splits=_spec_cv, embargo_pct=0.01
        )
        meta_oos_proba.loc[state_mask_train] = oos_proba_r
        meta_oos_signal.loc[state_mask_train] = oos_signal_r
        
        if len(X_test_r) > 0:
            _spec_long_idx = list(calibrated_specialist.classes_).index(1)
            meta_test_proba.loc[state_mask_test] = calibrated_specialist.predict_proba(X_test_r)[:, _spec_long_idx]
            meta_test_signal.loc[state_mask_test] = calibrated_specialist.predict(X_test_r)
            
        acc = calibrated_specialist.score(X_test_r, y_test_r) if len(X_test_r) > 0 else 0.0
        dump(calibrated_specialist, os.path.join(MODEL_DIR, f"{base_name}_{state}.joblib"))
        print(f"[INFO] {sector} {state} model for {symbol} saved (test_acc={acc:.4f}, n={len(X_train_r)})")

    feature_list = list(X_train.drop(columns=['regime']).columns)
    
    # 6. Train the Meta-Labeling Layer
    meta_valid = ~meta_oos_proba.isna()
    if meta_valid.sum() < 20:
         print(f"[WARN] Insufficient OOS meta features generated for {symbol}. Skipping Meta-Model.")
         return
         
    meta_y_train = MetaLabeler.generate_meta_labels(y_train[meta_valid], meta_oos_signal[meta_valid])
    meta_X_train = MetaLabeler.build_meta_features(
        df=df.loc[meta_y_train.index], 
        primary_signal=meta_oos_signal[meta_valid], 
        primary_proba=meta_oos_proba[meta_valid], 
        events_df=events_train.loc[meta_y_train.index]
    )
    
    meta_model = RandomForestClassifier(n_estimators=200, max_depth=6, random_state=42, class_weight='balanced', n_jobs=-1)
    calibrated_meta = CalibratedClassifierCV(
        meta_model, method=_calibration_method(meta_y_train), cv=3
    )
    calibrated_meta.fit(meta_X_train, meta_y_train)
    
    # 7. Evaluate Meta-Model Improvement on Test Holdout
    meta_y_test = MetaLabeler.generate_meta_labels(y_test, meta_test_signal)
    meta_X_test = MetaLabeler.build_meta_features(
        df=df.loc[meta_y_test.index],
        primary_signal=meta_test_signal,
        primary_proba=meta_test_proba,
        events_df=aligned_events.loc[meta_y_test.index]
    )
    
    meta_test_class_preds = calibrated_meta.predict(meta_X_test)
    taken_trades_mask = (meta_test_class_preds == 1)
    
    total_trades = len(meta_y_test)
    num_taken = taken_trades_mask.sum()
    accept_rate = num_taken / total_trades if total_trades > 0 else 0
    
    raw_win_rate = meta_y_test.mean()
    filtered_win_rate = meta_y_test[taken_trades_mask].mean() if num_taken > 0 else 0
    
    print(f"\n=== META-LABELING VALIDATION ({symbol}) ===")
    print(f"Raw Primary Win Rate:       {raw_win_rate*100:.1f}%")
    print(f"Filtered Meta Win Rate:     {filtered_win_rate*100:.1f}%")
    print(f"Trade Acceptance Rate:      {accept_rate*100:.1f}% ({num_taken}/{total_trades})")
    print(f"===========================================\n")
    
    dump(calibrated_meta, os.path.join(MODEL_DIR, f"{base_name}_META.joblib"))
    
    # ── Walk-forward validation (3-window) ────────────────────────────
    print(f"\n=== WALK-FORWARD VALIDATION ({symbol}) ===")
    wf_accs = []
    X_full = X.copy()  # full aligned feature set
    y_full = pd.Series(y, index=X.index) if not isinstance(y, pd.Series) else y
    n_full = len(X_full)
    wf_splits = [
        (int(n_full * 0.60), int(n_full * 0.70)),
        (int(n_full * 0.70), int(n_full * 0.80)),
        (int(n_full * 0.80), int(n_full * 0.90)),
    ]
    for win_i, (tr_end, te_end) in enumerate(wf_splits):
        if tr_end < 50 or te_end <= tr_end:
            continue
        X_wf_tr = X_full.iloc[:tr_end].drop(columns=["regime"], errors="ignore")
        X_wf_te = X_full.iloc[tr_end:te_end].drop(columns=["regime"], errors="ignore")
        y_wf_tr = y_full.iloc[:tr_end]
        y_wf_te = y_full.iloc[tr_end:te_end]
        try:
            wf_rf = RandomForestClassifier(
                n_estimators=100, max_depth=8, class_weight="balanced",
                n_jobs=-1, random_state=42,
            )
            wf_cal = CalibratedClassifierCV(
                wf_rf, method=_calibration_method(y_wf_tr), cv=3
            )
            wf_cal.fit(X_wf_tr, y_wf_tr)
            wf_acc = wf_cal.score(X_wf_te, y_wf_te)
            wf_accs.append(wf_acc)
            print(f"  Window {win_i+1}: train[0:{tr_end}]  test[{tr_end}:{te_end}]  acc={wf_acc:.4f}")
        except Exception as wf_e:
            print(f"  Window {win_i+1}: FAILED — {wf_e}")

    if wf_accs:
        wf_mean = sum(wf_accs) / len(wf_accs)
        wf_std  = (sum((a - wf_mean)**2 for a in wf_accs) / len(wf_accs)) ** 0.5
        print(f"  Walk-forward mean acc: {wf_mean:.4f} ± {wf_std:.4f}")
        wf_consistent = wf_mean >= 0.50 and wf_std < 0.10
        print(f"  Verdict: {'[OK] CONSISTENT' if wf_consistent else '[WARN] INCONSISTENT (regime-sensitive)'}")
    else:
        wf_consistent = False
        wf_mean = wf_std = 0.0
    print("=" * 44)

    # ── Feature importance pruning log ────────────────────────────────
    pruning_report = []
    try:
        base_rf = global_model  # underlying unfitted RF
        # The calibrated model wraps the RF; get feature importances if available
        inner_estimators = getattr(calibrated_global, "calibrated_classifiers_", [])
        all_importances = []
        for cc in inner_estimators:
            inner = getattr(cc, "estimator", getattr(cc, "base_estimator", None))
            if inner and hasattr(inner, "feature_importances_"):
                all_importances.append(inner.feature_importances_)
        if all_importances:
            import numpy as _np
            avg_imp = _np.mean(all_importances, axis=0)
            for feat_name, imp in zip(feature_list, avg_imp):
                pruning_report.append({"feature": feat_name, "importance": float(imp)})
            pruning_report.sort(key=lambda x: -x["importance"])
            low_importance = [r for r in pruning_report if r["importance"] < 0.005]
            if low_importance:
                print(f"[INFO] {len(low_importance)} features have importance < 0.005 — candidates for pruning:")
                for r in low_importance[:5]:
                    print(f"       {r['feature']}: {r['importance']:.5f}")
    except Exception as pi_e:
        print(f"[WARN] Feature importance analysis failed: {pi_e}")

    import json
    meta = {
        "feature_list": feature_list,
        "version": datetime.now(timezone.utc).isoformat(),
        "global_test_accuracy": global_acc,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "regimes_supported": regimes,
        "walk_forward": {
            "window_accuracies": wf_accs,
            "mean_accuracy": float(wf_mean) if wf_accs else None,
            "std_accuracy":  float(wf_std)  if wf_accs else None,
            "consistent": wf_consistent,
        },
        "calibration": calibration_metrics,
        "feature_importances": pruning_report[:20] if pruning_report else [],
        "low_importance_features": [r["feature"] for r in pruning_report if r["importance"] < 0.005],
    }
    meta_path = os.path.join(MODEL_DIR, f"{base_name}_metadata.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"[INFO] {sector} pipeline for {symbol} fully saved (global_acc={global_acc:.4f})")

# --- Unified Loop ---
def retrain_all():
    for sector, symbols in CONFIG["SYMBOLS"].items():
        if CONFIG["MARKETS"][sector.upper()]:
            for symbol, timeframes in symbols.items():
                try:
                    if sector == "CRYPTO":
                        df = fetch_crypto(symbol, "15m")
                    elif sector == "FOREX":
                        df = fetch_forex(symbol, "M15")
                    elif sector == "STOCKS":
                        df = fetch_stock(symbol, "15m")
                    else:
                        continue
                    train_random_forest(sector, symbol, df)
                except Exception as e:
                    print(f"[ERROR] Failed {sector} {symbol}: {e}")

if __name__ == "__main__":
    retrain_all()
