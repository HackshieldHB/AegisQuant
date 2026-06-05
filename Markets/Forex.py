import requests
import json
from typing import List, Dict
from datetime import datetime
from Markets.Base import MarketDataSource
from Data.Models import Candle
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER

class ForexMarket(MarketDataSource):
    def __init__(self):
        self.logger = AG_LOGGER
        self.api_key = CONFIG['BROKERS']['OANDA']['API_KEY']
        self.account_id = CONFIG['BROKERS']['OANDA']['ACCOUNT_ID']
        self.is_practice = CONFIG['BROKERS']['OANDA']['PRACTICE']
        
        if self.is_practice:
            self.base_url = "https://api-fxpractice.oanda.com/v3"
        else:
            self.base_url = "https://api-fxtrade.oanda.com/v3"
            
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        self.connect()

    def connect(self):
        # OANDA is REST-based, just verify creds or simple ping?
        # We'll fetch accounts to verify connection
        try:
            url = f"{self.base_url}/accounts"
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            self.logger.info(f"Connected to OANDA (Practice: {self.is_practice})")
        except Exception as e:
            self.logger.error(f"Failed to connect to OANDA: {e}")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[Candle]:
        try:
            # OANDA symbol format: EUR_USD
            # Timeframes need mapping? Config uses standard strings.
            # OANDA specific mapping might be needed if they differ. 
            # e.g. '1m' -> 'M1', '1h' -> 'H1'
            tf_map = {
                '1m': 'M1', '5m': 'M5', '15m': 'M15', '30m': 'M30',
                '1h': 'H1', '4h': 'H4', '1d': 'D1'
            }
            granularity = tf_map.get(timeframe, timeframe.upper())

            url = f"{self.base_url}/instruments/{symbol}/candles"
            params = {
                "granularity": granularity,
                "count": limit,
                "price": "M" # Midpoint candles
            }
            
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            candles = []
            for c in data.get('candles', []):
                if not c['complete']:
                    continue
                candles.append(Candle(
                    timestamp=datetime.strptime(c['time'].split('.')[0], "%Y-%m-%dT%H:%M:%S"), # ISO 8601
                    open=float(c['mid']['o']),
                    high=float(c['mid']['h']),
                    low=float(c['mid']['l']),
                    close=float(c['mid']['c']),
                    volume=float(c['volume'])
                ))
            return candles
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return []

    def get_ticker(self, symbol: str) -> Dict:
        try:
            url = f"{self.base_url}/accounts/{self.account_id}/pricing"
            params = {"instruments": symbol}
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            
            prices = response.json().get('prices', [])
            if prices:
                price = prices[0]
                return {
                    "bid": float(price['bids'][0]['price']),
                    "ask": float(price['asks'][0]['price']),
                    "last": (float(price['bids'][0]['price']) + float(price['asks'][0]['price'])) / 2
                }
            return {}
        except Exception as e:
            self.logger.error(f"Error fetching ticker for {symbol}: {e}")
            return {}

    def get_balance(self) -> Dict:
        try:
            url = f"{self.base_url}/accounts/{self.account_id}/summary"
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            account = response.json().get('account', {})
            return {
                "balance": float(account.get('balance', 0)),
                "unrealized_pl": float(account.get('unrealizedPL', 0)),
                "margin_used": float(account.get('marginUsed', 0)),
                "margin_available": float(account.get('marginAvailable', 0))
            }
        except Exception as e:
            self.logger.error(f"Error fetching balance: {e}")
            return {}
