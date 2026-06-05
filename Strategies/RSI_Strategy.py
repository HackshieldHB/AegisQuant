from typing import Dict, List
from Data.Models import Candle
from Strategies.Strategy_Base import StrategyBase

class RSIStrategy(StrategyBase):
    def __init__(self, period: int = 14, overbought: int = 70, oversold: int = 30):
        super().__init__("RSI")
        self.period = period
        self.overbought = overbought
        self.oversold = oversold

    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        df = self.prepare_data(candles)
        if df.empty or 'RSI' not in df.columns:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient data"}

        current_rsi = df['RSI'].iloc[-1]
        
        signal = "HOLD"
        confidence = 0.5
        reason = f"RSI: {current_rsi:.2f}"

        if current_rsi > self.overbought:
            signal = "SELL"
            # Higher RSI above threshold = higher confidence to sell? Or mean reversion?
            # Assuming mean reversion for now
            confidence = min(0.5 + (current_rsi - self.overbought) / 20, 0.95)
            reason = f"Overbought (RSI {current_rsi:.2f})"
        elif current_rsi < self.oversold:
            signal = "BUY"
            confidence = min(0.5 + (self.oversold - current_rsi) / 20, 0.95)
            reason = f"Oversold (RSI {current_rsi:.2f})"

        return {
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        }
