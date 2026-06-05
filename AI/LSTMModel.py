"""
LSTMModel — Sequential pattern recognition for AegisQuant.
------------------------------------------------------------
Complements the RF/XGB/LGB ensemble with temporal context.
Random Forest treats each bar independently; LSTM captures
multi-bar sequences (head-and-shoulders, flag, engulfing chains).

Architecture:
  Input  → LSTM(128) → Dropout(0.3) → LSTM(64) → Dropout(0.2)
         → Dense(32, relu) → Dense(1, sigmoid) → P(long)

Label convention (same as TripleBarrier):
  1  = long wins (hit TP)
 -1  = short wins (hit SL)
  0  = time-barrier (kept — trained with class_weight)

Usage:
  model = AegisLSTM(sequence_len=60)
  model.fit(X_sequences, y_binary)
  p_long, p_short = model.predict_proba_directional(X_live_seq)
"""

import os
import json
import logging
import numpy as np
from typing import Optional, Tuple, List

logger = logging.getLogger("AegisQuant")

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import DataLoader, TensorDataset
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    logger.warning("[LSTMModel] PyTorch not installed. LSTM disabled. pip install torch")


# ─────────────────────────────────────────────────────────────────────────────
# PyTorch LSTM network
# ─────────────────────────────────────────────────────────────────────────────

