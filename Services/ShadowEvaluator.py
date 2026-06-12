import csv
import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


class ShadowEvaluator:
    """Forward-evaluate model decisions without accessing execution services."""

    OUTCOME_FIELDS = [
        "opened_at", "closed_at", "trace_id", "symbol", "direction",
        "confidence", "directional_mass", "entry_price", "exit_price",
        "bars", "gross_return", "net_return", "mfe", "mae",
        "cost_bps", "market_regime", "blocking_reason",
    ]

    def __init__(self, log_dir: str | None = None) -> None:
        self.logger = AG_LOGGER
        self.cfg = CONFIG.get("SHADOW_EVALUATION", {})
        self.enabled = bool(self.cfg.get("ENABLED", True))
        self.log_dir = log_dir or CONFIG["REPORTING"]["LOG_DIR"]
        self.state_dir = os.path.join(self.log_dir, "state")
        self.signal_file = os.path.join(self.log_dir, "shadow_signals.jsonl")
        self.outcome_file = os.path.join(self.log_dir, "shadow_outcomes.csv")
        self.summary_file = os.path.join(self.log_dir, "shadow_summary.json")
        self.state_file = os.path.join(self.state_dir, "shadow_evaluator.json")
        self.symbols = {s.upper() for s in self.cfg.get("SYMBOLS", [])}
        self.horizon = max(1, int(self.cfg.get("HORIZON_BARS", 12)))
        self.min_mass = float(self.cfg.get("MIN_DIRECTIONAL_MASS", 0.50))
        self.min_samples = max(1, int(self.cfg.get("MIN_RECOMMENDATION_SAMPLES", 100)))
        self.thresholds = self._build_thresholds()
        self.pending: List[Dict[str, Any]] = []
        self.seen_trace_ids: List[str] = []
        if self.enabled:
            os.makedirs(self.state_dir, exist_ok=True)
            self._load_state()
            self._init_outcomes()

    def _build_thresholds(self) -> List[float]:
        start = float(self.cfg.get("MIN_CONFIDENCE", 0.55))
        end = float(self.cfg.get("MAX_CONFIDENCE", 0.74))
        step = max(0.001, float(self.cfg.get("THRESHOLD_STEP", 0.01)))
        count = int(math.floor((end - start) / step)) + 1
        values = [round(start + (i * step), 4) for i in range(max(1, count))]
        if not values or values[-1] < end - 1e-9:
            values.append(round(end, 4))
        return values

    def _load_state(self) -> None:
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                state = json.load(handle)
            self.pending = list(state.get("pending", []))
            self.seen_trace_ids = list(state.get("seen_trace_ids", []))[-5000:]
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self.pending = []
            self.seen_trace_ids = []

    def _atomic_json(self, path: str, payload: Any) -> None:
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(temp_path, path)

    def _save_state(self) -> None:
        self._atomic_json(
            self.state_file,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pending": self.pending,
                "seen_trace_ids": self.seen_trace_ids[-5000:],
            },
        )

    def _init_outcomes(self) -> None:
        if os.path.exists(self.outcome_file):
            return
        with open(self.outcome_file, "w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self.OUTCOME_FIELDS).writeheader()

    @staticmethod
    def _directional_return(direction: str, entry: float, price: float) -> float:
        if entry <= 0:
            return 0.0
        raw = (price - entry) / entry
        return raw if direction == "BUY" else -raw

    def _update_pending(self, prices: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        completed = []
        still_pending = []
        for item in self.pending:
            market = prices.get(item["symbol"])
            if not market or market["candle_ts"] <= item.get("last_candle_ts", 0):
                still_pending.append(item)
                continue
            item["last_candle_ts"] = market["candle_ts"]
            item["bars"] = int(item.get("bars", 0)) + 1
            entry = float(item["entry_price"])
            direction = item["direction"]
            favorable = market["high"] if direction == "BUY" else market["low"]
            adverse = market["low"] if direction == "BUY" else market["high"]
            item["mfe"] = max(
                float(item.get("mfe", 0.0)),
                self._directional_return(direction, entry, favorable),
            )
            item["mae"] = min(
                float(item.get("mae", 0.0)),
                self._directional_return(direction, entry, adverse),
            )
            if item["bars"] >= self.horizon:
                completed.append(self._complete(item, market["close"]))
            else:
                still_pending.append(item)
        self.pending = still_pending
        return completed

    def _complete(self, item: Dict[str, Any], exit_price: float) -> Dict[str, Any]:
        gross = self._directional_return(
            item["direction"], float(item["entry_price"]), exit_price
        )
        spread_bps = float(item.get("spread_bps", self.cfg.get("DEFAULT_SPREAD_BPS", 2.0)))
        cost_bps = (
            float(self.cfg.get("ROUND_TRIP_FEE_BPS", 20.0))
            + float(self.cfg.get("ROUND_TRIP_SLIPPAGE_BPS", 10.0))
            + spread_bps
        )
        return {
            **{key: item.get(key, "") for key in self.OUTCOME_FIELDS},
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "exit_price": exit_price,
            "gross_return": gross,
            "net_return": gross - (cost_bps / 10000.0),
            "cost_bps": cost_bps,
        }

    def _write_outcomes(self, outcomes: Iterable[Dict[str, Any]]) -> None:
        with open(self.outcome_file, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.OUTCOME_FIELDS)
            for outcome in outcomes:
                writer.writerow({key: outcome.get(key, "") for key in self.OUTCOME_FIELDS})

    def _record_signal(self, trace: Dict[str, Any]) -> None:
        record = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace["trace_id"],
            "symbol": trace["symbol"],
            "direction": trace["ai_signal"],
            "confidence": trace["confidence"],
            "directional_mass": trace.get("directional_mass", 0.0),
            "price": trace["price"],
            "market_regime": trace.get("market_regime", "UNKNOWN"),
            "terminal_state": trace.get("terminal_state", ""),
            "blocking_reason": trace.get("blocking_reason", ""),
        }
        with open(self.signal_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _open_observation(self, trace: Dict[str, Any]) -> None:
        spread_pct = max(0.0, float(trace.get("entry_spread_pct", 0.0)))
        self.pending.append(
            {
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "closed_at": "",
                "trace_id": trace["trace_id"],
                "symbol": trace["symbol"],
                "direction": trace["ai_signal"],
                "confidence": float(trace["confidence"]),
                "directional_mass": float(trace.get("directional_mass", 0.0)),
                "entry_price": float(trace["price"]),
                "exit_price": 0.0,
                "bars": 0,
                "gross_return": 0.0,
                "net_return": 0.0,
                "mfe": 0.0,
                "mae": 0.0,
                "cost_bps": 0.0,
                "spread_bps": (
                    spread_pct * 10000.0
                    if spread_pct > 0
                    else float(self.cfg.get("DEFAULT_SPREAD_BPS", 2.0))
                ),
                "market_regime": trace.get("market_regime", "UNKNOWN"),
                "blocking_reason": trace.get("blocking_reason", ""),
                "last_candle_ts": float(trace.get("candle_ts", 0.0)),
            }
        )

    def _read_outcomes(self) -> List[Dict[str, Any]]:
        try:
            with open(self.outcome_file, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                for key in ("confidence", "net_return", "gross_return", "mfe", "mae"):
                    row[key] = float(row[key])
            return rows
        except (FileNotFoundError, ValueError):
            return []

    @staticmethod
    def _metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        returns = [float(row["net_return"]) for row in rows]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "samples": len(rows),
            "win_rate": (len(wins) / len(rows)) if rows else 0.0,
            "expectancy": (sum(returns) / len(rows)) if rows else 0.0,
            "profit_factor": (
                gross_profit / gross_loss if gross_loss > 0 else None
            ),
            "avg_mfe": (
                sum(float(row["mfe"]) for row in rows) / len(rows) if rows else 0.0
            ),
            "avg_mae": (
                sum(float(row["mae"]) for row in rows) / len(rows) if rows else 0.0
            ),
            "brier_score": (
                sum((float(row["confidence"]) - (1.0 if float(row["net_return"]) > 0 else 0.0)) ** 2 for row in rows)
                / len(rows)
                if rows else None
            ),
        }

    def _write_summary(self) -> None:
        outcomes = self._read_outcomes()
        threshold_rows = []
        for threshold in self.thresholds:
            selected = [row for row in outcomes if float(row["confidence"]) >= threshold]
            metrics = self._metrics(selected)
            threshold_rows.append({"threshold": threshold, **metrics})
        eligible = [
            row for row in threshold_rows
            if row["samples"] >= self.min_samples
            and row["expectancy"] > 0
            and (
                row["profit_factor"] is None
                or row["profit_factor"] > 1.10
            )
        ]
        recommended = max(
            eligible,
            key=lambda row: (row["expectancy"], row["profit_factor"]),
            default=None,
        )
        self._atomic_json(
            self.summary_file,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "horizon_bars": self.horizon,
                "pending_samples": len(self.pending),
                "completed_samples": len(outcomes),
                "minimum_recommendation_samples": self.min_samples,
                "recommended_threshold": (
                    recommended["threshold"] if recommended else None
                ),
                "recommendation_ready": recommended is not None,
                "thresholds": threshold_rows,
            },
        )

    def process_cycle(self, traces: List[Dict[str, Any]]) -> None:
        if not self.enabled:
            return
        prices = {
            trace["symbol"]: {
                "close": float(trace.get("price", 0.0)),
                "high": float(trace.get("candle_high", trace.get("price", 0.0))),
                "low": float(trace.get("candle_low", trace.get("price", 0.0))),
                "candle_ts": float(trace.get("candle_ts", 0.0)),
            }
            for trace in traces
            if trace.get("symbol") and float(trace.get("price", 0.0)) > 0
        }
        outcomes = self._update_pending(prices)
        if outcomes:
            self._write_outcomes(outcomes)

        seen = set(self.seen_trace_ids)
        min_confidence = self.thresholds[0]
        for trace in traces:
            trace_id = str(trace.get("trace_id", ""))
            symbol = str(trace.get("symbol", "")).upper()
            direction = trace.get("ai_signal")
            confidence = float(trace.get("confidence", 0.0))
            mass = float(trace.get("directional_mass", 0.0))
            if (
                not trace_id
                or trace_id in seen
                or symbol not in self.symbols
                or direction not in ("BUY", "SELL")
                or float(trace.get("price", 0.0)) <= 0
                or confidence < min_confidence
                or mass < self.min_mass
            ):
                continue
            self._record_signal(trace)
            self._open_observation(trace)
            seen.add(trace_id)
            self.seen_trace_ids.append(trace_id)

        self._save_state()
        self._write_summary()
