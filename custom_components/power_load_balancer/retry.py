"""
Retry utilities for the Power Load Balancer integration.

This module provides retry decorators with exponential backoff.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import RetryableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def retry_with_backoff(
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    max_delay: float = 60.0,
    retry_on: tuple[type[Exception], ...] = (RetryableError,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Return a decorator that retries async functions with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_factor: Base delay multiplier for exponential backoff.
        max_delay: Maximum delay between retries in seconds.
        retry_on: Tuple of exception types to retry on.

    Returns:
        A decorator function.

    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Exception | None = None

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
