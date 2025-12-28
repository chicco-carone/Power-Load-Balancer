"""Sensor platform for the Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .power_balancer import PowerLoadBalancer

_LOGGER = logging.getLogger(__name__)

MAX_LOG_SIZE = 50


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up the Power Load Balancer sensor from a config entry.

    Args:
        hass: Home Assistant instance.
        config_entry: Configuration entry for this integration.
        async_add_entities: Callback to add entities.

    """
    _LOGGER.debug("Setting up power_load_balancer sensor platform")

    power_balancer: PowerLoadBalancer = hass.data[DOMAIN][config_entry.entry_id]

    sensor = PowerBalancerLogSensor(power_balancer)
    async_add_entities([sensor])


class PowerBalancerLogSensor(SensorEntity):
    """
    Sensor entity that tracks power balancing events.

    This sensor provides a log of all power balancing actions taken,
    including appliances turned on/off and the reasons for those actions.
    """

    _balancer: PowerLoadBalancer
    _attr_name: str
    _attr_unique_id: str
    _attr_native_value: str
    _attr_extra_state_attributes: dict[str, Any]

    def __init__(self, balancer: PowerLoadBalancer) -> None:
        """
        Initialize the sensor.

        Args:
            balancer: The PowerLoadBalancer instance to track.

        """
        self._balancer = balancer
        self._attr_name = "Power Load Balancer Log"
        self._attr_unique_id = f"{self._balancer.entry.entry_id}_event_log"
        self._attr_native_value = "Initialized"
        self._attr_extra_state_attributes = {"events": []}

    async def async_added_to_hass(self) -> None:
        """Register this sensor with the PowerLoadBalancer when added to hass."""
        await super().async_added_to_hass()
        self._balancer.register_event_log_sensor(self)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information for the sensor."""
        if self._balancer.entry.entry_id:
            return DeviceInfo(
                identifiers={(DOMAIN, self._balancer.entry.entry_id)},
                name="Power Load Balancer",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
            )
        return None

    @callback
    def add_log_entry(self, message: str) -> None:
        """
        Add an entry to the event log.

        Args:
            message: The log message to add.

        """
@callback
def add_log_entry(self, message: str) -> None:
    """
    Add an entry to the event log.

    Args:
        message: The log message to add.

    """
    from datetime import datetime
    
    timestamp = datetime.now().isoformat(timespec="seconds")
    log_entry = f"{timestamp} - {message}"
    self._attr_extra_state_attributes["events"].append(log_entry)

    if len(self._attr_extra_state_attributes["events"]) > MAX_LOG_SIZE:
        self._attr_extra_state_attributes["events"] = (
        self._attr_extra_state_attributes["events"][-MAX_LOG_SIZE:]
    )

    self._attr_native_value = message
    self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        """Return the current state of the sensor."""
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes containing the event log."""
        return self._attr_extra_state_attributes
