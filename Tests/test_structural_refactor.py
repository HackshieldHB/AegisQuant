"""
Structural Refactor Validation Tests
======================================
Confirms all 9 architectural corrections:
1) Spot SELL blocked when no position (enforce_account_constraints)
2) Futures SELL allowed
3) Spot SELL allowed when position held
4) Effective RR calculation accuracy (calculate_effective_rr)
5) Cooldown resets after expiry
6) No recursive cooldown after reset
7) Cooldown triggers at exact MAX_CONSECUTIVE_LOSSES only
8) Config contains all required new keys
"""
import unittest
import sys
import os
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from AegisQuantConfig import CONFIG


# --- Mock PortfolioManager ---
class MockPortfolioManager:
    def __init__(self, exposures=None):
        self.exposures = exposures or {}

    def get_exposure_for_symbol(self, symbol):
        return self.exposures.get(symbol, 0.0)


class TestSpotAccountSafety(unittest.TestCase):
    """Tests for enforce_account_constraints()"""

    def setUp(self):
        from Core.AsyncEngine import enforce_account_constraints
        self.enforce = enforce_account_constraints

    def test_spot_sell_blocked_no_position(self):
        """SELL on SPOT with no position must return HOLD."""
        original_mode = CONFIG["PROJECT"].get("ACCOUNT_MODE")
        CONFIG["PROJECT"]["ACCOUNT_MODE"] = "SPOT"
        try:
            pm = MockPortfolioManager(exposures={})
            result = self.enforce("SELL", "SOL/USDT", pm)
            self.assertEqual(result, "HOLD")
        finally:
            CONFIG["PROJECT"]["ACCOUNT_MODE"] = original_mode or "SPOT"

    def test_futures_sell_allowed(self):
        """SELL on FUTURES must pass through unchanged."""
        original_mode = CONFIG["PROJECT"].get("ACCOUNT_MODE")
        CONFIG["PROJECT"]["ACCOUNT_MODE"] = "FUTURES"
        try:
            pm = MockPortfolioManager(exposures={})
            result = self.enforce("SELL", "SOL/USDT", pm)
            self.assertEqual(result, "SELL")
        finally:
            CONFIG["PROJECT"]["ACCOUNT_MODE"] = original_mode or "SPOT"

    def test_spot_sell_with_position_allowed(self):
        """SELL on SPOT with existing position must pass through."""
        original_mode = CONFIG["PROJECT"].get("ACCOUNT_MODE")
        CONFIG["PROJECT"]["ACCOUNT_MODE"] = "SPOT"
        try:
            pm = MockPortfolioManager(exposures={"SOL/USDT": 15.0})
            result = self.enforce("SELL", "SOL/USDT", pm)
            self.assertEqual(result, "SELL")
        finally:
            CONFIG["PROJECT"]["ACCOUNT_MODE"] = original_mode or "SPOT"

    def test_buy_signal_passes_through(self):
        """BUY signals must never be blocked by account constraints."""
        original_mode = CONFIG["PROJECT"].get("ACCOUNT_MODE")
        CONFIG["PROJECT"]["ACCOUNT_MODE"] = "SPOT"
        try:
            pm = MockPortfolioManager(exposures={})
            result = self.enforce("BUY", "SOL/USDT", pm)
            self.assertEqual(result, "BUY")
        finally:
            CONFIG["PROJECT"]["ACCOUNT_MODE"] = original_mode or "SPOT"


class TestEffectiveRR(unittest.TestCase):
    """Tests for calculate_effective_rr()"""

    def setUp(self):
        from Core.AsyncEngine import calculate_effective_rr
        self.calc_rr = calculate_effective_rr

    def test_rr_below_threshold_blocked(self):
        """Small TP relative to SL+fees should produce low RR (<1.8)."""
        # SL=0.4%, TP=0.6%, fee=0.1%, spread=0.1%
        # effective_sl = 0.004 + 0.001 + 0.001 = 0.006
        # effective_tp = 0.006 - 0.001 - 0.001 = 0.004
        # RR = 0.004 / 0.006 = 0.667
        rr = self.calc_rr(0.004, 0.006, 0.001, 0.001)
        self.assertLess(rr, 1.8)

    def test_rr_above_threshold_allowed(self):
        """Adequate TP relative to SL+fees should produce high RR (>=1.8)."""
        # SL=0.8%, TP=2.0%, fee=0.1%, spread=0.05%
        # effective_sl = 0.008 + 0.001 + 0.0005 = 0.0095
        # effective_tp = 0.020 - 0.001 - 0.0005 = 0.0185
        # RR = 0.0185 / 0.0095 ≈ 1.947
        rr = self.calc_rr(0.008, 0.020, 0.001, 0.0005)
        self.assertGreaterEqual(rr, 1.8)

    def test_rr_zero_sl(self):
        """Zero SL with zero fees must return 0.0 (no division by zero)."""
        rr = self.calc_rr(0.0, 0.010, 0.0)
        self.assertEqual(rr, 0.0)

    def test_rr_zero_spread(self):
        """Zero spread should still compute correctly."""
        # SL=0.004, TP=0.012 (new floor), fee=0.001
        # effective_sl = 0.005, effective_tp = 0.011
        # RR = 0.011/0.005 = 2.2
        rr = self.calc_rr(0.004, 0.012, 0.001, 0.0)
        self.assertGreaterEqual(rr, 1.8)


