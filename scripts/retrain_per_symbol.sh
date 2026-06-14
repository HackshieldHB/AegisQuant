#!/bin/bash
# Retrain each symbol in its OWN process so RSS is released between symbols
# (single long-lived process accumulates memory and gets LVE-killed mid-run).
cd /home/aegisqua/aegisquant_app || exit 1
export PYTHONPATH=/home/aegisqua/aegisquant_app
export TRAIN_N_JOBS=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TRAIN_MONTHS=12
PY=/home/aegisqua/aegisquant_venv/bin/python
for s in BTC ETH SOL XRP DOGE SHIB PEPE; do
  echo "=== TRAIN ${s}/USDT @ $(date +%H:%M:%S) ==="
  "$PY" -u -c "from Data.Trainer.AegisQuantTrainer import train_random_forest, fetch_crypto; train_random_forest('CRYPTO','${s}/USDT', fetch_crypto('${s}/USDT','15m'))" 2>&1 | grep -viE "numba|warning:|UserWarning|warnings.warn"
  echo "=== ${s} exit=$? ==="
done
echo "ALL_SYMBOLS_DONE"
