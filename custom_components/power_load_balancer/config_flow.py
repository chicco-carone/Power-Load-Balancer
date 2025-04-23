"""Config flow for Power Load Balancer integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


CONF_MAIN_POWER_SENSOR: str = "main_power_sensor"
CONF_POWER_SENSORS: str = "power_sensors"
CONF_POWER_BUDGET_WATT: str = "power_budget_watt"
CONF_APPLIANCE: str = "appliance"
CONF_IMPORTANCE: str = "importance"
CONF_LAST_RESORT: str = "last_resort"

STEP_MAIN_CONFIG: str = "main_config"
STEP_ADD_OR_EDIT_SENSOR: str = "add_or_edit_sensor"
STEP_REMOVE_SENSOR: str = "remove_sensor"

STEP_MAIN_CONFIG_BASE_SCHEMA: vol.Schema = vol.Schema(
    {
        vol.Required(CONF_MAIN_POWER_SENSOR): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="power")
        ),
        vol.Required(CONF_POWER_BUDGET_WATT): int,
    }
)

STEP_ADD_OR_EDIT_SENSOR_SCHEMA: vol.Schema = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="power")
        ),
        vol.Optional(CONF_NAME): TextSelector(TextSelectorConfig()),
        vol.Required(CONF_IMPORTANCE, default=5): NumberSelector(
            NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
        ),
        vol.Required(CONF_LAST_RESORT, default=False): bool,
        vol.Required(CONF_APPLIANCE): EntitySelector(
            EntitySelectorConfig(domain=["switch", "light"])
        ),
    }
)


class PowerLoadBalancerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Power Load Balancer."""

    VERSION: int = 1
    _config_data: dict[str, Any]

    async def async_step_user(
        self,
        user_input: dict[str, object] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial configuration step and show the main menu."""
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
            if action and action.startswith("edit_sensor_"):
                try:
                    sensor_index = int(action.replace("edit_sensor_", ""))
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

        options: dict[str, str] = {}
        main_sensor_text = (
            "Edit Main Sensor Settings"
            if self._config_data.get(CONF_MAIN_POWER_SENSOR)
            else "Configure Main Sensor"
        )
        options["edit_main_sensor"] = main_sensor_text
        options["add_sensor"] = "Add New Monitored Sensor"

        configured_sensors = self._config_data.get(CONF_POWER_SENSORS, [])
        for i, sensor_config in enumerate(configured_sensors):
            sensor_name = sensor_config.get(CONF_NAME) or sensor_config.get(
                CONF_ENTITY_ID, f"Sensor {i + 1}"
            )
            options[f"edit_sensor_{i}"] = f"Edit: {sensor_name}"

        options["finish"] = "Finish Configuration"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("action"): vol.In(options)}),
            errors=errors,
        )

    @callback
    def _get_main_config_schema(self) -> vol.Schema:
        """Generate the schema for the main config step using vol.In and static labels."""
        options: dict[str, str] = {
            "edit_main_sensor": "Configure Main Sensor"
            if not self._config_data.get(CONF_MAIN_POWER_SENSOR)
            else "Edit Main Sensor Settings",
            "add_sensor": "Add New Monitored Sensor",
            "finish": "Finish Configuration",
        }

        configured_sensors: list[dict[str, Any]] = self._config_data.get(
            CONF_POWER_SENSORS, []
        )

        for i, sensor_config in enumerate(configured_sensors):
            sensor_name = sensor_config.get(CONF_NAME) or sensor_config.get(
                CONF_ENTITY_ID, f"Sensor {i + 1}"
            )
            options[f"edit_sensor_{i}"] = f"Edit: {sensor_name}"
        return vol.Schema({vol.Required("action"): vol.In(options)})

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Provide an options flow handler for editing existing configuration data."""
        return PowerLoadBalancerOptionsFlow(config_entry)

    async def async_step_main_config(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Handle the main configuration screen with radio buttons and actions."""
        _LOGGER.debug(
            "Opening config flow step: async_step_main_config, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}

        def _validate_finish() -> str | None:
            """Validate the data needed to complete the configuration."""
            if not self._config_data.get(CONF_MAIN_POWER_SENSOR):
                return "main_sensor_required"
            budget = self._config_data.get(CONF_POWER_BUDGET_WATT)
            if budget is None or not isinstance(budget, int) or budget <= 0:
                return "valid_budget_required"
            return None

        if user_input is not None:
            action: str | None = user_input.get("action")
            if action == "edit_main_sensor":
                return await self.async_step_edit_main_sensor()
            if action == "add_sensor":
                return await self.async_step_add_sensor()
            if action and action.startswith("edit_sensor_"):
                try:
                    sensor_index = int(action.replace("edit_sensor_", ""))
                    return await self.async_step_edit_sensor(sensor_index=sensor_index)
                except ValueError:
                    errors["base"] = "invalid_edit_action"
            elif action == "finish":
                err = _validate_finish()
                if err:
                    errors["base"] = err
                else:
                    return self.async_create_entry(
                        title="Power Load Balancer", data=self._config_data
                    )

        if not hasattr(self, "_config_data"):
            self._config_data = {CONF_POWER_SENSORS: []}

        data_schema: vol.Schema = self._get_main_config_schema()
        return self.async_show_form(
            step_id=STEP_MAIN_CONFIG,
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_edit_main_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Show form to add or edit the main sensor.

        Pre-filling with previous values if present.
        """
        _LOGGER.debug(
            "Opening config flow step: async_step_edit_main_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}
        initial: dict[str, object] = {}
        if hasattr(self, "_config_data"):
            if self._config_data.get(CONF_MAIN_POWER_SENSOR) is not None:
                initial[CONF_MAIN_POWER_SENSOR] = self._config_data[
                    CONF_MAIN_POWER_SENSOR
                ]
            if self._config_data.get(CONF_POWER_BUDGET_WATT) is not None:
                initial[CONF_POWER_BUDGET_WATT] = self._config_data[
                    CONF_POWER_BUDGET_WATT
                ]
        schema_dict: dict[object, object] = {}
        if CONF_MAIN_POWER_SENSOR in initial:
            schema_dict[
                vol.Required(
                    CONF_MAIN_POWER_SENSOR, default=initial[CONF_MAIN_POWER_SENSOR]
                )
            ] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            )
        else:
            schema_dict[vol.Required(CONF_MAIN_POWER_SENSOR)] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            )
        if CONF_POWER_BUDGET_WATT in initial:
            schema_dict[
                vol.Required(
                    CONF_POWER_BUDGET_WATT, default=initial[CONF_POWER_BUDGET_WATT]
                )
            ] = int
        else:
            schema_dict[vol.Required(CONF_POWER_BUDGET_WATT)] = int
        data_schema = vol.Schema(schema_dict)
        if user_input is not None:
            main_sensor = user_input.get(CONF_MAIN_POWER_SENSOR)
            power_budget = user_input.get(CONF_POWER_BUDGET_WATT)
            if (
                power_budget is None
                or not isinstance(power_budget, int)
                or power_budget <= 0
            ):
                errors[CONF_POWER_BUDGET_WATT] = "valid_budget_required"
            if not errors:
                self._config_data[CONF_MAIN_POWER_SENSOR] = main_sensor
                self._config_data[CONF_POWER_BUDGET_WATT] = power_budget
                return await self.async_step_main_config()
        return self.async_show_form(
            step_id="edit_main_sensor",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=initial,
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Show form to add a new monitored power sensor."""
        _LOGGER.debug(
            "Opening config flow step: async_step_add_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            sensor_entity_id = user_input.get(CONF_ENTITY_ID)
            appliance_entity_id = user_input.get(CONF_APPLIANCE)
            custom_name = user_input.get(CONF_NAME)
            if not sensor_entity_id:
                errors[CONF_ENTITY_ID] = "select_sensor_required"
            if not appliance_entity_id:
                errors[CONF_APPLIANCE] = "select_appliance_required"
            if not errors:
                friendly_name = self._get_friendly_name(sensor_entity_id)  # type: ignore[arg-type]
                if custom_name:
                    name_to_use = custom_name
                elif friendly_name:
                    name_to_use = friendly_name
                else:
                    name_to_use = str(sensor_entity_id)
                new_sensor_config = {
                    CONF_ENTITY_ID: sensor_entity_id,
                    CONF_NAME: name_to_use,
                    CONF_IMPORTANCE: user_input.get(CONF_IMPORTANCE, 5),
                    CONF_LAST_RESORT: user_input.get(CONF_LAST_RESORT, False),
                    CONF_APPLIANCE: appliance_entity_id,
                }
                if CONF_POWER_SENSORS not in self._config_data or not isinstance(
                    self._config_data[CONF_POWER_SENSORS], list
                ):
                    self._config_data[CONF_POWER_SENSORS] = []
                self._config_data[CONF_POWER_SENSORS].append(new_sensor_config)
                return await self.async_step_main_config()
        initial_data: dict[str, object] = {}
        schema = STEP_ADD_OR_EDIT_SENSOR_SCHEMA
        return self.async_show_form(
            step_id="add_sensor",
            data_schema=schema,
            errors=errors,
            description_placeholders=initial_data,
        )

    async def async_step_edit_sensor(
        self,
        user_input: dict[str, object] | None = None,
        sensor_index: int | None = None,
    ) -> ConfigFlowResult:
        """Show form to edit an existing monitored power sensor in options flow."""
        _LOGGER.debug(
            (
                "Opening config flow step: async_step_edit_sensor, "
                "user_input=%s, sensor_index=%s"
            ),
            user_input,
            sensor_index,
        )
        errors: dict[str, str] = {}
        sensors: list[dict[str, object]] = self._config_data.get(CONF_POWER_SENSORS, [])
        if sensor_index is None or sensor_index < 0 or sensor_index >= len(sensors):
            return self.async_abort(reason="invalid_sensor_index")
        current_sensor_config: dict[str, object] = sensors[sensor_index]
        if user_input is not None:
            if user_input.get("remove_sensor"):
                del self._config_data[CONF_POWER_SENSORS][sensor_index]
                return await self.async_step_sensor_menu()
            sensor_entity_id = user_input.get(CONF_ENTITY_ID)
            appliance_entity_id = user_input.get(CONF_APPLIANCE)
            custom_name = user_input.get(CONF_NAME)
            if not sensor_entity_id:
                errors[CONF_ENTITY_ID] = "select_sensor_required"
            if not appliance_entity_id:
                errors[CONF_APPLIANCE] = "select_appliance_required"
            if not errors:
                friendly_name = self._get_friendly_name(sensor_entity_id)
                if custom_name:
                    name_to_use = custom_name
                elif friendly_name:
                    name_to_use = friendly_name
                else:
                    name_to_use = str(sensor_entity_id)
                new_sensor_config = {
                    CONF_ENTITY_ID: sensor_entity_id,
                    CONF_NAME: name_to_use,
                    CONF_IMPORTANCE: user_input.get(CONF_IMPORTANCE, 5),
                    CONF_LAST_RESORT: user_input.get(CONF_LAST_RESORT, False),
                    CONF_APPLIANCE: appliance_entity_id,
                }
                self._config_data[CONF_POWER_SENSORS][sensor_index] = new_sensor_config
                return await self.async_step_sensor_menu()
        initial_data: dict[str, object] = {
            CONF_ENTITY_ID: current_sensor_config.get(CONF_ENTITY_ID),
            CONF_NAME: current_sensor_config.get(CONF_NAME),
            CONF_IMPORTANCE: current_sensor_config.get(CONF_IMPORTANCE, 5),
            CONF_LAST_RESORT: current_sensor_config.get(CONF_LAST_RESORT, False),
            CONF_APPLIANCE: current_sensor_config.get(CONF_APPLIANCE),
        }
        schema_dict: dict[object, object] = {}
        if initial_data[CONF_ENTITY_ID] is not None:
            schema_dict[
                vol.Required(CONF_ENTITY_ID, default=initial_data[CONF_ENTITY_ID])
            ] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            )
        else:
            schema_dict[vol.Required(CONF_ENTITY_ID)] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            )
        if initial_data[CONF_NAME] is not None:
            schema_dict[vol.Optional(CONF_NAME, default=initial_data[CONF_NAME])] = (
                TextSelector(TextSelectorConfig())
            )
        else:
            schema_dict[vol.Optional(CONF_NAME)] = TextSelector(TextSelectorConfig())
        if initial_data[CONF_IMPORTANCE] is not None:
            schema_dict[
                vol.Required(CONF_IMPORTANCE, default=initial_data[CONF_IMPORTANCE])
            ] = NumberSelector(
                NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
            )
        else:
            schema_dict[vol.Required(CONF_IMPORTANCE, default=5)] = NumberSelector(
                NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
            )
        if initial_data[CONF_LAST_RESORT] is not None:
            schema_dict[
                vol.Required(CONF_LAST_RESORT, default=initial_data[CONF_LAST_RESORT])
            ] = bool
        else:
            schema_dict[vol.Required(CONF_LAST_RESORT, default=False)] = bool
        if initial_data[CONF_APPLIANCE] is not None:
            schema_dict[
                vol.Required(CONF_APPLIANCE, default=initial_data[CONF_APPLIANCE])
            ] = EntitySelector(EntitySelectorConfig(domain=["switch", "light"]))
        else:
            schema_dict[vol.Required(CONF_APPLIANCE)] = EntitySelector(
                EntitySelectorConfig(domain=["switch", "light"])
            )
        schema_dict[vol.Optional("remove_sensor")] = bool
        schema = vol.Schema(schema_dict)
        return self.async_show_form(
            step_id="edit_sensor",
            data_schema=schema,
            errors=errors,
            description_placeholders=initial_data,
            last_step=False,
        )

    def _get_friendly_name(self, entity_id: str) -> str | None:
        """
        Return a friendly name for the device connected to the given sensor entity_id.

        This method tries to get the device name from the device registry.
        If not available, it falls back to the entity's original_name or name.
        """
        try:
            from homeassistant.helpers.device_registry import (
                async_get as async_get_device_registry,
            )
            from homeassistant.helpers.entity_registry import (
                async_get as async_get_entity_registry,
            )
        except ImportError:
            result: str | None = None
            return result
        hass = getattr(self, "hass", None)
        if hass is None:
            return None
        entity_registry = async_get_entity_registry(hass)
        if entity_registry is None:
            return None
        entity = entity_registry.entities.get(str(entity_id))
        device_name: str | None = None
        if entity is not None and entity.device_id:
            device_registry = async_get_device_registry(hass)
            if device_registry is not None:
                device = device_registry.devices.get(entity.device_id)
                if device is not None and device.name:
                    device_name = device.name
        if device_name is not None:
            return device_name
        entity_original_name: str | None = None
        if (
            entity is not None
            and hasattr(entity, "original_name")
            and entity.original_name
        ):
            entity_original_name = entity.original_name
        if entity_original_name is not None:
            return entity_original_name
        entity_name: str | None = None
        if entity is not None and hasattr(entity, "name") and entity.name:
            entity_name = entity.name
        return entity_name


class PowerLoadBalancerOptionsFlow(OptionsFlow):
    """Options flow to reconfigure Power Load Balancer after initial setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize PowerLoadBalancerOptionsFlow."""
        self._config_entry = config_entry
        data_source = config_entry.options or config_entry.data
        self._config_data: dict[str, Any] = dict(data_source)

    async def async_step_init(
        self,
        user_input: dict[str, object] | None = None,
        force_show_form: bool = False,  # noqa: FBT001, FBT002
    ) -> ConfigFlowResult:
        """
        First step in options flow.

        If already configured, go to sensor menu unless forced.
        """
        _LOGGER.debug(
            (
                "Opening options flow step: async_step_init, "
                "user_input=%s, force_show_form=%s"
            ),
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
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="power")
                    ),
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
        """Menu for managing monitored sensors."""
        _LOGGER.debug(
            "Opening options flow step: async_step_sensor_menu, user_input=%s",
            user_input,
        )
        if user_input is not None:
            if user_input.get("action") == "edit_main_sensor":
                return await self.async_step_edit_main_sensor()
            if user_input.get("action") == "add_sensor":
                return await self.async_step_add_sensor()
            if user_input.get("action") == "finish":
                return self.async_create_entry(title="", data=self._config_data)
            if user_input.get("action", "").startswith("edit_sensor_"):
                try:
                    sensor_index = int(user_input["action"].replace("edit_sensor_", ""))
                    return await self.async_step_edit_sensor(sensor_index=sensor_index)
                except ValueError:
                    pass

        options: dict[str, str] = {
            "edit_main_sensor": "Edit Main Sensor Settings",
            "add_sensor": "Add New Monitored Sensor",
        }

        configured_sensors: list[dict[str, Any]] = self._config_data.get(
            CONF_POWER_SENSORS, []
        )
        for i, sensor_config in enumerate(configured_sensors):
            sensor_name = sensor_config.get(CONF_NAME) or sensor_config.get(
                CONF_ENTITY_ID, f"Sensor {i + 1}"
            )
            options[f"edit_sensor_{i}"] = f"Edit: {sensor_name}"

        options["finish"] = "Save Configuration"

        return self.async_show_form(
            step_id="sensor_menu",
            data_schema=vol.Schema({vol.Required("action"): vol.In(options)}),
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Show form to add a new monitored power sensor in options flow."""
        _LOGGER.debug(
            "Opening options flow step: async_step_add_sensor, user_input=%s",
            user_input,
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            sensor_entity_id = user_input.get(CONF_ENTITY_ID)
            appliance_entity_id = user_input.get(CONF_APPLIANCE)
            custom_name = user_input.get(CONF_NAME)
            if not sensor_entity_id:
                errors[CONF_ENTITY_ID] = "select_sensor_required"
            if not appliance_entity_id:
                errors[CONF_APPLIANCE] = "select_appliance_required"
            if not errors:
                friendly_name = self._get_friendly_name(sensor_entity_id)  # type: ignore[arg-type]
                if custom_name:
                    name_to_use = custom_name
                elif friendly_name:
                    name_to_use = friendly_name
                else:
                    name_to_use = str(sensor_entity_id)
                new_sensor_config = {
                    CONF_ENTITY_ID: sensor_entity_id,
                    CONF_NAME: name_to_use,
                    CONF_IMPORTANCE: user_input.get(CONF_IMPORTANCE, 5),
                    CONF_LAST_RESORT: user_input.get(CONF_LAST_RESORT, False),
                    CONF_APPLIANCE: appliance_entity_id,
                }
                if CONF_POWER_SENSORS not in self._config_data or not isinstance(
                    self._config_data[CONF_POWER_SENSORS], list
                ):
                    self._config_data[CONF_POWER_SENSORS] = []
                self._config_data[CONF_POWER_SENSORS].append(new_sensor_config)
                return await self.async_step_sensor_menu()
        initial_data: dict[str, object] = {}
        schema = STEP_ADD_OR_EDIT_SENSOR_SCHEMA
        return self.async_show_form(
            step_id="add_sensor",
            data_schema=schema,
            errors=errors,
            description_placeholders=initial_data,
        )

    async def async_step_edit_sensor(
        self,
        user_input: dict[str, object] | None = None,
        sensor_index: int | None = None,
    ) -> ConfigFlowResult:
        """Show form to edit an existing monitored power sensor in options flow."""
        _LOGGER.debug(
            (
                "Opening options flow step: async_step_edit_sensor, "
                "user_input=%s, sensor_index=%s"
            ),
            user_input,
            sensor_index,
        )
        errors: dict[str, str] = {}
        sensors: list[dict[str, object]] = self._config_data.get(CONF_POWER_SENSORS, [])
        if sensor_index is None or sensor_index < 0 or sensor_index >= len(sensors):
            return self.async_abort(reason="invalid_sensor_index")
        current_sensor_config: dict[str, object] = sensors[sensor_index]
        if user_input is not None:
            if user_input.get("remove_sensor"):
                del self._config_data[CONF_POWER_SENSORS][sensor_index]
                return await self.async_step_sensor_menu()
            sensor_entity_id = user_input.get(CONF_ENTITY_ID)
            appliance_entity_id = user_input.get(CONF_APPLIANCE)
            custom_name = user_input.get(CONF_NAME)
            if not sensor_entity_id:
                errors[CONF_ENTITY_ID] = "select_sensor_required"
            if not appliance_entity_id:
                errors[CONF_APPLIANCE] = "select_appliance_required"
            if not errors:
                friendly_name = self._get_friendly_name(sensor_entity_id)
                if custom_name:
                    name_to_use = custom_name
                elif friendly_name:
                    name_to_use = friendly_name
                else:
                    name_to_use = str(sensor_entity_id)
                new_sensor_config = {
                    CONF_ENTITY_ID: sensor_entity_id,
                    CONF_NAME: name_to_use,
                    CONF_IMPORTANCE: user_input.get(CONF_IMPORTANCE, 5),
                    CONF_LAST_RESORT: user_input.get(CONF_LAST_RESORT, False),
                    CONF_APPLIANCE: appliance_entity_id,
                }
                self._config_data[CONF_POWER_SENSORS][sensor_index] = new_sensor_config
                return await self.async_step_sensor_menu()
        initial_data: dict[str, object] = {
            CONF_ENTITY_ID: current_sensor_config.get(CONF_ENTITY_ID),
            CONF_NAME: current_sensor_config.get(CONF_NAME),
            CONF_IMPORTANCE: current_sensor_config.get(CONF_IMPORTANCE, 5),
            CONF_LAST_RESORT: current_sensor_config.get(CONF_LAST_RESORT, False),
            CONF_APPLIANCE: current_sensor_config.get(CONF_APPLIANCE),
        }
        schema_dict: dict[object, object] = {}
        if initial_data[CONF_ENTITY_ID] is not None:
            schema_dict[
                vol.Required(CONF_ENTITY_ID, default=initial_data[CONF_ENTITY_ID])
            ] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            )
        else:
            schema_dict[vol.Required(CONF_ENTITY_ID)] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="power")
            )
        if initial_data[CONF_NAME] is not None:
            schema_dict[vol.Optional(CONF_NAME, default=initial_data[CONF_NAME])] = (
                TextSelector(TextSelectorConfig())
            )
        else:
            schema_dict[vol.Optional(CONF_NAME)] = TextSelector(TextSelectorConfig())
        if initial_data[CONF_IMPORTANCE] is not None:
            schema_dict[
                vol.Required(CONF_IMPORTANCE, default=initial_data[CONF_IMPORTANCE])
            ] = NumberSelector(
                NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
            )
        else:
            schema_dict[vol.Required(CONF_IMPORTANCE, default=5)] = NumberSelector(
                NumberSelectorConfig(min=1, max=10, mode=NumberSelectorMode.SLIDER)
            )
        if initial_data[CONF_LAST_RESORT] is not None:
            schema_dict[
                vol.Required(CONF_LAST_RESORT, default=initial_data[CONF_LAST_RESORT])
            ] = bool
        else:
            schema_dict[vol.Required(CONF_LAST_RESORT, default=False)] = bool
        if initial_data[CONF_APPLIANCE] is not None:
            schema_dict[
                vol.Required(CONF_APPLIANCE, default=initial_data[CONF_APPLIANCE])
            ] = EntitySelector(EntitySelectorConfig(domain=["switch", "light"]))
        else:
            schema_dict[vol.Required(CONF_APPLIANCE)] = EntitySelector(
                EntitySelectorConfig(domain=["switch", "light"])
            )
        schema_dict[vol.Optional("remove_sensor")] = bool
        schema = vol.Schema(schema_dict)
        return self.async_show_form(
            step_id="edit_sensor",
            data_schema=schema,
            errors=errors,
            description_placeholders=initial_data,
            last_step=False,
        )

    async def async_step_edit_main_sensor(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        """Show form to edit the main sensor and power budget in options flow."""
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
            return await self.async_step_sensor_menu()
        return self.async_show_form(
            step_id="edit_main_sensor",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAIN_POWER_SENSOR,
                        default=self._config_data.get(CONF_MAIN_POWER_SENSOR),
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="power")
                    ),
                    vol.Required(
                        CONF_POWER_BUDGET_WATT,
                        default=self._config_data.get(CONF_POWER_BUDGET_WATT),
                    ): int,
                }
            ),
        )

    def _get_friendly_name(self, entity_id: str) -> str | None:
        """
        Return a friendly name for the device connected to the given sensor entity_id.

        This method tries to get the device name from the device registry.
        If not available, it falls back to the entity's original_name or name.
        """
        try:
            from homeassistant.helpers.device_registry import (
                async_get as async_get_device_registry,
            )
            from homeassistant.helpers.entity_registry import (
                async_get as async_get_entity_registry,
            )
        except ImportError:
            result: str | None = None
            return result
        hass = getattr(self, "hass", None)
        if hass is None:
            return None
        entity_registry = async_get_entity_registry(hass)
        if entity_registry is None:
            return None
        entity = entity_registry.entities.get(str(entity_id))
        device_name: str | None = None
        if entity is not None and entity.device_id:
            device_registry = async_get_device_registry(hass)
            if device_registry is not None:
                device = device_registry.devices.get(entity.device_id)
                if device is not None and device.name:
                    device_name = device.name
        if device_name is not None:
            return device_name
        entity_original_name: str | None = None
        if (
            entity is not None
            and hasattr(entity, "original_name")
            and entity.original_name
        ):
            entity_original_name = entity.original_name
        if entity_original_name is not None:
            return entity_original_name
        entity_name: str | None = None
        if entity is not None and hasattr(entity, "name") and entity.name:
            entity_name = entity.name
        return entity_name
