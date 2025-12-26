"""
Service call utilities for the Power Load Balancer integration.

This module provides safe service call functionality with timeout and error handling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, NamedTuple

try:
    from homeassistant.exceptions import HomeAssistantError
except ImportError:

    class HomeAssistantError(Exception):
        """Local stub for Home Assistant's HomeAssistantError."""


from .context_logger import ContextLogger
from .exceptions import ServiceCallError, ServiceTimeoutError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class ServiceCallParams(NamedTuple):
    """
    Parameters for a Home Assistant service call.

    Attributes:
        hass: Home Assistant instance.
        domain: Service domain (e.g., 'switch', 'light').
        service: Service name (e.g., 'turn_on', 'turn_off').
        service_data: Optional data to pass to the service.
        logger: Optional ContextLogger for logging.

    """

    hass: HomeAssistant
    domain: str
    service: str
    service_data: dict[str, Any] | None = None
    logger: ContextLogger | None = None


async def safe_service_call(params: ServiceCallParams) -> None:
    """
    Perform a safe service call with timeout and error handling.

    Args:
        params: Service call parameters.

    Raises:
        ServiceTimeoutError: If service call times out.
        ServiceCallError: If service call fails.

    """
    logger = params.logger if params.logger else ContextLogger(_LOGGER, "service_call")
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
