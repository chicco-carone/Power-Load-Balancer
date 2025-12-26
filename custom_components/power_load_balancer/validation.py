"""
Validation utilities for the Power Load Balancer integration.

This module provides validation functions for entities and power values.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .exceptions import (
    EntityNotFoundError,
    EntityUnavailableError,
    PowerSensorError,
    ValidationError,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)


def validate_entity_id(entity_id: str) -> None:
    """
    Validate entity ID format.

    Args:
        entity_id: The entity ID to validate.

    Raises:
        ValidationError: If entity ID is invalid.

    """
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
    """
    Validate and convert power value to float.

    Args:
        value: The power value to validate.
        entity_id: The entity ID for error context.

    Returns:
        The validated power value as float.

    Raises:
        PowerSensorError: If value is invalid or negative.

    """
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
    except (ValueError, TypeError) as exc:
        raise PowerSensorError(
            message=f"Cannot convert power value to float for entity {entity_id}",
            details={"entity_id": entity_id, "value": value, "error": str(exc)},
        ) from exc
    else:
        return power


def validate_entity_state(hass: HomeAssistant, entity_id: str) -> State:
    """
    Validate that entity exists and has a valid state.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity ID to validate.

    Returns:
        The entity state object.

    Raises:
        EntityNotFoundError: If entity does not exist.
        EntityUnavailableError: If entity is unavailable.

    """
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
    """
    Convert power value to watts based on unit of measurement.

    Args:
        power: The power value to convert.
        state: The entity state containing unit_of_measurement attribute.

    Returns:
        Power value in watts.

    """
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
