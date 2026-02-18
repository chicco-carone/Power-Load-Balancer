"""Diagnostics support for the Power Load Balancer integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ENTITY_ID

from .const import CONF_APPLIANCE, CONF_MAIN_POWER_SENSOR, CONF_POWER_SENSORS, DOMAIN
from .power_balancer import PowerLoadBalancer

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

TO_REDACT = {
    CONF_APPLIANCE,
    CONF_ENTITY_ID,
    CONF_MAIN_POWER_SENSOR,
    CONF_POWER_SENSORS,
    "context_id",
    "entry_id",
    "main_power_sensor_entity_id",
    "monitored_sensors",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    domain_data = hass.data.get(DOMAIN, {})
    power_balancer = domain_data.get(entry.entry_id)

    runtime_data: dict[str, Any] = {"available": False}
    if isinstance(power_balancer, PowerLoadBalancer):
        runtime_data = {
            "available": True,
            "snapshot": power_balancer.get_diagnostics_snapshot(),
        }

    diagnostics_data: dict[str, Any] = {
        "entry": entry.as_dict(),
        "runtime": runtime_data,
    }

    return async_redact_data(diagnostics_data, TO_REDACT)
