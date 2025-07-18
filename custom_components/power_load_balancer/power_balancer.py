"""Main module for the Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Context, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import (
    DeviceInfo,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import (
    async_get as async_get_device_registry,
)
from homeassistant.helpers.event import async_track_state_change_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.typing import StateType

from .const import (
    CONF_APPLIANCE,
    CONF_IMPORTANCE,
    CONF_LAST_RESORT,
    CONF_MAIN_POWER_SENSOR,
    CONF_POWER_BUDGET_WATT,
    CONF_POWER_SENSORS,
    DOMAIN,
)
from .exceptions import (
    ApplianceControlError,
    ConfigurationError,
    EntityNotFoundError,
    EntityUnavailableError,
    PowerSensorError,
    RetryableError,
    ServiceCallError,
    ServiceTimeoutError,
)
from .utils import (
    ContextLogger,
    ServiceCallParams,
    convert_power_to_watts,
    retry_with_backoff,
    safe_service_call,
    validate_entity_id,
    validate_entity_state,
    validate_power_value,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_MANUFACTURER = "Power Load Balancer"
DEVICE_MODEL = "Power Load Balancer"


class PowerLoadBalancer:
    """Core class for the Power Load Balancer."""

    hass: HomeAssistant
    entry: ConfigEntry
    _config_data: dict[str, Any]
    _is_balancing_enabled: bool
    _main_power_sensor_entity_id: str
    _monitored_sensors: list[dict[str, Any]]
    _power_budget: int
    _current_sensor_power: dict[str, float]
    _balanced_off_appliances: dict[str, Any]
    _estimated_total_power: float
    _event_log_sensor: PowerBalancerLogSensor | None
    _device_id: str | None
    _main_power_sensor_unsub: Callable[[], None] | None
    _monitored_sensors_unsub: Callable[[], None] | None
    _appliance_unsub: Callable[[], None] | None

    def __init__(
        self,
        hass: HomeAssistant,
        config_data: dict[str, Any],
        entry: ConfigEntry,
    ) -> None:
        """

        Initialize the PowerLoadBalancer.

        Logs all monitored sensors on first initialization.
        """
        self.hass = hass
        self.entry = entry
        self._config_data = config_data
        self._is_balancing_enabled = True
        self._main_power_sensor_entity_id = config_data[CONF_MAIN_POWER_SENSOR]
        self._monitored_sensors = config_data.get(CONF_POWER_SENSORS, [])
        self._power_budget = config_data[CONF_POWER_BUDGET_WATT]
        self._current_sensor_power = {}
        self._balanced_off_appliances = {}
        self._estimated_total_power = 0.0
        self._event_log_sensor = None
        self._device_id = None
        self._main_power_sensor_unsub = None
        self._monitored_sensors_unsub = None
        self._appliance_unsub = None

        monitored_sensor_ids = [
            sensor.get(CONF_ENTITY_ID, "unknown") for sensor in self._monitored_sensors
        ]
        _LOGGER.info(
            "PowerLoadBalancer initialized with monitored sensors: %s",
            monitored_sensor_ids,
        )

    async def async_setup(self) -> None:
        """
        Set up the listeners and create entities.

        The estimated total power is always set to the main power sensor value
        to avoid double counting. This ensures that single sensors are not
        summed with the main sensor, preventing overestimation of total power.
        """
        _LOGGER.debug("Setting up PowerLoadBalancer listeners and entities")

        device_registry = async_get_device_registry(self.hass)
        device_entry = device_registry.async_get_or_create(
            config_entry_id=self._config_data["entry_id"],
            identifiers={(DOMAIN, self._config_data["entry_id"])},
            name="Power Load Balancer",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )
        self._device_id = getattr(device_entry, "id", None)

        self._current_sensor_power = {}
        main_sensor_state: State | None = self.hass.states.get(
            self._main_power_sensor_entity_id
        )
        if main_sensor_state is not None and main_sensor_state.state not in (
            "unknown",
            "unavailable",
        ):
            try:
                raw_power = float(main_sensor_state.state)
                self._estimated_total_power = self._convert_power_to_watts(
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
                    power = self._convert_power_to_watts(raw_power, state)
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

        self._main_power_sensor_unsub = async_track_state_change_event(
            self.hass,
            self._main_power_sensor_entity_id,
            self._handle_power_sensor_state_change,
        )

        monitored_sensor_entity_ids = [
            s[CONF_ENTITY_ID] for s in self._monitored_sensors
        ]
        if monitored_sensor_entity_ids:
            self._monitored_sensors_unsub = async_track_state_change_event(
                self.hass,
                monitored_sensor_entity_ids,
                self._handle_power_sensor_state_change,
            )

        appliance_entity_ids = [s[CONF_APPLIANCE] for s in self._monitored_sensors]
        if appliance_entity_ids:
            self._appliance_unsub = async_track_state_change_event(
                self.hass,
                appliance_entity_ids,
                self._handle_appliance_state_change,
            )

        _LOGGER.debug("PowerLoadBalancer setup complete.")

    async def async_cleanup(self) -> None:
        """Clean up the PowerLoadBalancer and unsubscribe from events."""
        logger = ContextLogger(_LOGGER, "cleanup").new_operation(
            "power_balancer_cleanup"
        )

        try:
            logger.debug("Starting PowerLoadBalancer cleanup")

            if self._main_power_sensor_unsub:
                logger.debug("Unsubscribing from main power sensor events")
                self._main_power_sensor_unsub()
                self._main_power_sensor_unsub = None

            if self._monitored_sensors_unsub:
                logger.debug("Unsubscribing from monitored sensor events")
                self._monitored_sensors_unsub()
                self._monitored_sensors_unsub = None

            if self._appliance_unsub:
                logger.debug("Unsubscribing from appliance events")
                self._appliance_unsub()
                self._appliance_unsub = None

            self._current_sensor_power.clear()
            self._balanced_off_appliances.clear()
            self._estimated_total_power = 0.0
            self._event_log_sensor = None

            logger.info("PowerLoadBalancer cleanup completed successfully")

        except Exception as exc:
            logger.exception("Error during PowerLoadBalancer cleanup")
            msg = f"Failed to cleanup PowerLoadBalancer: {exc}"
            raise ConfigurationError(
                msg,
                details={"error": str(exc)},
            ) from exc

    @property
    def is_balancing_enabled(self) -> bool:
        """Return the current state of balancing."""
        return self._is_balancing_enabled

    @property
    def device_id(self) -> str | None:
        """Return the device ID."""
        return self._device_id

    def enable_balancing(self) -> None:
        """Enable the load balancing."""
        self._is_balancing_enabled = True
        _LOGGER.info("Power load balancing enabled.")
        if self._event_log_sensor:
            self._event_log_sensor.add_log_entry("Power load balancing enabled.")
        self.async_check_and_balance()

    def disable_balancing(self) -> None:
        """Disable the load balancing."""
        self._is_balancing_enabled = False
        _LOGGER.info("Power load balancing disabled.")
        if self._event_log_sensor:
            self._event_log_sensor.add_log_entry("Power load balancing disabled.")

    def register_event_log_sensor(self, sensor: PowerBalancerLogSensor) -> None:
        """Register the event log sensor instance."""
        self._event_log_sensor = sensor

    @staticmethod
    def _convert_power_to_watts(power: float, state: State) -> float:
        """Convert power value to watts based on unit of measurement."""
        unit = state.attributes.get("unit_of_measurement", "W").lower()
        if unit in ["kw", "kilowatt", "kilowatts"]:
            return power * 1000
        if unit in ["mw", "megawatt", "megawatts"]:
            return power * 1000000
        return power

    async def _handle_power_sensor_state_change(self, event: Any) -> None:
        """Handle state changes for power sensors triggered by Home Assistant events."""
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

            self.async_check_and_balance()

        except Exception as exc:
            logger.exception("Unexpected error in power sensor state change handler")
            msg = f"Failed to handle power sensor state change: {exc}"
            raise PowerSensorError(
                msg,
                details={"error": str(exc)},
            ) from exc

    async def _handle_appliance_state_change(self, event: Any) -> None:
        """Handle state changes for controllable appliances."""
        entity_id = event.data.get("entity_id")
        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")

        if new_state is None or new_state.state in ("unknown", "unavailable"):
            _LOGGER.debug("Ignoring invalid state for appliance %s", entity_id)
            return

        _LOGGER.debug(
            "Appliance %s state changed from %s to %s",
            entity_id,
            old_state.state if old_state else None,
            new_state.state,
        )

        if new_state.state == "on":
            await self._handle_appliance_turn_on(entity_id)
        elif new_state.state == "off" and old_state and old_state.state == "on":
            self._handle_appliance_turn_off(entity_id)

        self.async_check_and_balance()

    async def _handle_appliance_turn_on(self, entity_id: str) -> None:
        """Handle an appliance being turned on."""
        if entity_id in self._balanced_off_appliances:
            self._balanced_off_appliances.pop(entity_id)

        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == entity_id:
                power_to_add = self._calculate_sensor_power(sensor_config)

                if self._would_exceed_budget(power_to_add):
                    await self._turn_off_appliance(
                        entity_id,
                        reason=f"Power {power_to_add}W would exceed budget",
                    )
                    return

                self._update_power_estimates(sensor_config, power_to_add)
                self.async_check_and_balance()
                break

    def _handle_appliance_turn_off(self, entity_id: str) -> None:
        """Handle an appliance being turned off."""
        sensor_id = self._get_sensor_for_appliance(entity_id)

        if (
            entity_id not in self._balanced_off_appliances
            and sensor_id in self._current_sensor_power
        ):
            self._estimated_total_power -= self._current_sensor_power[sensor_id]
            self._current_sensor_power.pop(sensor_id)

    def _calculate_sensor_power(self, sensor_config: dict[str, Any]) -> float:
        """Calculate the current power consumption for a sensor."""
        sensor_id = sensor_config.get(CONF_ENTITY_ID)
        if not sensor_id:
            return 0.0

        sensor_state = self.hass.states.get(sensor_id)
        if not sensor_state or sensor_state.state in ("unknown", "unavailable"):
            return 0.0

        try:
            raw_power = float(sensor_state.state)
            current_power = self._convert_power_to_watts(raw_power, sensor_state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Could not convert state for sensor %s to float: %s",
                sensor_id,
                sensor_state.state,
            )
            return 0.0
        else:
            return current_power if current_power > 0 else 0.0

    def _would_exceed_budget(self, power_to_add: float) -> bool:
        """Check if adding the given power would exceed the budget."""
        return (self._estimated_total_power + power_to_add) > self._power_budget

    def _update_power_estimates(
        self, sensor_config: dict[str, Any], power_to_add: float
    ) -> None:
        """Update the power estimates with the new power value."""
        self._estimated_total_power += power_to_add
        sensor_id = sensor_config.get(CONF_ENTITY_ID)
        if sensor_id:
            self._current_sensor_power[sensor_id] = power_to_add
            _LOGGER.debug(
                "Added power %s W for %s to estimate",
                power_to_add,
                sensor_config.get(CONF_APPLIANCE),
            )

    @callback
    def get_total_house_power(self) -> float:
        """Return the current total house power as measured by the main power sensor."""
        return self._estimated_total_power

    @callback
    def async_check_and_balance(self) -> None:
        """Check power usage and perform balancing if necessary."""
        if not self.is_balancing_enabled:
            _LOGGER.debug("Balancing is disabled. Skipping check.")
            return

        current_total_power = self.get_total_house_power()
        _LOGGER.debug(
            "Checking balance: Current total power = %s W, Budget = %s W, "
            "Estimated total = %s W",
            current_total_power,
            self._power_budget,
            self._estimated_total_power,
        )

        if current_total_power > self._power_budget:
            _LOGGER.warning(
                "Total power %s W exceeds budget %s W. Initiating balancing.",
                current_total_power,
                self._power_budget,
            )
            self._balance_down()
        elif current_total_power <= self._power_budget:
            _LOGGER.debug(
                "Total power %s W is within budget %s W.",
                current_total_power,
                self._power_budget,
            )

    @callback
    def _balance_down(self) -> None:
        """Turn off appliances to bring power usage below budget."""
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

            appliance_state: State | None = self.hass.states.get(appliance_entity_id)

            if (
                appliance_state
                and appliance_state.state == "on"
                and appliance_entity_id not in self._balanced_off_appliances
                and not is_last_resort
            ):
                # Get the expected power reduction before turning off the appliance
                expected_power_reduction = self._get_sensor_power_for_appliance(
                    appliance_entity_id
                )

                _LOGGER.info(
                    "Turning off %s (importance %s) to balance power. "
                    "Expected power reduction: %s W",
                    appliance_entity_id,
                    sensor_config.get(CONF_IMPORTANCE),
                    expected_power_reduction,
                )

                # Optimistically subtract the expected power reduction immediately
                # to prevent race conditions in rapid successive balancing calls
                if expected_power_reduction > 0:
                    self._estimated_total_power -= expected_power_reduction
                    _LOGGER.debug(
                        "Optimistically reduced estimated power by %s W for %s. "
                        "New estimated total: %s W",
                        expected_power_reduction,
                        appliance_entity_id,
                        self._estimated_total_power,
                    )

                async def turn_off_task(
                    appliance_entity_id: str = appliance_entity_id,
                ) -> None:
                    try:
                        await self.async_turn_off_appliance_service(
                            appliance_entity_id,
                            reason=(
                                f"Automatic balancing: Exceeded budget of "
                                f"{self._power_budget} W"
                            ),
                        )
                    except Exception:
                        _LOGGER.exception(
                            "Failed to turn off appliance %s",
                            appliance_entity_id,
                        )

                self.hass.async_create_task(turn_off_task())
                return

        _LOGGER.warning(
            "Could not balance power below budget by turning off non-last-resort "
            "appliances."
        )

    @retry_with_backoff(max_retries=2, backoff_factor=0.5, retry_on=(RetryableError,))
    async def _turn_off_appliance(self, entity_id: str, reason: str = "") -> None:
        """
        Turn off a specified appliance and record it.

        Adds context to the service call to indicate the action was performed by
        the Power Load Balancer.
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

            self._balanced_off_appliances[entity_id] = {
                "timestamp": self.hass.loop.time(),
                "reason": reason or "Turned off by Power Load Balancer",
            }

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

    @callback
    def _get_appliance_for_sensor(self, sensor_entity_id: str) -> str | None:
        """Get the appliance entity ID associated with a monitored sensor."""
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_ENTITY_ID) == sensor_entity_id:
                return sensor_config.get(CONF_APPLIANCE)
        return None

    @callback
    def _get_sensor_for_appliance(self, appliance_entity_id: str) -> str | None:
        """Get the monitored sensor entity ID associated with an appliance."""
        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == appliance_entity_id:
                return sensor_config.get(CONF_ENTITY_ID)
        return None

    @callback
    def _get_sensor_power_for_appliance(self, appliance_entity_id: str) -> float:
        """Get the current power consumption for an appliance's sensor."""
        sensor_id = self._get_sensor_for_appliance(appliance_entity_id)
        if sensor_id and sensor_id in self._current_sensor_power:
            return self._current_sensor_power[sensor_id]
        return 0.0

    def manages_entity(self, entity_id: str) -> bool:
        """Check if this PowerLoadBalancer instance manages the given entity."""
        if entity_id == self._main_power_sensor_entity_id:
            return True

        monitored_entities = [s.get(CONF_ENTITY_ID) for s in self._monitored_sensors]
        if entity_id in monitored_entities:
            return True

        appliance_entities = [s.get(CONF_APPLIANCE) for s in self._monitored_sensors]
        return entity_id in appliance_entities

    async def async_turn_off_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """Handle the turn_off_appliance service call with enhanced logging."""
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

    async def async_turn_on_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """Handle the turn_on_appliance service call with enhanced logging."""
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


