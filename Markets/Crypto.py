import ccxt
import time
from typing import List, Dict
from datetime import datetime
from Markets.Base import MarketDataSource
from Data.Models import Candle
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER

class CryptoMarket(MarketDataSource):
    def __init__(self):
        self.logger = AG_LOGGER
        self.exchange = None
        self.connect()

    def connect(self):
        try:
            exchange_id = 'binance'
            exchange_class = getattr(ccxt, exchange_id)
            self.exchange = exchange_class({
                'apiKey': CONFIG['BROKERS']['BINANCE']['API_KEY'],
                'secret': CONFIG['BROKERS']['BINANCE']['SECRET'],
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'spot',  # Changed from 'future' to 'spot' for standard trading account
                    # Load only spot markets — prevents CCXT from calling
                    # /sapi/v1/margin/allPairs which requires margin permissions.
                    'fetchMarkets': ['spot'],
                    # Do NOT fetch currencies (/sapi/v1/capital/config/getall)
                    # — requires withdrawal permissions this key doesn't have.
                    'fetchCurrencies': False,
                }
            })
            if CONFIG['BROKERS']['BINANCE']['TESTNET']:
                self.exchange.set_sandbox_mode(True)

            self.exchange.load_markets()
            self.logger.info(f"Connected to {exchange_id} (Testnet: {CONFIG['BROKERS']['BINANCE']['TESTNET']})")

            # NOTE: Removed sanity check - fetch_balance() during init can fail if API key has IP restrictions
            # Balance will be verified when actual trades are attempted

        except Exception as e:
            self.logger.error(f"Failed to connect to Binance: {e}")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[Candle]:
        try:
            if '/' not in symbol:
                symbol = symbol.replace('USDT', '/USDT')
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            candles = []
            for candle in ohlcv:
                candles.append(Candle(
                    timestamp=datetime.fromtimestamp(candle[0] / 1000),
                    open=float(candle[1]),
                    high=float(candle[2]),
                    low=float(candle[3]),
                    close=float(candle[4]),
                    volume=float(candle[5])
                ))
            return candles
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return []

    def get_ticker(self, symbol: str) -> Dict:
        try:
            if '/' not in symbol:
                symbol = symbol.replace('USDT', '/USDT')
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                "bid": ticker['bid'],
                "ask": ticker['ask'],
                "last": ticker['last']
            }
        except Exception as e:
            self.logger.error(f"Error fetching ticker for {symbol}: {e}")
            return {}

    def get_balance(self) -> Dict:
        try:
            balance = self.exchange.fetch_balance({'type': self.exchange.options.get('defaultType', 'spot')})
            return balance
        except Exception as e:
            self.logger.error(f"Error fetching balance: {e}")
            return {}