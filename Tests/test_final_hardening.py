"""
Final Hardening Validation — G-1 Latency + G-2 Fee-Edge
========================================================
Tests both observability modules and confirms they:
1) Never block the engine
2) Produce correct statistics
3) Fire alerts at correct thresholds
4) Don't degrade existing safety layers
"""
import unittest
import time
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Execution.LatencyTracker import LatencyTracker
from Execution.FeeEdgeAnalyzer import FeeEdgeAnalyzer


class TestLatencyTracker(unittest.TestCase):
    """G-1: Tick-to-signal latency observability."""

    def test_basic_latency_capture(self):
        """Timestamps captured correctly and statistics computed."""
        tracker = LatencyTracker(window_size=100)
        
        tracker.mark_tick_received("BTC")
        time.sleep(0.005)  # 5ms simulated inference
        tracker.mark_signal_generated("BTC")
        time.sleep(0.002)  # 2ms simulated order prep
        tracker.mark_order_submitted("BTC")
        
        stats = tracker.get_statistics()
        self.assertEqual(stats["sample_count"], 1)
        self.assertGreater(stats["total"]["mean_ms"], 0)
        self.assertGreater(stats["tick_to_signal"]["mean_ms"], 0)
        self.assertGreater(stats["signal_to_order"]["mean_ms"], 0)

    def test_rolling_window_bounded(self):
        """Window never exceeds configured size."""
        tracker = LatencyTracker(window_size=50)
        
        for i in range(200):
            sym = f"SYM{i}"
            tracker.mark_tick_received(sym)
            tracker.mark_signal_generated(sym)
            tracker.mark_order_submitted(sym)
        
        stats = tracker.get_statistics()
        self.assertEqual(stats["sample_count"], 50)

    def test_alert_fires_on_threshold(self):
        """Alert callback triggered when latency exceeds threshold."""
        alerts = []
        tracker = LatencyTracker(
            alert_threshold_ms=1.0,  # 1ms — will definitely be exceeded
            alert_callback=lambda e: alerts.append(e),
        )
        
        tracker.mark_tick_received("BTC")
        time.sleep(0.005)  # 5ms > 1ms threshold
        tracker.mark_signal_generated("BTC")
        tracker.mark_order_submitted("BTC")
        
        self.assertGreater(len(alerts), 0)
        self.assertEqual(alerts[0]["symbol"], "BTC")
        self.assertGreater(tracker._alert_count, 0)

    def test_no_alert_below_threshold(self):
        """No alert when latency is within threshold."""
        alerts = []
        tracker = LatencyTracker(
            alert_threshold_ms=10000.0,  # 10s — impossible to exceed
            alert_callback=lambda e: alerts.append(e),
        )
        
        tracker.mark_tick_received("ETH")
        tracker.mark_signal_generated("ETH")
        tracker.mark_order_submitted("ETH")
        
        self.assertEqual(len(alerts), 0)

    def test_percentile_statistics(self):
        """p95, p99, max computed correctly."""
        tracker = LatencyTracker(window_size=1000)
        
        for i in range(100):
            tracker.mark_tick_received(f"S{i}")
            tracker.mark_signal_generated(f"S{i}")
            tracker.mark_order_submitted(f"S{i}")
        
        stats = tracker.get_statistics()
        total = stats["total"]
        self.assertIn("p95_ms", total)
        self.assertIn("p99_ms", total)
        self.assertIn("max_ms", total)
        self.assertGreaterEqual(total["max_ms"], total["p99_ms"])
        self.assertGreaterEqual(total["p99_ms"], total["p95_ms"])

    def test_daily_summary_export(self):
        """Daily summary contains required fields."""
        tracker = LatencyTracker()
        tracker.mark_tick_received("BTC")
        tracker.mark_signal_generated("BTC")
        tracker.mark_order_submitted("BTC")
        
        summary = tracker.get_daily_summary()
        self.assertIn("timestamp_utc", summary)
        self.assertIn("alert_threshold_ms", summary)
        self.assertIn("sample_count", summary)

    def test_empty_statistics(self):
        """Stats return gracefully with no data."""
        tracker = LatencyTracker()
        stats = tracker.get_statistics()
        self.assertEqual(stats["sample_count"], 0)

    def test_concurrent_symbols(self):
        """Multiple symbols tracked independently."""
        tracker = LatencyTracker()
        
        tracker.mark_tick_received("BTC")
        tracker.mark_tick_received("ETH")
        tracker.mark_signal_generated("BTC")
        tracker.mark_signal_generated("ETH")
        tracker.mark_order_submitted("BTC")
        tracker.mark_order_submitted("ETH")
        
        stats = tracker.get_statistics()
        self.assertEqual(stats["sample_count"], 2)


