"""
Circuit breaker utilities for the Power Load Balancer integration.

This module provides circuit breaker pattern implementation for
preventing cascading failures.
"""

from __future__ import annotations

import functools
import time
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import CircuitBreakerOpenError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

T = TypeVar("T")


class CircuitBreaker:
    """
    Circuit breaker implementation for preventing cascading failures.

    The circuit breaker pattern prevents repeated calls to a failing service,
    allowing it time to recover.

    States:
        CLOSED: Normal operation, requests are allowed.
        OPEN: Failure threshold reached, requests are blocked.
        HALF_OPEN: Testing if service has recovered.

    Attributes:
        failure_threshold: Number of failures before opening circuit.
        timeout: Seconds to wait before trying again.
        expected_exception: Exception type to catch.

    """

    def __init__(
        self,
        failure_threshold: int = 5,
        timeout: float = 60.0,
        expected_exception: type[Exception] = Exception,
    ) -> None:
        """
        Initialize the circuit breaker.

        Args:
            failure_threshold: Number of failures to trigger open state.
            timeout: Seconds before transitioning from OPEN to HALF_OPEN.
            expected_exception: Exception type to track for failures.

        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state = "CLOSED"

    def _should_allow_request(self) -> bool:
        """Check if request should be allowed based on circuit state."""
        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            if (
                self.last_failure_time is not None
                and time.time() - self.last_failure_time >= self.timeout
            ):
                self.state = "HALF_OPEN"
                return True
            return False

        return True

    def record_success(self) -> None:
        """Record a successful operation, resetting the circuit."""
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self) -> None:
        """Record a failed operation, potentially opening the circuit."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"

    def __call__(
        self, func: Callable[..., Awaitable[T]]
    ) -> Callable[..., Awaitable[T]]:
        """
        Decorate an async function with circuit breaker protection.

        Args:
            func: The async function to protect.

        Returns:
            Wrapped function with circuit breaker logic.

        """

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            if not self._should_allow_request():
                raise CircuitBreakerOpenError(
                    message=f"Circuit breaker is OPEN for {func.__name__}",
                    details={
                        "failure_count": self.failure_count,
                        "last_failure_time": self.last_failure_time,
                        "timeout": self.timeout,
                    },
                )

            try:
                result = await func(*args, **kwargs)
            except self.expected_exception:
                self.record_failure()
                raise
            else:
                self.record_success()
                return result

        return wrapper
