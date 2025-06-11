"""Exception classes for the Power Load Balancer integration."""

from __future__ import annotations

from typing import Any


class PowerLoadBalancerError(Exception):
    """Base exception for Power Load Balancer integration."""

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the exception."""
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}

    def __str__(self) -> str:
        """Return string representation of the exception."""
        if self.details:
            return f"{self.message} (Code: {self.error_code}, Details: {self.details})"
        return f"{self.message} (Code: {self.error_code})"


class ConfigurationError(PowerLoadBalancerError):
    """Exception raised for configuration-related errors."""


class EntityNotFoundError(PowerLoadBalancerError):
    """Exception raised when a required entity is not found."""


class EntityUnavailableError(PowerLoadBalancerError):
    """Exception raised when an entity is unavailable."""


class InvalidStateError(PowerLoadBalancerError):
    """Exception raised when an entity has an invalid state."""


class ServiceCallError(PowerLoadBalancerError):
    """Exception raised when a Home Assistant service call fails."""


class ServiceTimeoutError(ServiceCallError):
    """Exception raised when a service call times out."""


class PowerSensorError(PowerLoadBalancerError):
    """Exception raised for power sensor-related errors."""


class ApplianceControlError(PowerLoadBalancerError):
    """Exception raised for appliance control errors."""


class BalancingError(PowerLoadBalancerError):
    """Exception raised during power balancing operations."""


class ValidationError(PowerLoadBalancerError):
    """Exception raised for validation errors."""


class RetryableError(PowerLoadBalancerError):
    """Base class for errors that can be retried."""


class NonRetryableError(PowerLoadBalancerError):
    """Base class for errors that should not be retried."""


class CircuitBreakerOpenError(NonRetryableError):
    """Exception raised when circuit breaker is open."""


class RateLimitError(RetryableError):
    """Exception raised when rate limit is exceeded."""