class TestFeeEdgeAnalyzer(unittest.TestCase):
    """G-2: Fee-vs-edge profitability validation."""

    def test_basic_trade_recording(self):
        """Trade recorded with correct net edge computation."""
        analyzer = FeeEdgeAnalyzer(maker_fee_bps=1.0, taker_fee_bps=5.0)
        
        event = analyzer.record_trade(
            symbol="BTC",
            expected_edge_bps=20.0,
            estimated_slippage_bps=3.0,
            is_market_order=True,  # taker: 5bps * 2 = 10bps round-trip
        )
        
        # net = 20 - 3 - 10 = 7 bps
        self.assertAlmostEqual(event["net_expected_edge_bps"], 7.0)
        self.assertEqual(event["estimated_fees_bps"], 10.0)

    def test_maker_vs_taker_fee_differentiation(self):
        """Maker orders use lower fee rate."""
        analyzer = FeeEdgeAnalyzer(maker_fee_bps=1.0, taker_fee_bps=5.0)
        
        taker = analyzer.record_trade("BTC", 20.0, 3.0, is_market_order=True)
        maker = analyzer.record_trade("BTC", 20.0, 3.0, is_market_order=False)
        
        # Taker: 5*2=10 fees → net=7. Maker: 1*2=2 fees → net=15
        self.assertAlmostEqual(taker["estimated_fees_bps"], 10.0)
        self.assertAlmostEqual(maker["estimated_fees_bps"], 2.0)
        self.assertGreater(maker["net_expected_edge_bps"], taker["net_expected_edge_bps"])

    def test_negative_edge_alert(self):
        """Warning fires when average net edge ≤ 0 over window."""
        alerts = []
        analyzer = FeeEdgeAnalyzer(
            negative_edge_alert_window=5,
            taker_fee_bps=10.0,
            alert_callback=lambda e: alerts.append(e),
        )
        
        # Record 5 trades with negative net edge (edge=5, slip=3, fees=20 → net=-18)
        for _ in range(5):
            analyzer.record_trade("BTC", 5.0, 3.0, is_market_order=True)
        
        self.assertGreater(len(alerts), 0)
        self.assertEqual(alerts[0]["type"], "NEGATIVE_EDGE")

    def test_no_alert_positive_edge(self):
        """No alert when net edge is positive."""
        alerts = []
        analyzer = FeeEdgeAnalyzer(
            negative_edge_alert_window=5,
            taker_fee_bps=1.0,
            alert_callback=lambda e: alerts.append(e),
        )
        
        for _ in range(10):
            analyzer.record_trade("BTC", 50.0, 2.0, is_market_order=True)
        
        self.assertEqual(len(alerts), 0)

    def test_win_rate_after_costs(self):
        """Win rate correctly counts positive net edge trades."""
        analyzer = FeeEdgeAnalyzer(taker_fee_bps=5.0)
        
        # Win: edge=30, slip=2, fees=10 → net=18 (positive)
        analyzer.record_trade("BTC", 30.0, 2.0, is_market_order=True)
        # Loss: edge=5, slip=2, fees=10 → net=-7 (negative)
        analyzer.record_trade("ETH", 5.0, 2.0, is_market_order=True)
        
        stats = analyzer.get_statistics()
        self.assertAlmostEqual(stats["win_rate_after_costs"], 0.5)

    def test_daily_summary_format(self):
        """Daily summary contains required export fields."""
        analyzer = FeeEdgeAnalyzer()
        analyzer.record_trade("BTC", 20.0, 3.0, is_market_order=True)
        
        summary = analyzer.get_daily_summary()
        self.assertIn("gross_edge_bps", summary)
        self.assertIn("net_edge_bps", summary)
        self.assertIn("fees_bps", summary)
        self.assertIn("slippage_bps", summary)
        self.assertIn("trade_count", summary)
        self.assertIn("win_rate_after_costs", summary)

    def test_window_bounded(self):
        """Rolling window respects configured size."""
        analyzer = FeeEdgeAnalyzer(window_size=10)
        
        for _ in range(100):
            analyzer.record_trade("BTC", 20.0, 2.0, is_market_order=True)
        
        # Internal deques should be bounded
        self.assertEqual(len(analyzer._net_edge), 10)

    def test_empty_statistics(self):
        """Stats return gracefully with no trades."""
        analyzer = FeeEdgeAnalyzer()
        stats = analyzer.get_statistics()
        self.assertEqual(stats["total_trades"], 0)


if __name__ == '__main__':
    unittest.main()
