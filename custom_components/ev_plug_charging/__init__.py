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
    CONF_CHARGE_CURRENT_A,
    CONF_CHARGE_EFFICIENCY,
    CONF_CHARGE_POWER_KW,
    CONF_EFFICIENCY_PRIOR,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_TARGETS,
    CONF_SOURCE_TYPE,
    CONFIG_VERSION,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_CHARGE_POWER_KW,
    DOMAIN,
    PLATFORMS,
    SOURCE_TYPE_PSACC,
    SUPPLY_VOLTAGE_V,
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
    """Options changed (poll interval, capacity/current, notify targets,
    ...) -- reload the entry to pick them up. A capacity change
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

    v2 -> v3: the single scalar CONF_NOTIFY_SERVICE becomes the list
    CONF_NOTIFY_TARGETS. Read-time coercion (accepting either shape
    forever in notify.py) was rejected on purpose -- it never actually
    finishes migrating anyone, and it means every future read site has to
    ask "which shape is this?" A real version bump means an entry is
    unambiguously old or new.

    CONF_NOTIFY_SERVICE has, as far as this codebase's own config flow
    goes, only ever been written into `options` -- the setup wizard's
    advanced step never asks for it. `data` is checked too, defensively,
    in case an entry was hand-edited or came from an earlier build.

    v3 -> v4: CONF_CHARGE_POWER_KW (a kW figure) becomes CONF_CHARGE_CURRENT_A
    (amps, converted at a fixed 230V -- exact for anyone actually on 230V,
    and the coordinator's measured-power path supersedes this arithmetic
    the moment real telemetry is available). CONF_CHARGE_EFFICIENCY becomes
    CONF_EFFICIENCY_PRIOR, carried forward UNCHANGED -- a tuned value stays
    tuned, it just isn't reachable from any form field any more.

    Unlike CONF_NOTIFY_SERVICE, both old keys can genuinely be in EITHER
    data (written there by the initial setup wizard) OR options (written
    there by a later Options save, which resubmits every advanced-step
    field) -- an entry that has been through Options at least once has the
    stale value in `data` and the current one in `options` simultaneously.
    So both dicts are popped unconditionally, not short-circuited, with
    `options` winning as the more recent value when both are present.
    """
    if entry.version == CONFIG_VERSION:
        return True

    data = {**entry.data}
    options = {**entry.options}

    if entry.version < 2:
        data.setdefault(CONF_SOURCE_TYPE, SOURCE_TYPE_PSACC)

    if entry.version < 3:
        legacy_target = options.pop(CONF_NOTIFY_SERVICE, None) or data.pop(
            CONF_NOTIFY_SERVICE, None
        )
        options.setdefault(
            CONF_NOTIFY_TARGETS, [legacy_target] if legacy_target else []
        )

    if entry.version < 4:
        legacy_power_kw = data.pop(CONF_CHARGE_POWER_KW, None)
        legacy_power_kw = options.pop(CONF_CHARGE_POWER_KW, legacy_power_kw)
        if legacy_power_kw is None:
            legacy_power_kw = DEFAULT_CHARGE_POWER_KW

        legacy_efficiency = data.pop(CONF_CHARGE_EFFICIENCY, None)
        legacy_efficiency = options.pop(CONF_CHARGE_EFFICIENCY, legacy_efficiency)
        if legacy_efficiency is None:
            legacy_efficiency = DEFAULT_CHARGE_EFFICIENCY

        options.setdefault(
            CONF_CHARGE_CURRENT_A, legacy_power_kw * 1000.0 / SUPPLY_VOLTAGE_V
        )
        data.setdefault(CONF_EFFICIENCY_PRIOR, legacy_efficiency)

    hass.config_entries.async_update_entry(
        entry, data=data, options=options, version=CONFIG_VERSION
    )
    _LOGGER.debug("Migrated config entry to version %s", CONFIG_VERSION)
    return True
