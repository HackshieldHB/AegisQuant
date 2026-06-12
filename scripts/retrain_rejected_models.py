import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from AegisQuantConfig import CONFIG
from Data.Trainer import AegisQuantTrainer as trainer


def _base(sector: str, symbol: str) -> str:
    return f"RandomForest_{sector}_{symbol.replace('/', '')}"


def _metadata(model_dir: str, sector: str, symbol: str) -> dict:
    path = os.path.join(model_dir, f"{_base(sector, symbol)}_metadata.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _admitted(metadata: dict) -> bool:
    gate = CONFIG["MODEL_ADMISSION"]
    return (
        float(metadata.get("global_test_accuracy", 0.0) or 0.0)
        >= float(gate["MIN_TEST_ACCURACY"])
        and float(metadata.get("walk_forward", {}).get("mean_accuracy", 0.0) or 0.0)
        >= float(gate["MIN_WALK_FORWARD_ACCURACY"])
        and int(metadata.get("n_train", 0) or 0) >= int(gate["MIN_TRAIN_SAMPLES"])
    )


def _promote(challenger_dir: str, production_dir: str, sector: str, symbol: str) -> None:
    prefix = _base(sector, symbol)
    candidates = [
        name for name in os.listdir(challenger_dir)
        if name.startswith(prefix)
    ]
    if not candidates:
        raise RuntimeError(f"No challenger artifacts produced for {sector}/{symbol}")
    backup_dir = os.path.join(
        production_dir,
        "Backups",
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        prefix,
    )
    os.makedirs(backup_dir, exist_ok=True)
    for name in candidates:
        destination = os.path.join(production_dir, name)
        if os.path.exists(destination):
            shutil.copy2(destination, os.path.join(backup_dir, name))
    for name in candidates:
        source = os.path.join(challenger_dir, name)
        temp_destination = os.path.join(production_dir, f".{name}.promoting")
        shutil.copy2(source, temp_destination)
        os.replace(temp_destination, os.path.join(production_dir, name))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retrain rejected models as challengers and promote admitted artifacts only."
    )
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    production_dir = CONFIG["AI"]["MODEL_PATH"]
    challenger_dir = os.path.join(production_dir, "Challengers")
    os.makedirs(challenger_dir, exist_ok=True)
    configured = list(CONFIG["SYMBOLS"]["CRYPTO"])
    requested = [symbol.upper() for symbol in (args.symbols or configured)]
    unknown = sorted(set(requested) - set(configured))
    if unknown:
        raise ValueError(f"Unknown configured symbols: {unknown}")

    rejected = [
        symbol for symbol in requested
        if not _admitted(_metadata(production_dir, "CRYPTO", symbol))
    ]
    if not rejected:
        print("No rejected models require retraining.")
        return 0
    print(f"Rejected models: {', '.join(rejected)}")
    if args.dry_run:
        return 0

    trainer.MODEL_DIR = challenger_dir
    promoted = []
    retained = []
    for symbol in rejected:
        print(f"[CHALLENGER] Training {symbol}")
        frame = trainer.fetch_crypto(symbol, "15m", months=args.months)
        trainer.train_random_forest("CRYPTO", symbol, frame)
        metadata = _metadata(challenger_dir, "CRYPTO", symbol)
        if _admitted(metadata):
            _promote(challenger_dir, production_dir, "CRYPTO", symbol)
            promoted.append(symbol)
            print(f"[PROMOTED] {symbol} passed model admission.")
        else:
            retained.append(symbol)
            print(f"[REJECTED] {symbol} challenger remains isolated.")

    print(f"Promoted: {promoted or 'none'}")
    print(f"Still rejected: {retained or 'none'}")
    return 0 if not retained else 2


if __name__ == "__main__":
    raise SystemExit(main())
