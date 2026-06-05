from typing import Dict, List
from Data.Models import Candle
from Strategies.Strategy_Base import StrategyBase
from Core.Logger import AG_LOGGER


class EnsembleStrategy(StrategyBase):
    """
    Weighted Confidence Ensemble Strategy
    
    Aggregates signals from multiple strategies using weighted scoring:
    - Each strategy has a WEIGHT attribute (default 1.0)
    - Final confidence = weighted average of all confidences
    - Signal is determined by weighted vote, not simple majority
    - Agreement bonus: if 80%+ strategies agree, confidence gets a boost
    """
    def __init__(self, strategies: List[StrategyBase]):
        super().__init__("Ensemble")
        self.strategies = strategies
        self.logger.info(f"Ensemble initialized with {len(strategies)} strategies: "
                         f"{[s.name for s in strategies]}")

    def analyze(self, candles: List[Candle], extra_data: Dict = None) -> Dict:
        if not candles:
             return {"signal": "HOLD", "confidence": 0.0, "reason": "No data"}

        # Collect signals from all strategies
        results = []
        for strategy in self.strategies:
            try:
                result = strategy.analyze(candles, extra_data)
                weight = getattr(strategy, 'WEIGHT', 1.0)
                results.append({
                    "name": strategy.name,
                    "signal": result.get("signal", "HOLD"),
                    "confidence": result.get("confidence", 0.0),
                    "weight": weight,
                    "reason": result.get("reason", "")
                })
            except Exception as e:
                self.logger.error(f"Strategy {strategy.name} failed: {e}")

        if not results:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "All strategies failed"}

        # --- WEIGHTED SCORING ---
        buy_score = 0.0
        sell_score = 0.0
        hold_score = 0.0
        total_weight = 0.0

        for r in results:
            w = r['weight']
            total_weight += w
            
            if r['signal'] == "BUY":
                buy_score += r['confidence'] * w
            elif r['signal'] == "SELL":
                sell_score += r['confidence'] * w
            else:
                hold_score += r['confidence'] * w

        # Normalize scores
        buy_normalized = buy_score / total_weight if total_weight > 0 else 0
        sell_normalized = sell_score / total_weight if total_weight > 0 else 0

        # --- DETERMINE SIGNAL ---
        final_signal = "HOLD"
        final_confidence = 0.5
        
        # Count agreements
        buy_count = sum(1 for r in results if r['signal'] == "BUY")
        sell_count = sum(1 for r in results if r['signal'] == "SELL")
        total = len(results)

        if buy_normalized > sell_normalized and buy_normalized > 0.40:
            final_signal = "BUY"
            final_confidence = buy_normalized
        elif sell_normalized > buy_normalized and sell_normalized > 0.40:
            final_signal = "SELL"
            final_confidence = sell_normalized

        # --- AGREEMENT BONUS ---
        # If 80%+ of active (non-HOLD) strategies agree, boost confidence
        active_count = buy_count + sell_count
        if active_count > 0:
            if final_signal == "BUY" and buy_count >= active_count * 0.8:
                agreement_pct = buy_count / total
                final_confidence = min(final_confidence + agreement_pct * 0.10, 0.95)
            elif final_signal == "SELL" and sell_count >= active_count * 0.8:
                agreement_pct = sell_count / total
                final_confidence = min(final_confidence + agreement_pct * 0.10, 0.95)

        # --- CONFLICT PENALTY ---
        # If strategies are strongly split (close scores), reduce confidence
        if final_signal != "HOLD":
            score_diff = abs(buy_normalized - sell_normalized)
            if score_diff < 0.10:
                # Strong disagreement — reduce confidence or go HOLD
                final_confidence *= 0.7
                if final_confidence < 0.45:
                    final_signal = "HOLD"
                    final_confidence = 0.5

        # --- BUILD REASON ---
        details = [f"{r['name']}:{r['signal']}({r['confidence']:.2f})" for r in results]
        
        vote_summary = f"BUY:{buy_count} SELL:{sell_count} HOLD:{total - buy_count - sell_count}"
        reason = f"{vote_summary} | {', '.join(details)}"

        return {
            "signal": final_signal,
            "confidence": round(final_confidence, 4),
            "reason": reason,
            "votes": {"BUY": buy_count, "SELL": sell_count, "HOLD": total - buy_count - sell_count, "TOTAL": total},
            "strategy_results": results,  # per-strategy signals for alpha decay tracking
        }
