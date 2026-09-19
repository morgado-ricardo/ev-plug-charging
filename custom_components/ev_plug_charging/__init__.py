"""EV Plug Charging: schedule-based charging via a switched plug, with
state of charge from a pluggable telemetry source. See README.md.

Imports of homeassistant.* and of coordinator.py (which itself imports
homeassistant.*) are deferred into the function bodies below, deliberately:
this is a package, and Python runs __init__.py on ANY `import
ev_plug_charging.<submodule>` -- module-level HA imports here would mean
`import ev_plug_charging.logic` could never succeed without Home Assistant
installed, defeating the whole point of logic.py (and models.py, session.py,
rate_model.py, source.py, store.py) having zero HA imports so they are
testable with plain pytest. See tests/conftest.py.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import (
    CONF_SOURCE_TYPE,
    CONFIG_VERSION,
    DOMAIN,
    PLATFORMS,
    SOURCE_TYPE_PSACC,
)

if TYPE_CHECKING:
    # `from __future__ import annotations` makes every annotation below a
    # string, never evaluated at runtime, so this import is free of the
    # module-level-HA-import problem the module docstring explains --
    # type checkers and IDEs still see real types.
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    from .coordinator import EvPlugChargingCoordinator

    coordinator = EvPlugChargingCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    from .services import async_setup_services

    async_setup_services(hass)

    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    """Options changed (poll interval, capacity/power/efficiency, notify
    service, ...) -- reload the entry to pick them up. A capacity change
    does NOT automatically clear the rate-learning buffer, although stale
    samples plus a new seed do give a discontinuous cap --
    `button.<name>_reset_rate_learning` does that
    explicitly, since telling a genuine capacity correction (same car,
    better number) apart from a different car isn't possible from the
    config diff alone."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Brings older config entries forward.

    v1 -> v2: `source_type` did not exist, because PSACC was the only
    thing this could talk to and was hardcoded. Every v1 entry is
    therefore a PSACC entry; backfill it so the source registry can look
    it up like any other.
    """
    if entry.version == CONFIG_VERSION:
        return True

    data = {**entry.data}

    if entry.version < 2:
        data.setdefault(CONF_SOURCE_TYPE, SOURCE_TYPE_PSACC)

    hass.config_entries.async_update_entry(entry, data=data, version=CONFIG_VERSION)
    _LOGGER.debug("Migrated config entry to version %s", CONFIG_VERSION)
    return True
