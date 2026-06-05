import pandas as pd
import os
from datetime import datetime
from typing import List
from Data.Models import Candle
from AI.Model import RandomForestModel, AIModelBase
from Core.Logger import AG_LOGGER
from AegisQuantConfig import CONFIG

class ContinuousLearner:
    """
    Handles data accumulation and model retraining.
    """
    def __init__(self):
        self.logger = AG_LOGGER
        self.model = RandomForestModel() # Default model
        self.data_path = os.path.join("Data", "training_data.csv")
        self.min_samples_to_train = 500

    def save_training_data(self, history: List[Candle], indicators: pd.DataFrame, target: int):
        """
        Appends new trade outcome/market data to the training dataset.
        target: 1 (Profitable), 0 (Neutral/Loss) - simplified
        """
        if not history:
            return

        # Use the last candle/indicator state that led to this outcome
        # Ideally we capture the state at entry time. 
        # For simplicity, we assume 'indicators' dataframe aligns with history
        
        try:
            # Get last row of features
            last_row = indicators.iloc[-1].to_dict()
            last_row['target'] = target
            last_row['timestamp'] = history[-1].timestamp
            
            df = pd.DataFrame([last_row])
            
            # Append to CSV
            header = not os.path.exists(self.data_path)
            df.to_csv(self.data_path, mode='a', header=header, index=False)
            self.logger.info("Saved new training sample.")
            
        except Exception as e:
            self.logger.error(f"Failed to save training data: {e}")

    def retrain_model(self):
        """
        Retrains the model if enough data is available.
        """
        if not os.path.exists(self.data_path):
            self.logger.info("No training data found.")
            return

        try:
            df = pd.read_csv(self.data_path)
            if len(df) < self.min_samples_to_train:
                self.logger.info(f"Not enough data to retrain ({len(df)}/{self.min_samples_to_train})")
                return

            self.logger.info(f"Retraining model with {len(df)} samples...")
            self.model.train(df)
            self.logger.info("Model retraining complete.")
            
        except Exception as e:
            self.logger.error(f"Retraining failed: {e}")