class PowerBalancerControlSwitch(SwitchEntity):
    """Representation of the Power Load Balancer control switch."""

    _balancer: PowerLoadBalancer
    _attr_name: str
    _attr_unique_id: str
    _attr_is_on: bool

    def __init__(self, balancer: PowerLoadBalancer) -> None:
        """Initialize the switch."""
        self._balancer = balancer
        self._attr_name = "Power Load Balancer Control"
        self._attr_unique_id = f"{self._balancer.entry.entry_id}_control_switch"
        self._attr_is_on = self._balancer.is_balancing_enabled

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return the device info."""
        if self._balancer.entry.entry_id:
            return DeviceInfo(
                identifiers={(DOMAIN, self._balancer.entry.entry_id)},
                name="Power Load Balancer",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
            )
        return None

    async def async_turn_on(self) -> None:
        """Turn the balancer on."""
        self._balancer.enable_balancing()
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the balancer off."""
        self._balancer.disable_balancing()
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._attr_is_on


class PowerBalancerLogSensor(SensorEntity):
    """Representation of the Power Load Balancer event log sensor."""

    _balancer: PowerLoadBalancer
    _attr_name: str
    _attr_unique_id: str
    _attr_native_value: str
    _attr_extra_state_attributes: dict[str, Any]

    def __init__(self, balancer: PowerLoadBalancer) -> None:
        """Initialize the sensor."""
        self._balancer = balancer
        self._attr_name = "Power Load Balancer Log"
        self._attr_unique_id = f"{self._balancer.entry.entry_id}_event_log"
        self._attr_native_value = "Initialized"
        self._attr_extra_state_attributes = {"events": []}

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self._balancer.register_event_log_sensor(self)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return the device info."""
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
        """Add an entry to the event log."""
        timestamp = self.hass.loop.time()
        log_entry = f"{timestamp:.0f} - {message}"
        self._attr_extra_state_attributes["events"].append(log_entry)
        max_log_size = 50
        if len(self._attr_extra_state_attributes["events"]) > max_log_size:
            self._attr_extra_state_attributes["events"] = (
                self._attr_extra_state_attributes["events"][-max_log_size:]
            )

        self._attr_native_value = message
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes."""
        return self._attr_extra_state_attributes