class _LSTMNet(nn.Module if _TORCH_AVAILABLE else object):
    def __init__(self, input_size: int, hidden: int = 128, dropout: float = 0.3):
        if not _TORCH_AVAILABLE:
            raise ImportError("PyTorch required")
        super().__init__()
        self.lstm1 = nn.LSTM(input_size, hidden, batch_first=True)
        self.drop1 = nn.Dropout(dropout)
        self.lstm2 = nn.LSTM(hidden, hidden // 2, batch_first=True)
        self.drop2 = nn.Dropout(max(dropout - 0.1, 0.1))
        self.fc1   = nn.Linear(hidden // 2, 32)
        self.relu  = nn.ReLU()
        self.fc2   = nn.Linear(32, 1)
        self.sig   = nn.Sigmoid()

    def forward(self, x):
        out, _ = self.lstm1(x)
        out = self.drop1(out)
        out, _ = self.lstm2(out)
        out = self.drop2(out[:, -1, :])   # take last time-step
        out = self.relu(self.fc1(out))
        return self.sig(self.fc2(out)).squeeze(-1)


# ─────────────────────────────────────────────────────────────────────────────
# Public wrapper with sklearn-compatible interface
# ─────────────────────────────────────────────────────────────────────────────

class AegisLSTM:
    """
    Sklearn-compatible wrapper. predict_proba() returns shape (N, 2)
    where [:, 0] = P(short/-1) and [:, 1] = P(long/1).
    """

    def __init__(
        self,
        sequence_len: int = 60,
        hidden: int = 128,
        dropout: float = 0.3,
        epochs: int = 50,
        batch_size: int = 64,
        lr: float = 1e-3,
        feature_names: Optional[List[str]] = None,
    ):
        self.sequence_len = sequence_len
        self.hidden = hidden
        self.dropout = dropout
        self.epochs = epochs
        self.batch_size = batch_size
        self.lr = lr
        self.feature_names_in_ = np.array(feature_names) if feature_names else None
        self.classes_ = np.array([-1, 1])
        self._net: Optional[object] = None
        self._input_size: int = 0
        self._device = "cpu"
        self._fitted = False
        self._scaler_mean: Optional[np.ndarray] = None
        self._scaler_std:  Optional[np.ndarray] = None

    # ── Feature normalisation (z-score, fit only on train) ───────────────────

    def _fit_scaler(self, X: np.ndarray) -> None:
        self._scaler_mean = X.mean(axis=(0, 1))
        self._scaler_std  = X.std(axis=(0, 1)) + 1e-8

    def _scale(self, X: np.ndarray) -> np.ndarray:
        if self._scaler_mean is None:
            return X
        return (X - self._scaler_mean) / self._scaler_std

    # ── Build sequences from a 2-D feature matrix ────────────────────────────

    def _make_sequences(self, X_2d: np.ndarray) -> np.ndarray:
        """Slide a window of length sequence_len over X_2d → (N, seq, feats)."""
        n = len(X_2d)
        if n < self.sequence_len:
            raise ValueError(
                f"LSTMModel: need ≥{self.sequence_len} rows, got {n}."
            )
        seqs = np.stack(
            [X_2d[i : i + self.sequence_len] for i in range(n - self.sequence_len + 1)]
        )
        return seqs  # (N-seq+1, seq, feats)

    # ── Training ─────────────────────────────────────────────────────────────

    def fit(self, X_2d: np.ndarray, y: np.ndarray) -> "AegisLSTM":
        if not _TORCH_AVAILABLE:
            logger.warning("[LSTMModel] PyTorch missing — skipping fit.")
            return self

        seqs = self._make_sequences(X_2d)
        # Align labels to sequence end indices
        y_aligned = y[self.sequence_len - 1 :]
        # Convert {-1, 0, 1} labels to binary (0 = short/neutral, 1 = long)
        y_bin = (y_aligned == 1).astype(np.float32)

        self._fit_scaler(seqs)
        seqs_scaled = self._scale(seqs).astype(np.float32)

        self._input_size = seqs_scaled.shape[2]
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._net = _LSTMNet(self._input_size, self.hidden, self.dropout).to(self._device)

        # Class weights to handle imbalance
        n_pos = float(y_bin.sum())
        n_neg = float(len(y_bin) - n_pos)
        pos_weight = torch.tensor([n_neg / max(n_pos, 1)], device=self._device)

        criterion = nn.BCELoss(weight=None)
        bce_wt    = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
        optimizer = optim.Adam(self._net.parameters(), lr=self.lr, weight_decay=1e-5)
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=self.epochs)

        dataset = TensorDataset(
            torch.tensor(seqs_scaled),
            torch.tensor(y_bin),
        )
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True, drop_last=False)

        self._net.train()
        best_loss = float("inf")
        patience_count = 0
        patience = 10

        for epoch in range(self.epochs):
            epoch_loss = 0.0
            for xb, yb in loader:
                xb, yb = xb.to(self._device), yb.to(self._device)
                optimizer.zero_grad()
                preds = self._net(xb)
                loss  = criterion(preds, yb)
                loss.backward()
                nn.utils.clip_grad_norm_(self._net.parameters(), 1.0)
                optimizer.step()
                epoch_loss += loss.item()
            scheduler.step()
            avg_loss = epoch_loss / max(len(loader), 1)
            if avg_loss < best_loss - 1e-4:
                best_loss = avg_loss
                patience_count = 0
            else:
                patience_count += 1
                if patience_count >= patience:
                    logger.info("[LSTMModel] Early stopping at epoch %d (loss=%.4f)", epoch, avg_loss)
                    break

        self._fitted = True
        self._net.eval()
        logger.info("[LSTMModel] Training complete — %d sequences, best_loss=%.4f", len(seqs), best_loss)
        return self

    # ── Inference ────────────────────────────────────────────────────────────

    def predict_proba(self, X_2d: np.ndarray) -> np.ndarray:
        """Return shape (N, 2): col0=P(short), col1=P(long)."""
        if not _TORCH_AVAILABLE or not self._fitted or self._net is None:
            n = max(len(X_2d) - self.sequence_len + 1, 1)
            return np.full((n, 2), 0.5)

        seqs = self._make_sequences(X_2d)
        seqs_scaled = self._scale(seqs).astype(np.float32)
        self._net.eval()
        with torch.no_grad():
            x_t = torch.tensor(seqs_scaled).to(self._device)
            p_long = self._net(x_t).cpu().numpy()
        p_long  = np.clip(p_long, 1e-6, 1 - 1e-6)
        p_short = 1.0 - p_long
        return np.column_stack([p_short, p_long])

    def predict_proba_directional(self, X_2d: np.ndarray) -> Tuple[float, float]:
        """Return (P_long, P_short) for the most recent bar."""
        probs = self.predict_proba(X_2d)
        row   = probs[-1]
        return float(row[1]), float(row[0])

    def score(self, X_2d: np.ndarray, y: np.ndarray) -> float:
        probs    = self.predict_proba(X_2d)
        y_bin    = (y[self.sequence_len - 1 :] == 1).astype(int)
        preds    = (probs[:, 1] >= 0.5).astype(int)
        n        = min(len(preds), len(y_bin))
        return float((preds[:n] == y_bin[:n]).mean()) if n > 0 else 0.5

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, model_dir: str, base_name: str) -> None:
        if not _TORCH_AVAILABLE or not self._fitted or self._net is None:
            return
        path = os.path.join(model_dir, f"{base_name}_LSTM.pt")
        torch.save(
            {
                "state_dict":    self._net.state_dict(),
                "input_size":    self._input_size,
                "hidden":        self.hidden,
                "dropout":       self.dropout,
                "sequence_len":  self.sequence_len,
                "scaler_mean":   self._scaler_mean.tolist() if self._scaler_mean is not None else None,
                "scaler_std":    self._scaler_std.tolist()  if self._scaler_std  is not None else None,
                "feature_names": self.feature_names_in_.tolist() if self.feature_names_in_ is not None else [],
            },
            path,
        )
        logger.info("[LSTMModel] Saved → %s", path)

    @classmethod
    def load(cls, model_dir: str, base_name: str) -> Optional["AegisLSTM"]:
        if not _TORCH_AVAILABLE:
            return None
        path = os.path.join(model_dir, f"{base_name}_LSTM.pt")
        if not os.path.exists(path):
            return None
        try:
            ckpt = torch.load(path, map_location="cpu")
            obj = cls(
                sequence_len=ckpt["sequence_len"],
                hidden=ckpt["hidden"],
                dropout=ckpt["dropout"],
                feature_names=ckpt.get("feature_names") or None,
            )
            obj._input_size = ckpt["input_size"]
            obj._net = _LSTMNet(obj._input_size, obj.hidden, obj.dropout)
            obj._net.load_state_dict(ckpt["state_dict"])
            obj._net.eval()
            if ckpt.get("scaler_mean"):
                obj._scaler_mean = np.array(ckpt["scaler_mean"])
                obj._scaler_std  = np.array(ckpt["scaler_std"])
            obj._fitted = True
            logger.info("[LSTMModel] Loaded ← %s", path)
            return obj
        except Exception as e:
            logger.error("[LSTMModel] Load failed %s: %s", path, e)
            return None
