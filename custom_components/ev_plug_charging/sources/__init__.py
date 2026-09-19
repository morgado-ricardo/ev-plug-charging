"""Registry of telemetry sources.

Adding a source is meant to be a closed job:

  1. Write `sources/<name>.py` with a `TelemetrySource` subclass -- an
     `async_fetch()` that returns a normalised `TelemetrySnapshot` (calling
     `source.derive_freshness()` for the clock), an `async_validate()` for
     the config flow, and `async_request_refresh()` only if the provider
     actually has a way to ask for a fresh reading.
  2. Add a `SOURCE_TYPE_*` constant in const.py and one line to `SOURCES`
     below.
  3. Add a config-flow step for its fields, plus its strings.

Nothing in logic.py, session.py, rate_model.py, coordinator.py or any
entity platform needs to change -- they only ever see the normalised
snapshot. That separation is the whole point: the projection and stop
machinery was built for telemetry that is expensive to fetch and arrives in
bursts, and that premise holds for several vehicle APIs, not just the one
implemented today.
"""
from __future__ import annotations

from typing import Any

from ..const import CONF_SOURCE_TYPE, SOURCE_TYPE_PSACC
from .base import (
    SourceConnectionError,
    SourceError,
    SourceNoVehicleData,
    SourceResponseError,
    TelemetrySource,
)
from .psacc import PsaccSource

__all__ = [
    "SOURCES",
    "SOURCE_LABELS",
    "SourceConnectionError",
    "SourceError",
    "SourceNoVehicleData",
    "SourceResponseError",
    "TelemetrySource",
    "async_create_source",
    "get_source_class",
]

#: source_type -> implementation. One entry today; see the module docstring.
SOURCES: dict[str, type[TelemetrySource]] = {
    SOURCE_TYPE_PSACC: PsaccSource,
}

#: Labels for the config-flow picker, in display order.
SOURCE_LABELS: dict[str, str] = {
    SOURCE_TYPE_PSACC: "PSA Car Controller (PSACC)",
}


def get_source_class(source_type: str) -> type[TelemetrySource]:
    try:
        return SOURCES[source_type]
    except KeyError:
        raise SourceError(f"Unknown telemetry source type: {source_type!r}") from None


def async_create_source(hass: Any, entry: Any) -> TelemetrySource:
    """Build the configured source for a config entry.

    `source_type` is backfilled to PSACC by async_migrate_entry for entries
    created before the setting existed, so `.get()` with a default is belt
    and braces rather than the normal path.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    config = {**entry.data, **entry.options}
    source_type = config.get(CONF_SOURCE_TYPE, SOURCE_TYPE_PSACC)
    source_cls = get_source_class(source_type)
    return source_cls.from_config(async_get_clientsession(hass), config)
