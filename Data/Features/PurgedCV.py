import pandas as pd
import numpy as np
from sklearn.model_selection import KFold
from sklearn.base import clone

class PurgedKFold:
    """
    Purged Time-Series Cross Validation.
    Prevents forward leakage by:
    1. Purging: Dropping any training samples whose path (t_start to t_end) overlaps the validation set.
    2. Embargo: Dropping a chunk of training samples immediately following the validation set to eliminate chronological correlation.
    """
    def __init__(self, n_splits=5, embargo_pct=0.01):
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct
        
    def split(self, X, y=None, events_df=None):
        """
        Yields (train_indices, test_indices)
        X: DataFrame of features.
        events_df: DataFrame with aligned length to X, containing 't_start' and 't_end'.
        """
        if events_df is None:
            raise ValueError("events_df containing t_start and t_end must be provided")
            
        n_samples = len(X)
        if hasattr(events_df, "reset_index"):
            t_starts = pd.Series(events_df['t_start'].values)
            t_ends = pd.Series(events_df['t_end'].values)
        else:
            t_starts = pd.Series(events_df['t_start'])
            t_ends = pd.Series(events_df['t_end'])
            
        kf = KFold(n_splits=self.n_splits, shuffle=False)
        embargo_length = int(max(1, n_samples * self.embargo_pct))
        
        for train_idx, test_idx in kf.split(X):
            # Define validation boundary
            test_start_time = t_starts.iloc[test_idx[0]]
            max_test_end = t_ends.iloc[test_idx].max()
            
            # Rule 1: Purging.
            # Mask out any training data that overlaps into the validation timeframe
            train_t_starts = t_starts.iloc[train_idx]
            train_t_ends = t_ends.iloc[train_idx]
            
            overlap_mask = (train_t_starts <= max_test_end) & (train_t_ends >= test_start_time)
            
            # Rule 2: Embargo.
            # Mask out training data immediately following the validation set bounds
            # Since indices are chronological, we blanket-drop 'embargo_length' samples after test
            # Or exactly drop samples where t_start is within embargo interval of max_test_end.
            # We'll use index-based embargo assuming sorted chronological data.
            embargo_end_idx = test_idx[-1] + embargo_length
            embargo_mask = (train_idx > test_idx[-1]) & (train_idx <= embargo_end_idx)
            
            # Filter the train set
            keep_mask = ~(overlap_mask.values | embargo_mask)
            
            yield train_idx[keep_mask], test_idx

def cross_val_predict_purged(estimator, X, y, events_df, n_splits=5, embargo_pct=0.01):
    """
    Generates Out-Of-Sample (OOS) predictions using Purged K-Fold CV.
    Critical for generating training labels for the Meta-Model without leakage.
    
    Returns:
        pd.Series of probabilities
        pd.Series of class predictions
    """
    pkf = PurgedKFold(n_splits=n_splits, embargo_pct=embargo_pct)
    
    preds_proba = np.full(len(X), np.nan)
    preds_class = np.full(len(X), np.nan)
    
    X_arr = X.values if isinstance(X, pd.DataFrame) else X
    y_arr = y.values if isinstance(y, pd.Series) else y
    
    for train_idx, test_idx in pkf.split(X, events_df=events_df):
        model = clone(estimator)
        
        X_train, y_train = X_arr[train_idx], y_arr[train_idx]
        X_test = X_arr[test_idx]
        
        # Failsafe for insufficient purged data
        if len(X_train) < 5 or len(np.unique(y_train)) < 2:
            continue
            
        model.fit(X_train, y_train)
        
        if hasattr(model, "predict_proba"):
            probs = model.predict_proba(X_test)
            classes = model.classes_
            if 1 in classes:
                class_1_idx = np.where(classes == 1)[0][0]
                preds_proba[test_idx] = probs[:, class_1_idx]
            else:
                preds_proba[test_idx] = 0.0 # Extreme edge case: model never predicts a +1 TP hit
        else:
             # Failsafe mapping of predict if no proba exists
             preds_proba[test_idx] = (model.predict(X_test) == 1).astype(float)
             
        preds_class[test_idx] = model.predict(X_test)
        
    return pd.Series(preds_proba, index=X.index), pd.Series(preds_class, index=X.index)
