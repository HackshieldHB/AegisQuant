"""
StateManager — Persistent state and recovery system.
====================================================

Persists system state across restarts:
- Last known balance
- Open positions
- Trade history
- Risk metrics
- System events

Enables graceful recovery on restart:
- Restores last balance
- Queries exchange for open positions
- Resumes monitoring
- Reconciles discrepancies
- Prevents duplicate trades

State stored in JSON files in logs/ directory.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER


class StateManager:
    """Manages persistent system state across restarts."""
    
    STATE_DIR = os.path.join(CONFIG["REPORTING"]["LOG_DIR"], "state")
    
    # State file names
    BALANCE_FILE = "balance.json"
    POSITIONS_FILE = "positions.json"
    TRADES_FILE = "trades.json"
    METRICS_FILE = "metrics.json"
    EVENTS_FILE = "events.json"
    
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self.state_dir = self.STATE_DIR
        os.makedirs(self.state_dir, exist_ok=True)
        self._ensure_state_files()

    def _ensure_state_files(self) -> None:
        """Ensure all state files exist."""
        for filename in [self.BALANCE_FILE, self.POSITIONS_FILE, self.TRADES_FILE, self.METRICS_FILE, self.EVENTS_FILE]:
            path = os.path.join(self.state_dir, filename)
            if not os.path.exists(path):
                try:
                    with open(path, "w") as f:
                        if filename == self.BALANCE_FILE:
                            json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "balance": 0.0}, f)
                        else:
                            json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "data": []}, f)
                except Exception as e:
                    self.logger.warning(f"Failed to create state file {filename}: {e}")

    def save_balance(self, balance: float) -> None:
        """Save current balance to persistent storage."""
        try:
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "balance": balance,
            }
            path = os.path.join(self.state_dir, self.BALANCE_FILE)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            self.logger.warning(f"Failed to save balance state: {e}")

    def load_balance(self) -> Optional[float]:
        """Load last known balance from persistent storage."""
        try:
            path = os.path.join(self.state_dir, self.BALANCE_FILE)
            if not os.path.exists(path):
                return None
            with open(path, "r") as f:
                data = json.load(f)
                balance = data.get("balance", 0.0)
                self.logger.info(f"Loaded balance from state: {balance}")
                return balance
        except Exception as e:
            self.logger.warning(f"Failed to load balance state: {e}")
            return None

    def save_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Save current open positions."""
        try:
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "count": len(positions),
                "positions": positions,
            }
            path = os.path.join(self.state_dir, self.POSITIONS_FILE)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save positions state: {e}")

    def load_positions(self) -> List[Dict[str, Any]]:
        """Load last known open positions."""
        try:
            path = os.path.join(self.state_dir, self.POSITIONS_FILE)
            if not os.path.exists(path):
                return []
            with open(path, "r") as f:
                data = json.load(f)
                positions = data.get("positions", [])
                self.logger.info(f"Loaded {len(positions)} positions from state")
                return positions
        except Exception as e:
            self.logger.warning(f"Failed to load positions state: {e}")
            return []

    def append_trade(self, trade: Dict[str, Any]) -> None:
        """Append a trade to the persistent trade history."""
        try:
            path = os.path.join(self.state_dir, self.TRADES_FILE)
            trades = []
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                        trades = data.get("trades", [])
                except Exception:
                    trades = []
            
            trades.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **trade
            })
            
            # Keep only last 10000 trades to avoid huge files
            if len(trades) > 10000:
                trades = trades[-10000:]
            
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "count": len(trades),
                "trades": trades,
            }
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            self.logger.warning(f"Failed to save trade: {e}")

    def load_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Load recent trades from history."""
        try:
            path = os.path.join(self.state_dir, self.TRADES_FILE)
            if not os.path.exists(path):
                return []
            with open(path, "r") as f:
                data = json.load(f)
                trades = data.get("trades", [])
                # Return most recent trades
                return trades[-limit:] if limit > 0 else trades
        except Exception as e:
            self.logger.warning(f"Failed to load trades: {e}")
            return []

    def save_metrics(self, metrics: Dict[str, Any]) -> None:
        """Save current risk/performance metrics."""
        try:
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **metrics
            }
            path = os.path.join(self.state_dir, self.METRICS_FILE)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save metrics: {e}")

    def load_metrics(self) -> Dict[str, Any]:
        """Load last known metrics."""
        try:
            path = os.path.join(self.state_dir, self.METRICS_FILE)
            if not os.path.exists(path):
                return {}
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.warning(f"Failed to load metrics: {e}")
            return {}

    def log_event(self, event_type: str, message: str, severity: str = "INFO") -> None:
        """Log a system event."""
        try:
            path = os.path.join(self.state_dir, self.EVENTS_FILE)
            events = []
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                        events = data.get("events", [])
                except Exception:
                    events = []
            
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": event_type,
                "severity": severity,
                "message": message,
            }
            events.append(event)
            
            # Keep only last 10000 events
            if len(events) > 10000:
                events = events[-10000:]
            
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "count": len(events),
                "events": events,
            }
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            self.logger.warning(f"Failed to log event: {e}")

    def load_recent_events(self, limit: int = 100, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load recent system events."""
        try:
            path = os.path.join(self.state_dir, self.EVENTS_FILE)
            if not os.path.exists(path):
                return []
            with open(path, "r") as f:
                data = json.load(f)
                events = data.get("events", [])
                if event_type:
                    events = [e for e in events if e.get("type") == event_type]
                return events[-limit:] if limit > 0 else events
        except Exception as e:
            self.logger.warning(f"Failed to load events: {e}")
            return []

    def get_state_summary(self) -> Dict[str, Any]:
        """Return a summary of the current state."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance": self.load_balance(),
            "open_positions": len(self.load_positions()),
            "recent_trades": len(self.load_trades(limit=10)),
            "state_dir": self.state_dir,
        }
