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

from .const import AUTO_TURN_ON_DELAY_SECONDS, CONF_APPLIANCE, DOMAIN
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
    from homeassistant.core import Context, HomeAssistant

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
        event_log_sensor: Any | None = None,
    ) -> None:
        """
        Initialize the ApplianceController.

        Args:
            hass: Home Assistant instance.
            monitored_sensors: List of monitored sensor configurations.
            event_log_sensor: Optional event log sensor for logging events.

        """
        self.hass = hass
        self._monitored_sensors = monitored_sensors
        self._event_log_sensor = event_log_sensor
        self._balanced_off_appliances: dict[str, Any] = {}
        self._scheduled_auto_turn_ons: dict[str, asyncio.Task[Any]] = {}
        self._expected_power_restoration: dict[str, float] = {}

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

    def schedule_auto_turn_on(
        self,
        entity_id: str,
        expected_power: float,
        get_total_power_callback: Any,
        power_budget: int,
        is_balancing_enabled_callback: Any,
    ) -> None:
        """
        Schedule an automatic turn-on of an appliance after the configured delay.

        Args:
            entity_id: Entity ID of the appliance.
            expected_power: Expected power consumption in watts.
            get_total_power_callback: Callback to get current total power.
            power_budget: Maximum power budget in watts.
            is_balancing_enabled_callback: Callback to check if balancing is enabled.

        """
        logger = ContextLogger(_LOGGER, "auto_turn_on").new_operation("schedule")

        try:
            logger.debug(
                "Scheduling auto turn-on for %s in %s seconds",
                entity_id,
                AUTO_TURN_ON_DELAY_SECONDS,
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

            async def auto_turn_on_task(entity_to_restore: str) -> None:
                try:
                    logger.debug(
                        "Waiting for auto turn-on delay",
                        delay=AUTO_TURN_ON_DELAY_SECONDS,
                    )
                    await asyncio.sleep(AUTO_TURN_ON_DELAY_SECONDS)

                    logger.info(
                        "Auto turn-on timer expired", entity_id=entity_to_restore
                    )

                    if not is_balancing_enabled_callback():
                        logger.debug("Balancing disabled, cancelling auto turn-on")
                        return

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
                        logger.warning(
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
                        f"Automatic restoration after {AUTO_TURN_ON_DELAY_SECONDS}s "
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

                    self._expected_power_restoration.pop(entity_id, None)

                    if self._event_log_sensor:
                        self._event_log_sensor.add_log_entry(
                            f"Auto turn-on failed for {entity_id}. "
                            f"Error: {type(exc).__name__}"
                        )

            task = self.hass.async_create_task(auto_turn_on_task(entity_id))
            self._scheduled_auto_turn_ons[entity_id] = task

            logger.debug("Auto turn-on scheduled successfully", entity_id=entity_id)

        except Exception:
            logger.exception("Failed to schedule auto turn-on", entity_id=entity_id)

    @retry_with_backoff(max_retries=2, backoff_factor=0.5, retry_on=(RetryableError,))
    async def turn_off_appliance(self, entity_id: str, reason: str = "") -> None:
        """
        Turn off a specified appliance and record it.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.

        """
        logger = ContextLogger(_LOGGER, "appliance").new_operation("turn_off")

        try:
            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if appliance_state.state != "on":
                logger.warning(
                    "Appliance is not in 'on' state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".")[0]
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
                service="turn_off",
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

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.
            context: Optional Home Assistant context.

        """
        logger = ContextLogger(_LOGGER, "service").new_operation(
            "turn_off_appliance_service"
        )

        try:
            logger.info(
                "Service turn_off_appliance called",
                entity_id=entity_id,
                reason=reason,
                context_id=str(context.id) if context else None,
            )

            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if appliance_state.state != "on":
                logger.warning(
                    "Appliance is not in 'on' state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".")[0]
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
                service="turn_off",
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

        Args:
            entity_id: Entity ID of the appliance to turn on.
            reason: Reason for turning on the appliance.
            context: Optional Home Assistant context.

        """
        logger = ContextLogger(_LOGGER, "service").new_operation(
            "turn_on_appliance_service"
        )

        try:
            logger.info(
                "Service turn_on_appliance called",
                entity_id=entity_id,
                reason=reason,
                context_id=str(context.id) if context else None,
            )

            validate_entity_id(entity_id)
            appliance_state = validate_entity_state(self.hass, entity_id)

            if appliance_state.state != "off":
                logger.warning(
                    "Appliance is not in 'off' state",
                    entity_id=entity_id,
                    current_state=appliance_state.state,
                )
                return

            domain = entity_id.split(".")[0]
            service_data = {"entity_id": entity_id}

            logger.debug(
                "Calling turn_on service",
                entity_id=entity_id,
                domain=domain,
                reason=reason,
            )

            params = ServiceCallParams(
                hass=self.hass,
                domain=domain,
                service="turn_on",
                service_data=service_data,
                logger=logger,
            )
            await safe_service_call(params)

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
