import csv
import json
import os
import tempfile
import unittest

from AegisQuantConfig import CONFIG
from Services.ShadowEvaluator import ShadowEvaluator


class TestShadowEvaluator(unittest.TestCase):
    def setUp(self):
        self.original = dict(CONFIG["SHADOW_EVALUATION"])
        CONFIG["SHADOW_EVALUATION"].update(
            {
                "ENABLED": True,
                "SYMBOLS": ["BTC/USDT"],
                "MIN_CONFIDENCE": 0.55,
                "MAX_CONFIDENCE": 0.60,
                "THRESHOLD_STEP": 0.01,
                "HORIZON_BARS": 2,
                "ROUND_TRIP_FEE_BPS": 10.0,
                "ROUND_TRIP_SLIPPAGE_BPS": 5.0,
                "DEFAULT_SPREAD_BPS": 1.0,
                "MIN_DIRECTIONAL_MASS": 0.50,
                "MIN_RECOMMENDATION_SAMPLES": 1,
            }
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.evaluator = ShadowEvaluator(log_dir=self.temp_dir.name)

    def tearDown(self):
        CONFIG["SHADOW_EVALUATION"].clear()
        CONFIG["SHADOW_EVALUATION"].update(self.original)
        self.temp_dir.cleanup()

    @staticmethod
    def trace(trace_id, candle_ts, price, high, low, confidence=0.60):
        return {
            "trace_id": trace_id,
            "symbol": "BTC/USDT",
            "ai_signal": "BUY",
            "confidence": confidence,
            "directional_mass": 0.90,
            "price": price,
            "candle_high": high,
            "candle_low": low,
            "candle_ts": candle_ts,
            "market_regime": "TRENDING",
            "terminal_state": "HOLD",
            "blocking_reason": "Below live threshold",
        }

    def test_forward_outcome_includes_cost_mfe_and_mae(self):
        self.evaluator.process_cycle([self.trace("one", 1, 100.0, 101.0, 99.0)])
        self.evaluator.process_cycle([self.trace("two", 2, 101.0, 102.0, 98.0)])
        self.evaluator.process_cycle([self.trace("three", 3, 102.0, 104.0, 100.0)])

        with open(self.evaluator.outcome_file, newline="", encoding="utf-8") as handle:
            outcomes = list(csv.DictReader(handle))

        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertAlmostEqual(float(outcome["gross_return"]), 0.02)
        self.assertAlmostEqual(float(outcome["cost_bps"]), 16.0)
        self.assertAlmostEqual(float(outcome["net_return"]), 0.0184)
        self.assertAlmostEqual(float(outcome["mfe"]), 0.04)
        self.assertAlmostEqual(float(outcome["mae"]), -0.02)

    def test_summary_recommends_only_after_completed_positive_sample(self):
        self.evaluator.process_cycle([self.trace("one", 1, 100.0, 100.0, 100.0)])
        self.evaluator.process_cycle([self.trace("two", 2, 101.0, 101.0, 101.0)])
        self.evaluator.process_cycle([self.trace("three", 3, 102.0, 102.0, 102.0)])

        with open(self.evaluator.summary_file, encoding="utf-8") as handle:
            summary = json.load(handle)

        self.assertTrue(summary["recommendation_ready"])
        self.assertIsNotNone(summary["recommended_threshold"])
        row = next(item for item in summary["thresholds"] if item["threshold"] == 0.60)
        self.assertEqual(row["samples"], 1)
        self.assertGreater(row["expectancy"], 0)

    def test_ignores_unapproved_symbol_and_low_confidence(self):
        other = self.trace("other", 1, 100.0, 101.0, 99.0)
        other["symbol"] = "DOGE/USDT"
        low = self.trace("low", 1, 100.0, 101.0, 99.0, confidence=0.54)
        self.evaluator.process_cycle([other, low])

        self.assertEqual(self.evaluator.pending, [])
        self.assertFalse(os.path.exists(self.evaluator.signal_file))


class TestLiveUniverse(unittest.TestCase):
    def test_default_live_universe_contains_only_admitted_focus_symbols(self):
        self.assertEqual(
            CONFIG["PROJECT"]["LIVE_SYMBOLS"],
            ["BTC/USDT", "ETH/USDT", "XRP/USDT", "PEPE/USDT"],
        )


if __name__ == "__main__":
    unittest.main()
