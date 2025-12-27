"""
Balancing engine module for the Power Load Balancer integration.

This module contains the logic for balancing power consumption by turning
appliances on and off based on the configured power budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback

from .const import CONF_APPLIANCE, CONF_IMPORTANCE, CONF_LAST_RESORT

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class BalancingCallbacks:
    """Callbacks used by the balancing engine."""

    get_total_power: Callable[[], float]
    get_expected_power_restoration: Callable[[str], float]
    get_sensor_power_for_appliance: Callable[[str], float]
    cancel_scheduled_turn_on: Callable[[str], None]
    reduce_estimated_power: Callable[[float], None]
    is_appliance_balanced_off: Callable[[str], bool]


class BalancingEngine:
    """
    Manages the power balancing logic.

    This class determines when and which appliances should be turned off
    to stay within the power budget, and when appliances can be safely
    restored.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        monitored_sensors: list[dict[str, Any]],
        power_budget: int,
    ) -> None:
        """
        Initialize the BalancingEngine.

        Args:
            hass: Home Assistant instance.
            monitored_sensors: List of monitored sensor configurations.
            power_budget: Maximum power budget in watts.

        """
        self.hass = hass
        self._monitored_sensors = monitored_sensors
        self._power_budget = power_budget

    @callback
    def balance_up(
        self,
        callbacks: BalancingCallbacks,
        balanced_off_appliances: list[str],
        restore_appliance_callback: Any,
    ) -> None:
        """
        Turn on appliances that can safely fit within power budget.

        Args:
            callbacks: Callbacks for power and appliance operations.
            balanced_off_appliances: List of appliances turned off by the balancer.
            restore_appliance_callback: Callback to restore an appliance.

        """
        appliances_to_restore = list(balanced_off_appliances)

        _LOGGER.debug(
            "Considering appliances for restoration: %s", appliances_to_restore
        )

        for appliance_entity_id in appliances_to_restore:
            appliance_state = self.hass.states.get(appliance_entity_id)
            if appliance_state and appliance_state.state != "off":
                continue

            current_power = callbacks.get_total_power()
            available_budget = self._power_budget - current_power

            expected_power = callbacks.get_expected_power_restoration(
                appliance_entity_id
            )
            if expected_power <= 0:
                expected_power = callbacks.get_sensor_power_for_appliance(
                    appliance_entity_id
                )

            _LOGGER.debug(
                "Restoration check for %s: Current power %s W, Available budget %s W, "
                "Expected power %s W",
                appliance_entity_id,
                current_power,
                available_budget,
                expected_power,
            )

            if available_budget >= expected_power > 0:
                _LOGGER.info(
                    "Restoring %s: sufficient power headroom "
                    "(%s W available, %s W needed)",
                    appliance_entity_id,
                    available_budget,
                    expected_power,
                )

                callbacks.cancel_scheduled_turn_on(appliance_entity_id)

                async def restore_task(
                    appliance_entity_id: str = appliance_entity_id,
                ) -> None:
                    try:
                        await restore_appliance_callback(
                            appliance_entity_id,
                            "restored by automatic balancing",
                        )
                    except Exception:
                        _LOGGER.exception(
                            "Failed to restore appliance %s", appliance_entity_id
                        )

                self.hass.async_create_task(restore_task())
                return

        _LOGGER.debug("No appliances eligible for restoration")

    @callback
    def balance_down(
        self,
        get_sensor_power_for_appliance_callback: Any,
        reduce_estimated_power_callback: Any,
        is_appliance_balanced_off_callback: Any,
        turn_off_appliance_callback: Any,
    ) -> None:
        """
        Turn off appliances to bring power usage below budget.

        Args:
            get_sensor_power_for_appliance_callback: Callback to get sensor
                power for appliance.
            reduce_estimated_power_callback: Callback to reduce estimated power.
            is_appliance_balanced_off_callback: Callback to check if appliance
                is balanced off.
            turn_off_appliance_callback: Callback to turn off an appliance.

        """
        sensors_to_consider = sorted(
            self._monitored_sensors, key=lambda x: x.get(CONF_IMPORTANCE, 5)
        )

        _LOGGER.debug(
            "Appliances considered for balancing (sorted by importance): %s",
            [s.get(CONF_APPLIANCE) for s in sensors_to_consider],
        )

        for sensor_config in sensors_to_consider:
            appliance_entity_id = sensor_config.get(CONF_APPLIANCE)
            is_last_resort = sensor_config.get(CONF_LAST_RESORT, False)

            if not appliance_entity_id:
                continue

            appliance_state = self.hass.states.get(appliance_entity_id)

            if (
                appliance_state
                and appliance_state.state == "on"
                and not is_appliance_balanced_off_callback(appliance_entity_id)
                and not is_last_resort
            ):
                expected_power_reduction = get_sensor_power_for_appliance_callback(
                    appliance_entity_id
                )

                _LOGGER.info(
                    "Turning off %s (importance %s) to balance power. "
                    "Expected power reduction: %s W",
                    appliance_entity_id,
                    sensor_config.get(CONF_IMPORTANCE),
                    expected_power_reduction,
                )

                if expected_power_reduction > 0:
                    reduce_estimated_power_callback(expected_power_reduction)
                    _LOGGER.debug(
                        "Optimistically reduced estimated power by %s W for %s",
                        expected_power_reduction,
                        appliance_entity_id,
                    )

                async def turn_off_task(inner_appliance_id: str) -> None:
                    try:
                        appliance_state_before = self.hass.states.get(
                            inner_appliance_id
                        )
                        if (
                            appliance_state_before
                            and appliance_state_before.state != "on"
                        ):
                            _LOGGER.debug(
                                "Skipping turn_off: appliance %s already in %s",
                                inner_appliance_id,
                                appliance_state_before.state,
                            )
                            return

                        await turn_off_appliance_callback(
                            inner_appliance_id,
                            f"Automatic balancing: Exceeded budget of "
                            f"{self._power_budget} W",
                        )
                    except Exception:
                        _LOGGER.exception(
                            "Failed to turn off appliance %s", inner_appliance_id
                        )

                self.hass.async_create_task(turn_off_task(appliance_entity_id))
                return

        _LOGGER.warning(
            "Could not balance power below budget by turning off non-last-resort "
            "appliances."
        )
