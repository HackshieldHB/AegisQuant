from typing import Dict, List
from Data.Models import Candle
from Strategies.Strategy_Base import StrategyBase


class BollingerStrategy(StrategyBase):
    """
    Bollinger Band Strategy (Mean Reversion)
    
    Logic:
    - BUY when price touches/breaks lower band (oversold bounce expected)
    - SELL when price touches/breaks upper band (overbought pullback expected)
    - Confirms with RSI for extra safety (avoids buying into a crash)
    - Confidence scales with distance from middle band
    
    Weight: 1.0x
    """
    WEIGHT = 1.0

    def __init__(self):
        super().__init__("Bollinger")

    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        df = self.prepare_data(candles)
        
        required = ['BBU_20_2.0', 'BBL_20_2.0', 'BBM_20_2.0', 'RSI', 'close']
        if df.empty or not all(col in df.columns for col in required):
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient BB data"}

        close = df['close'].iloc[-1]
        upper = df['BBU_20_2.0'].iloc[-1]
        lower = df['BBL_20_2.0'].iloc[-1]
        middle = df['BBM_20_2.0'].iloc[-1]
        rsi = df['RSI'].iloc[-1]
        
        # Check for NaN
        if any(v != v for v in [close, upper, lower, middle, rsi]):  # NaN check
            return {"signal": "HOLD", "confidence": 0.0, "reason": "NaN in BB indicators"}

        band_width = upper - lower
        if band_width <= 0:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Invalid band width"}

        signal = "HOLD"
        confidence = 0.5
        reason = f"BB: Close={close:.2f} Upper={upper:.2f} Lower={lower:.2f}"

        # --- OVERSOLD: Price at or below lower band ---
        if close <= lower:
            # How far below the lower band? (% of band width)
            penetration = (lower - close) / band_width
            
            # RSI confirmation: only buy if RSI also shows oversold (< 40)
            if rsi < 40:
                confidence = min(0.55 + penetration * 0.3 + (40 - rsi) / 100, 0.93)
                signal = "BUY"
                reason = f"BB Lower Touch + RSI Oversold ({rsi:.1f})"
            elif rsi < 50:
                # Weaker signal without strong RSI confirmation
                confidence = min(0.50 + penetration * 0.2, 0.70)
                signal = "BUY"
                reason = f"BB Lower Touch (RSI: {rsi:.1f})"

        # --- OVERBOUGHT: Price at or above upper band ---
        elif close >= upper:
            penetration = (close - upper) / band_width
            
            if rsi > 60:
                confidence = min(0.55 + penetration * 0.3 + (rsi - 60) / 100, 0.93)
                signal = "SELL"
                reason = f"BB Upper Touch + RSI Overbought ({rsi:.1f})"
            elif rsi > 50:
                confidence = min(0.50 + penetration * 0.2, 0.70)
                signal = "SELL"
                reason = f"BB Upper Touch (RSI: {rsi:.1f})"

        # --- SQUEEZE DETECTION: Narrow bands predict breakout ---
        else:
            # Calculate band width as % of middle
            bb_pct = band_width / middle * 100
            if bb_pct < 2.0:  # Very tight squeeze
                # No trade, but log awareness
                reason = f"BB Squeeze Detected ({bb_pct:.1f}%) - Breakout Imminent"

        return {
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        }
