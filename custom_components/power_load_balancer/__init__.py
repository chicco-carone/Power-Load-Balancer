import logging  # noqa: D104

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .power_balancer import PowerLoadBalancer

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Power Load Balancer from a config entry."""
    _LOGGER.info("Setting up Power Load Balancer integration from config entry")

    config_data = {
        **(entry.options or entry.data),
        "entry_id": entry.entry_id,
        "config_entry": entry,
    }

    power_balancer = PowerLoadBalancer(hass, config_data, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = power_balancer

    await power_balancer.async_setup()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("Power Load Balancer integration setup complete.")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Power Load Balancer integration config entry")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok and entry.entry_id in hass.data[DOMAIN]:
        del hass.data[DOMAIN][entry.entry_id]

    _LOGGER.info("Power Load Balancer integration unloaded.")

    return unload_ok
