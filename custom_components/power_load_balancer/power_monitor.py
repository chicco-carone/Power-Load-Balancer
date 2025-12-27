"""
Power monitoring module for the Power Load Balancer integration.

This module handles all power sensor tracking, state changes, and power calculations.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_ENTITY_ID

from .const import CONF_APPLIANCE
from .context_logger import ContextLogger
from .exceptions import PowerSensorError
from .validation import convert_power_to_watts, validate_power_value

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class PowerMonitor:
    """
    Manages power sensor monitoring and state tracking.

    This class is responsible for tracking power consumption from the main sensor
    and individual appliance sensors, handling state changes, and maintaining
    power consumption estimates.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        main_power_sensor_entity_id: str,
        monitored_sensors: list[dict[str, Any]],
        power_budget: int,
    ) -> None:
        """
        Initialize the PowerMonitor.

        Args:
            hass: Home Assistant instance.
            main_power_sensor_entity_id: Entity ID of the main power sensor.
            monitored_sensors: List of monitored sensor configurations.
            power_budget: Maximum power budget in watts.

        """
        self.hass = hass
        self._main_power_sensor_entity_id = main_power_sensor_entity_id
        self._monitored_sensors = monitored_sensors
        self._power_budget = power_budget
        self._current_sensor_power: dict[str, float] = {}
        self._estimated_total_power: float = 0.0

    def initialize_power_tracking(self) -> None:
        """Initialize power tracking by reading initial sensor states."""
        self._current_sensor_power = {}

        main_sensor_state = self.hass.states.get(self._main_power_sensor_entity_id)
        if main_sensor_state is not None and main_sensor_state.state not in (
            "unknown",
            "unavailable",
        ):
            try:
                raw_power = float(main_sensor_state.state)
                self._estimated_total_power = convert_power_to_watts(
                    raw_power, main_sensor_state
                )
                _LOGGER.debug(
                    "Initial main power sensor %s: %s W (raw: %s %s)",
                    self._main_power_sensor_entity_id,
                    self._estimated_total_power,
                    raw_power,
                    main_sensor_state.attributes.get("unit_of_measurement", "W"),
                )
            except (ValueError, TypeError):
                self._estimated_total_power = 0.0
                _LOGGER.warning(
                    "Could not convert state for main power sensor %s to float: %s",
                    self._main_power_sensor_entity_id,
                    main_sensor_state.state,
                )
        else:
            self._estimated_total_power = 0.0

        for sensor_config in self._monitored_sensors:
            sensor_entity_id = sensor_config[CONF_ENTITY_ID]
            state = self.hass.states.get(sensor_entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    raw_power = float(state.state)
                    power = convert_power_to_watts(raw_power, state)
                    self._current_sensor_power[sensor_entity_id] = power
                    _LOGGER.debug(
                        "Initial power for %s: %s W (raw: %s %s)",
                        sensor_entity_id,
                        power,
                        raw_power,
                        state.attributes.get("unit_of_measurement", "W"),
                    )
                except (ValueError, TypeError):
                    _LOGGER.warning(
                        "Could not convert state for sensor %s to float: %s",
                        sensor_entity_id,
                        state.state,
                    )

    async def handle_power_sensor_state_change(
        self, event: Any, on_change_callback: Any
    ) -> None:
        """
        Handle state changes for power sensors.

        Args:
            event: The state change event from Home Assistant.
            on_change_callback: Callback to execute after processing state change.

        """
        logger = ContextLogger(_LOGGER, "power_sensor").new_operation("state_change")

        try:
            if not hasattr(event, "data") or not isinstance(event.data, dict):
                logger.warning("Event data is not a dictionary or missing", event=event)
                return

            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")

            if not entity_id or not isinstance(entity_id, str):
                logger.warning("Invalid entity_id in event", entity_id=entity_id)
                return

            if new_state is None or new_state.state in ("unknown", "unavailable"):
                logger.debug("Ignoring invalid state for sensor", entity_id=entity_id)
                if entity_id in self._current_sensor_power:
                    self._current_sensor_power.pop(entity_id, None)
                return

            try:
                power = validate_power_value(new_state.state, entity_id)
                power_watts = convert_power_to_watts(power, new_state)

                logger.debug(
                    "Power sensor changed",
                    entity_id=entity_id,
                    power_watts=power_watts,
                    raw_power=power,
                    unit=new_state.attributes.get("unit_of_measurement", "W"),
                )

            except PowerSensorError as exc:
                logger.warning(
                    "Failed to process power value for sensor",
                    entity_id=entity_id,
                    error=str(exc),
                )
                return

            if entity_id == self._main_power_sensor_entity_id:
                self._estimated_total_power = power_watts
                logger.debug(
                    "Updated estimated total power",
                    total_power=self._estimated_total_power,
                )
            else:
                self._current_sensor_power[entity_id] = power_watts

            on_change_callback()

        except Exception as exc:
            logger.exception("Unexpected error in power sensor state change handler")
            msg = f"Failed to handle power sensor state change: {exc}"
            raise PowerSensorError(
                msg,
                details={"error": str(exc)},
            ) from exc

    def calculate_sensor_power(self, sensor_config: dict[str, Any]) -> float:
        """
        Calculate the current power consumption for a sensor.

        Args:
            sensor_config: Configuration dictionary for the sensor.

        Returns:
            Power consumption in watts, or 0.0 if unavailable.

        """
        sensor_id = sensor_config.get(CONF_ENTITY_ID)
        if not sensor_id:
            return 0.0

        sensor_state = self.hass.states.get(sensor_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return 0.0

        try:
            raw_power = float(sensor_state.state)
            current_power = convert_power_to_watts(raw_power, sensor_state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Could not convert state for sensor %s to float: %s",
                sensor_id,
                sensor_state.state,
            )
            return 0.0
        else:
            return current_power if current_power > 0 else 0.0

    def would_exceed_budget(self, power_to_add: float) -> bool:
        """
        Check if adding the given power would exceed the budget.

        Args:
            power_to_add: Power in watts to potentially add.

        Returns:
            True if adding the power would exceed the budget, False otherwise.

        """
        return (self._estimated_total_power + power_to_add) > self._power_budget

    def update_power_estimates(
        self, sensor_config: dict[str, Any], power_to_add: float
    ) -> None:
        """
        Update the power estimates with the new power value.

        Args:
            sensor_config: Configuration dictionary for the sensor.
            power_to_add: Power in watts to add to the estimate.

        """
        self._estimated_total_power += power_to_add
        sensor_id = sensor_config.get(CONF_ENTITY_ID)
        if sensor_id:
            self._current_sensor_power[sensor_id] = power_to_add
            _LOGGER.debug(
                "Added power %s W for %s to estimate",
                power_to_add,
                sensor_config.get(CONF_APPLIANCE),
            )

    def reduce_estimated_power(self, power_to_reduce: float) -> None:
        """
        Reduce the estimated total power.

        Args:
            power_to_reduce: Power in watts to subtract from the estimate.

        """
        self._estimated_total_power -= power_to_reduce
        _LOGGER.debug(
            "Reduced estimated power by %s W. New total: %s W",
            power_to_reduce,
            self._estimated_total_power,
        )

    def remove_sensor_power(self, sensor_id: str) -> None:
        """
        Remove a sensor's power from tracking.

        Args:
            sensor_id: Entity ID of the sensor to remove.

        """
        if sensor_id in self._current_sensor_power:
            power = self._current_sensor_power.pop(sensor_id)
            self._estimated_total_power -= power
            _LOGGER.debug(
                "Removed sensor %s with power %s W from tracking",
                sensor_id,
                power,
            )

    def get_sensor_power(self, sensor_id: str) -> float:
        """
        Get the current power for a specific sensor.

        Args:
            sensor_id: Entity ID of the sensor.

        Returns:
            Power in watts, or 0.0 if not tracked.

        """
        return self._current_sensor_power.get(sensor_id, 0.0)

    def get_total_house_power(self) -> float:
        """
        Get the current total house power.

        Returns:
            Total power consumption in watts.

        """
        return self._estimated_total_power

    def clear_tracking(self) -> None:
        """Clear all power tracking data."""
        self._current_sensor_power.clear()
        self._estimated_total_power = 0.0
