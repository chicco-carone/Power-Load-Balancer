"""
Power Load Balancer integration for Home Assistant.

This integration monitors household power consumption and automatically turns off
less critical appliances when exceeding a configured power budget. Appliances are
prioritized by importance and automatically restored when power headroom allows.
"""

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ENTITY_ID,
    ATTR_REASON,
    DOMAIN,
    SERVICE_TURN_OFF_APPLIANCE,
    SERVICE_TURN_ON_APPLIANCE,
)
from .context_logger import ContextLogger
from .exceptions import ConfigurationError, PowerLoadBalancerError
from .power_balancer import PowerLoadBalancer

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

SERVICE_TURN_OFF_APPLIANCE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_REASON, default=""): cv.string,
    }
)

SERVICE_TURN_ON_APPLIANCE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_REASON, default=""): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Power Load Balancer from a config entry."""
    logger = ContextLogger(_LOGGER, "setup").new_operation("setup_entry")

    try:
        logger.info(
            "Setting up Power Load Balancer integration", entry_id=entry.entry_id
        )

        if not entry.data and not entry.options:
            error_message = "No configuration data found in config entry"
            raise ConfigurationError(  # noqa: TRY301
                error_message,
                details={"entry_id": entry.entry_id},
            )

        config_data = {
            **(entry.options or entry.data),
            "entry_id": entry.entry_id,
            "config_entry": entry,
        }

        required_fields = ["main_power_sensor", "power_budget_watt"]
        missing_fields = [
            field for field in required_fields if field not in config_data
        ]

        def _raise_missing_fields() -> None:
            msg = "Missing required configuration fields: " + str(missing_fields)
            raise ConfigurationError(  # noqa: TRY301
                msg,
                details={"missing_fields": missing_fields, "entry_id": entry.entry_id},
            )

        if missing_fields:
            _raise_missing_fields()

        logger.debug("Creating PowerLoadBalancer instance", config_data=config_data)
        power_balancer = PowerLoadBalancer(hass, config_data, entry)

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = power_balancer

        logger.debug("Initializing PowerLoadBalancer")
        await power_balancer.async_setup()

        logger.debug("Setting up platforms", platforms=PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        await _async_register_services(hass, entry)

        logger.info("Power Load Balancer integration setup complete")
    except ConfigurationError:
        logger.exception("Configuration error during setup")
        raise
    except PowerLoadBalancerError:
        logger.exception("Power Load Balancer error during setup")
        raise
    except Exception as exc:
        logger.exception("Unexpected error during Power Load Balancer setup")
        msg = "Failed to set up Power Load Balancer: " + str(exc)
        raise ConfigurationError(
            msg,
            details={"entry_id": entry.entry_id, "error": str(exc)},
        ) from exc
    else:
        return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    logger = ContextLogger(_LOGGER, "setup").new_operation("unload_entry")

    try:
        logger.info(
            "Unloading Power Load Balancer integration", entry_id=entry.entry_id
        )

        if entry.entry_id in hass.data.get(DOMAIN, {}):
            power_balancer = hass.data[DOMAIN][entry.entry_id]
            logger.debug("Cleaning up PowerLoadBalancer instance")

            if hasattr(power_balancer, "async_cleanup"):
                await power_balancer.async_cleanup()

            logger.debug("Unloading platforms", platforms=PLATFORMS)
            unload_ok = await hass.config_entries.async_unload_platforms(
                entry, PLATFORMS
            )
        if unload_ok and entry.entry_id in hass.data.get(DOMAIN, {}):
            logger.debug("Removing entry from hass data")
            del hass.data[DOMAIN][entry.entry_id]

            if not hass.data[DOMAIN]:
                del hass.data[DOMAIN]

        if unload_ok:
            logger.info("Power Load Balancer integration unloaded successfully")
        else:
            logger.warning("Failed to unload some platforms")
    except Exception:
        logger.exception("Error during Power Load Balancer unload")
        return False
    else:
        return unload_ok


async def _async_register_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the Power Load Balancer services."""
    logger = ContextLogger(_LOGGER, "services").new_operation("register_services")

    try:
        logger.debug("Registering Power Load Balancer services")

        if not hass.services.has_service(DOMAIN, SERVICE_TURN_OFF_APPLIANCE):
            hass.services.async_register(
                DOMAIN,
                SERVICE_TURN_OFF_APPLIANCE,
                _handle_turn_off_appliance_service,
                schema=SERVICE_TURN_OFF_APPLIANCE_SCHEMA,
            )
            logger.debug("Registered turn_off_appliance service")

        if not hass.services.has_service(DOMAIN, SERVICE_TURN_ON_APPLIANCE):
            hass.services.async_register(
                DOMAIN,
                SERVICE_TURN_ON_APPLIANCE,
                _handle_turn_on_appliance_service,
                schema=SERVICE_TURN_ON_APPLIANCE_SCHEMA,
            )
            logger.debug("Registered turn_on_appliance service")

        logger.info("Power Load Balancer services registered successfully")

    except Exception as exc:
        logger.exception("Failed to register Power Load Balancer services")
        msg = "Failed to register services: " + str(exc)
        raise ConfigurationError(
            msg,
            details={"entry_id": entry.entry_id, "error": str(exc)},
        ) from exc


async def _handle_turn_off_appliance_service(call: ServiceCall) -> None:
    """Handle the turn_off_appliance service call."""
    logger = ContextLogger(_LOGGER, "services").new_operation("turn_off_appliance")

    try:
        entity_id = call.data[ATTR_ENTITY_ID]
        reason = call.data.get(ATTR_REASON, "")

        logger.info(
            "Turn off appliance service called",
            entity_id=entity_id,
            reason=reason,
            context=call.context,
        )

        power_balancer = _get_power_balancer_for_entity(call.hass, entity_id)

        if power_balancer:
            await power_balancer.async_turn_off_appliance_service(
                entity_id, reason, call.context
            )
        else:
            logger.warning(
                "No Power Load Balancer instance found for entity", entity_id=entity_id
            )

    except Exception:
        logger.exception("Error handling turn_off_appliance service call")
        raise


async def _handle_turn_on_appliance_service(call: ServiceCall) -> None:
    """Handle the turn_on_appliance service call."""
    logger = ContextLogger(_LOGGER, "services").new_operation("turn_on_appliance")

    try:
        entity_id = call.data[ATTR_ENTITY_ID]
        reason = call.data.get(ATTR_REASON, "")

        logger.info(
            "Turn on appliance service called",
            entity_id=entity_id,
            reason=reason,
            context=call.context,
        )

        power_balancer = _get_power_balancer_for_entity(call.hass, entity_id)

        if power_balancer:
            await power_balancer.async_turn_on_appliance_service(
                entity_id, reason, call.context
            )
        else:
            logger.warning(
                "No Power Load Balancer instance found for entity", entity_id=entity_id
            )

    except Exception:
        logger.exception("Error handling turn_on_appliance service call")
        raise


def _get_power_balancer_for_entity(
    hass: HomeAssistant, entity_id: str
) -> PowerLoadBalancer | None:
    """Find the PowerLoadBalancer instance that manages the given entity."""
    domain_data = hass.data.get(DOMAIN, {})

    for power_balancer in domain_data.values():
        if power_balancer.manages_entity(entity_id):
            return power_balancer

    if domain_data:
        return next(iter(domain_data.values()))

    return None
