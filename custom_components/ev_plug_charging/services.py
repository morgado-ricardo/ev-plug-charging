"""Four services:

- mark_completion_notified: lets an external automation (e.g. a ported
  packages/opel.yaml's opel_charge_complete, which reads
  input_boolean.ev_charge_complete_notified directly today) claim the
  one-push-per-session latch the same way the car-confirmed "Finished"
  status does. See MIGRATION.md.
- refresh_source: manual equivalent of the one rescue wakeup (D5), for a
  human who wants a fresh reading right now rather than waiting for the
  automatic 2x-gap trigger.
- reset_rate_learning: clears the rate-model sample buffer (plan section 5)
  -- use after a capacity/car change, since stale samples plus a new seed
  give a discontinuous cap.
- vehicle_command: the abstract vehicle-command API (sources/base.py's
  TelemetrySource.supported_commands / async_vehicle_command). One
  service, not nine, because the vocabulary (const.VEHICLE_COMMANDS) is
  open-ended and services.yaml selectors are static. Deliberately no
  entity, no options toggle and no arm gate here -- a service is invisible
  until something calls it, which is why this doesn't make the
  integration feel like anything other than a charging scheduler; see
  MIGRATION.md for the intended use (retiring packages/opel.yaml's
  rest_command: block in favour of this, once the transport -- host, VIN,
  timeout, error handling -- has exactly one owner instead of two).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from . import rate_model
from .const import (
    DOMAIN,
    SERVICE_MARK_COMPLETION_NOTIFIED,
    SERVICE_REFRESH_SOURCE,
    SERVICE_RESET_RATE_LEARNING,
    SERVICE_VEHICLE_COMMAND,
    VEHICLE_COMMANDS,
)
from .sources import SourceConnectionError, SourceError, SourceResponseError

_ENTRY_SCHEMA = vol.Schema({vol.Required("config_entry_id"): cv.string})
_VEHICLE_COMMAND_SCHEMA = _ENTRY_SCHEMA.extend(
    {
        vol.Required("command"): vol.In(sorted(VEHICLE_COMMANDS)),
        vol.Optional("params"): dict,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_MARK_COMPLETION_NOTIFIED):
        return  # already registered by another config entry's setup

    async def _mark_completion_notified(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call)
        coordinator._session_state = replace(  # noqa: SLF001 -- the one
            # legitimate external mutation point; this mirrors what
            # opel_charge_complete does to input_boolean.ev_charge_complete_notified.
            coordinator._session_state, complete_notified=True
        )

    async def _refresh_source(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call)
        source = coordinator.source
        if source is not None and source.supports_refresh:
            await source.async_request_refresh()

    async def _reset_rate_learning(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call)
        coordinator._session_state = rate_model.reset_samples(coordinator._session_state)  # noqa: SLF001

    async def _vehicle_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _get_coordinator(hass, call)
        source = coordinator.source
        command = call.data["command"]
        params: Optional[dict[str, Any]] = call.data.get("params")

        if source is None or command not in source.supported_commands:
            supported = sorted(source.supported_commands) if source is not None else []
            raise HomeAssistantError(
                f"{command!r} is not supported by this source. "
                f"Supported commands: {supported or 'none'}."
            )
        try:
            result = await source.async_vehicle_command(command, params)
        except (SourceConnectionError, SourceResponseError, SourceError) as err:
            raise HomeAssistantError(str(err)) from err
        return {"result": result} if result is not None else None

    hass.services.async_register(
        DOMAIN, SERVICE_MARK_COMPLETION_NOTIFIED, _mark_completion_notified, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_SOURCE, _refresh_source, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_RATE_LEARNING, _reset_rate_learning, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VEHICLE_COMMAND,
        _vehicle_command,
        schema=_VEHICLE_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _get_coordinator(hass: HomeAssistant, call: ServiceCall):
    entry_id = call.data["config_entry_id"]
    return hass.data[DOMAIN][entry_id]
