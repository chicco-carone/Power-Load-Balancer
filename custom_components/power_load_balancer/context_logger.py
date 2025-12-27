"""
Logging utilities for the Power Load Balancer integration.

This module provides structured logging with operation tracking.
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class ContextLogger:
    """
    Logger with operation context for correlation tracking.

    Provides structured logging with automatic context injection,
    making it easier to trace operations across the integration.

    Attributes:
        _logger: The underlying Python logger.
        _component: Component name for context.
        _operation_id: Unique operation ID for correlation.

    """

    def __init__(self, logger: logging.Logger, component: str) -> None:
        """
        Initialize the context logger.

        Args:
            logger: The Python logger to wrap.
            component: Name of the component for context.

        """
        self._logger = logger
        self._component = component
        self._operation_id: str | None = None

    def new_operation(self, operation: str) -> ContextLogger:
        """
        Create a new operation context with unique ID.

        Args:
            operation: Name of the operation.

        Returns:
            A new ContextLogger instance with operation context.

        """
        new_logger = ContextLogger(self._logger, self._component)
        new_logger._operation_id = f"{operation}_{uuid.uuid4().hex[:8]}"
        return new_logger

    def _format_message(self, message: str, **kwargs: Any) -> str:
        """
        Format message with context.

        Args:
            message: The log message.
            **kwargs: Additional context key-value pairs.

        Returns:
            Formatted message with context prefix.

        """
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
        """Log exception message with context and traceback."""
        self._logger.exception(self._format_message(message, **kwargs))


def log_performance(
    logger: ContextLogger | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Return a decorator to log function execution time.

    Args:
        logger: Optional ContextLogger instance.

    Returns:
        A decorator function.

    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            perf_logger = logger if logger else ContextLogger(_LOGGER, "performance")

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
