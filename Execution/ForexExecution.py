"""
ForexExecution — OANDA with verify, retry, cooldown, structured result.
----------------------------------------------------------------------
Only used when FOREX_ENABLED is True.
"""

import time
import random
from typing import Dict, Optional, Any, List

import requests
from Execution.BaseExecution import BaseExecution, execution_result
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


class ForexExecution(BaseExecution):
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        broker = CONFIG["BROKERS"]["OANDA"]
        self.account_id = broker.get("ACCOUNT_ID") or ""
        self.api_key = broker.get("API_KEY") or ""
        self.is_practice = broker.get("PRACTICE", True)
        self.base_url = "https://api-fxpractice.oanda.com/v3" if self.is_practice else "https://api-fxtrade.oanda.com/v3"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
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
        """Rate Limit Protection for OANDA REST API."""
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
                    self.logger.warning("OANDA Network Error: Retrying %s in %.1fs. Err: %s", method, delay, e)
                    time.sleep(delay)
                else:
                    self.logger.critical("OANDA Network FATAL: Max retries exceeded for %s.", method)
                    raise e
        raise last_error

    def open_position(
        self,
        symbol: str,
        qty: float,
        side: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.account_id or not self.api_key:
            return execution_result("rejected", symbol, side, message="OANDA not configured")
        if self._in_cooldown(symbol, side):
            return execution_result("rejected", symbol, side, message="Cooldown active")
        units = str(int(round(qty))) if side.lower() == "buy" else str(-int(round(qty)))
        data = {
            "order": {
                "instrument": symbol,
                "units": units,
                "type": "MARKET",
                "positionFill": "DEFAULT",
            }
        }
        if sl is not None:
            data["order"]["stopLossOnFill"] = {"price": str(round(sl, 5))}
        if tp is not None:
            data["order"]["takeProfitOnFill"] = {"price": str(round(tp, 5))}
        url = f"{self.base_url}/accounts/{self.account_id}/orders"
        last_error = None
        for attempt in range(self._retry_attempts):
            try:
                r = self._with_backoff("POST", url, json=data, headers=self.headers, timeout=30)
                res = r.json()
                if res.get("orderFillTransaction"):
                    fill = res["orderFillTransaction"]
                    filled_qty = abs(float(fill.get("units", units)))
                    price = float(fill.get("price", 0) or 0)
                    order_id = str(fill.get("id", ""))
                    self._set_cooldown(symbol, side)
                    return execution_result(
                        "filled", symbol, side,
                        filled_qty=filled_qty, avg_price=price, order_id=order_id,
                    )
                if res.get("orderCancelTransaction"):
                    return execution_result(
                        "rejected", symbol, side,
                        message=res["orderCancelTransaction"].get("reason", "Canceled"),
                    )
                last_error = "No fill or cancel in response"
            except Exception as e:
                last_error = e
                if attempt < self._retry_attempts - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    time.sleep(delay + random.uniform(0, delay * 0.25))
        self._set_cooldown(symbol, side)
        return execution_result("rejected", symbol, side, message=str(last_error))

    def close_position(
        self,
        symbol: str,
        position_id: Optional[str] = None,
        qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.account_id or not self.api_key:
            return execution_result("rejected", symbol, "SELL", message="OANDA not configured")
        url_trades = f"{self.base_url}/accounts/{self.account_id}/openTrades"
        try:
            r = self._with_backoff("GET", url_trades, headers=self.headers, timeout=10)
            trades = r.json().get("trades", [])
        except Exception as e:
            self.logger.error("OANDA get openTrades failed: %s", e)
            return execution_result("rejected", symbol, "SELL", message=str(e))
        for t in trades:
            if t.get("instrument") != symbol:
                continue
            tid = t.get("id") or position_id
            if not tid:
                continue
            url = f"{self.base_url}/accounts/{self.account_id}/trades/{tid}/close"
            try:
                r = self._with_backoff("PUT", url, headers=self.headers, json={"units": "ALL"}, timeout=30)
                res = r.json()
                if res.get("orderFillTransaction"):
                    fill = res["orderFillTransaction"]
                    return execution_result(
                        "filled", symbol, "SELL",
                        filled_qty=abs(float(fill.get("units", 0))),
                        avg_price=float(fill.get("price", 0) or 0),
                        order_id=str(fill.get("id", "")),
                    )
            except Exception as e:
                self.logger.error("OANDA close trade %s failed: %s", tid, e)
        if position_id:
            url = f"{self.base_url}/accounts/{self.account_id}/trades/{position_id}/close"
            try:
                r = self._with_backoff("PUT", url, headers=self.headers, json={"units": "ALL"}, timeout=30)
                res = r.json()
                if res.get("orderFillTransaction"):
                    fill = res["orderFillTransaction"]
                    return execution_result(
                        "filled", symbol, "SELL",
                        filled_qty=abs(float(fill.get("units", 0))),
                        avg_price=float(fill.get("price", 0) or 0),
                        order_id=str(fill.get("id", "")),
                    )
                return execution_result("rejected", symbol, "SELL", order_id=position_id, message="Close not filled")
            except Exception as e:
                self.logger.error("OANDA close failed: %s", e)
                return execution_result("rejected", symbol, "SELL", message=str(e))
        return execution_result("rejected", symbol, "SELL", message="No open position found")

    def get_open_positions(self) -> Any:
        try:
            url = f"{self.base_url}/accounts/{self.account_id}/openPositions"
            r = self._with_backoff("GET", url, headers=self.headers, timeout=10)
            return r.json().get("positions", [])
        except Exception as e:
            self.logger.error("OANDA get_open_positions failed: %s", e)
            return []

    def get_balance(self, asset: Optional[str] = None) -> float:
        try:
            url = f"{self.base_url}/accounts/{self.account_id}/summary"
            r = self._with_backoff("GET", url, headers=self.headers, timeout=10)
            return float(r.json().get("account", {}).get("NAV", 0))
        except Exception as e:
            self.logger.error("OANDA get_balance failed: %s", e)
            return 0.0

    def get_account_summary(self) -> Dict[str, Any]:
        try:
            url = f"{self.base_url}/accounts/{self.account_id}/summary"
            r = self._with_backoff("GET", url, headers=self.headers, timeout=10)
            acc = r.json().get("account", {})
            return {
                "equity": float(acc.get("NAV", 0)),
                "margin_available": float(acc.get("marginAvailable", 0)),
                "margin_used": float(acc.get("marginUsed", 0)),
                "balance": float(acc.get("balance", 0)),
                "currency": acc.get("currency", "USD"),
            }
        except Exception as e:
            self.logger.error("OANDA get_account_summary failed: %s", e)
            return {}


OANDAExecution = ForexExecution
