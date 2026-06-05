from abc import ABC, abstractmethod
from typing import Dict, List
from Data.Models import Candle
from Core.Indicator_Engine import IndicatorEngine
from Core.Logger import AG_LOGGER
import pandas as pd


class StrategyBase(ABC):
    def __init__(self, name: str):
        self.name = name
        self.indicator_engine = IndicatorEngine()
        self.logger = AG_LOGGER

    @abstractmethod
    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        """
        Analyze market data and return a signal.

        Returns:
            Dict: {
                "signal": "BUY" | "SELL" | "HOLD",
                "confidence": float (0.0 - 1.0),
                "reason": str
            }
        """
        pass

    def prepare_data(self, candles: List[Candle]) -> pd.DataFrame:
        """
        Helper to convert candles to DataFrame with indicators.
        """
        return self.indicator_engine.calculate_indicators(candles)