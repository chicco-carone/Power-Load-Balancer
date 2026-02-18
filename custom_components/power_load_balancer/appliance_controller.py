"""
Appliance control module for the Power Load Balancer integration.

This module handles turning appliances on and off, scheduling automatic turn-ons,
and managing appliance state tracking.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_APPLIANCE,
    CONF_DEVICE_COOLDOWN,
    DEFAULT_COOLDOWN_SECONDS,
    DOMAIN,
)
from .context_logger import ContextLogger
from .exceptions import (
    ApplianceControlError,
    EntityNotFoundError,
    EntityUnavailableError,
    RetryableError,
    ServiceCallError,
    ServiceTimeoutError,
)
from .retry import retry_with_backoff
from .service import ServiceCallParams, safe_service_call
from .validation import validate_entity_id, validate_entity_state

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant, State

_LOGGER = logging.getLogger(__name__)


class ApplianceController:
    """
    Manages appliance control operations and scheduling.

    This class handles turning appliances on and off, tracking which appliances
    have been turned off by the balancer, and scheduling automatic restoration
    after a configured delay.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        monitored_sensors: list[dict[str, Any]],
        global_cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        event_log_sensor: Any | None = None,
    ) -> None:
        """
        Initialize the ApplianceController.

        Args:
            hass: Home Assistant instance.
            monitored_sensors: List of monitored sensor configurations.
            global_cooldown_seconds: Global cooldown time in seconds before
                auto turn-on.
            event_log_sensor: Optional event log sensor for logging events.

        """
        self.hass = hass
        self._monitored_sensors = monitored_sensors
        self._global_cooldown_seconds = global_cooldown_seconds
        self._event_log_sensor = event_log_sensor
        self._balanced_off_appliances: dict[str, Any] = {}
        self._scheduled_auto_turn_ons: dict[str, asyncio.Task[Any]] = {}
        self._expected_power_restoration: dict[str, float] = {}
        self._previous_hvac_modes: dict[str, str] = {}

    def set_event_log_sensor(self, sensor: Any) -> None:
        """
        Set the event log sensor instance.

        Args:
            sensor: The PowerBalancerLogSensor instance.

        """
        self._event_log_sensor = sensor

    def get_sensor_for_appliance(self, appliance_entity_id: str) -> str | None:
        """
        Get the monitored sensor entity ID associated with an appliance.

        Args:
            appliance_entity_id: Entity ID of the appliance.

        Returns:
            Entity ID of the associated sensor, or None if not found.

        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == appliance_entity_id:
                return sensor_config.get(CONF_ENTITY_ID)
        return None

    def get_appliance_for_sensor(self, sensor_entity_id: str) -> str | None:
        """
        Get the appliance entity ID associated with a monitored sensor.

        Args:
            sensor_entity_id: Entity ID of the sensor.

        Returns:
            Entity ID of the associated appliance, or None if not found.

        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_ENTITY_ID) == sensor_entity_id:
                return sensor_config.get(CONF_APPLIANCE)
        return None

    def is_appliance_balanced_off(self, entity_id: str) -> bool:
        """
        Check if an appliance was turned off by the balancer.

        Args:
            entity_id: Entity ID of the appliance.

        Returns:
            True if the appliance was turned off by the balancer, False otherwise.

        """
        return entity_id in self._balanced_off_appliances

    def mark_appliance_balanced_off(self, entity_id: str, reason: str) -> None:
        """
        Mark an appliance as turned off by the balancer.

        Args:
            entity_id: Entity ID of the appliance.
            reason: Reason for turning off the appliance.

        """
        self._balanced_off_appliances[entity_id] = {
            "timestamp": self.hass.loop.time(),
            "reason": reason or "Turned off by Power Load Balancer",
        }

    def remove_from_balanced_off(self, entity_id: str) -> None:
        """
        Remove an appliance from the balanced off tracking.

        Args:
            entity_id: Entity ID of the appliance.

        """
        self._balanced_off_appliances.pop(entity_id, None)

    def get_balanced_off_appliances(self) -> list[str]:
        """
        Get list of appliances that were turned off by the balancer.

        Returns:
            List of appliance entity IDs.

        """
        return list(self._balanced_off_appliances.keys())

    def cancel_scheduled_turn_on(self, entity_id: str) -> None:
        """
        Cancel a scheduled automatic turn-on for an appliance.

        Args:
            entity_id: Entity ID of the appliance.

        """
        if entity_id in self._scheduled_auto_turn_ons:
            task = self._scheduled_auto_turn_ons[entity_id]
            if not task.done():
                task.cancel()
            self._scheduled_auto_turn_ons.pop(entity_id, None)
            self._expected_power_restoration.pop(entity_id, None)

    def set_expected_power_restoration(
        self, entity_id: str, expected_power: float
    ) -> None:
        """
        Set the expected power consumption when an appliance is restored.

        Args:
            entity_id: Entity ID of the appliance.
            expected_power: Expected power consumption in watts.

        """
        self._expected_power_restoration[entity_id] = expected_power

    def get_expected_power_restoration(self, entity_id: str) -> float:
        """
        Get the expected power consumption when an appliance is restored.

        Args:
            entity_id: Entity ID of the appliance.

        Returns:
            Expected power consumption in watts, or 0.0 if not set.

        """
        return self._expected_power_restoration.get(entity_id, 0.0)

    def _is_climate_entity(self, entity_id: str) -> bool:
        """
        Check if an entity is a climate entity.

        Args:
            entity_id: Entity ID to check.

        Returns:
            True if the entity is a climate entity, False otherwise.

        """
        return entity_id.startswith("climate.")

    def _get_supported_hvac_modes(self, entity_id: str) -> list[str]:
        """Get supported HVAC modes for a climate entity."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return []

        hvac_modes = state.attributes.get("hvac_modes", [])
        if not isinstance(hvac_modes, list):
            return []

        return [str(mode) for mode in hvac_modes]

    def _get_current_hvac_mode(self, appliance_state: State) -> str | None:
        """Get current HVAC mode from state attributes with safe fallback."""
        hvac_mode = appliance_state.attributes.get("hvac_mode")
        if isinstance(hvac_mode, str) and hvac_mode:
            return hvac_mode

        if appliance_state.state not in ("unknown", "unavailable"):
            return appliance_state.state

        return None

    def _is_valid_active_hvac_mode(
        self, mode: str | None, supported_modes: list[str]
    ) -> bool:
        """Check if a mode is valid and represents an active HVAC mode."""
        if not mode:
            return False

        if mode in ("off", "unknown", "unavailable"):
            return False

        return not supported_modes or mode in supported_modes

    def _select_fallback_hvac_mode(self, entity_id: str) -> str | None:
        """Select a fallback active HVAC mode for restoration."""
        for mode in self._get_supported_hvac_modes(entity_id):
            if mode not in ("off", "unknown", "unavailable"):
                return mode
        return None

    def _resolve_hvac_restore_mode(
        self, entity_id: str, appliance_state: State
    ) -> str | None:
        """Resolve the best HVAC mode to restore when turning climate back on."""
        supported_modes = self._get_supported_hvac_modes(entity_id)

        previous_mode = self.get_previous_hvac_mode(entity_id)
        if self._is_valid_active_hvac_mode(previous_mode, supported_modes):
            return previous_mode

        current_mode = self._get_current_hvac_mode(appliance_state)
        if self._is_valid_active_hvac_mode(current_mode, supported_modes):
            return current_mode

        return self._select_fallback_hvac_mode(entity_id)

    def _prepare_turn_on_service_call(
        self,
        entity_id: str,
        appliance_state: State,
        reason: str,
        logger: ContextLogger,
    ) -> tuple[str, str, dict[str, str]] | None:
        """Prepare domain/service payload for turning on an appliance."""
        domain = entity_id.split(".", maxsplit=1)[0]

        if self._is_climate_entity(entity_id):
            previous_mode = self._resolve_hvac_restore_mode(entity_id, appliance_state)
            if previous_mode is None:
                supported_hvac_modes = self._get_supported_hvac_modes(entity_id)
                logger.warning(
                    "Skipping climate restore: no valid HVAC mode available",
                    entity_id=entity_id,
                    stored_previous_mode=self.get_previous_hvac_mode(entity_id),
                    supported_hvac_modes=supported_hvac_modes,
                )
                if self._event_log_sensor:
                    self._event_log_sensor.add_log_entry(
                        f"Service call skipped: Cannot restore {entity_id}. "
                        "No valid HVAC mode available"
                    )
                return None

            logger.debug(
                "Restoring climate entity to previous mode",
                entity_id=entity_id,
                hvac_mode=previous_mode,
                reason=reason,
            )
            return (
                domain,
                "set_hvac_mode",
                {
                    "entity_id": entity_id,
                    "hvac_mode": previous_mode,
                },
            )

        logger.debug(
            "Calling turn_on service",
            entity_id=entity_id,
            domain=domain,
            reason=reason,
        )
        return domain, "turn_on", {"entity_id": entity_id}

    def _is_appliance_active(self, entity_id: str, state: str) -> bool:
        """
        Check if an appliance is in an active state.

        For climate entities, any state other than 'off' is considered active.
        For other entities, only 'on' is considered active.

        Args:
            entity_id: Entity ID of the appliance.
            state: Current state of the appliance.

        Returns:
            True if the appliance is active, False otherwise.

        """
        if self._is_climate_entity(entity_id):
            return state not in ("off", "unknown", "unavailable")
        return state == "on"

    def _is_appliance_off(self, _entity_id: str, state: str) -> bool:
        """
        Check if an appliance is in an off state.

        Args:
            _entity_id: Entity ID of the appliance (unused, for API consistency).
            state: Current state of the appliance.

        Returns:
            True if the appliance is off, False otherwise.

        """
        return state == "off"

    def get_previous_hvac_mode(self, entity_id: str) -> str | None:
        """
        Get the previous HVAC mode for a climate entity.

        Args:
            entity_id: Entity ID of the climate entity.

        Returns:
            Previous HVAC mode, or None if not stored.

        """
        return self._previous_hvac_modes.get(entity_id)

    def set_previous_hvac_mode(self, entity_id: str, hvac_mode: str) -> None:
        """
        Store the previous HVAC mode for a climate entity.

        Args:
            entity_id: Entity ID of the climate entity.
            hvac_mode: HVAC mode to store.

        """
        self._previous_hvac_modes[entity_id] = hvac_mode

    def clear_previous_hvac_mode(self, entity_id: str) -> None:
        """
        Clear the stored previous HVAC mode for a climate entity.

        Args:
            entity_id: Entity ID of the climate entity.

        """
        self._previous_hvac_modes.pop(entity_id, None)

    def get_cooldown_for_appliance(self, appliance_entity_id: str) -> int:
        """
        Get the cooldown time for a specific appliance.

        Returns the device-specific cooldown if configured,
        otherwise the global cooldown.

        Args:
            appliance_entity_id: Entity ID of the appliance.

        Returns:
            Cooldown time in seconds.

        """
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == appliance_entity_id:
                device_cooldown = sensor_config.get(CONF_DEVICE_COOLDOWN)
                if device_cooldown is not None and device_cooldown > 0:
                    return int(device_cooldown)
                break
        return self._global_cooldown_seconds

    def schedule_auto_turn_on(
        self,
        entity_id: str,
        expected_power: float,
        get_total_power_callback: Any,
        power_budget: int,
    ) -> None:
        """
        Schedule an automatic turn-on of an appliance after the configured delay.

        Args:
            entity_id: Entity ID of the appliance.
            expected_power: Expected power consumption in watts.
            get_total_power_callback: Callback to get current total power.
            power_budget: Maximum power budget in watts.

        """
        logger = ContextLogger(_LOGGER, "auto_turn_on").new_operation("schedule")

        try:
            cooldown_seconds = self.get_cooldown_for_appliance(entity_id)

            logger.debug(
                "Scheduling auto turn-on for appliance",
                entity_id=entity_id,
                cooldown_seconds=cooldown_seconds,
            )

            if entity_id in self._scheduled_auto_turn_ons:
                existing_task = self._scheduled_auto_turn_ons[entity_id]
                if not existing_task.done():
                    logger.debug(
                        "Cancelling existing auto turn-on task for %s",
                        entity_id=entity_id,
                    )
                    existing_task.cancel()
                self._scheduled_auto_turn_ons.pop(entity_id)

            self._expected_power_restoration[entity_id] = expected_power

            async def auto_turn_on_task(
                entity_to_restore: str, delay_seconds: int
            ) -> None:
                try:
                    logger.debug(
                        "Waiting for auto turn-on delay",
                        delay=delay_seconds,
                    )
                    await asyncio.sleep(delay_seconds)

                    logger.debug(
                        "Auto turn-on timer expired", entity_id=entity_to_restore
                    )

                    appliance_state = self.hass.states.get(entity_to_restore)
                    if appliance_state and appliance_state.state != "off":
                        logger.debug(
                            "Appliance is no longer off, cancelling auto turn-on",
                            entity_id=entity_to_restore,
                        )
                        return

                    current_power = get_total_power_callback()
                    available_budget = power_budget - current_power

                    expected_power_value = self._expected_power_restoration.get(
                        entity_to_restore, 0.0
                    )

                    logger.debug(
                        "Auto turn-on check",
                        entity_id=entity_to_restore,
                        current_power=current_power,
                        available_budget=available_budget,
                        expected_power=expected_power_value,
                    )

                    if available_budget < expected_power_value:
                        logger.debug(
                            "Cannot auto turn-on: Insufficient power headroom",
                            entity_id=entity_to_restore,
                            available_budget=available_budget,
                            expected_power=expected_power_value,
                        )
                        return

                    logger.info(
                        "Auto turning on appliance after delay",
                        entity_id=entity_to_restore,
                    )
                    await self.turn_on_appliance_service(
                        entity_to_restore,
                        f"Automatic restoration after {delay_seconds}s "
                        "power budget timeout",
                    )

                    self._expected_power_restoration.pop(entity_to_restore, None)
                    logger.debug(
                        "Auto turn-on completed successfully",
                        entity_id=entity_to_restore,
                    )

                except Exception as exc:
                    logger.exception(
                        "Auto turn-on task failed", entity_id=entity_to_restore
                    )

                    self._expected_power_restoration.pop(entity_to_restore, None)

                    if self._event_log_sensor:
                        self._event_log_sensor.add_log_entry(
                            f"Auto turn-on failed for {entity_to_restore}. "
                            f"Error: {type(exc).__name__}"
                        )

            task = self.hass.async_create_task(
                auto_turn_on_task(entity_id, cooldown_seconds)
            )
            self._scheduled_auto_turn_ons[entity_id] = task

            logger.debug("Auto turn-on scheduled successfully", entity_id=entity_id)

        except Exception:
            logger.exception("Failed to schedule auto turn-on", entity_id=entity_id)

    @retry_with_backoff(max_retries=2, backoff_factor=0.5, retry_on=(RetryableError,))
    async def turn_off_appliance(self, entity_id: str, reason: str = "") -> None:
        """
        Turn off a specified appliance and record it.

        For climate entities, this sets the HVAC mode to 'off' and stores the
        previous mode for later restoration.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.

        """
        logger = ContextLogger(_LOGGER, "appliance").new_operation("turn_off")

        try:
            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if not self._is_appliance_active(entity_id, appliance_state.state):
                logger.debug(
                    "Appliance is not in an active state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".", maxsplit=1)[0]

            if self._is_climate_entity(entity_id):
                current_hvac_mode = self._get_current_hvac_mode(appliance_state)
                supported_hvac_modes = self._get_supported_hvac_modes(entity_id)

                if current_hvac_mode is not None and self._is_valid_active_hvac_mode(
                    current_hvac_mode, supported_hvac_modes
                ):
                    self.set_previous_hvac_mode(entity_id, str(current_hvac_mode))
                else:
                    logger.warning(
                        "Climate is active but current HVAC mode cannot be saved",
                        entity_id=entity_id,
                        current_hvac_mode=current_hvac_mode,
                        supported_hvac_modes=supported_hvac_modes,
                    )

                service = "set_hvac_mode"
                service_data = {"entity_id": entity_id, "hvac_mode": "off"}
                logger.debug(
                    "Turning off climate entity",
                    entity_id=entity_id,
                    previous_mode=self.get_previous_hvac_mode(entity_id),
                    reason=reason,
                )
            else:
                service = "turn_off"
                service_data = {"entity_id": entity_id}
                logger.debug(
                    "Turning off appliance",
                    entity_id=entity_id,
                    reason=reason,
                    domain=domain,
                )

            params = ServiceCallParams(
                hass=self.hass,
                domain=domain,
                service=service,
                service_data=service_data,
                logger=logger,
            )
            await safe_service_call(params)

            self.mark_appliance_balanced_off(entity_id, reason)

            logger.info(
                "Successfully turned off appliance", entity_id=entity_id, reason=reason
            )

            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Turned off {entity_id}. Reason: {reason}"
                )

        except (EntityNotFoundError, EntityUnavailableError) as exc:
            logger.exception(
                "Appliance entity issue", entity_id=entity_id, error=str(exc)
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{entity_id}_unavailable",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="device_unavailable",
                translation_placeholders={"entity_id": entity_id},
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: {exc.error_code}"
                )
            msg = f"Cannot control appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except (ServiceCallError, ServiceTimeoutError) as exc:
            logger.exception("Service call failed", entity_id=entity_id, error=str(exc))
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: Service call failed"
                )
            msg = f"Failed to turn off appliance {entity_id}: {exc}"
            raise RetryableError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except Exception as exc:
            logger.exception(
                "Unexpected error turning off appliance", entity_id=entity_id
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: {type(exc).__name__}"
                )
            msg = f"Unexpected error turning off appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "error": str(exc)},
            ) from exc

    async def turn_off_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """
        Handle the turn_off_appliance service call.

        For climate entities, this sets the HVAC mode to 'off' and stores the
        previous mode for later restoration.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.
            context: Optional Home Assistant context.

        """
        logger = ContextLogger(_LOGGER, "service").new_operation(
            "turn_off_appliance_service"
        )

        try:
            logger.debug(
                "Service turn_off_appliance called",
                entity_id=entity_id,
                reason=reason,
                context_id=str(context.id) if context else None,
            )

            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if not self._is_appliance_active(entity_id, appliance_state.state):
                logger.debug(
                    "Appliance is not in an active state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".", maxsplit=1)[0]

            if self._is_climate_entity(entity_id):
                current_hvac_mode = self._get_current_hvac_mode(appliance_state)
                supported_hvac_modes = self._get_supported_hvac_modes(entity_id)

                if current_hvac_mode is not None and self._is_valid_active_hvac_mode(
                    current_hvac_mode, supported_hvac_modes
                ):
                    self.set_previous_hvac_mode(entity_id, str(current_hvac_mode))
                else:
                    logger.warning(
                        "Climate is active but current HVAC mode cannot be saved",
                        entity_id=entity_id,
                        current_hvac_mode=current_hvac_mode,
                        supported_hvac_modes=supported_hvac_modes,
                    )

                service = "set_hvac_mode"
                service_data = {"entity_id": entity_id, "hvac_mode": "off"}
                logger.debug(
                    "Calling set_hvac_mode service for climate entity",
                    entity_id=entity_id,
                    previous_mode=self.get_previous_hvac_mode(entity_id),
                    reason=reason,
                )
            else:
                service = "turn_off"
                service_data = {"entity_id": entity_id}
                logger.debug(
                    "Calling turn_off service",
                    entity_id=entity_id,
                    domain=domain,
                    reason=reason,
                )

            params = ServiceCallParams(
                hass=self.hass,
                domain=domain,
                service=service,
                service_data=service_data,
                logger=logger,
            )
            await safe_service_call(params)

            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": "Power Load Balancer",
                    "message": f"turned off {entity_id}"
                    + (f": {reason}" if reason else ""),
                    "domain": DOMAIN,
                    "entity_id": entity_id,
                    "context_id": str(context.id) if context else None,
                },
                context=context,
            )

            logger.info(
                "Successfully turned off appliance via service",
                entity_id=entity_id,
                reason=reason,
            )

            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call: Turned off {entity_id}. Reason: {reason}"
                )

        except (EntityNotFoundError, EntityUnavailableError) as exc:
            logger.exception(
                "Service call failed - entity issue",
                entity_id=entity_id,
                error=str(exc),
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{entity_id}_unavailable",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="device_unavailable",
                translation_placeholders={"entity_id": entity_id},
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Cannot turn off {entity_id}. "
                    f"Error: {exc.error_code}"
                )
            msg = f"Cannot control appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except (ServiceCallError, ServiceTimeoutError) as exc:
            logger.exception("Service call failed", entity_id=entity_id, error=str(exc))
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn off {entity_id}. "
                    "Error: Service call failed"
                )
            msg = f"Failed to turn off appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except Exception as exc:
            logger.exception("Unexpected error in service call", entity_id=entity_id)
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn off {entity_id}. "
                    f"Error: {type(exc).__name__}"
                )
            msg = f"Unexpected error turning off appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "error": str(exc)},
            ) from exc

    async def turn_on_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """
        Handle the turn_on_appliance service call.

        For climate entities, this restores the previously stored HVAC mode.
        If no previous mode is stored, it defaults to 'heat'.

        Args:
            entity_id: Entity ID of the appliance to turn on.
            reason: Reason for turning on the appliance.
            context: Optional Home Assistant context.

        """
        logger = ContextLogger(_LOGGER, "service").new_operation(
            "turn_on_appliance_service"
        )

        try:
            logger.debug(
                "Service turn_on_appliance called",
                entity_id=entity_id,
                reason=reason,
                context_id=str(context.id) if context else None,
            )

            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if not self._is_appliance_off(entity_id, appliance_state.state):
                logger.debug(
                    "Appliance is not in 'off' state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            prepared_service_call = self._prepare_turn_on_service_call(
                entity_id,
                appliance_state,
                reason,
                logger,
            )
            if prepared_service_call is None:
                return

            domain, service, service_data = prepared_service_call

            params = ServiceCallParams(
                hass=self.hass,
                domain=domain,
                service=service,
                service_data=service_data,
                logger=logger,
            )
            await safe_service_call(params)

            if self._is_climate_entity(entity_id):
                self.clear_previous_hvac_mode(entity_id)

            self.hass.bus.async_fire(
                "logbook_entry",
                {
                    "name": "Power Load Balancer",
                    "message": f"turned on {entity_id}"
                    + (f": {reason}" if reason else ""),
                    "domain": DOMAIN,
                    "entity_id": entity_id,
                    "context_id": str(context.id) if context else None,
                },
                context=context,
            )

            logger.info(
                "Successfully turned on appliance via service",
                entity_id=entity_id,
                reason=reason,
            )

            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call: Turned on {entity_id}. Reason: {reason}"
                )

        except (EntityNotFoundError, EntityUnavailableError) as exc:
            logger.exception(
                "Service call failed - entity issue",
                entity_id=entity_id,
                error=str(exc),
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Cannot turn on {entity_id}. "
                    f"Error: {exc.error_code}"
                )
            msg = f"Cannot control appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except (ServiceCallError, ServiceTimeoutError) as exc:
            logger.exception("Service call failed", entity_id=entity_id, error=str(exc))
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn on {entity_id}. "
                    "Error: Service call failed"
                )
            msg = f"Failed to turn on appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "original_error": str(exc)},
            ) from exc

        except Exception as exc:
            logger.exception("Unexpected error in service call", entity_id=entity_id)
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Service call failed: Turn on {entity_id}. "
                    f"Error: {type(exc).__name__}"
                )
            msg = f"Unexpected error turning on appliance {entity_id}: {exc}"
            raise ApplianceControlError(
                msg,
                details={"entity_id": entity_id, "error": str(exc)},
            ) from exc

    def cleanup(self) -> None:
        """Clean up the controller and cancel all scheduled tasks."""
        for task in self._scheduled_auto_turn_ons.values():
            if not task.done():
                task.cancel()
        self._scheduled_auto_turn_ons.clear()
        self._expected_power_restoration.clear()
        self._balanced_off_appliances.clear()
        self._previous_hvac_modes.clear()

    def get_diagnostics_snapshot(self) -> dict[str, Any]:
        """Return runtime diagnostics data for troubleshooting."""
        scheduled_tasks = {
            entity_id: {
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            for entity_id, task in self._scheduled_auto_turn_ons.items()
        }

        return {
            "balanced_off_appliances": dict(self._balanced_off_appliances),
            "expected_power_restoration_watt": dict(self._expected_power_restoration),
            "scheduled_auto_turn_on_tasks": scheduled_tasks,
            "stored_previous_hvac_modes": dict(self._previous_hvac_modes),
        }
