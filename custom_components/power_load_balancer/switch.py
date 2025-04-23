import logging  # noqa: D100

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .power_balancer import (
    PowerBalancerControlSwitch,
    PowerLoadBalancer,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Power Load Balancer switch from a config entry."""
    _LOGGER.debug("Setting up power_load_balancer switch platform")

    power_balancer: PowerLoadBalancer = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities([PowerBalancerControlSwitch(power_balancer)])
