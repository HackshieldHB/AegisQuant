from typing import Dict, List
from Data.Models import Candle
from Strategies.Strategy_Base import StrategyBase
import numpy as np


class VWAPATRStrategy(StrategyBase):
    """
    Volume-Weighted Breakout Strategy (ATR + Volume Confirmation)
    
    Logic:
    - Detects when price moves beyond 1.5x ATR from EMA_21 average
    - Confirms breakout with above-average volume (volume spike)
    - Filters out low-conviction / noise moves
    - Confidence = volume_ratio * ATR_expansion
    
    Weight: 1.3x
    """
    WEIGHT = 1.3

    def __init__(self, atr_multiplier: float = 1.5, volume_lookback: int = 20):
        super().__init__("VWAP_ATR")
        self.atr_multiplier = atr_multiplier
        self.volume_lookback = volume_lookback

    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        df = self.prepare_data(candles)
        
        required = ['ATRr_14', 'EMA_21', 'close', 'volume']
        if df.empty or not all(col in df.columns for col in required):
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient ATR/Volume data"}

        if len(df) < self.volume_lookback:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Not enough history for volume analysis"}

        close = df['close'].iloc[-1]
        ema21 = df['EMA_21'].iloc[-1]
        atr = df['ATRr_14'].iloc[-1]
        current_volume = df['volume'].iloc[-1]
        
        # NaN check
        if any(v != v for v in [close, ema21, atr, current_volume]):
            return {"signal": "HOLD", "confidence": 0.0, "reason": "NaN in data"}

        if atr <= 0 or ema21 <= 0:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Invalid ATR/EMA values"}

        # Calculate average volume over lookback period
        avg_volume = df['volume'].iloc[-self.volume_lookback:].mean()
        if avg_volume <= 0:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "No volume data"}

        # Volume ratio (current vs average)
        volume_ratio = current_volume / avg_volume

        # Distance from EMA21 in ATR multiples
        distance = close - ema21
        atr_distance = abs(distance) / atr

        signal = "HOLD"
        confidence = 0.5
        reason = f"ATR Dist: {atr_distance:.2f}x | Vol Ratio: {volume_ratio:.2f}x"

        # --- BULLISH BREAKOUT ---
        if distance > 0 and atr_distance >= self.atr_multiplier and volume_ratio >= 1.3:
            # Price is significantly above EMA21 with high volume = breakout
            vol_boost = min((volume_ratio - 1.0) * 0.1, 0.15)
            atr_boost = min((atr_distance - self.atr_multiplier) * 0.05, 0.15)
            confidence = min(0.55 + vol_boost + atr_boost, 0.90)
            signal = "BUY"
            reason = f"Bullish Breakout ({atr_distance:.1f}x ATR, Vol: {volume_ratio:.1f}x)"

        # --- BEARISH BREAKOUT ---
        elif distance < 0 and atr_distance >= self.atr_multiplier and volume_ratio >= 1.3:
            vol_boost = min((volume_ratio - 1.0) * 0.1, 0.15)
            atr_boost = min((atr_distance - self.atr_multiplier) * 0.05, 0.15)
            confidence = min(0.55 + vol_boost + atr_boost, 0.90)
            signal = "SELL"
            reason = f"Bearish Breakdown ({atr_distance:.1f}x ATR, Vol: {volume_ratio:.1f}x)"

        # --- VOLUME SPIKE WARNING (no price breakout yet) ---
        elif volume_ratio >= 2.0:
            # Massive volume but no breakout yet = something is brewing
            # Don't trade, but log for awareness
            reason = f"Volume Spike ({volume_ratio:.1f}x avg) - Watching for Breakout"

        # --- QUIET MARKET ---
        elif volume_ratio < 0.5 and atr_distance < 0.5:
            reason = f"Low Activity (Vol: {volume_ratio:.1f}x, ATR Dist: {atr_distance:.1f}x)"

        return {
            "signal": signal,
            "confidence": confidence,
            "reason": reason
        }
