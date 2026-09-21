"""Coordinator-level wiring for Phase-2 efficiency self-calibration:
the per-tick calibration-pair snapshot, the measured-power p90
settlement, the deferred/retried efficiency-sample recording, and the
power-mismatch Repairs issue.

tests/test_rate_model.py covers the pure math (seed_rate_calibrated,
working_efficiency, record_efficiency_sample, measured_ac_power_p90) in
isolation; this file covers the coordinator plumbing in coordinator.py
that feeds those functions real ticks -- in particular the retry
mechanism _try_record_efficiency_sample needs because a late SoC report
can complete the calibration pair well after the session that owns it
has already ended (see rate_model.py's module docstring).

Reaches into coordinator internals (_session_state, _power_samples, the
new _track_calibration_pair/_try_record_efficiency_sample/
_settle_measured_power methods) the same way diagnostics.py legitimately
does -- there is no other way to drive these deterministically without
reproducing a whole PSACC HTTP conversation per tick.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

import custom_components  # noqa: E402

_our_path = str(_REPO_ROOT / "custom_components")
if _our_path not in custom_components.__path__:
    custom_components.__path__.append(_our_path)

from datetime import datetime  # noqa: E402

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_mock_service,
)

pytest_plugins = "pytest_homeassistant_custom_component"

VEHICLE_INFO_RESPONSE = {
    "energy": [{"level": 55, "charging": {"status": "Disconnected", "plugged": False}}]
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _setup_entry(hass, aioclient_mock):
    from ev_plug_charging.const import (
        CONF_BATTERY_CAPACITY_KWH,
        CONF_CHARGE_CURRENT_A,
        CONF_PLUG_ENERGY_SENSOR,
        CONF_PLUG_POWER_SENSOR,
        CONF_PLUG_SWITCH,
        CONF_POLL_INTERVAL,
        CONF_PSACC_URL,
        CONF_SOURCE_TYPE,
        CONF_TEMP_LIMIT,
        CONF_VIN,
        CONFIG_VERSION,
        DOMAIN,
        SOURCE_TYPE_PSACC,
    )

    hass.states.async_set("switch.plug", "off")
    hass.states.async_set("sensor.plug_power", "0")
    hass.states.async_set("sensor.plug_energy", "0")

    aioclient_mock.get(
        "http://psacc.example/get_vehicleinfo/VF1TESTVIN?from_cache=1",
        json=VEHICLE_INFO_RESPONSE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_VERSION,
        data={
            CONF_SOURCE_TYPE: SOURCE_TYPE_PSACC,
            CONF_PSACC_URL: "http://psacc.example",
            CONF_VIN: "VF1TESTVIN",
            CONF_PLUG_SWITCH: "switch.plug",
            CONF_PLUG_POWER_SENSOR: "sensor.plug_power",
            CONF_PLUG_ENERGY_SENSOR: "sensor.plug_energy",
            CONF_BATTERY_CAPACITY_KWH: 50.0,
            CONF_CHARGE_CURRENT_A: 8.0,
            CONF_TEMP_LIMIT: 65.0,
            CONF_POLL_INTERVAL: 120,
        },
    )
    entry.add_to_hass(hass)

    # Same reasoning as test_integration_setup.py's _setup_entry: the first
    # refresh can actuate before the switch platform's real services exist.
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from ev_plug_charging.const import DOMAIN as _DOMAIN

    return entry, hass.data[_DOMAIN][entry.entry_id]


def _inputs(**overrides):
    """A minimal, fully-populated Inputs -- only the fields this file's
    tests actually vary are exposed as overrides."""
    from ev_plug_charging.models import ChargeMode, Inputs, RateSnapshot

    kwargs = dict(
        now=datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc),
        mode=ChargeMode.SMART,
        enabled=True,
        target_soc=80.0,
        min_soc_override=15.0,
        window_start=datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc).time(),
        window_end=datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc).time(),
        window_open_edge=False,
        window_close_edge=False,
        expected_gap_min=40.0,
        power_threshold_w=50.0,
        overheat_protection=True,
        temp_limit_c=65.0,
        rescue_refresh_enabled=True,
        soc=55.0,
        soc_changed_at=None,
        source_reachable=True,
        car_charging=True,
        plug_switch_on=True,
        plug_power_w=1840.0,
        plug_temp_c=30.0,
        plugged=True,
        rate=RateSnapshot(minutes_per_percent=20.2),
        ha_start_edge=False,
    )
    kwargs.update(overrides)
    return Inputs(**kwargs)


class _Decision:
    """A stand-in for logic.Decision -- _sample_measured_power and
    _maybe_record_session only ever read .charging_active/.reason off it."""

    def __init__(self, charging_active: bool, reason: str = "power_drop"):
        self.charging_active = charging_active
        self.reason = reason


# --------------------------------------------------------------------------- #
# _track_calibration_pair
# --------------------------------------------------------------------------- #


async def test_calibration_pair_captures_first_then_last_fresh_reading(hass, aioclient_mock):
    entry, coordinator = await _setup_entry(hass, aioclient_mock)

    start = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    coordinator._session_state = replace(coordinator._session_state, charge_started_at=start)
    prev_state = coordinator._session_state

    # First fresh reading -> opens the pair.
    new_state = replace(coordinator._session_state, session_energy_kwh=1.0)
    inp1 = _inputs(now=start, soc=50.0, soc_changed_at=start)
    result = coordinator._track_calibration_pair(prev_state, new_state, inp1)
    assert result.calib_soc_first == 50.0
    assert result.calib_energy_first == 1.0
    assert result.calib_soc_last is None

    # A second, later fresh reading -> updates last, leaves first alone.
    prev_state2 = result
    later = start + timedelta(hours=3)
    new_state2 = replace(result, session_energy_kwh=9.0)
    inp2 = _inputs(now=later, soc=65.0, soc_changed_at=later)
    result2 = coordinator._track_calibration_pair(prev_state2, new_state2, inp2)
    assert result2.calib_soc_first == 50.0
    assert result2.calib_energy_first == 1.0
    assert result2.calib_soc_last == 65.0
    assert result2.calib_energy_last == 9.0


async def test_calibration_pair_ignores_a_repeated_soc_changed_at(hass, aioclient_mock):
    """Same freshness test as everywhere else in this codebase: an
    unchanged soc_changed_at is not a new observation."""
    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    start = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    state = replace(
        coordinator._session_state,
        charge_started_at=start,
        prev_soc_changed_at=start,
        calib_soc_first=50.0,
        calib_energy_first=1.0,
    )
    coordinator._session_state = state

    new_state = replace(state, session_energy_kwh=5.0)
    inp = _inputs(now=start + timedelta(hours=1), soc=60.0, soc_changed_at=start)  # unchanged
    result = coordinator._track_calibration_pair(state, new_state, inp)
    assert result.calib_soc_last is None  # no update -- not a fresh reading


async def test_calibration_pair_untouched_with_no_open_session(hass, aioclient_mock):
    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    state = coordinator._session_state  # charge_started_at is None
    assert state.charge_started_at is None
    new_state = replace(state, session_energy_kwh=0.0)
    inp = _inputs(soc=50.0, soc_changed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = coordinator._track_calibration_pair(state, new_state, inp)
    assert result.calib_soc_first is None


# --------------------------------------------------------------------------- #
# The deferred/retried efficiency sample -- the sparse-telemetry case
# --------------------------------------------------------------------------- #


async def test_late_soc_reading_after_the_charge_still_produces_a_sample(hass, aioclient_mock):
    """The real-world case rate_model.py's module docstring documents: the
    SoC report that closes the pair lands hours after the session already
    completed. A one-shot record attempt at completion time would see an
    incomplete pair (soc_gain == 0) and reject it forever; the retry
    mechanism must catch it on the later tick that updates calib_soc_last
    instead."""
    from ev_plug_charging.models import SessionAnchor

    entry, coordinator = await _setup_entry(hass, aioclient_mock)

    start = datetime(2026, 1, 1, 23, 1, tzinfo=timezone.utc)
    coordinator._session_state = replace(
        coordinator._session_state,
        charge_started_at=start,
        anchor=SessionAnchor(soc=69.0, captured_at=start, provisional=False, corrected=False),
        session_stayed_on_plug=True,
        session_ran_above_target=False,
        calib_soc_first=69.0,
        calib_energy_first=0.0,
        calib_soc_last=69.0,  # no reading moved it before the charge stopped
        calib_energy_last=10.0,  # the full session's energy, already settled
    )

    # Completion tick: the pair's soc_gain is 0 (69 -> 69) so this must NOT
    # record a sample yet, but it DOES arm the retry.
    prev_state = replace(coordinator._session_state, complete_notified=False)
    new_state = replace(coordinator._session_state, complete_notified=True)
    coordinator._session_state = new_state
    coordinator._maybe_record_session(
        prev_state, new_state, _Decision(charging_active=False, reason="power_drop"),
        _inputs(now=start + timedelta(hours=3, minutes=47), soc=69.0, soc_changed_at=start),
    )
    assert coordinator._session_state.efficiency_samples == ()
    assert coordinator._pending_efficiency_stop_reason == "power_drop"

    # 09:19 the next morning: a fresh 79% reading finally lands. Energy has
    # not moved since the charge stopped, so this closes the pair.
    coordinator._session_state = replace(coordinator._session_state, calib_soc_last=79.0)
    coordinator._try_record_efficiency_sample()

    assert len(coordinator._session_state.efficiency_samples) == 1
    # capacity(50) * (79-69)/100 / 10 kWh == 0.5
    assert coordinator._session_state.efficiency_samples[0] == pytest.approx(0.5)
    assert coordinator._pending_efficiency_stop_reason is None


async def test_retry_is_a_no_op_once_the_next_session_resets_the_pair(hass, aioclient_mock):
    """If nothing ever closes the pair before the NEXT session starts,
    session.py's new_session reset clears calib_soc_first -- the retry
    must recognise that and stop trying, not wait forever."""
    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    coordinator._pending_efficiency_stop_reason = "power_drop"
    coordinator._session_state = replace(
        coordinator._session_state,
        charge_started_at=datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc),
        calib_soc_first=None,  # already reset by session.py for a new session
        calib_soc_last=None,
    )
    coordinator._try_record_efficiency_sample()
    assert coordinator._pending_efficiency_stop_reason is None
    assert coordinator._session_state.efficiency_samples == ()


# --------------------------------------------------------------------------- #
# Measured AC power: settlement and the disagreement Repairs issue
# --------------------------------------------------------------------------- #


async def test_measured_power_samples_only_while_charging_below_target_and_above_floor(
    hass, aioclient_mock
):
    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    assert coordinator._power_samples == []

    # Not charging -- ignored.
    coordinator._sample_measured_power(
        _inputs(plug_power_w=1840.0, soc=50.0, target_soc=80.0), _Decision(charging_active=False)
    )
    assert coordinator._power_samples == []

    # Below the delivering-power floor -- ignored.
    coordinator._sample_measured_power(
        _inputs(plug_power_w=10.0, soc=50.0, target_soc=80.0, power_threshold_w=50.0),
        _Decision(charging_active=True),
    )
    assert coordinator._power_samples == []

    # In the taper margin (target 80, margin 5 -> >=75 excluded) -- ignored.
    coordinator._sample_measured_power(
        _inputs(plug_power_w=1840.0, soc=76.0, target_soc=80.0), _Decision(charging_active=True)
    )
    assert coordinator._power_samples == []

    # Healthy sample.
    coordinator._sample_measured_power(
        _inputs(plug_power_w=1840.0, soc=50.0, target_soc=80.0), _Decision(charging_active=True)
    )
    assert coordinator._power_samples == [pytest.approx(1.84)]


async def test_measured_power_settlement_raises_repair_on_disagreement(hass, aioclient_mock):
    from homeassistant.helpers import issue_registry as ir

    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    # Configured 8A (~1.84kW); measure a p90 far enough below that to trip
    # MEASURED_POWER_DISAGREEMENT_THRESHOLD (0.25).
    coordinator._power_samples = [1.20] * 10

    coordinator._settle_measured_power()

    assert coordinator._session_state.measured_ac_power_kw == pytest.approx(1.20)
    registry = ir.async_get(hass)
    issue_id = coordinator._power_mismatch_issue_id()
    assert (entry.domain, issue_id) in registry.issues


async def test_measured_power_settlement_clears_repair_once_it_agrees(hass, aioclient_mock):
    from homeassistant.helpers import issue_registry as ir

    entry, coordinator = await _setup_entry(hass, aioclient_mock)

    # First: raise it.
    coordinator._power_samples = [1.20] * 10
    coordinator._settle_measured_power()
    registry = ir.async_get(hass)
    issue_id = coordinator._power_mismatch_issue_id()
    assert (entry.domain, issue_id) in registry.issues

    # Then: a later session measures close to the configured amps -- the
    # issue must self-clear, the same way the existing PERSISTENT_EVENTS do.
    coordinator._power_samples = [1.84] * 10
    coordinator._settle_measured_power()
    assert (entry.domain, issue_id) not in registry.issues


async def test_measured_power_below_min_samples_does_not_settle(hass, aioclient_mock):
    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    coordinator._power_samples = [1.20]  # below MEASURED_POWER_MIN_SAMPLES
    before = coordinator._session_state.measured_ac_power_kw
    coordinator._settle_measured_power()
    assert coordinator._session_state.measured_ac_power_kw == before


# --------------------------------------------------------------------------- #
# _effective_rate wiring
# --------------------------------------------------------------------------- #


async def test_effective_rate_uses_the_calibrated_seed(hass, aioclient_mock):
    from ev_plug_charging import rate_model

    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    coordinator._session_state = replace(
        coordinator._session_state,
        efficiency_samples=(0.80, 0.81, 0.82, 0.80, 0.81),
        measured_ac_power_kw=1.90,
    )
    result = coordinator._effective_rate()

    prior = 0.75  # DEFAULT_EFFICIENCY_PRIOR
    working = rate_model.working_efficiency(coordinator._session_state.efficiency_samples, prior)
    expected_seed = rate_model.seed_rate_calibrated(50.0, 8.0, 230.0, prior, 1.90, working)
    assert result.minutes_per_percent == pytest.approx(expected_seed)


# --------------------------------------------------------------------------- #
# Power samples reset with the energy baseline (a new session)
# --------------------------------------------------------------------------- #


async def test_power_samples_reset_alongside_the_energy_baseline_on_a_new_session(
    hass, aioclient_mock
):
    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    coordinator._power_samples = [1.5, 1.6, 1.7]
    hass.states.async_set("sensor.plug_energy", "3.0")

    prev_state = replace(coordinator._session_state, session_energy_kwh=5.0)
    new_state = replace(coordinator._session_state, session_energy_kwh=0.0)  # logic just reset it
    coordinator._track_energy(prev_state, new_state)

    assert coordinator._power_samples == []


# --------------------------------------------------------------------------- #
# Diagnostics visibility
# --------------------------------------------------------------------------- #


async def test_diagnostics_reports_efficiency_calibration_state(hass, aioclient_mock):
    """No silent change: diagnostics must always show the working
    efficiency, its sample count/source, the measured AC power, and
    whether the MAX_GAIN cap is currently binding."""
    from ev_plug_charging.diagnostics import async_get_config_entry_diagnostics

    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    coordinator._session_state = replace(
        coordinator._session_state,
        efficiency_samples=(0.80, 0.81, 0.82),
        measured_ac_power_kw=1.90,
        calib_soc_first=40.0,
        calib_energy_first=0.5,
    )

    result = await async_get_config_entry_diagnostics(hass, entry)
    block = result["efficiency_calibration"]
    assert block["efficiency_prior"] == 0.75
    assert block["efficiency_source"] == "calibrated"
    assert block["sample_count"] == 3
    assert block["samples"] == [0.80, 0.81, 0.82]
    assert block["measured_ac_power_kw"] == 1.90
    assert block["pending_calibration_pair"]["soc_first"] == 40.0
    assert isinstance(block["max_gain_binding"], bool)
    assert result["rate_model"]["seed_configured_minutes_per_percent"] is not None


async def test_diagnostics_reports_prior_source_before_min_samples(hass, aioclient_mock):
    from ev_plug_charging.diagnostics import async_get_config_entry_diagnostics

    entry, coordinator = await _setup_entry(hass, aioclient_mock)
    assert coordinator._session_state.efficiency_samples == ()

    result = await async_get_config_entry_diagnostics(hass, entry)
    block = result["efficiency_calibration"]
    assert block["efficiency_source"] == "prior"
    assert block["efficiency_working"] == block["efficiency_prior"]
