from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from Data.Models import Candle

class MarketDataSource(ABC):
    """
    Abstract base class for all market data sources and execution interfaces.
    """

    @abstractmethod
    def connect(self):
        """
        Establish connection to the market API.
        """
        pass

    @abstractmethod
    def fetch_ohlcv(
            self,
            symbol: str,
            timeframe: str,
            limit: int = 100
    ) -> List[Candle]:
        """
        Fetch OHLCV data for a given symbol.
        """
        pass

    @abstractmethod
    def get_ticker(self, symbol: str) -> Dict:
        """
        Get current ticker information (bid, ask, last).
        """
        pass

    @abstractmethod
    def get_balance(self) -> Dict:
        """
        Get account balance.
        """
        pass
