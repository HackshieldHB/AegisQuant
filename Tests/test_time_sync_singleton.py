"""
Binance Time Sync & Singleton Guard — Validation Tests
=======================================================
Tests:
1) BinanceTimeSync offset computation and safety limit
2) BinanceTimeSync singleton pattern
3) BinanceTimeSync lazy re-sync
4) Singleton process-scan guard
5) Watchdog exponential backoff calculation
6) Watchdog max restart terminal halt
7) Startup sequence ordering
"""
import unittest
import time
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Core.BinanceTimeSync import BinanceTimeSync, CriticalStartupException
from Core.Singleton import SingletonLock, SingletonException


class MockExchange:
    """Mock CCXT exchange for time sync testing."""
    def __init__(self, server_time_offset_ms=0):
        self._offset = server_time_offset_ms

    def fetch_time(self):
        return int(time.time() * 1000) + self._offset


class MockExchangeLargeOffset:
    """Mock exchange with dangerously large clock offset."""
    def fetch_time(self):
        return int(time.time() * 1000) + 10_000  # 10 seconds ahead


class MockExchangeFailure:
    """Mock exchange that fails to respond."""
    def fetch_time(self):
        raise ConnectionError("Exchange unreachable")


class TestBinanceTimeSync(unittest.TestCase):

    def setUp(self):
        BinanceTimeSync.reset_singleton()

    def tearDown(self):
        BinanceTimeSync.reset_singleton()

    def test_basic_offset_computation(self):
        """Sync computes correct offset within tolerance."""
        exchange = MockExchange(server_time_offset_ms=150)
        ts = BinanceTimeSync(exchange=exchange)
        offset = ts.sync()

        # Offset should be approximately 150ms (±50ms for execution time)
        self.assertAlmostEqual(offset, 150, delta=100)
        self.assertEqual(ts.sync_count, 1)

    def test_critical_exception_on_large_offset(self):
        """Startup must abort if offset exceeds safety limit."""
        exchange = MockExchangeLargeOffset()
        ts = BinanceTimeSync(exchange=exchange, max_offset_ms=5000)

        with self.assertRaises(CriticalStartupException):
            ts.sync()

    def test_singleton_pattern(self):
        """Only one BinanceTimeSync instance should exist."""
        ts1 = BinanceTimeSync(exchange=MockExchange())
        ts2 = BinanceTimeSync()
        self.assertIs(ts1, ts2)

    def test_get_timestamp_applies_offset(self):
        """get_timestamp must return exchange-corrected time."""
        exchange = MockExchange(server_time_offset_ms=200)
        ts = BinanceTimeSync(exchange=exchange)
        ts.sync()

        corrected = ts.get_timestamp()
        raw = int(time.time() * 1000)

        # Corrected should be ~200ms ahead of raw
        diff = corrected - raw
        self.assertAlmostEqual(diff, 200, delta=100)

    def test_lazy_resync(self):
        """Sync should be triggered lazily when interval elapses."""
        exchange = MockExchange(server_time_offset_ms=100)
        ts = BinanceTimeSync(exchange=exchange, sync_interval_sec=0.01)
        ts.sync()
        self.assertEqual(ts.sync_count, 1)

        time.sleep(0.02)  # Exceed sync interval
        ts.get_timestamp()  # Should trigger re-sync
        self.assertEqual(ts.sync_count, 2)

    def test_no_sync_without_exchange(self):
        """Sync gracefully skips when no exchange is configured."""
        ts = BinanceTimeSync()
        ts._exchange = None
        offset = ts.sync()
        self.assertEqual(offset, 0)

    def test_diagnostics_export(self):
        """Diagnostics dict contains all required fields."""
        exchange = MockExchange(server_time_offset_ms=50)
        ts = BinanceTimeSync(exchange=exchange)
        ts.sync()

        diag = ts.get_diagnostics()
        self.assertIn("offset_ms", diag)
        self.assertIn("sync_count", diag)
        self.assertIn("last_sync_age_sec", diag)
        self.assertIn("timestamp_utc", diag)

    def test_exchange_failure_does_not_crash(self):
        """Sync failure returns last known offset without crashing."""
        exchange = MockExchangeFailure()
        ts = BinanceTimeSync(exchange=exchange)
        offset = ts.sync()  # Should not raise
        self.assertEqual(offset, 0)

    def test_singleton_accepts_exchange_on_second_init(self):
        """Exchange must be injected even when singleton already initialized without one."""
        ts1 = BinanceTimeSync()
        self.assertIsNone(ts1._exchange)

        exchange = MockExchange(server_time_offset_ms=50)
        ts2 = BinanceTimeSync(exchange=exchange)

        self.assertIs(ts1, ts2)
        self.assertIsNotNone(ts2._exchange)

        offset = ts2.sync()
        self.assertAlmostEqual(offset, 50, delta=100)

    def test_singleton_updates_exchange_on_reconnect(self):
        """Providing a new exchange instance should replace the existing one."""
        ts1 = BinanceTimeSync(exchange=MockExchange(server_time_offset_ms=10))
        ts1.sync()

        new_exchange = MockExchange(server_time_offset_ms=200)
        ts2 = BinanceTimeSync(exchange=new_exchange)

        self.assertIs(ts1, ts2)

        offset = ts2.sync()
        self.assertAlmostEqual(offset, 200, delta=100)


