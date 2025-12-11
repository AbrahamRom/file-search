"""
Circuit Breaker implementation for fault tolerance.

Implements the Circuit Breaker pattern to prevent cascading failures
when Storage Nodes become unavailable. Provides automatic failover
and recovery.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, TypeVar, Any

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open and requests are rejected."""
    def __init__(self, name: str, until: float):
        self.name = name
        self.until = until
        remaining = max(0, until - time.time())
        super().__init__(
            f"Circuit breaker '{name}' is open. Retry in {remaining:.1f}s"
        )


@dataclass
class CircuitBreaker:
    """
    Circuit Breaker for protecting against cascading failures.
    
    Usage:
        breaker = CircuitBreaker(name="storage_1")
        
        async with breaker:
            result = await storage_client.call()
    
    Configuration:
        - failure_threshold: Number of failures before opening
        - recovery_timeout: Seconds to wait before attempting recovery
        - half_open_max_calls: Max calls allowed in half-open state
    """
    
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3
    
    # Internal state
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: Optional[float] = field(default=None, init=False)
    _half_open_calls: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    
    @property
    def state(self) -> CircuitState:
        """Get current circuit state, checking for recovery."""
        if self._state == CircuitState.OPEN:
            if self._should_attempt_recovery():
                return CircuitState.HALF_OPEN
        return self._state
    
    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED
    
    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN
    
    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self._last_failure_time is None:
            return True
        return time.time() - self._last_failure_time >= self.recovery_timeout
    
    async def _transition_to(self, new_state: CircuitState):
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state
        
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
            
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            
        logger.info(
            "Circuit breaker '%s': %s -> %s",
            self.name,
            old_state.value,
            new_state.value,
        )
    
    async def record_success(self):
        """Record a successful call."""
        async with self._lock:
            self._success_count += 1
            
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1
                # If we've had enough successful calls, close the circuit
                if self._half_open_calls >= self.half_open_max_calls:
                    await self._transition_to(CircuitState.CLOSED)
    
    async def record_failure(self, error: Optional[Exception] = None):
        """Record a failed call."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if error:
                logger.warning(
                    "Circuit breaker '%s': failure #%d - %s",
                    self.name,
                    self._failure_count,
                    str(error)[:100],
                )
            
            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open state opens the circuit
                await self._transition_to(CircuitState.OPEN)
                
            elif self._state == CircuitState.CLOSED:
                # Check if we've reached the threshold
                if self._failure_count >= self.failure_threshold:
                    await self._transition_to(CircuitState.OPEN)
    
    async def __aenter__(self):
        """Check if request should proceed."""
        state = self.state
        
        if state == CircuitState.OPEN:
            # Check if we should transition to half-open
            if self._should_attempt_recovery():
                async with self._lock:
                    await self._transition_to(CircuitState.HALF_OPEN)
            else:
                raise CircuitBreakerOpen(
                    self.name,
                    self._last_failure_time + self.recovery_timeout,
                )
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Record result of the call."""
        if exc_type is None:
            await self.record_success()
        else:
            await self.record_failure(exc_val)
        
        # Don't suppress exceptions
        return False
    
    def get_status(self) -> dict:
        """Get current status for monitoring."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time,
            "recovery_timeout": self.recovery_timeout,
        }
    
    async def reset(self):
        """Manually reset the circuit breaker."""
        async with self._lock:
            await self._transition_to(CircuitState.CLOSED)
            logger.info("Circuit breaker '%s' manually reset", self.name)


class CircuitBreakerRegistry:
    """
    Registry for managing multiple circuit breakers.
    Each Storage Node gets its own circuit breaker.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._lock = asyncio.Lock()
    
    async def get_breaker(self, name: str) -> CircuitBreaker:
        """Get or create a circuit breaker for the given name."""
        async with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(
                    name=name,
                    failure_threshold=self._failure_threshold,
                    recovery_timeout=self._recovery_timeout,
                )
            return self._breakers[name]
    
    def get_all_status(self) -> dict:
        """Get status of all circuit breakers."""
        return {
            name: breaker.get_status()
            for name, breaker in self._breakers.items()
        }
    
    async def reset_all(self):
        """Reset all circuit breakers."""
        for breaker in self._breakers.values():
            await breaker.reset()


# Global registry instance
_registry: Optional[CircuitBreakerRegistry] = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Get the global circuit breaker registry."""
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpen",
    "CircuitState",
    "CircuitBreakerRegistry",
    "get_circuit_breaker_registry",
]
