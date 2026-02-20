"""
Core balancing logic for the Power Load Balancer integration.

This module contains the PowerLoadBalancer class which manages power monitoring,
appliance control, and automatic load balancing based on a configured power budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Context, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from .appliance_controller import ApplianceController
from .balancing_engine import BalancingCallbacks, BalancingEngine
from .const import (
    CONF_APPLIANCE,
    CONF_COOLDOWN_SECONDS,
    CONF_MAIN_POWER_SENSOR,
    CONF_POWER_BUDGET_WATT,
    CONF_POWER_SENSORS,
    DEFAULT_COOLDOWN_SECONDS,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
)
from .context_logger import ContextLogger
from .exceptions import ConfigurationError
from .power_monitor import PowerMonitor

_LOGGER = logging.getLogger(__name__)
AVAILABILITY_EVENT_HISTORY_SIZE = 100
UNAVAILABLE_ENTITY_ISSUE_PREFIX = "balancing_entity_unavailable"


class PowerLoadBalancer:
    """
    Core class for managing power load balancing.

    This class coordinates the power monitor, appliance controller, and balancing engine
    to automatically turn off appliances when power exceeds the budget and restore them
    when power headroom allows.

    Attributes:
        hass: Home Assistant instance.
        entry: Configuration entry for this integration.

    """

    hass: HomeAssistant
    entry: ConfigEntry
    _config_data: dict[str, Any]
    _event_log_sensor: Any
    _device_id: str | None
    _main_power_sensor_entity_id: str
    _monitored_sensors: list[dict[str, Any]]
    _power_budget: int
    _main_power_sensor_unsub: Callable[[], None] | None
    _monitored_sensors_unsub: Callable[[], None] | None
    _appliance_unsub: Callable[[], None] | None
    _power_monitor: PowerMonitor
    _appliance_controller: ApplianceController
    _balancing_engine: BalancingEngine
    _was_over_budget: bool
    _unavailable_entities: dict[str, dict[str, Any]]
    _availability_events: list[dict[str, Any]]

    def __init__(
        self,
        hass: HomeAssistant,
        config_data: dict[str, Any],
        entry: ConfigEntry,
    ) -> None:
        """
        Initialize the PowerLoadBalancer.

        Args:
            hass: Home Assistant instance.
            config_data: Configuration data dictionary.
            entry: Configuration entry for this integration.

        """
        self.hass = hass
        self.entry = entry
        self._config_data = config_data
        self._event_log_sensor = None
        self._device_id = None
        self._main_power_sensor_unsub = None
        self._monitored_sensors_unsub = None
        self._appliance_unsub = None
        self._was_over_budget = False
        self._unavailable_entities = {}
        self._availability_events = []

        self._main_power_sensor_entity_id = config_data[CONF_MAIN_POWER_SENSOR]
        self._monitored_sensors = config_data.get(CONF_POWER_SENSORS, [])
        self._power_budget = config_data[CONF_POWER_BUDGET_WATT]
        self._global_cooldown_seconds = config_data.get(
            CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS
        )

        self._power_monitor = PowerMonitor(
            hass,
            self._main_power_sensor_entity_id,
            self._monitored_sensors,
            self._power_budget,
        )
        self._appliance_controller = ApplianceController(
            hass, self._monitored_sensors, self._global_cooldown_seconds
        )
        self._balancing_engine = BalancingEngine(
            hass, self._monitored_sensors, self._power_budget
        )

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

        Initializes state tracking, registers event listeners for power sensors
        and appliances, and creates the device registry entry.
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

        self._power_monitor.initialize_power_tracking()

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

        self._initialize_availability_tracking()

        _LOGGER.debug("PowerLoadBalancer setup complete.")

    def _record_availability_event(self, event: dict[str, Any]) -> None:
        """Record an availability event for diagnostics history."""
        self._availability_events.append(event)
        if len(self._availability_events) > AVAILABILITY_EVENT_HISTORY_SIZE:
            self._availability_events = self._availability_events[
                -AVAILABILITY_EVENT_HISTORY_SIZE:
            ]

    def _get_unavailable_issue_id(self, entity_id: str) -> str:
        """Return a stable issue ID for an unavailable balancing entity."""
        sanitized_entity = entity_id.replace(".", "_")
        return (
            f"{UNAVAILABLE_ENTITY_ISSUE_PREFIX}_{self.entry.entry_id}_"
            f"{sanitized_entity}"
        )

    def _mark_entity_unavailable(
        self, entity_id: str, entity_type: str, state: str | None
    ) -> None:
        """Track when an entity becomes unavailable for balancing."""
        now_iso = dt.utcnow().isoformat(timespec="seconds")
        if entity_id in self._unavailable_entities:
            self._unavailable_entities[entity_id]["last_seen"] = now_iso
            self._unavailable_entities[entity_id]["state"] = state
            return

        unavailable_info = {
            "entity": entity_id,
            "entity_type": entity_type,
            "state": state,
            "first_seen": now_iso,
            "last_seen": now_iso,
        }
        self._unavailable_entities[entity_id] = unavailable_info

        self._record_availability_event(
            {
                "timestamp": now_iso,
                "event": "became_unavailable",
                "entity": entity_id,
                "entity_type": entity_type,
                "state": state,
                "reason": "entity unavailable for balancing",
            }
        )

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._get_unavailable_issue_id(entity_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRANSLATION_KEY_DEVICE_UNAVAILABLE,
            translation_placeholders={"entity_id": entity_id},
        )

        _LOGGER.warning(
            "Entity %s became unavailable for balancing (type=%s, state=%s)",
            entity_id,
            entity_type,
            state,
        )

    def _mark_entity_available(
        self, entity_id: str, entity_type: str, state: str | None
    ) -> None:
        """Track when an entity becomes available again for balancing."""
        unavailable_info = self._unavailable_entities.pop(entity_id, None)
        if unavailable_info is None:
            return

        now_iso = dt.utcnow().isoformat(timespec="seconds")
        self._record_availability_event(
            {
                "timestamp": now_iso,
                "event": "restored",
                "entity": entity_id,
                "entity_type": entity_type,
                "state": state,
                "unavailable_since": unavailable_info.get("first_seen"),
                "reason": "entity restored for balancing",
            }
        )

        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            self._get_unavailable_issue_id(entity_id),
        )

        _LOGGER.info(
            "Entity %s is available again for balancing (type=%s, state=%s)",
            entity_id,
            entity_type,
            state,
        )

    def _initialize_availability_tracking(self) -> None:
        """Capture initial availability of all entities relevant to balancing."""
        tracked_entities: list[tuple[str, str]] = [
            (self._main_power_sensor_entity_id, "main_power_sensor"),
        ]
        tracked_entities.extend(
            (sensor_config[CONF_ENTITY_ID], "power_sensor")
            for sensor_config in self._monitored_sensors
        )
        tracked_entities.extend(
            (sensor_config[CONF_APPLIANCE], "appliance")
            for sensor_config in self._monitored_sensors
        )

        for entity_id, entity_type in tracked_entities:
            state = self.hass.states.get(entity_id)
            state_value = state.state if state is not None else None
            if state is None or state_value in ("unknown", "unavailable"):
                self._mark_entity_unavailable(entity_id, entity_type, state_value)

    def _clear_unavailable_entity_issues(self) -> None:
        """Delete all outstanding Repairs issues for unavailable entities."""
        for entity_id in list(self._unavailable_entities):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._get_unavailable_issue_id(entity_id),
            )

    async def async_cleanup(self) -> None:
        """
        Clean up the PowerLoadBalancer and unsubscribe from events.

        Cancels all scheduled tasks and clears internal state.
        """
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

            self._power_monitor.clear_tracking()
            self._appliance_controller.cleanup()
            self._clear_unavailable_entity_issues()
            self._unavailable_entities.clear()
            self._availability_events.clear()
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
    def device_id(self) -> str | None:
        """Return the device ID."""
        return self._device_id

    def register_event_log_sensor(self, sensor: Any) -> None:
        """
        Register the event log sensor instance.

        Args:
            sensor: The PowerBalancerLogSensor instance to register.

        """
        self._event_log_sensor = sensor
        self._appliance_controller.set_event_log_sensor(sensor)

    async def _handle_power_sensor_state_change(self, event: Any) -> None:
        """
        Handle state changes for power sensors triggered by Home Assistant events.

        Args:
            event: The state change event from Home Assistant.

        """
        entity_id = event.data.get("entity_id") if hasattr(event, "data") else None
        new_state = event.data.get("new_state") if hasattr(event, "data") else None

        if isinstance(entity_id, str):
            is_main_sensor = entity_id == self._main_power_sensor_entity_id
            entity_type = "main_power_sensor" if is_main_sensor else "power_sensor"
            state_value = new_state.state if new_state is not None else None

            if new_state is None or state_value in ("unknown", "unavailable"):
                self._mark_entity_unavailable(entity_id, entity_type, state_value)
            else:
                self._mark_entity_available(entity_id, entity_type, state_value)

        await self._power_monitor.handle_power_sensor_state_change(
            event, self.async_check_and_balance
        )

    async def _handle_appliance_state_change(self, event: Any) -> None:
        """Handle state changes for controllable appliances."""
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        state_value = new_state.state if new_state is not None else None

        if new_state is None or state_value in ("unknown", "unavailable"):
            if isinstance(entity_id, str):
                self._mark_entity_unavailable(entity_id, "appliance", state_value)
            _LOGGER.debug("Ignoring invalid state for appliance %s", entity_id)
            return

        if isinstance(entity_id, str):
            self._mark_entity_available(entity_id, "appliance", state_value)

        _LOGGER.debug(
            "Appliance %s state changed from %s to %s",
            entity_id,
            old_state.state if old_state else None,
            new_state.state,
        )

        is_climate = entity_id.startswith("climate.")
        old_state_value = old_state.state if old_state else None

        if is_climate:
            is_now_active = new_state.state not in ("off", "unknown", "unavailable")
            was_active = old_state_value not in ("off", "unknown", "unavailable", None)
        else:
            is_now_active = new_state.state == "on"
            was_active = old_state_value == "on"

        if is_now_active and not was_active:
            await self._handle_appliance_turn_on(entity_id)
        elif not is_now_active and was_active:
            self._handle_appliance_turn_off(entity_id)

        self.async_check_and_balance()

    async def _handle_appliance_turn_on(self, entity_id: str) -> None:
        """Handle an appliance being turned on."""
        self._appliance_controller.remove_from_balanced_off(entity_id)
        self._appliance_controller.cancel_scheduled_turn_on(entity_id)

        for sensor_config in self._monitored_sensors:
            if sensor_config.get(CONF_APPLIANCE) == entity_id:
                power_to_add = self._power_monitor.calculate_sensor_power(sensor_config)

                if self._power_monitor.would_exceed_budget(power_to_add):
                    await self._appliance_controller.turn_off_appliance(
                        entity_id,
                        reason=f"Power {power_to_add}W would exceed budget",
                    )
                    return

                self._power_monitor.update_power_estimates(sensor_config, power_to_add)
                self.async_check_and_balance()
                break

    def _handle_appliance_turn_off(self, entity_id: str) -> None:
        """Handle an appliance being turned off."""
        sensor_id = self._appliance_controller.get_sensor_for_appliance(entity_id)

        if (
            not self._appliance_controller.is_appliance_balanced_off(entity_id)
            and sensor_id
        ):
            self._power_monitor.remove_sensor_power(sensor_id)

    @callback
    def get_total_house_power(self) -> float:
        """Return the current total house power as measured by the main power sensor."""
        return self._power_monitor.get_total_house_power()

    @callback
    def async_check_and_balance(self) -> None:
        """Check power usage and perform balancing if necessary."""
        current_total_power = self.get_total_house_power()
        is_over_budget = current_total_power > self._power_budget

        _LOGGER.debug(
            "Checking balance: Current total power = %s W, Budget = %s W",
            current_total_power,
            self._power_budget,
        )

        if is_over_budget:
            if not self._was_over_budget:
                _LOGGER.warning(
                    "Total power %s W exceeds budget %s W. Initiating balancing.",
                    current_total_power,
                    self._power_budget,
                )
            else:
                _LOGGER.debug(
                    "Total power %s W remains above budget %s W.",
                    current_total_power,
                    self._power_budget,
                )
            self._balance_down()
        else:
            if self._was_over_budget:
                _LOGGER.info(
                    "Power returned within budget: %s W <= %s W",
                    current_total_power,
                    self._power_budget,
                )
            _LOGGER.debug(
                "Total power %s W is within budget %s W.",
                current_total_power,
                self._power_budget,
            )
            self._balance_up()

        self._was_over_budget = is_over_budget

    def get_diagnostics_snapshot(self) -> dict[str, Any]:
        """Return runtime diagnostics data for troubleshooting."""
        return {
            "entry_id": self.entry.entry_id,
            "power_budget_watt": self._power_budget,
            "main_power_sensor_entity_id": self._main_power_sensor_entity_id,
            "monitored_sensor_count": len(self._monitored_sensors),
            "monitored_sensors": self._monitored_sensors,
            "is_over_budget": self._was_over_budget,
            "listener_status": {
                "main_power_sensor_listener": self._main_power_sensor_unsub is not None,
                "monitored_sensors_listener": self._monitored_sensors_unsub is not None,
                "appliance_listener": self._appliance_unsub is not None,
            },
            "availability": {
                "currently_unavailable": dict(self._unavailable_entities),
                "recent_events": list(self._availability_events),
            },
            "power_monitor": self._power_monitor.get_diagnostics_snapshot(),
            "appliance_controller": (
                self._appliance_controller.get_diagnostics_snapshot()
            ),
        }

    @callback
    def _balance_up(self) -> None:
        """Turn on appliances that can safely fit within power budget."""
        callbacks = BalancingCallbacks(
            get_total_power=self.get_total_house_power,
            get_expected_power_restoration=(
                self._appliance_controller.get_expected_power_restoration
            ),
            get_sensor_power_for_appliance=self._get_sensor_power_for_appliance,
            cancel_scheduled_turn_on=(
                self._appliance_controller.cancel_scheduled_turn_on
            ),
            reduce_estimated_power=self._power_monitor.reduce_estimated_power,
            is_appliance_balanced_off=(
                self._appliance_controller.is_appliance_balanced_off
            ),
        )

        self._balancing_engine.balance_up(
            callbacks,
            self._appliance_controller.get_balanced_off_appliances(),
            self._restore_appliance,
        )

    async def _restore_appliance(self, entity_id: str, reason: str) -> None:
        """
        Restore an appliance and remove it from balanced off tracking.

        Args:
            entity_id: Entity ID of the appliance to restore.
            reason: Reason for restoring the appliance.

        """
        await self._appliance_controller.turn_on_appliance_service(entity_id, reason)
        self._appliance_controller.remove_from_balanced_off(entity_id)

    @callback
    def _balance_down(self) -> None:
        """Turn off appliances to bring power usage below budget."""
        self._balancing_engine.balance_down(
            self._get_sensor_power_for_appliance,
            self._power_monitor.reduce_estimated_power,
            self._appliance_controller.is_appliance_balanced_off,
            self._turn_off_appliance_for_balancing,
        )

    async def _turn_off_appliance_for_balancing(
        self, entity_id: str, reason: str
    ) -> None:
        """
        Turn off an appliance for balancing and schedule auto turn on.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.

        """
        await self._appliance_controller.turn_off_appliance_service(entity_id, reason)
        self._appliance_controller.mark_appliance_balanced_off(entity_id, reason)

        expected_power = self._get_sensor_power_for_appliance(entity_id)
        self._appliance_controller.schedule_auto_turn_on(
            entity_id,
            expected_power,
            self.get_total_house_power,
            self._power_budget,
        )

    def _get_sensor_power_for_appliance(self, appliance_entity_id: str) -> float:
        """Get the current power consumption for an appliance's sensor."""
        sensor_id = self._appliance_controller.get_sensor_for_appliance(
            appliance_entity_id
        )
        if sensor_id:
            return self._power_monitor.get_sensor_power(sensor_id)
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
        """
        Handle the turn_off_appliance service call.

        Args:
            entity_id: Entity ID of the appliance to turn off.
            reason: Reason for turning off the appliance.
            context: Optional Home Assistant context.

        """
        await self._appliance_controller.turn_off_appliance_service(
            entity_id, reason, context
        )

    async def async_turn_on_appliance_service(
        self, entity_id: str, reason: str, context: Context | None = None
    ) -> None:
        """
        Handle the turn_on_appliance service call.

        Args:
            entity_id: Entity ID of the appliance to turn on.
            reason: Reason for turning on the appliance.
            context: Optional Home Assistant context.

        """
        await self._appliance_controller.turn_on_appliance_service(
            entity_id, reason, context
        )
