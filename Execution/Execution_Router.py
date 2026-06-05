"""
ExecutionRouter — Routes orders by asset class; hard-blocks disabled assets.
---------------------------------------------------------------------------
"""

from typing import Dict, Optional, Any

from AegisQuantConfig import CONFIG, assert_asset_enabled
from Core.Logger import AG_LOGGER


class ExecutionRouter:
    def __init__(
        self,
        crypto_exec: Optional[Any] = None,
        forex_exec: Optional[Any] = None,
        stock_exec: Optional[Any] = None,
    ) -> None:
        self.logger = AG_LOGGER
        self._executors: Dict[str, Any] = {}
        if CONFIG["PROJECT"]["CRYPTO_ENABLED"] and crypto_exec is not None:
            self._executors["CRYPTO"] = crypto_exec
        if CONFIG["PROJECT"]["FOREX_ENABLED"] and forex_exec is not None:
            self._executors["FOREX"] = forex_exec
        if CONFIG["PROJECT"]["STOCKS_ENABLED"] and stock_exec is not None:
            self._executors["STOCKS"] = stock_exec

    def get_executor(self, asset_class: str) -> Any:
        assert_asset_enabled(asset_class)
        ex = self._executors.get(asset_class.upper())
        if ex is None:
            raise RuntimeError(f"No executor registered for asset class: {asset_class}")
        return ex

    def open_position(
        self,
        asset_class: str,
        symbol: str,
        qty: float,
        side: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Dict[str, Any]:
        assert_asset_enabled(asset_class)
        executor = self.get_executor(asset_class)
        return executor.open_position(symbol, qty, side, sl=sl, tp=tp)

    def close_position(
        self,
        asset_class: str,
        symbol: str,
        position_id: Optional[str] = None,
        qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        assert_asset_enabled(asset_class)
        return self.get_executor(asset_class).close_position(symbol, position_id, qty)

    def get_open_positions(self, asset_class: Optional[str] = None) -> Any:
        if asset_class is not None:
            assert_asset_enabled(asset_class)
            return self.get_executor(asset_class).get_open_positions()
        out = []
        for ac, ex in self._executors.items():
            try:
                out.extend(ex.get_open_positions())
            except Exception as e:
                self.logger.warning("get_open_positions %s failed: %s", ac, e)
        return out

    def get_balance(self, asset_class: Optional[str] = None) -> Optional[float]:
        if asset_class is not None:
            assert_asset_enabled(asset_class)
            return self.get_executor(asset_class).get_balance()
        total = 0.0
        has_unknown = False
        for ac, ex in self._executors.items():
            try:
                bal = ex.get_balance()
                if bal is None:
                    has_unknown = True
                else:
                    total += bal
            except Exception as e:
                self.logger.warning("get_balance %s failed: %s", ac, e)
                has_unknown = True
        if has_unknown:
            return None
        return total