class TestDirectionalConfidence(unittest.TestCase):
    """Tests for three-class directional probability normalization."""

    def setUp(self):
        from Core.AsyncEngine import calculate_directional_confidence
        self.calculate = calculate_directional_confidence

    def test_excludes_neutral_mass_from_directional_conviction(self):
        confidence, mass = self.calculate(0.55, 0.15)
        self.assertAlmostEqual(mass, 0.70)
        self.assertAlmostEqual(confidence, 0.55 / 0.70)

    def test_balanced_direction_remains_low_confidence(self):
        confidence, mass = self.calculate(0.30, 0.30)
        self.assertAlmostEqual(mass, 0.60)
        self.assertAlmostEqual(confidence, 0.50)

    def test_zero_directional_mass_is_safe(self):
        confidence, mass = self.calculate(0.0, 0.0)
        self.assertEqual((confidence, mass), (0.0, 0.0))


class TestCooldownFix(unittest.TestCase):
    """Tests for Risk_Manager cooldown reset logic."""

    def setUp(self):
        from Core.Risk_Manager import RiskManager
        self.rm = RiskManager(balance=22.0)

    def test_cooldown_resets_after_expiry(self):
        """After cooldown expires, cooldown_active() must return False and reset counter."""
        max_losses = CONFIG.get("RISK", {}).get("MAX_CONSECUTIVE_LOSSES", 5)
        for _ in range(max_losses):
            self.rm.record_realized_pnl(is_win=False)

        # Cooldown should be active
        self.assertTrue(self.rm.cooldown_active())

        # Simulate cooldown expiry
        self.rm._cooldown_until = time.time() - 1

        # Now cooldown should be inactive AND counter should reset
        self.assertFalse(self.rm.cooldown_active())
        self.assertEqual(self.rm._consecutive_losses, 0)
        self.assertEqual(self.rm._cooldown_until, 0.0)

    def test_no_recursive_cooldown_after_reset(self):
        """After cooldown reset, a single loss must NOT re-trigger cooldown."""
        max_losses = CONFIG.get("RISK", {}).get("MAX_CONSECUTIVE_LOSSES", 5)
        for _ in range(max_losses):
            self.rm.record_realized_pnl(is_win=False)

        # Simulate cooldown expiry
        self.rm._cooldown_until = time.time() - 1
        self.rm.cooldown_active()  # triggers reset

        # One more loss should NOT trigger cooldown
        self.rm.record_realized_pnl(is_win=False)
        self.assertEqual(self.rm._consecutive_losses, 1)
        self.assertFalse(self.rm.cooldown_active())

    def test_cooldown_triggers_at_exact_max(self):
        """Cooldown must trigger at exactly MAX_CONSECUTIVE_LOSSES, not before."""
        max_losses = CONFIG.get("RISK", {}).get("MAX_CONSECUTIVE_LOSSES", 5)

        # Record max - 1 losses: should NOT trigger
        for _ in range(max_losses - 1):
            self.rm.record_realized_pnl(is_win=False)
        self.assertFalse(self.rm.cooldown_active())

        # One more loss: should trigger
        self.rm.record_realized_pnl(is_win=False)
        self.assertTrue(self.rm.cooldown_active())

    def test_win_resets_counter(self):
        """A win must reset the consecutive loss counter to 0."""
        self.rm.record_realized_pnl(is_win=False)
        self.rm.record_realized_pnl(is_win=False)
        self.assertEqual(self.rm._consecutive_losses, 2)

        self.rm.record_realized_pnl(is_win=True)
        self.assertEqual(self.rm._consecutive_losses, 0)


class TestConfigKeys(unittest.TestCase):
    """Validate all required new config keys exist."""

    def test_account_mode_exists(self):
        self.assertIn("ACCOUNT_MODE", CONFIG["PROJECT"])
        self.assertIn(CONFIG["PROJECT"]["ACCOUNT_MODE"], ("SPOT", "FUTURES"))

    def test_min_sl_micro_exists(self):
        self.assertIn("MIN_SL_MICRO", CONFIG.get("STOPS", {}))
        self.assertGreater(CONFIG["STOPS"]["MIN_SL_MICRO"], 0)

    def test_min_tp_micro_exists(self):
        self.assertIn("MIN_TP_MICRO", CONFIG.get("STOPS", {}))
        self.assertGreater(CONFIG["STOPS"]["MIN_TP_MICRO"], 0)

    def test_symbol_reentry_cooldown_exists(self):
        self.assertIn("SYMBOL_REENTRY_COOLDOWN_SEC", CONFIG.get("EXECUTION", {}))
        self.assertGreater(CONFIG["EXECUTION"]["SYMBOL_REENTRY_COOLDOWN_SEC"], 0)

    def test_max_consecutive_losses_exists(self):
        self.assertIn("MAX_CONSECUTIVE_LOSSES", CONFIG.get("RISK", {}))
        self.assertGreater(CONFIG["RISK"]["MAX_CONSECUTIVE_LOSSES"], 0)

    def test_tp_exceeds_sl_micro(self):
        """MIN_TP_MICRO must be greater than MIN_SL_MICRO for valid RR."""
        self.assertGreater(
            CONFIG["STOPS"]["MIN_TP_MICRO"],
            CONFIG["STOPS"]["MIN_SL_MICRO"],
        )


