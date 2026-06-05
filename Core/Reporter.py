import csv
import os
from datetime import datetime, timezone
from typing import Dict
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER

class Reporter:
    """
    Handles CSV logging of trades and performance.
    """
    def __init__(self):
        self.logger = AG_LOGGER
        self.log_dir = CONFIG['REPORTING']['LOG_DIR']
        self.trades_file = os.path.join(self.log_dir, "trades.csv")
        
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
            
        self._init_csv()

    def _init_csv(self):
        if not os.path.exists(self.trades_file):
            with open(self.trades_file, mode='w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(["Timestamp", "Symbol", "Side", "Quantity", "Price", "Result", "PnL", "Confidence", "Edge_Score", "Reason", "Risk_Tier"])

    def log_trade(self, symbol: str, side: str, qty: float, price: float, result: str = "OPEN", pnl: float = 0.0, confidence: float = 0.0, edge_score: float = 0.0, reason: str = "", risk_tier: str = "STANDARD"):
        """
        Log a trade event.
        """
        try:
            with open(self.trades_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    side,
                    qty,
                    price,
                    result,
                    pnl,
                    f"{confidence:.2f}",
                    f"{edge_score:.4f}",
                    reason,
                    risk_tier
                ])
            self.logger.info(f"Logged trade: {symbol} {side} @ {price} ({risk_tier})")
        except Exception as e:
            self.logger.error(f"Failed to log trade: {e}")
