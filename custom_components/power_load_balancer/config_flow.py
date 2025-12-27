"""
Config flow for Power Load Balancer integration.

This module handles the configuration UI for setting up the Power Load Balancer,
including the main power sensor configuration and monitored appliance setup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_APPLIANCE,
    CONF_COOLDOWN_SECONDS,
    CONF_DEVICE_COOLDOWN,
    CONF_IMPORTANCE,
    CONF_LAST_RESORT,
    CONF_MAIN_POWER_SENSOR,
    CONF_POWER_BUDGET_WATT,
    CONF_POWER_SENSORS,
    DEFAULT_COOLDOWN_SECONDS,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceRegistry
    from homeassistant.helpers.entity_registry import EntityRegistry

try:
    from homeassistant.helpers.device_registry import (
        async_get as async_get_device_registry,
    )
    from homeassistant.helpers.entity_registry import (
        async_get as async_get_entity_registry,
    )
except ImportError:
    async_get_device_registry = None
    async_get_entity_registry = None

_LOGGER = logging.getLogger(__name__)

DEFAULT_IMPORTANCE = 5


def _get_power_sensor_selector() -> EntitySelector:
    """Return the entity selector for power sensors."""
    return EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))


def _get_appliance_selector() -> EntitySelector:
    """Return the entity selector for controllable appliances."""
    return EntitySelector(EntitySelectorConfig(domain=["switch", "light"]))


def _build_sensor_edit_schema(initial_data: dict[str, Any]) -> vol.Schema:
    """
    Build a schema for editing a sensor configuration.

    Args:
        initial_data: Current values to use as defaults.

    Returns:
        A voluptuous schema with the current values as defaults.

    """
    schema_dict: dict[Any, Any] = {}

    entity_id = initial_data.get(CONF_ENTITY_ID)
    if entity_id is not None:
        schema_dict[vol.Required(CONF_ENTITY_ID, default=entity_id)] = (
            _get_power_sensor_selector()
        )
    else:
        schema_dict[vol.Required(CONF_ENTITY_ID)] = _get_power_sensor_selector()

    name = initial_data.get(CONF_NAME)
    if name is not None:
        schema_dict[vol.Optional(CONF_NAME, default=name)] = TextSelector(
            TextSelectorConfig()
        )
    else:
        schema_dict[vol.Optional(CONF_NAME)] = TextSelector(TextSelectorConfig())

    importance = initial_data.get(CONF_IMPORTANCE, DEFAULT_IMPORTANCE)
    schema_dict[vol.Required(CONF_IMPORTANCE, default=importance)] = NumberSelector(
        NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
    )

    last_resort = initial_data.get(CONF_LAST_RESORT, False)
    schema_dict[vol.Required(CONF_LAST_RESORT, default=last_resort)] = bool

    appliance = initial_data.get(CONF_APPLIANCE)
    if appliance is not None:
        schema_dict[vol.Required(CONF_APPLIANCE, default=appliance)] = (
            _get_appliance_selector()
        )
    else:
        schema_dict[vol.Required(CONF_APPLIANCE)] = _get_appliance_selector()

    device_cooldown = initial_data.get(CONF_DEVICE_COOLDOWN)
    schema_dict[vol.Optional(CONF_DEVICE_COOLDOWN, default=device_cooldown)] = (
        NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=3600,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )
    )

    schema_dict[vol.Optional("remove_sensor")] = bool

    return vol.Schema(schema_dict)


def _get_friendly_name_for_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """
    Return a friendly name for the device connected to the given sensor.

    Tries to get the device name from the device registry.
    Falls back to the entity's original_name or name.

    Args:
        hass: Home Assistant instance.
        entity_id: The entity ID to look up.

    Returns:
        A friendly name string, or None if unavailable.

    """
    if async_get_device_registry is None or async_get_entity_registry is None:
        return None

    entity_registry: EntityRegistry | None = async_get_entity_registry(hass)
    if entity_registry is None:
        return None

    entity = entity_registry.entities.get(str(entity_id))
    if entity is None:
        return None

    result: str | None = None

    if entity.device_id:
        device_registry: DeviceRegistry | None = async_get_device_registry(hass)
        if device_registry is not None:
            device = device_registry.devices.get(entity.device_id)
            if device is not None and device.name:
                result = device.name

    if result is None and hasattr(entity, "original_name") and entity.original_name:
        result = entity.original_name

    if result is None and hasattr(entity, "name") and entity.name:
        result = entity.name

    return result


def _build_menu_options(
    config_data: dict[str, Any],
    *,
    include_finish: bool = True,
) -> dict[str, str]:
    """
    Build the menu options for the config flow.

    Args:
        config_data: Current configuration data.
        include_finish: Whether to include the finish option.

    Returns:
        Dictionary mapping action keys to display labels.

    """
    options: dict[str, str] = {}

    main_sensor_text = (
        "Edit Main Sensor Settings"
        if config_data.get(CONF_MAIN_POWER_SENSOR)
        else "Configure Main Sensor"
    )
    options["edit_main_sensor"] = main_sensor_text
    options["add_sensor"] = "Add New Monitored Sensor"

    configured_sensors: list[dict[str, Any]] = config_data.get(CONF_POWER_SENSORS, [])
    for i, sensor_config in enumerate(configured_sensors):
        sensor_name = sensor_config.get(CONF_NAME) or sensor_config.get(
            CONF_ENTITY_ID, f"Sensor {i + 1}"
        )
        options[f"edit_sensor_{i}"] = f"Edit: {sensor_name}"

    if include_finish:
        options["finish"] = "Save Configuration"

    return options


def _process_sensor_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """
    Process and validate sensor input.

    Args:
        hass: Home Assistant instance.
        user_input: User input dictionary.

    Returns:
        Tuple of (errors dict, processed sensor config or None if errors).

    """
    errors: dict[str, str] = {}

    sensor_entity_id = user_input.get(CONF_ENTITY_ID)
    appliance_entity_id = user_input.get(CONF_APPLIANCE)
    custom_name = user_input.get(CONF_NAME)

    if not sensor_entity_id:
        errors[CONF_ENTITY_ID] = "select_sensor_required"
    if not appliance_entity_id:
        errors[CONF_APPLIANCE] = "select_appliance_required"

    if errors:
        return errors, None

    friendly_name = _get_friendly_name_for_entity(hass, str(sensor_entity_id))
    if custom_name:
        name_to_use = str(custom_name)
    elif friendly_name:
        name_to_use = friendly_name
    else:
        name_to_use = str(sensor_entity_id)

    device_cooldown = user_input.get(CONF_DEVICE_COOLDOWN)
    if device_cooldown is not None and device_cooldown <= 0:
        device_cooldown = None

    sensor_config: dict[str, Any] = {
        CONF_ENTITY_ID: sensor_entity_id,
        CONF_NAME: name_to_use,
        CONF_IMPORTANCE: user_input.get(CONF_IMPORTANCE, DEFAULT_IMPORTANCE),
        CONF_LAST_RESORT: user_input.get(CONF_LAST_RESORT, False),
        CONF_APPLIANCE: appliance_entity_id,
    }

    if device_cooldown is not None:
        sensor_config[CONF_DEVICE_COOLDOWN] = int(device_cooldown)

    return errors, sensor_config


STEP_ADD_SENSOR_SCHEMA: vol.Schema = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): _get_power_sensor_selector(),
        vol.Optional(CONF_NAME): TextSelector(TextSelectorConfig()),
        vol.Required(CONF_IMPORTANCE, default=DEFAULT_IMPORTANCE): NumberSelector(
            NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
        ),
        vol.Required(CONF_LAST_RESORT, default=False): bool,
        vol.Required(CONF_APPLIANCE): _get_appliance_selector(),
        vol.Optional(CONF_DEVICE_COOLDOWN): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=3600,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
    }
)


class PowerLoadBalancerConfigFlow(ConfigFlow, domain=DOMAIN):
    """
    Handle a config flow for Power Load Balancer.

    This class manages the initial setup flow for the integration,
    guiding users through main sensor configuration and appliance setup.
    """

    VERSION: int = 1
    _config_data: dict[str, Any]

    async def async_step_user(
        self,
        user_input: dict[str, object] | None = None,
    ) -> ConfigFlowResult:
        """
        Handle the initial configuration step and show the main menu.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult directing to the next step or completing setup.

        """
        _LOGGER.debug(
            "Opening config flow step: async_step_user, user_input=%s", user_input
        )
        if not hasattr(self, "_config_data"):
            self._config_data = {CONF_POWER_SENSORS: []}

        errors: dict[str, str] = {}

        if user_input is not None:
            action = user_input.get("action")
            if action == "edit_main_sensor":
                return await self.async_step_edit_main_sensor()
            if action == "add_sensor":
                return await self.async_step_add_sensor()
            if action and str(action).startswith("edit_sensor_"):
                try:
                    sensor_index = int(str(action).replace("edit_sensor_", ""))
                    return await self.async_step_edit_sensor(sensor_index=sensor_index)
                except ValueError:
                    errors["base"] = "invalid_edit_action"
            if action == "finish":
                if not self._config_data.get(CONF_MAIN_POWER_SENSOR):
                    errors["base"] = "main_sensor_required"
                else:
                    return self.async_create_entry(
                        title="Power Load Balancer", data=self._config_data
                    )

        options = _build_menu_options(self._config_data)
        options["finish"] = "Finish Configuration"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("action"): vol.In(options)}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Provide an options flow handler for editing existing configuration."""
        return PowerLoadBalancerOptionsFlow(config_entry)

    async def async_step_edit_main_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to add or edit the main sensor configuration.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening config flow step: async_step_edit_main_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            power_budget = user_input.get(CONF_POWER_BUDGET_WATT)
            if (
                power_budget is None
                or not isinstance(power_budget, int)
                or power_budget <= 0
            ):
                errors[CONF_POWER_BUDGET_WATT] = "valid_budget_required"
            if not errors:
                self._config_data[CONF_MAIN_POWER_SENSOR] = user_input[
                    CONF_MAIN_POWER_SENSOR
                ]
                self._config_data[CONF_POWER_BUDGET_WATT] = power_budget
                cooldown = user_input.get(CONF_COOLDOWN_SECONDS)
                if cooldown is not None:
                    self._config_data[CONF_COOLDOWN_SECONDS] = int(cooldown)
                else:
                    self._config_data[CONF_COOLDOWN_SECONDS] = DEFAULT_COOLDOWN_SECONDS
                return await self.async_step_user()

        schema_dict: dict[Any, Any] = {}
        current_sensor = self._config_data.get(CONF_MAIN_POWER_SENSOR)
        current_budget = self._config_data.get(CONF_POWER_BUDGET_WATT)
        current_cooldown = self._config_data.get(
            CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS
        )

        if current_sensor is not None:
            schema_dict[
                vol.Required(CONF_MAIN_POWER_SENSOR, default=current_sensor)
            ] = _get_power_sensor_selector()
        else:
            schema_dict[vol.Required(CONF_MAIN_POWER_SENSOR)] = (
                _get_power_sensor_selector()
            )

        if current_budget is not None:
            schema_dict[
                vol.Required(CONF_POWER_BUDGET_WATT, default=current_budget)
            ] = int
        else:
            schema_dict[vol.Required(CONF_POWER_BUDGET_WATT)] = int

        schema_dict[vol.Required(CONF_COOLDOWN_SECONDS, default=current_cooldown)] = (
            NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=3600,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            )
        )

        return self.async_show_form(
            step_id="edit_main_sensor",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to add a new monitored power sensor.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening config flow step: async_step_add_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, sensor_config = _process_sensor_input(self.hass, dict(user_input))
            if not errors and sensor_config:
                if CONF_POWER_SENSORS not in self._config_data or not isinstance(
                    self._config_data[CONF_POWER_SENSORS], list
                ):
                    self._config_data[CONF_POWER_SENSORS] = []
                self._config_data[CONF_POWER_SENSORS].append(sensor_config)
                return await self.async_step_user()

        return self.async_show_form(
            step_id="add_sensor",
            data_schema=STEP_ADD_SENSOR_SCHEMA,
            errors=errors,
        )

    async def async_step_edit_sensor(
        self,
        user_input: dict[str, object] | None = None,
        sensor_index: int | None = None,
    ) -> ConfigFlowResult:
        """
        Show form to edit an existing monitored power sensor.

        Args:
            user_input: User input from the form, or None on first display.
            sensor_index: Index of the sensor to edit in the sensors list.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening config flow step: async_step_edit_sensor, "
            "user_input=%s, sensor_index=%s",
            user_input,
            sensor_index,
        )
        errors: dict[str, str] = {}
        sensors: list[dict[str, Any]] = self._config_data.get(CONF_POWER_SENSORS, [])

        if sensor_index is None or sensor_index < 0 or sensor_index >= len(sensors):
            return self.async_abort(reason="invalid_sensor_index")

        current_sensor_config = sensors[sensor_index]

        if user_input is not None:
            if user_input.get("remove_sensor"):
                del self._config_data[CONF_POWER_SENSORS][sensor_index]
                return await self.async_step_user()

            errors, sensor_config = _process_sensor_input(self.hass, dict(user_input))
            if not errors and sensor_config:
                self._config_data[CONF_POWER_SENSORS][sensor_index] = sensor_config
                return await self.async_step_user()

        initial_data = {
            CONF_ENTITY_ID: current_sensor_config.get(CONF_ENTITY_ID),
            CONF_NAME: current_sensor_config.get(CONF_NAME),
            CONF_IMPORTANCE: current_sensor_config.get(
                CONF_IMPORTANCE, DEFAULT_IMPORTANCE
            ),
            CONF_LAST_RESORT: current_sensor_config.get(CONF_LAST_RESORT, False),
            CONF_APPLIANCE: current_sensor_config.get(CONF_APPLIANCE),
            CONF_DEVICE_COOLDOWN: current_sensor_config.get(CONF_DEVICE_COOLDOWN),
        }

        return self.async_show_form(
            step_id="edit_sensor",
            data_schema=_build_sensor_edit_schema(initial_data),
            errors=errors,
            last_step=False,
        )


class PowerLoadBalancerOptionsFlow(OptionsFlow):
    """
    Options flow to reconfigure Power Load Balancer after initial setup.

    This class handles reconfiguration of an existing integration instance,
    allowing users to modify sensors, power budgets, and monitored appliances.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """
        Initialize PowerLoadBalancerOptionsFlow.

        Args:
            config_entry: The existing configuration entry to modify.

        """
        self._config_entry = config_entry
        data_source = config_entry.options or config_entry.data
        self._config_data: dict[str, Any] = dict(data_source)

    async def async_step_init(
        self,
        user_input: dict[str, object] | None = None,
        *,
        force_show_form: bool = False,
    ) -> ConfigFlowResult:
        """
        First step in options flow.

        Args:
            user_input: User input from the form, or None on first display.
            force_show_form: Force showing the form even if already configured.

        Returns:
            ConfigFlowResult directing to the next step.

        """
        _LOGGER.debug(
            "Opening options flow step: async_step_init, "
            "user_input=%s, force_show_form=%s",
            user_input,
            force_show_form,
        )
        main_sensor = self._config_data.get(CONF_MAIN_POWER_SENSOR)
        power_budget = self._config_data.get(CONF_POWER_BUDGET_WATT)

        if (
            not force_show_form
            and main_sensor is not None
            and power_budget is not None
            and user_input is None
        ):
            return await self.async_step_sensor_menu()

        if user_input is not None:
            self._config_data[CONF_MAIN_POWER_SENSOR] = user_input[
                CONF_MAIN_POWER_SENSOR
            ]
            self._config_data[CONF_POWER_BUDGET_WATT] = user_input[
                CONF_POWER_BUDGET_WATT
            ]
            return await self.async_step_sensor_menu()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAIN_POWER_SENSOR,
                        default=self._config_data.get(CONF_MAIN_POWER_SENSOR),
                    ): _get_power_sensor_selector(),
                    vol.Required(
                        CONF_POWER_BUDGET_WATT,
                        default=self._config_data.get(CONF_POWER_BUDGET_WATT),
                    ): int,
                }
            ),
        )

    async def async_step_sensor_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Menu for managing monitored sensors.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult directing to the selected action.

        """
        _LOGGER.debug(
            "Opening options flow step: async_step_sensor_menu, user_input=%s",
            user_input,
        )
        if user_input is not None:
            action = user_input.get("action", "")
            if action == "edit_main_sensor":
                return await self.async_step_edit_main_sensor()
            if action == "add_sensor":
                return await self.async_step_add_sensor()
            if action == "finish":
                return self.async_create_entry(title="", data=self._config_data)
            if str(action).startswith("edit_sensor_"):
                try:
                    sensor_index = int(str(action).replace("edit_sensor_", ""))
                    return await self.async_step_edit_sensor(sensor_index=sensor_index)
                except ValueError:
                    pass

        options = _build_menu_options(self._config_data)

        return self.async_show_form(
            step_id="sensor_menu",
            data_schema=vol.Schema({vol.Required("action"): vol.In(options)}),
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to add a new monitored power sensor.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening options flow step: async_step_add_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, sensor_config = _process_sensor_input(self.hass, dict(user_input))
            if not errors and sensor_config:
                if CONF_POWER_SENSORS not in self._config_data or not isinstance(
                    self._config_data[CONF_POWER_SENSORS], list
                ):
                    self._config_data[CONF_POWER_SENSORS] = []
                self._config_data[CONF_POWER_SENSORS].append(sensor_config)
                return await self.async_step_sensor_menu()

        return self.async_show_form(
            step_id="add_sensor",
            data_schema=STEP_ADD_SENSOR_SCHEMA,
            errors=errors,
        )

    async def async_step_edit_sensor(
        self,
        user_input: dict[str, object] | None = None,
        sensor_index: int | None = None,
    ) -> ConfigFlowResult:
        """
        Show form to edit an existing monitored power sensor.

        Args:
            user_input: User input from the form, or None on first display.
            sensor_index: Index of the sensor to edit in the sensors list.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening options flow step: async_step_edit_sensor, "
            "user_input=%s, sensor_index=%s",
            user_input,
            sensor_index,
        )
        errors: dict[str, str] = {}
        sensors: list[dict[str, Any]] = self._config_data.get(CONF_POWER_SENSORS, [])

        if sensor_index is None or sensor_index < 0 or sensor_index >= len(sensors):
            return self.async_abort(reason="invalid_sensor_index")

        current_sensor_config = sensors[sensor_index]

        if user_input is not None:
            if user_input.get("remove_sensor"):
                del self._config_data[CONF_POWER_SENSORS][sensor_index]
                return await self.async_step_sensor_menu()

            errors, sensor_config = _process_sensor_input(self.hass, dict(user_input))
            if not errors and sensor_config:
                self._config_data[CONF_POWER_SENSORS][sensor_index] = sensor_config
                return await self.async_step_sensor_menu()

        initial_data = {
            CONF_ENTITY_ID: current_sensor_config.get(CONF_ENTITY_ID),
            CONF_NAME: current_sensor_config.get(CONF_NAME),
            CONF_IMPORTANCE: current_sensor_config.get(
                CONF_IMPORTANCE, DEFAULT_IMPORTANCE
            ),
            CONF_LAST_RESORT: current_sensor_config.get(CONF_LAST_RESORT, False),
            CONF_APPLIANCE: current_sensor_config.get(CONF_APPLIANCE),
            CONF_DEVICE_COOLDOWN: current_sensor_config.get(CONF_DEVICE_COOLDOWN),
        }

        return self.async_show_form(
            step_id="edit_sensor",
            data_schema=_build_sensor_edit_schema(initial_data),
            errors=errors,
            last_step=False,
        )

    async def async_step_edit_main_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to edit the main sensor and power budget.

        Args:
            user_input: User input from the form, or None on first display.

        Returns:
            ConfigFlowResult with form or directing to next step.

        """
        _LOGGER.debug(
            "Opening options flow step: async_step_edit_main_sensor, user_input=%s",
            user_input,
        )
        if user_input is not None:
            self._config_data[CONF_MAIN_POWER_SENSOR] = user_input[
                CONF_MAIN_POWER_SENSOR
            ]
            self._config_data[CONF_POWER_BUDGET_WATT] = user_input[
                CONF_POWER_BUDGET_WATT
            ]
            cooldown = user_input.get(CONF_COOLDOWN_SECONDS)
            if cooldown is not None:
                self._config_data[CONF_COOLDOWN_SECONDS] = int(cooldown)
            else:
                self._config_data[CONF_COOLDOWN_SECONDS] = DEFAULT_COOLDOWN_SECONDS
            return await self.async_step_sensor_menu()

        current_cooldown = self._config_data.get(
            CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS
        )

        return self.async_show_form(
            step_id="edit_main_sensor",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAIN_POWER_SENSOR,
                        default=self._config_data.get(CONF_MAIN_POWER_SENSOR),
                    ): _get_power_sensor_selector(),
                    vol.Required(
                        CONF_POWER_BUDGET_WATT,
                        default=self._config_data.get(CONF_POWER_BUDGET_WATT),
                    ): int,
                    vol.Required(
                        CONF_COOLDOWN_SECONDS,
                        default=current_cooldown,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=3600,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                }
            ),
        )
