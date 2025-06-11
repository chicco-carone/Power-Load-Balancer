"""Utility functions for the Power Load Balancer integration."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

from homeassistant.exceptions import HomeAssistantError

from .exceptions import (
    EntityNotFoundError,
    EntityUnavailableError,
    PowerSensorError,
    RetryableError,
    ServiceCallError,
    ServiceTimeoutError,
    ValidationError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant, State


_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class ContextLogger:
    """Logger with operation context for correlation tracking."""

    def __init__(self, logger: logging.Logger, component: str) -> None:
        """Initialize the context logger."""
        self._logger = logger
        self._component = component
        self._operation_id: str | None = None

    def new_operation(self, operation: str) -> ContextLogger:
        """Create a new operation context."""
        new_logger = ContextLogger(self._logger, self._component)
        new_logger._operation_id = f"{operation}_{uuid.uuid4().hex[:8]}"
        return new_logger

    def _format_message(self, message: str, **kwargs: Any) -> str:
        """Format message with context."""
        context_parts = [f"component={self._component}"]
        if self._operation_id:
            context_parts.append(f"operation_id={self._operation_id}")

        for key, value in kwargs.items():
            context_parts.append(f"{key}={value}")

        context = ", ".join(context_parts)
        return f"[{context}] {message}"

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message with context."""
        self._logger.debug(self._format_message(message, **kwargs))

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message with context."""
        self._logger.info(self._format_message(message, **kwargs))

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message with context."""
        self._logger.warning(self._format_message(message, **kwargs))

    def error(self, message: str, **kwargs: Any) -> None:
        """Log error message with context."""
        self._logger.error(self._format_message(message, **kwargs))

    def exception(self, message: str, **kwargs: Any) -> None:
        """Log exception message with context."""
        self._logger.exception(self._format_message(message, **kwargs))


def validate_entity_id(entity_id: str) -> None:
    """Validate entity ID format."""
    if not entity_id or not isinstance(entity_id, str):
        raise ValidationError(
            message="Entity ID must be a non-empty string",
            details={"entity_id": entity_id},
        )

    if "." not in entity_id:
        raise ValidationError(
            message=(
                "Entity ID must contain a domain and entity name separated by a dot"
            ),
            details={"entity_id": entity_id},
        )


def validate_power_value(value: Any, entity_id: str) -> float:
    """Validate and convert power value."""
    if value is None:
        raise PowerSensorError(
            message=f"Power value is None for entity {entity_id}",
            details={"entity_id": entity_id, "value": value},
        )

    try:
        power = float(value)
        if power < 0:
            raise PowerSensorError(
                message=f"Power value cannot be negative for entity {entity_id}",
                details={"entity_id": entity_id, "value": power},
            )
        return power
    except (ValueError, TypeError) as exc:
        raise PowerSensorError(
            message=f"Cannot convert power value to float for entity {entity_id}",
            details={"entity_id": entity_id, "value": value, "error": str(exc)},
        ) from exc


def validate_entity_state(hass: HomeAssistant, entity_id: str) -> State:
    """Validate that entity exists and has a valid state."""
    validate_entity_id(entity_id)

    state = hass.states.get(entity_id)
    if state is None:
        raise EntityNotFoundError(
            message=f"Entity {entity_id} not found", details={"entity_id": entity_id}
        )

    if state.state in ("unknown", "unavailable"):
        raise EntityUnavailableError(
            message=f"Entity {entity_id} is {state.state}",
            details={"entity_id": entity_id, "state": state.state},
        )

    return state


def convert_power_to_watts(power: float, state: State) -> float:
    """Convert power value to watts based on unit of measurement."""
    unit = state.attributes.get("unit_of_measurement", "W").lower()

    if unit in ["kw", "kilowatt", "kilowatts"]:
        return power * 1000
    if unit in ["mw", "megawatt", "megawatts"]:
        return power * 1000000
    if unit in ["gw", "gigawatt", "gigawatts"]:
        return power * 1000000000
    if unit in ["w", "watt", "watts"]:
        return power
    _LOGGER.warning(
        "Unknown power unit '%s' for entity %s, assuming watts",
        unit,
        state.entity_id,
    )
    return power


