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
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_CURRENT_A,
    CONF_EFFICIENCY_PRIOR,
    CONF_PSACC_URL,
    CONF_SOURCE_TYPE,
    CONF_VIN,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGE_CURRENT_A,
    DEFAULT_EFFICIENCY_PRIOR,
    DOMAIN,
    EFFICIENCY_MAX_GAIN,
    EFFICIENCY_MIN_SAMPLES,
    SUPPLY_VOLTAGE_V,
)
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

    capacity = entry.options.get(
        CONF_BATTERY_CAPACITY_KWH, entry.data.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)
    )
    current_a = entry.options.get(
        CONF_CHARGE_CURRENT_A, entry.data.get(CONF_CHARGE_CURRENT_A, DEFAULT_CHARGE_CURRENT_A)
    )
    prior = entry.data.get(CONF_EFFICIENCY_PRIOR, DEFAULT_EFFICIENCY_PRIOR)
    working = rate_model.working_efficiency(state.efficiency_samples, prior)

    seed = None
    seed_configured = None
    learned = None
    try:
        seed_configured = rate_model.seed_rate_from_amps(capacity, current_a, SUPPLY_VOLTAGE_V, prior)
        seed = rate_model.seed_rate_calibrated(
            capacity, current_a, SUPPLY_VOLTAGE_V, prior, state.measured_ac_power_kw, working
        )
        learned = rate_model.learned_rate(state.rate_samples)
    except (ValueError, ZeroDivisionError):
        pass

    # Whether EFFICIENCY_MAX_GAIN is currently the binding constraint on
    # the calibrated seed, i.e. the calibration is being held back by the
    # cap rather than by the measured evidence itself -- see
    # rate_model.seed_rate_calibrated's docstring.
    gain_capped = (
        seed is not None
        and seed_configured is not None
        and seed <= seed_configured / EFFICIENCY_MAX_GAIN * 1.0001
    )

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
            "seed_configured_minutes_per_percent": seed_configured,
            "learned_minutes_per_percent": learned,
            "sample_count": len(state.rate_samples),
            "samples": list(state.rate_samples),
        },
        "efficiency_calibration": {
            "efficiency_prior": prior,
            "efficiency_working": working,
            "efficiency_source": (
                "prior"
                if len(state.efficiency_samples) < EFFICIENCY_MIN_SAMPLES
                else "calibrated"
            ),
            "sample_count": len(state.efficiency_samples),
            "samples": list(state.efficiency_samples),
            "measured_ac_power_kw": state.measured_ac_power_kw,
            "max_gain_binding": gain_capped,
            "pending_calibration_pair": {
                "soc_first": state.calib_soc_first,
                "energy_first_kwh": state.calib_energy_first,
                "soc_last": state.calib_soc_last,
                "energy_last_kwh": state.calib_energy_last,
            },
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
