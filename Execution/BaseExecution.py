"""
BaseExecution — Abstract interface for all asset classes.
---------------------------------------------------------
Structured result: status (filled | rejected | timeout), symbol, side, filled_qty, avg_price, order_id, timestamp.
"""

import time
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


def execution_result(
    status: str,
    symbol: str,
    side: str,
    filled_qty: float = 0.0,
    avg_price: float = 0.0,
    order_id: str = "",
    message: str = "",
    sl_status: str = "",
    **kwargs
) -> Dict[str, Any]:
    res = {
        "status": status,
        "symbol": symbol,
        "side": side,
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "order_id": order_id,
        "timestamp": int(time.time()),
        "message": message or "",
        "sl_status": sl_status,
    }
    res.update(kwargs)
    return res


class BaseExecution(ABC):
    """Abstract base for CryptoExecution, ForexExecution, StockExecution."""

    @abstractmethod
    def open_position(
        self,
        symbol: str,
        qty: float,
        side: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Open position. Returns structured result (filled | rejected | timeout)."""
        pass

    @abstractmethod
    def close_position(
        self,
        symbol: str,
        position_id: Optional[str] = None,
        qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Close position. Returns structured result."""
        pass

    @abstractmethod
    def get_open_positions(self) -> Any:
        """Return list of open positions."""
        pass

    @abstractmethod
    def get_balance(self, asset: Optional[str] = None) -> float:
        """Return account balance as float."""
        pass

    @abstractmethod
    def get_account_summary(self) -> Dict[str, Any]:
        """Return broker-specific summary dict."""
        pass
