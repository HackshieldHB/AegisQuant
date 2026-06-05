"""
StockExecution — Alpaca (or broker API) with structured result, retry, cooldown.
------------------------------------------------------------------------------
Only used when STOCKS_ENABLED is True. Requires BROKERS.ALPACA in CONFIG.
"""

import time
import random
from typing import Dict, Optional, Any

import requests
from Execution.BaseExecution import BaseExecution, execution_result
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


class StockExecution(BaseExecution):
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        broker = CONFIG.get("BROKERS", {}).get("ALPACA", {})
        self.api_key = broker.get("API_KEY") or ""
        self.secret = broker.get("SECRET") or ""
        self.is_paper = broker.get("PAPER", True)
        self.base_url = "https://paper-api.alpaca.markets/v2" if self.is_paper else "https://api.alpaca.markets/v2"
        self.headers = {
            "Apca-Api-Key-Id": self.api_key,
            "Apca-Api-Secret-Key": self.secret,
            "Accept": "application/json",
        }
        self._last_order_ts: Dict[str, float] = {}
        self._cooldown_sec = CONFIG.get("EXECUTION", {}).get("ORDER_COOLDOWN_SEC", 60)
        self._fill_poll_interval = CONFIG.get("EXECUTION", {}).get("FILL_POLL_INTERVAL_SEC", 2)
        self._fill_poll_timeout = CONFIG.get("EXECUTION", {}).get("FILL_POLL_TIMEOUT_SEC", 30)
        self._retry_attempts = CONFIG.get("EXECUTION", {}).get("RETRY_ATTEMPTS", 3)
        self._retry_base_delay = CONFIG.get("EXECUTION", {}).get("RETRY_BASE_DELAY_SEC", 2)

    def _cooldown_key(self, symbol: str, side: str) -> str:
        return f"{symbol}:{side.upper()}"

    def _in_cooldown(self, symbol: str, side: str) -> bool:
        last = self._last_order_ts.get(self._cooldown_key(symbol, side), 0)
        return (time.time() - last) < self._cooldown_sec

    def _set_cooldown(self, symbol: str, side: str) -> None:
        self._last_order_ts[self._cooldown_key(symbol, side)] = time.time()

    def _with_backoff(self, method: str, url: str, **kwargs) -> requests.Response:
        """Rate Limit Protection for Alpaca REST API."""
        last_error = None
        for attempt in range(self._retry_attempts):
            try:
                r = requests.request(method, url, **kwargs)
                if r.status_code == 429:
                    raise Exception("HTTP 429: Too Many Requests")
                r.raise_for_status()
                return r
            except Exception as e:
                last_error = e
                if attempt < self._retry_attempts - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    jitter = random.uniform(0, delay * 0.25)
                    delay += jitter
                    self.logger.warning("Alpaca Network Error: Retrying %s in %.1fs. Err: %s", method, delay, e)
                    time.sleep(delay)
                else:
                    self.logger.critical("Alpaca Network FATAL: Max retries exceeded for %s.", method)
                    raise e
        raise last_error

    def _wait_for_fill(self, symbol: str, order_id: str, side: str) -> Dict[str, Any]:
        deadline = time.time() + self._fill_poll_timeout
        while time.time() < deadline:
            try:
                r = self._with_backoff("GET", f"{self.base_url}/orders/{order_id}", headers=self.headers, timeout=10)
                res = r.json()
                status = (res.get("status") or "").lower()
                if status in ("filled", "closed"):
                    filled = float(res.get("filled_qty", 0) or 0)
                    avg = float(res.get("filled_avg_price", 0) or 0)
                    return execution_result("filled", symbol, side, filled_qty=filled, avg_price=avg, order_id=order_id)
                if status in ("canceled", "rejected", "expired"):
                    return execution_result("rejected", symbol, side, order_id=order_id, message=status)
            except Exception as e:
                self.logger.warning("Fetch order %s failed: %s", order_id, e)
            time.sleep(self._fill_poll_interval)
        return execution_result("timeout", symbol, side, order_id=order_id, message="Fill poll timeout")

    def open_position(
        self,
        symbol: str,
        qty: float,
        side: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.api_key or not self.secret:
            return execution_result("rejected", symbol, side, message="Alpaca not configured")
        if self._in_cooldown(symbol, side):
            return execution_result("rejected", symbol, side, message="Cooldown active")
        mode = CONFIG["PROJECT"]["MODE"]
        if mode in ("BACKTEST", "PAPER"):
            self._set_cooldown(symbol, side)
            return execution_result("filled", symbol, side, filled_qty=qty, avg_price=0, order_id="paper_stock")
        url = f"{self.base_url}/orders"
        payload = {
            "symbol": symbol,
            "qty": int(round(qty)),
            "side": side.lower(),
            "type": "market",
            "time_in_force": "day",
        }
        last_error = None
        for attempt in range(self._retry_attempts):
            try:
                r = self._with_backoff("POST", url, json=payload, headers=self.headers, timeout=30)
                res = r.json()
                order_id = str(res.get("id", ""))
                status = (res.get("status") or "").lower()
                if status in ("filled", "closed"):
                    filled = float(res.get("filled_qty", qty))
                    avg = float(res.get("filled_avg_price", 0) or 0)
                    self._set_cooldown(symbol, side)
                    return execution_result("filled", symbol, side, filled_qty=filled, avg_price=avg, order_id=order_id)
                if status in ("canceled", "rejected", "expired"):
                    return execution_result("rejected", symbol, side, order_id=order_id, message=status)
                result = self._wait_for_fill(symbol, order_id, side)
                self._set_cooldown(symbol, side)
                return result
            except Exception as e:
                last_error = e
                if attempt < self._retry_attempts - 1:
                    time.sleep(self._retry_base_delay * (2 ** attempt))
        self._set_cooldown(symbol, side)
        return execution_result("rejected", symbol, side, message=str(last_error))

    def close_position(
        self,
        symbol: str,
        position_id: Optional[str] = None,
        qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.api_key or not self.secret:
            return execution_result("rejected", symbol, "SELL", message="Alpaca not configured")
        try:
            url = f"{self.base_url}/positions/{symbol}"
            r = self._with_backoff("DELETE", url, headers=self.headers, timeout=30)
            res = r.json() if r.content else {}
            return execution_result("filled", symbol, "SELL", order_id=str(res.get("id", "")))
        except Exception as e:
            self.logger.error("Alpaca close failed: %s", e)
            return execution_result("rejected", symbol, "SELL", message=str(e))

    def get_open_positions(self) -> Any:
        try:
            r = self._with_backoff("GET", f"{self.base_url}/positions", headers=self.headers, timeout=10)
            return r.json()
        except Exception as e:
            self.logger.error("Alpaca get_open_positions failed: %s", e)
            return []

    def get_balance(self, asset: Optional[str] = None) -> float:
        try:
            r = self._with_backoff("GET", f"{self.base_url}/account", headers=self.headers, timeout=10)
            return float(r.json().get("portfolio_value", 0))
        except Exception as e:
            self.logger.error("Alpaca get_balance failed: %s", e)
            return 0.0

    def get_account_summary(self) -> Dict[str, Any]:
        try:
            r = self._with_backoff("GET", f"{self.base_url}/account", headers=self.headers, timeout=10)
            j = r.json()
            return {
                "equity": float(j.get("portfolio_value", 0)),
                "cash": float(j.get("cash", 0)),
                "buying_power": float(j.get("buying_power", 0)),
                "currency": j.get("currency", "USD"),
                "status": j.get("status", "UNKNOWN"),
            }
        except Exception as e:
            self.logger.error("Alpaca get_account_summary failed: %s", e)
            return {}
