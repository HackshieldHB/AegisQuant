import json
import threading
import time
import websocket
from typing import Dict, List, Callable, Optional
from datetime import datetime
from Core.Logger import AG_LOGGER
from Data.Models import Candle

class BinanceStream:
    """
    Manages a real-time WebSocket connection to Binance for K-line data.
    Stream URL: wss://stream.binance.com:9443/ws
    """
    def __init__(self, symbols: List[str], timeframe: str = "1m"):
        self.logger = AG_LOGGER
        self.symbols = [s.replace('/', '').lower() for s in symbols]
        self.timeframe = timeframe
        self.base_url = "wss://stream.binance.com:9443/ws"
        self.ws: Optional[websocket.WebSocketApp] = None
        self.wst: Optional[threading.Thread] = None
        self.running = False
        
        # Cache: { "BTCUSDT": Candle(...) }
        self.latest_candles: Dict[str, Candle] = {}
        self.lock = threading.Lock()

    def start(self):
        """Start the WebSocket in a separate thread."""
        streams = [f"{s}@kline_{self.timeframe}" for s in self.symbols]
        stream_str = "/".join(streams)
        url = f"{self.base_url}/{stream_str}"
        
        self.logger.info(f"Connecting to Binance Stream: {url}")
        
        self.ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        
        self.wst = threading.Thread(target=self.ws.run_forever)
        self.wst.daemon = True
        self.wst.start()
        self.running = True

    def stop(self):
        """Stop the WebSocket."""
        self.running = False
        if self.ws:
            self.ws.close()
        if self.wst:
            self.wst.join(timeout=2)
        self.logger.info("Binance Stream Stopped")

    def get_latest_candle(self, symbol: str) -> Optional[Candle]:
        """Thread-safe retrieval of the latest candle."""
        formatted = symbol.replace('/', '').lower()
        with self.lock:
            return self.latest_candles.get(formatted)

    def _on_message(self, ws, message):
        """
        Handle incoming JSON message.
        Format:
        {
          "e": "kline",     // Event type
          "E": 123456789,   // Event time
          "s": "BNBBTC",    // Symbol
          "k": {
            "t": 123400000, // Kline start time
            "T": 123460000, // Kline close time
            "s": "BNBBTC",  // Symbol
            "o": "0.0010",  // Open price
            "c": "0.0020",  // Close price
            "h": "0.0025",  // High price
            "l": "0.0015",  // Low price
            "v": "1000",    // Base asset volume
            ...
          }
        }
        """
        try:
            data = json.loads(message)
            if 'e' in data and data['e'] == 'kline':
                k = data['k']
                symbol = k['s'].lower()
                
                # Create Candle Object
                candle = Candle(
                    timestamp=datetime.utcfromtimestamp(k['t'] / 1000),
                    open=float(k['o']),
                    high=float(k['h']),
                    low=float(k['l']),
                    close=float(k['c']),
                    volume=float(k['v'])
                )
                
                with self.lock:
                    self.latest_candles[symbol] = candle
                    
                # Optional: Logging every update is too noisy
                # self.logger.debug(f"Stream update for {symbol}: {candle.close}")

        except Exception as e:
            self.logger.error(f"Stream Parse Error: {e}")

    def _on_error(self, ws, error):
        self.logger.error(f"Binance Stream Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self.logger.warning("Binance Stream Closed")
        # Reconnection logic could go here if needed, 
        # but run_watchdog.bat handles full restarts.

    def _on_open(self, ws):
        self.logger.info("Binance Stream Connected")
