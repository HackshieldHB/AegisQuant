from typing import Dict, List
from Data.Models import Candle
from Strategies.Strategy_Base import StrategyBase


class EMACrossStrategy(StrategyBase):
    """
    EMA Crossover Strategy (Trend Following)
    
    Logic:
    - BUY when EMA_9 crosses above EMA_21 (Golden Cross) with EMA_50 trend confirmation
    - SELL when EMA_9 crosses below EMA_21 (Death Cross) with EMA_50 trend confirmation
    - Confidence scales with EMA separation (wider gap = stronger trend)
    - Extra confirmation: Price must be on the correct side of EMA_50
    
    Weight: 1.2x
    """
    WEIGHT = 1.2

    def __init__(self):
        super().__init__("EMA_Cross")

    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        df = self.prepare_data(candles)
        
        required = ['EMA_9', 'EMA_21', 'EMA_50', 'close']
        if df.empty or not all(col in df.columns for col in required):
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient EMA data"}

        if len(df) < 2:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Not enough bars"}

        # Current values
        ema9_now = df['EMA_9'].iloc[-1]
        ema21_now = df['EMA_21'].iloc[-1]
        ema50_now = df['EMA_50'].iloc[-1]
        close = df['close'].iloc[-1]

        # Previous values (for crossover detection)
        ema9_prev = df['EMA_9'].iloc[-2]
        ema21_prev = df['EMA_21'].iloc[-2]

        # NaN check
        vals = [ema9_now, ema21_now, ema50_now, close, ema9_prev, ema21_prev]
        if any(v != v for v in vals):
            return {"signal": "HOLD", "confidence": 0.0, "reason": "NaN in EMA data"}

        # Detect crossovers
        golden_cross = (ema9_prev <= ema21_prev) and (ema9_now > ema21_now)
        death_cross = (ema9_prev >= ema21_prev) and (ema9_now < ema21_now)

        # Sustained trend (no fresh cross, but aligned)
        bullish_aligned = ema9_now > ema21_now
        bearish_aligned = ema9_now < ema21_now

        # EMA_50 trend direction
        trend_bullish = close > ema50_now
        trend_bearish = close < ema50_now

        # Calculate separation strength (% difference between EMA9 and EMA21)
        separation = abs(ema9_now - ema21_now) / ema21_now * 100

        signal = "HOLD"
        confidence = 0.5
        reason = f"EMA9: {ema9_now:.2f} | EMA21: {ema21_now:.2f} | EMA50: {ema50_now:.2f}"

        if golden_cross:
            # Fresh Golden Cross
            confidence = 0.60
            signal = "BUY"
            reason = f"Golden Cross (EMA9 > EMA21)"

            # Boost confidence if trend-aligned with EMA50
            if trend_bullish:
                confidence += 0.15
                reason += " + Trend Confirmed (> EMA50)"
            
            # Boost with separation strength
            confidence += min(separation * 0.02, 0.10)
            confidence = min(confidence, 0.93)

        elif death_cross:
            # Fresh Death Cross
            confidence = 0.60
            signal = "SELL"
            reason = f"Death Cross (EMA9 < EMA21)"

            if trend_bearish:
                confidence += 0.15
                reason += " + Trend Confirmed (< EMA50)"
            
            confidence += min(separation * 0.02, 0.10)
            confidence = min(confidence, 0.93)

        elif bullish_aligned and trend_bullish and separation > 0.3:
            # Strong sustained bullish trend (no fresh cross needed)
            confidence = min(0.50 + separation * 0.03, 0.72)
            signal = "BUY"
            reason = f"Sustained Bullish (Sep: {separation:.2f}%)"

        elif bearish_aligned and trend_bearish and separation > 0.3:
            # Strong sustained bearish trend
            confidence = min(0.50 + separation * 0.03, 0.72)
            signal = "SELL"
            reason = f"Sustained Bearish (Sep: {separation:.2f}%)"

        return {
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        }