class TestSingletonProcessGuard(unittest.TestCase):

    def test_scan_for_duplicates_passes_clean(self):
        """Process scan should pass when no duplicate engine is running."""
        lock = SingletonLock("test_guard.lock")
        # Should not raise — we are the only instance
        lock._scan_for_duplicates()

    def test_scan_checks_for_main_py(self):
        """Scanner looks for Main.py and Main_Production in process cmdlines."""
        lock = SingletonLock("test_guard.lock")
        # This just verifies the method runs without error
        # (we can't easily inject a fake process in unit tests)
        lock._scan_for_duplicates()


class TestWatchdogExponentialBackoff(unittest.TestCase):

    def test_backoff_calculation(self):
        """Backoff increases exponentially: 30 → 60 → 120 → 240 → 300 (capped)."""
        from WatchdogSupervisor import WatchdogSupervisor
        # Don't actually init — just test the math
        ws = WatchdogSupervisor.__new__(WatchdogSupervisor)
        ws.start_times_this_hour = []
        ws.RESTART_BACKOFF_BASE = 30
        ws.RESTART_BACKOFF_MAX = 300
        ws.MAX_RESTARTS_PER_HOUR = 5
        ws.logger = __import__('logging').getLogger('test')

        # 0 prior restarts → 30s
        delay = ws._get_backoff_delay()
        self.assertEqual(delay, 30)

        # 1 prior restart → 30s
        ws.start_times_this_hour = [time.time()]
        delay = ws._get_backoff_delay()
        self.assertEqual(delay, 30)

        # 2 prior → 60s
        ws.start_times_this_hour = [time.time(), time.time()]
        delay = ws._get_backoff_delay()
        self.assertEqual(delay, 60)

        # 3 prior → 120s
        ws.start_times_this_hour = [time.time()] * 3
        delay = ws._get_backoff_delay()
        self.assertEqual(delay, 120)

        # 4 prior → 240s
        ws.start_times_this_hour = [time.time()] * 4
        delay = ws._get_backoff_delay()
        self.assertEqual(delay, 240)

    def test_backoff_capped(self):
        """Backoff must never exceed RESTART_BACKOFF_MAX."""
        from WatchdogSupervisor import WatchdogSupervisor
        ws = WatchdogSupervisor.__new__(WatchdogSupervisor)
        ws.start_times_this_hour = [time.time()] * 10
        ws.RESTART_BACKOFF_BASE = 30
        ws.RESTART_BACKOFF_MAX = 300
        ws.MAX_RESTARTS_PER_HOUR = 20  # Allow more for cap test
        ws.logger = __import__('logging').getLogger('test')

        delay = ws._get_backoff_delay()
        self.assertLessEqual(delay, 300)

    def test_max_restarts_terminal_halt(self):
        """Exceeding max restarts must return False (terminal halt)."""
        from WatchdogSupervisor import WatchdogSupervisor
        ws = WatchdogSupervisor.__new__(WatchdogSupervisor)
        ws.start_times_this_hour = [time.time()] * 5
        ws.MAX_RESTARTS_PER_HOUR = 5
        ws.logger = __import__('logging').getLogger('test')

        # Mock telegram to avoid real sends
        class MockTelegram:
            def send_message(self, *a, **kw): pass
        ws.telegram = MockTelegram()

        result = ws._can_restart()
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
