"""
CircuitBreaker — Prevent cascading failures from repeated API errors.
---------------------------------------------------------------------
Tracks consecutive failures per endpoint and halts requests if threshold exceeded.
"""

import time
from typing import Dict, Callable
from datetime import datetime, timezone

from Core.Logger import AG_LOGGER


class CircuitBreaker:
    """Prevents repeated calls to failing endpoints."""
    
    FAILURE_THRESHOLD = 5  # Fail after 5 consecutive errors
    RESET_TIMEOUT_SEC = 300  # Reset after 5 minutes of no calls
    
    def __init__(self, name: str, failure_threshold: int = FAILURE_THRESHOLD):
        self.logger = AG_LOGGER
        self.name = name
        self.failure_threshold = failure_threshold
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.last_call_time = 0.0
        self.is_open = False  # True = circuit open (not calling endpoint)
    
    def call(self, func: Callable, *args, **kwargs):
        """
        Execute func with circuit breaker protection.
        Raises RuntimeError if circuit is open.
        """
        now = time.time()
        
        # Auto-reset if no failure for RESET_TIMEOUT_SEC
        # Use last_failure_time (not last_call_time) so repeated checks on an open
        # circuit don't keep restarting the timer and trapping it open forever.
        if self.last_failure_time > 0 and now - self.last_failure_time > self.RESET_TIMEOUT_SEC:
            self.reset()
        
        self.last_call_time = now
        
        if self.is_open:
            raise RuntimeError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"{self.failure_count} consecutive failures. "
                f"Auto-reset in {self.RESET_TIMEOUT_SEC}s."
            )
        
        try:
            result = func(*args, **kwargs)
            self.on_success()
            return result
        except Exception as e:
            self.on_failure(str(e))
            raise
    
    def on_success(self) -> None:
        """Reset failure count on success."""
        if self.failure_count > 0:
            self.logger.info("Circuit breaker '%s' recovered after %d failures", self.name, self.failure_count)
        self.failure_count = 0
        self.is_open = False
    
    def on_failure(self, error: str) -> None:
        """Increment failure count and open circuit if threshold exceeded."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            self.logger.critical(
                "Circuit breaker '%s' OPENED after %d failures: %s",
                self.name, self.failure_count, error
            )
        else:
            self.logger.warning(
                "Circuit breaker '%s' failure %d/%d: %s",
                self.name, self.failure_count, self.failure_threshold, error
            )
    
    def reset(self) -> None:
        """Manually reset circuit breaker."""
        self.failure_count = 0
        self.is_open = False
        self.logger.info("Circuit breaker '%s' manually reset", self.name)
    
    def status(self) -> Dict:
        """Return current status."""
        return {
            "name": self.name,
            "is_open": self.is_open,
            "failure_count": self.failure_count,
            "last_failure": datetime.fromtimestamp(self.last_failure_time, tz=timezone.utc).isoformat()
                           if self.last_failure_time else None,
        }
