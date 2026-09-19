"""Config flow: user step (source + actuator, connection-tested), advanced
step (optional signals and the rate model), and an options flow covering
the same advanced fields plus notifications.

Runtime-tunable settings (target SoC, min-SoC override, expected gap,
window times, mode) are deliberately NOT here -- they are entities (select,
number, time), because they belong on a dashboard, not behind a reconfigure
flow. See plan section 8.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .sources import (
    SOURCE_LABELS,
    SourceConnectionError,
    SourceNoVehicleData,
    SourceResponseError,
    get_source_class,
)
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_CHARGE_FINISHED_STATE_STRING,
    CONF_CHARGE_POWER_KW,
    CONF_CHARGING_STATE_STRING,
    CONF_MUTED_EVENTS,
    CONF_NOTIFY_SERVICE,
    CONF_PLUG_ENERGY_SENSOR,
    CONF_PLUG_POWER_SENSOR,
    CONF_PLUG_SWITCH,
    CONF_SOURCE_TYPE,
    CONF_PLUG_TEMP_SENSOR,
    CONF_POLL_INTERVAL,
    CONF_PSACC_URL,
    CONF_RESCUE_REFRESH_ENABLED,
    CONF_TEMP_LIMIT,
    CONF_VIN,
    CONFIG_VERSION,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_CHARGE_FINISHED_STATE_STRING,
    DEFAULT_CHARGE_POWER_KW,
    DEFAULT_CHARGING_STATE_STRING,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PSACC_URL,
    DEFAULT_TEMP_LIMIT_C,
    SOURCE_TYPE_PSACC,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SOURCE_TYPE, default=SOURCE_TYPE_PSACC): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=key, label=label)
                    for key, label in SOURCE_LABELS.items()
                ],
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
    }
)

PSACC_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PSACC_URL, default=DEFAULT_PSACC_URL): str,
        vol.Required(CONF_VIN): str,
        vol.Required(
            CONF_CHARGING_STATE_STRING, default=DEFAULT_CHARGING_STATE_STRING
        ): str,
        vol.Required(
            CONF_CHARGE_FINISHED_STATE_STRING, default=DEFAULT_CHARGE_FINISHED_STATE_STRING
        ): str,
    }
)

ACTUATOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PLUG_SWITCH): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="switch")
        ),
        vol.Required(CONF_PLUG_POWER_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        ),
    }
)

#: Per-source config step. Adding a source means adding its schema here and
#: an `async_step_<source_type>` below.
SOURCE_SCHEMAS = {
    SOURCE_TYPE_PSACC: PSACC_SCHEMA,
}


def _advanced_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_PLUG_ENERGY_SENSOR, default=defaults.get(CONF_PLUG_ENERGY_SENSOR)
            ): vol.Any(
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
                ),
                None,
            ),
            vol.Optional(
                CONF_PLUG_TEMP_SENSOR, default=defaults.get(CONF_PLUG_TEMP_SENSOR)
            ): vol.Any(
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                None,
            ),
            vol.Required(
                CONF_TEMP_LIMIT, default=defaults.get(CONF_TEMP_LIMIT, DEFAULT_TEMP_LIMIT_C)
            ): vol.Coerce(float),
            vol.Required(
                CONF_BATTERY_CAPACITY_KWH,
                default=defaults.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
            ): vol.Coerce(float),
            vol.Required(
                CONF_CHARGE_POWER_KW,
                default=defaults.get(CONF_CHARGE_POWER_KW, DEFAULT_CHARGE_POWER_KW),
            ): vol.Coerce(float),
            vol.Required(
                CONF_CHARGE_EFFICIENCY,
                default=defaults.get(CONF_CHARGE_EFFICIENCY, DEFAULT_CHARGE_EFFICIENCY),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
            vol.Required(
                CONF_POLL_INTERVAL,
                default=defaults.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_POLL_INTERVAL_SECONDS, max=MAX_POLL_INTERVAL_SECONDS),
            ),
        }
    )


class EvPlugChargingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick the telemetry source. One option today; the picker exists so
        adding another is a config-flow step and a registry line, not a
        rework (see sources/__init__.py)."""
        if user_input is not None:
            self._data[CONF_SOURCE_TYPE] = user_input[CONF_SOURCE_TYPE]
            return await self._async_step_for_source()

        return self.async_show_form(step_id="user", data_schema=SOURCE_SCHEMA)

    async def _async_step_for_source(self) -> FlowResult:
        source_type = self._data[CONF_SOURCE_TYPE]
        if source_type == SOURCE_TYPE_PSACC:
            return await self.async_step_psacc()
        # Unreachable while SOURCE_LABELS has one entry, but keeps the
        # failure explicit if a registry entry ever lands without a step.
        return self.async_abort(reason="unsupported_source")

    async def async_step_psacc(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """PSACC's own fields, connection-tested before moving on so a wrong
        URL or vehicle ID is caught here rather than three retries into the
        first poll."""
        errors: dict[str, str] = {}
        if user_input is not None:
            source_cls = get_source_class(SOURCE_TYPE_PSACC)
            try:
                info = await source_cls.async_validate(self.hass, user_input)
            except SourceConnectionError:
                errors["base"] = "cannot_connect"
            except SourceNoVehicleData:
                errors["base"] = "no_vehicle_data"
            except SourceResponseError:
                errors["base"] = "invalid_response"
            else:
                _LOGGER.debug(
                    "Source connection test OK: soc=%s status=%s",
                    info.get("soc"),
                    info.get("charging_status"),
                )
                self._data.update(user_input)
                return await self.async_step_actuator()

        return self.async_show_form(
            step_id="psacc", data_schema=PSACC_SCHEMA, errors=errors
        )

    async def async_step_actuator(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The plug. Source-agnostic, hence its own step -- a second source
        reuses it unchanged."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_advanced()

        return self.async_show_form(step_id="actuator", data_schema=ACTUATOR_SCHEMA)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            data = {**self._data, **user_input}
            await self.async_set_unique_id(self._unique_id_for(data))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=self._title_for(data), data=data)

        return self.async_show_form(
            step_id="advanced", data_schema=_advanced_schema({})
        )

    @staticmethod
    def _unique_id_for(data: dict[str, Any]) -> str:
        source_type = data.get(CONF_SOURCE_TYPE, SOURCE_TYPE_PSACC)
        if source_type == SOURCE_TYPE_PSACC:
            return f"{source_type}::{data[CONF_PSACC_URL]}::{data[CONF_VIN]}"
        return f"{source_type}::{data.get(CONF_VIN, '')}"

    @staticmethod
    def _title_for(data: dict[str, Any]) -> str:
        vehicle = data.get(CONF_VIN)
        return f"EV Plug Charging ({vehicle})" if vehicle else "EV Plug Charging"

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EvPlugChargingOptionsFlow":
        return EvPlugChargingOptionsFlow(config_entry)


class EvPlugChargingOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        schema = _advanced_schema(defaults).extend(
            {
                vol.Optional(
                    CONF_NOTIFY_SERVICE, default=defaults.get(CONF_NOTIFY_SERVICE)
                ): vol.Any(
                    selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="notify")
                    ),
                    None,
                ),
                vol.Optional(
                    CONF_MUTED_EVENTS, default=defaults.get(CONF_MUTED_EVENTS, [])
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            "charge_started",
                            "charge_not_started",
                            "soc_stale",
                            "bypass_detected",
                            "evse_no_power",
                            "soc_full",
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_RESCUE_REFRESH_ENABLED,
                    default=defaults.get(CONF_RESCUE_REFRESH_ENABLED, True),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
