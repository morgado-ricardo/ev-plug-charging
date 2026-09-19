"""Full anchor/projection/rate-model state dump, plus the last decision's
`reason`.

This is the one place that shows WHY the integration did what it did. Every
failure worth debugging here -- a charge that stopped early, one that never
started, a projection that drifted -- is a question about the anchor and the
rate model, and neither is visible from the entity states alone.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import rate_model
from .const import CONF_PSACC_URL, CONF_SOURCE_TYPE, CONF_VIN, DOMAIN
from .coordinator import EvPlugChargingCoordinator
from .store import to_dict

# Anything that identifies the vehicle or the owner's network. Listed
# per-key rather than per-source because a diagnostics dump must fail
# safe: a new source's address/ID key should be added here the moment
# it is added to const.py.
TO_REDACT = {CONF_PSACC_URL, CONF_VIN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    from homeassistant.components.diagnostics import async_redact_data

    coordinator: EvPlugChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    state = coordinator._session_state  # noqa: SLF001 -- diagnostics is the
    # one legitimate reason to reach into coordinator internals; there is
    # no other consumer of the raw persisted state.

    seed = None
    learned = None
    try:
        seed = rate_model.seed_rate(
            entry.options.get("battery_capacity_kwh", entry.data.get("battery_capacity_kwh", 50.0)),
            entry.options.get("charge_power_kw", entry.data.get("charge_power_kw", 1.84)),
            entry.options.get("charge_efficiency", entry.data.get("charge_efficiency", 0.82)),
        )
        learned = rate_model.learned_rate(state.rate_samples)
    except (ValueError, ZeroDivisionError):
        pass

    last_decision = coordinator.last_decision

    return {
        "source": (
            coordinator.source.diagnostics()
            if coordinator.source is not None
            else {"source_type": entry.data.get(CONF_SOURCE_TYPE)}
        ),
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "session_state": to_dict(state),
        "telemetry": {
            "soc": coordinator._telemetry.soc if coordinator._telemetry else None,  # noqa: SLF001
            "soc_changed_at": (
                coordinator._telemetry.soc_changed_at.isoformat()  # noqa: SLF001
                if coordinator._telemetry and coordinator._telemetry.soc_changed_at
                else None
            ),
            "source_reachable": (
                coordinator._telemetry.source_reachable if coordinator._telemetry else None  # noqa: SLF001
            ),
            "used_payload_timestamp": (
                coordinator._telemetry.used_payload_timestamp if coordinator._telemetry else None  # noqa: SLF001
            ),
        },
        "rate_model": {
            "seed_minutes_per_percent": seed,
            "learned_minutes_per_percent": learned,
            "sample_count": len(state.rate_samples),
            "samples": list(state.rate_samples),
        },
        "last_decision": {
            "plug": last_decision.plug.value if last_decision else None,
            "reason": last_decision.reason if last_decision else None,
            "projected_soc": last_decision.projected_soc if last_decision else None,
            "silence_minutes": last_decision.silence_minutes if last_decision else None,
            "charge_source": last_decision.charge_source.value if last_decision else None,
            "soc_stale": last_decision.soc_stale if last_decision else None,
        }
        if last_decision
        else None,
    }