class TestSafetyBoundaries(unittest.TestCase):
    def test_portfolio_resize_updates_sector_total(self):
        from Core.Portfolio_Manager import PortfolioManager

        portfolio = PortfolioManager()
        portfolio.record_open("CRYPTO", "BTC/USDT", 100.0, entry_price=50.0)
        portfolio.record_resize("CRYPTO", "BTC/USDT", 40.0)

        self.assertEqual(portfolio.get_exposure_for_symbol("BTC/USDT"), 40.0)
        self.assertEqual(portfolio.get_exposure_for_sector("CRYPTO"), 40.0)

    def test_telegram_commands_require_owner_chat(self):
        from Services.TelegramService import TelegramService

        telegram = TelegramService.__new__(TelegramService)
        telegram.chat_id = "12345"

        self.assertTrue(telegram._is_authorized_message({"chat": {"id": 12345}}))
        self.assertFalse(telegram._is_authorized_message({"chat": {"id": 99999}}))
        self.assertFalse(telegram._is_authorized_message({}))

    def test_live_execution_requires_exact_live_mode(self):
        from AegisQuantConfig import is_live_trading_enabled

        original = CONFIG["PROJECT"]["MODE"]
        try:
            CONFIG["PROJECT"]["MODE"] = "PAPER"
            self.assertFalse(is_live_trading_enabled())
            CONFIG["PROJECT"]["MODE"] = "BACKTEST"
            self.assertFalse(is_live_trading_enabled())
            CONFIG["PROJECT"]["MODE"] = "LIVE"
            self.assertTrue(is_live_trading_enabled())
        finally:
            CONFIG["PROJECT"]["MODE"] = original

    def test_paper_executor_never_submits_exchange_order(self):
        from Execution.CryptoExecution import CryptoExecution

        class FakeExchange:
            options = {"defaultType": "spot"}
            markets = {"BTC/USDT": {}}
            create_calls = 0

            def market(self, symbol):
                return {
                    "limits": {
                        "cost": {"min": 5.0},
                        "amount": {"min": 0.00001},
                    }
                }

            def amount_to_precision(self, symbol, amount):
                return str(amount)

            def fetch_ticker(self, symbol):
                return {"last": 100.0}

            def create_order(self, *args, **kwargs):
                self.create_calls += 1
                raise AssertionError("PAPER mode reached create_order")

        executor = CryptoExecution.__new__(CryptoExecution)
        executor.exchange = FakeExchange()
        executor.logger = type("Logger", (), {"info": lambda *a, **k: None,
                                               "error": lambda *a, **k: None})()
        executor._last_order_ts = {}
        executor._cooldown_sec = 0
        executor._retry_attempts = 1
        executor._retry_base_delay = 0
        executor.get_balance = lambda asset=None: 100.0

        original = CONFIG["PROJECT"]["MODE"]
        try:
            CONFIG["PROJECT"]["MODE"] = "PAPER"
            opened = executor.open_position("BTC/USDT", 0.1, "BUY")
            closed = executor.close_position("BTC/USDT", qty=0.1)
        finally:
            CONFIG["PROJECT"]["MODE"] = original

        self.assertEqual(opened["status"], "filled")
        self.assertEqual(closed["status"], "filled")
        self.assertEqual(executor.exchange.create_calls, 0)

    def test_spot_recovery_uses_total_balance_and_filters_dust(self):
        from Execution.CryptoExecution import CryptoExecution

        class FakeExchange:
            options = {"defaultType": "spot"}
            markets = {"BTC/USDT": {}, "ETH/USDT": {}}

            def fetch_balance(self, params):
                return {"total": {"BTC": 0.01, "ETH": 0.000001}}

            def fetch_ticker(self, symbol):
                return {"last": 1000.0 if symbol == "BTC/USDT" else 2000.0}

            def market(self, symbol):
                return {"limits": {"cost": {"min": 5.0}}}

        executor = CryptoExecution.__new__(CryptoExecution)
        executor.exchange = FakeExchange()
        executor.logger = type("Logger", (), {"error": lambda *a, **k: None})()
        executor._retry_attempts = 1

        positions = executor.get_open_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "BTC/USDT")
        self.assertEqual(positions[0]["contracts"], 0.01)


if __name__ == '__main__':
    unittest.main()