def retry_with_backoff(
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    max_delay: float = 60.0,
    retry_on: tuple[type[Exception], ...] = (RetryableError,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Return a decorator that retries async functions with exponential backoff."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    last_exception = exc

                    if not isinstance(exc, retry_on):
                        raise

                    if attempt == max_retries:
                        raise

                    delay = min(backoff_factor * (2**attempt), max_delay)

                    _LOGGER.warning(
                        "Attempt %d/%d failed for %s, retrying in %.2f seconds: %s",
                        attempt + 1,
                        max_retries + 1,
                        func.__name__,
                        delay,
                        str(exc),
                    )

                    await asyncio.sleep(delay)

            if last_exception:
                raise last_exception

            msg = "Unexpected error in retry logic"
            raise RuntimeError(msg)

        return wrapper

    return decorator


class ServiceCallParams(NamedTuple):
    """Parameters for a Home Assistant service call."""

    hass: HomeAssistant
    domain: str
    service: str
    service_data: dict[str, Any] | None = None
    logger: ContextLogger | None = None


async def safe_service_call(
    params: ServiceCallParams,
) -> None:
    """Perform a safe service call with timeout and error handling."""
    if params.logger is None:
        logger = ContextLogger(_LOGGER, "service_call")
    else:
        logger = params.logger

    service_data = params.service_data or {}
    timeout: float = 10.0

    try:
        logger.debug(
            "Calling service",
            domain=params.domain,
            service=params.service,
            service_data=service_data,
            timeout=timeout,
        )

        try:
            await asyncio.wait_for(
                params.hass.services.async_call(
                    params.domain, params.service, service_data, blocking=True
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise ServiceTimeoutError(
                message=(
                    f"Service call {params.domain}.{params.service} timed out after "
                    f"{timeout}s"
                ),
                details={
                    "domain": params.domain,
                    "service": params.service,
                    "service_data": service_data,
                    "timeout": timeout,
                },
            ) from exc

        logger.debug(
            "Service call completed successfully",
            domain=params.domain,
            service=params.service,
        )

    except HomeAssistantError as exc:
        raise ServiceCallError(
            message=f"Service call {params.domain}.{params.service} failed: {exc}",
            details={
                "domain": params.domain,
                "service": params.service,
                "service_data": service_data,
                "error": str(exc),
            },
        ) from exc

    except Exception as exc:
        raise ServiceCallError(
            message=(
                f"Unexpected error during service call "
                f"{params.domain}.{params.service}: {exc}"
            ),
            details={
                "domain": params.domain,
                "service": params.service,
                "service_data": service_data,
                "error": str(exc),
            },
        ) from exc


class CircuitBreaker:
    """Circuit breaker implementation for preventing cascading failures."""

    def __init__(
        self,
        failure_threshold: int = 5,
        timeout: float = 60.0,
        expected_exception: type[Exception] = Exception,
    ) -> None:
        """Initialize the circuit breaker."""
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state = "CLOSED"

    def _should_allow_request(self) -> bool:
        """Check if request should be allowed."""
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
        """Record a successful operation."""
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self) -> None:
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"

    def __call__(
        self, func: Callable[..., Awaitable[T]]
    ) -> Callable[..., Awaitable[T]]:
        """Return a decorator for circuit breaker."""

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            if not self._should_allow_request():
                from .exceptions import CircuitBreakerOpenError

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


def log_performance(
    logger: ContextLogger | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Return a decorator to log function performance."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            if logger is None:
                perf_logger = ContextLogger(_LOGGER, "performance")
            else:
                perf_logger = logger

            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                duration = time.time() - start_time
                perf_logger.warning(
                    "Function failed",
                    function=func.__name__,
                    duration_ms=round(duration * 1000, 2),
                    success=False,
                    error=str(exc),
                )
                raise
            else:
                duration = time.time() - start_time
                perf_logger.debug(
                    "Function completed",
                    function=func.__name__,
                    duration_ms=round(duration * 1000, 2),
                    success=True,
                )
                return result

        return wrapper

    return decorator
