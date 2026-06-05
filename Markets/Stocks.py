import yfinance as yf
from typing import List, Dict
from datetime import datetime
from Markets.Base import MarketDataSource
from Data.Models import Candle
from Core.Logger import AG_LOGGER

class StockMarket(MarketDataSource):
    def __init__(self):
        self.logger = AG_LOGGER
        self.connect()

    def connect(self):
        # yfinance doesn't need explicit connection
        self.logger.info("Ready to fetch Stock data via yfinance")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[Candle]:
        try:
            # yfinance timeframes: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo
            # Map standard to yfinance
            tf = timeframe
            if timeframe == '1h': tf = '1h' # matches but explicit is good
            
            # Download recent data
            # Calculate period based on limit * timeframe to be efficient? 
            # yfinance 'period' is easier: '1d', '5d', '1mo'
            # let's just grab '5d' for intraday or '1mo' for others
            period = '5d'
            if timeframe in ['1d', '1wk']:
                period = '1y'
            elif timeframe in ['1h']:
                period = '1mo'
                
            df = yf.download(symbol, period=period, interval=tf, progress=False)
            
            if df.empty:
                return []
                
            # Tail limit
            df = df.tail(limit)
            
            candles = []
            for index, row in df.iterrows():
                # Index is datetime
                candles.append(Candle(
                    timestamp=index.to_pydatetime(),
                    open=float(row['Open']),
                    high=float(row['High']),
                    low=float(row['Low']),
                    close=float(row['Close']),
                    volume=float(row['Volume'])
                ))
            return candles
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return []

    def get_ticker(self, symbol: str) -> Dict:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            # yfinance info can be slow or inconsistent
            price = info.get('currentPrice') or info.get('regularMarketPrice')
            return {
                "bid": info.get('bid', price), 
                "ask": info.get('ask', price), 
                "last": price
            }
        except Exception as e:
            self.logger.error(f"Error fetching ticker for {symbol}: {e}")
            return {}

    def get_balance(self) -> Dict:
        # Stock trading via API is complex (often needs IBKR or Alpaca).
        # Since requirements said 'Yahoo Finance' which is data only usually,
        # we might assume paper trading for stocks or just data?
        # For now, return a separate dummy balance or not implemented.
        self.logger.warning("Stock balance fetch not implemented (Data Only mode)")
        return {}
