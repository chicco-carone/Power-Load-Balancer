"""Main module for the Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity import DeviceInfo
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

    async def _handle_power_sensor_state_change(self, event: object) -> None:
        """Handle state changes for power sensors triggered by Home Assistant events."""
        entity_id: str | None = None
        new_state: State | None = None
        if hasattr(event, "data") and isinstance(event.data, dict):
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
        else:
            return

        if new_state is None or new_state.state in ("unknown", "unavailable"):
            _LOGGER.debug("Ignoring invalid state for sensor %s", entity_id)
            if entity_id in self._current_sensor_power and entity_id is not None:
                self._current_sensor_power.pop(entity_id, None)
            return

        try:
            raw_power: float = float(new_state.state)
            power: float = self._convert_power_to_watts(raw_power, new_state)
            _LOGGER.debug(
                "Power sensor %s changed: %s W (raw: %s %s)",
                entity_id,
                power,
                raw_power,
                new_state.attributes.get("unit_of_measurement", "W"),
            )
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Could not convert state for sensor %s to float: %s",
                entity_id,
                new_state.state,
            )
            return

        if entity_id == self._main_power_sensor_entity_id:
            self._estimated_total_power = power
            _LOGGER.debug(
                "Updated estimated total power to: %s W", self._estimated_total_power
            )
        else:
            self._current_sensor_power[entity_id] = power

        self.async_check_and_balance()

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
            if entity_id in self._balanced_off_appliances:
                self._balanced_off_appliances.pop(entity_id)

            for sensor_config in self._monitored_sensors:
                if sensor_config.get(CONF_APPLIANCE) == entity_id:
                    sensor_id = sensor_config.get(CONF_ENTITY_ID)

                    current_power = 0.0
                    if sensor_id:
                        sensor_state = self.hass.states.get(sensor_id)
                        if sensor_state and sensor_state.state not in (
                            "unknown",
                            "unavailable",
                        ):
                            try:
                                raw_power = float(sensor_state.state)
                                current_power = self._convert_power_to_watts(
                                    raw_power, sensor_state
                                )
                            except (ValueError, TypeError):
                                _LOGGER.warning(
                                    "Could not convert state for sensor %s to float: %s",
                                    sensor_id,
                                    sensor_state.state,
                                )

                    power_to_add = current_power if current_power > 0 else 0.0

                    if (
                        self._estimated_total_power + power_to_add
                    ) > self._power_budget:
                        _LOGGER.warning(
                            "Turning off %s - power %s W would exceed budget %s W",
                            entity_id,
                            power_to_add,
                            self._power_budget,
                        )
                        await self._turn_off_appliance(
                            entity_id,
                            reason=f"Power {power_to_add}W would exceed budget",
                        )
                        return

                    self._estimated_total_power += power_to_add
                    if sensor_id:
                        self._current_sensor_power[sensor_id] = power_to_add
                    _LOGGER.debug(
                        "Added power %s W for %s to estimate",
                        power_to_add,
                        entity_id,
                    )

                    self.async_check_and_balance()

        elif new_state.state == "off" and old_state and old_state.state == "on":
            sensor_id = self._get_sensor_for_appliance(entity_id)

            if entity_id not in self._balanced_off_appliances:
                if sensor_id in self._current_sensor_power:
                    self._estimated_total_power -= self._current_sensor_power[sensor_id]
                    self._current_sensor_power.pop(sensor_id)
                else:
                    for sensor_config in self._monitored_sensors:
                        if sensor_config.get(CONF_APPLIANCE) == entity_id:
                            break

        self.async_check_and_balance()

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
                _LOGGER.info(
                    "Turning off %s (importance %s) to balance power.",
                    appliance_entity_id,
                    sensor_config.get(CONF_IMPORTANCE),
                )
                self.hass.async_create_task(
                    self._turn_off_appliance(
                        appliance_entity_id,
                        reason=f"Exceeded budget of {self._power_budget} W",
                    )
                )
                return

        _LOGGER.warning(
            "Could not balance power below budget by turning off non-last-resort "
            "appliances."
        )

    async def _turn_off_appliance(self, entity_id: str, reason: str = "") -> None:
        """Turn off a specified appliance and record it."""
        _LOGGER.debug("Calling turn_off service for %s", entity_id)
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "turn_off",
                {"entity_id": entity_id},
                blocking=False,
            )
            self._balanced_off_appliances[entity_id] = {
                "timestamp": self.hass.loop.time(),
                "reason": reason,
            }
            _LOGGER.info(
                "Turned off appliance %s by balancer. Reason: %s", entity_id, reason
            )
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Turned off {entity_id}. Reason: {reason}"
                )

        except RuntimeError:
            _LOGGER.exception("Failed to turn off appliance %s", entity_id)
            if self._event_log_sensor:
                self._event_log_sensor.add_log_entry(
                    f"Failed to turn off {entity_id}. Error: RuntimeError"
                )

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
