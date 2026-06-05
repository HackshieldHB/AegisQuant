from typing import Dict, List
from Data.Models import Candle
from Strategies.Strategy_Base import StrategyBase


class MACDStrategy(StrategyBase):
    """
    MACD Crossover Strategy (Momentum / Trend Confirmation)
    
    Logic:
    - BUY when MACD crosses above Signal line AND histogram is positive
    - SELL when MACD crosses below Signal line AND histogram is negative
    - Confidence scales with histogram magnitude (stronger momentum = higher confidence)
    
    Weight: 1.5x (MACD is a strong trend indicator)
    """
    WEIGHT = 1.5

    def __init__(self):
        super().__init__("MACD")

    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        df = self.prepare_data(candles)
        
        required = ['MACD_12_26_9', 'MACDs_12_26_9', 'MACDh_12_26_9']
        if df.empty or not all(col in df.columns for col in required):
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient MACD data"}

        # Need at least 2 rows to detect crossover
        if len(df) < 2:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Not enough bars"}

        # Current and previous values
        macd_now = df['MACD_12_26_9'].iloc[-1]
        signal_now = df['MACDs_12_26_9'].iloc[-1]
        hist_now = df['MACDh_12_26_9'].iloc[-1]

        macd_prev = df['MACD_12_26_9'].iloc[-2]
        signal_prev = df['MACDs_12_26_9'].iloc[-2]

        # Detect crossover
        bullish_cross = (macd_prev <= signal_prev) and (macd_now > signal_now)
        bearish_cross = (macd_prev >= signal_prev) and (macd_now < signal_now)

        # Current trend confirmation (histogram direction)
        hist_positive = hist_now > 0
        hist_negative = hist_now < 0

        signal = "HOLD"
        confidence = 0.5
        reason = f"MACD: {macd_now:.4f} | Signal: {signal_now:.4f} | Hist: {hist_now:.4f}"

        if bullish_cross and hist_positive:
            # Fresh bullish crossover with positive momentum
            price = df['close'].iloc[-1]
            hist_strength = abs(hist_now) / price * 1000  # Normalize
            confidence = min(0.55 + hist_strength * 0.1, 0.92)
            signal = "BUY"
            reason = f"Bullish MACD Cross (Hist: {hist_now:.4f})"

        elif bearish_cross and hist_negative:
            # Fresh bearish crossover with negative momentum
            price = df['close'].iloc[-1]
            hist_strength = abs(hist_now) / price * 1000
            confidence = min(0.55 + hist_strength * 0.1, 0.92)
            signal = "SELL"
            reason = f"Bearish MACD Cross (Hist: {hist_now:.4f})"

        elif hist_positive and macd_now > signal_now:
            # Sustained bullish momentum (no fresh cross but strong trend)
            price = df['close'].iloc[-1]
            hist_strength = abs(hist_now) / price * 1000
            if hist_strength > 0.5:  # Only if momentum is significant
                confidence = min(0.50 + hist_strength * 0.05, 0.75)
                signal = "BUY"
                reason = f"Sustained Bullish MACD (Hist: {hist_now:.4f})"

        elif hist_negative and macd_now < signal_now:
            # Sustained bearish momentum
            price = df['close'].iloc[-1]
            hist_strength = abs(hist_now) / price * 1000
            if hist_strength > 0.5:
                confidence = min(0.50 + hist_strength * 0.05, 0.75)
                signal = "SELL"
                reason = f"Sustained Bearish MACD (Hist: {hist_now:.4f})"

        return {
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        }
