"""The project's core requirements, each traced to the test that pins it.

README.md says what the integration promises and AGENTS.md says what must
never break; this file is the index from each of those promises to a test.
Requirements already pinned elsewhere are listed with the test that pins
them; the ones that had no test of their own get one below.

| Requirement | Pinned by |
|---|---|
| In the window, the plug is on unless a named reason holds | test_window_invariant.py |
| A completion in an earlier window never blocks this one | test_scenarios.py R17 |
| Smart stops at target on the projection, through silence | test_scenarios.py R1, R2 |
| Projection clocks from the real charge start, not plug-on | test_scenarios.py R4 |
| Restart past target stops immediately | test_scenarios.py R5 |
| No reading: fail closed, but push once the grace expires | test_scenarios.py R6, R16 |
| Bypass charging is detected and warned | test_scenarios.py R7 |
| Overheat cuts the charge and disables; never acts on an idle plug | test_scenarios.py R9 |
| A flicker never resets the meter or repeats the completion | test_scenarios.py R11 |
| Window callbacks are authoritative over a jittery clock | test_scenarios.py R12, test_logic_window.py |
| Never starts outside the window; mid-window cable-in starts | test_scenarios.py R13 |
| Window close stops the charge and notifies, in both modes | test_scenarios.py R14 |
| Only OUR plug's power starts / completes a session | test_scenarios.py R15 |
| An unreachable source still stops a running charge at target | test_scenarios.py R16 |
| Timed charges the whole window, whatever the reading | test_window_invariant.py |
| A human off in the window holds until it closes, and only then | test_manual_off_latch.py, test_logic_ownership.py |
| Our own stop is never mistaken for a human off | test_own_off_vs_manual_off.py |
| Restart with an unknown owner outside the window cuts the plug | test_logic_ownership.py |
| Learned rate can only push the stop later | test_rate_model.py (effective_rate_never_below_seed) |
| Efficiency calibration bounded by EFFICIENCY_MAX_GAIN | test_rate_model.py, test_efficiency_calibration.py |
| A routine read never wakes the car (from_cache=1) | test_source_psacc.py |
| A typed SoC is a reading, and is spent after one charge | test_manual_soc.py |
| Daily wakeup: once a day, only when it buys something | test_daily_wakeup.py |
| The dashboard always says why, in words | test_charging_status.py |
| Dashboards reference only real entities | test_dashboard_examples.py |
| Outside the window, a plug WE turned on is turned off | below |
| A human's plug outside the window is left alone | below |
| Emergency low SoC charges now, Smart only | below |
| Master switch off never actuates (overheat still cuts) | below |
| The rescue wakeup is spent at most once per session, behind every gate | below |
| The pure core imports nothing from Home Assistant or third parties | below |
"""
from __future__ import annotations

import ast
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from ev_plug_charging.logic import reduce
from ev_plug_charging.models import ChargeMode, Owner, PlugAction, SessionState

from conftest import base_inputs, dt

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ev_plug_charging"


# --- outside the window -----------------------------------------------------


def test_outside_window_a_plug_we_turned_on_is_turned_off():
    state = SessionState(prev_plug_switch_on=True, plug_turned_on_by=Owner.US)
    inp = base_inputs().set(now=dt(15, 0), soc=50.0, plug_switch_on=True).build()
    _, decision = reduce(state, inp)
    assert decision.plug == PlugAction.OFF
    assert decision.reason == "outside_window_we_own_it"


@pytest.mark.parametrize("mode", [ChargeMode.SMART, ChargeMode.TIMED])
def test_outside_window_a_humans_plug_is_left_alone(mode):
    state = SessionState(prev_plug_switch_on=True, plug_turned_on_by=Owner.EXTERNAL)
    inp = base_inputs().set(now=dt(15, 0), soc=50.0, plug_switch_on=True, mode=mode).build()
    _, decision = reduce(state, inp)
    assert decision.plug == PlugAction.UNCHANGED


# --- emergency low SoC ------------------------------------------------------


def test_emergency_low_soc_charges_outside_the_window_in_smart():
    inp = base_inputs().set(
        now=dt(15, 0), soc=10.0, min_soc_override=15.0, plug_switch_on=False,
        plug_power_w=0.0, car_charging=False,
    ).build()
    _, decision = reduce(SessionState(), inp)
    assert decision.plug == PlugAction.ON
    assert decision.reason == "emergency_low_soc"


def test_emergency_low_soc_is_smart_only():
    inp = base_inputs().set(
        now=dt(15, 0), soc=10.0, min_soc_override=15.0, plug_switch_on=False,
        plug_power_w=0.0, car_charging=False, mode=ChargeMode.TIMED,
    ).build()
    _, decision = reduce(SessionState(), inp)
    assert decision.plug == PlugAction.UNCHANGED


def test_emergency_low_soc_never_trusts_an_unreachable_reading():
    inp = base_inputs().set(
        now=dt(15, 0), soc=10.0, min_soc_override=15.0, plug_switch_on=False,
        plug_power_w=0.0, car_charging=False, source_reachable=False,
    ).build()
    _, decision = reduce(SessionState(), inp)
    assert decision.plug == PlugAction.UNCHANGED


# --- master switch ----------------------------------------------------------


@pytest.mark.parametrize("hour", [15, 23])
@pytest.mark.parametrize("switch_on", [False, True])
@pytest.mark.parametrize("soc", [None, 10.0, 50.0, 95.0])
def test_disabled_never_actuates(hour, switch_on, soc):
    """Observe and report only -- not even emergency low SoC or a plug we
    own outside the window moves it."""
    state = SessionState(prev_plug_switch_on=switch_on, plug_turned_on_by=Owner.US)
    inp = base_inputs().set(
        now=dt(hour, 30), enabled=False, soc=soc, plug_switch_on=switch_on,
        plug_power_w=1800.0 if switch_on else 0.0,
    ).build()
    _, decision = reduce(state, inp)
    assert decision.plug == PlugAction.UNCHANGED
    assert decision.reason == "disabled"


def test_overheat_cuts_even_when_disabled():
    """Safety outranks the master switch (overheat is step 1, disabled
    step 2, in reduce()'s ladder)."""
    inp = base_inputs().set(now=dt(23, 30), enabled=False, plug_temp_c=90.0).build()
    _, decision = reduce(SessionState(prev_plug_switch_on=True), inp)
    assert decision.plug == PlugAction.OFF
    assert decision.force_disable is True


# --- rescue wakeup ----------------------------------------------------------


def _silent_charge(now, **overrides):
    kwargs = dict(
        now=now, soc=50.0, soc_changed_at=dt(23, 0), plug_switch_on=True,
        plug_power_w=1800.0, car_charging=False, expected_gap_min=40.0,
    )
    kwargs.update(overrides)
    return base_inputs().set(**kwargs).build()


def test_rescue_wakeup_is_spent_at_most_once_per_session():
    state = SessionState(charge_started_at=dt(23, 0), prev_plug_switch_on=True,
                         prev_charging_active=True, prev_soc_changed_at=dt(23, 0))
    refreshes = 0
    now = dt(23, 5)
    for _ in range(60):  # five hours of silence, 5-minute ticks
        state, decision = reduce(state, _silent_charge(now))
        refreshes += decision.request_refresh
        now += timedelta(minutes=5)
    assert refreshes == 1


@pytest.mark.parametrize(
    "override",
    [
        {"rescue_refresh_enabled": False},
        {"source_reachable": False},
        {"mode": ChargeMode.TIMED},
    ],
)
def test_rescue_wakeup_respects_every_gate(override):
    state = SessionState(charge_started_at=dt(23, 0), prev_plug_switch_on=True,
                         prev_charging_active=True, prev_soc_changed_at=dt(23, 0))
    now = dt(23, 5)
    for _ in range(60):
        state, decision = reduce(state, _silent_charge(now, **override))
        assert decision.request_refresh is False
        now += timedelta(minutes=5)


def test_rescue_wakeup_not_spent_before_twice_the_expected_gap():
    state = SessionState(charge_started_at=dt(23, 0), prev_plug_switch_on=True,
                         prev_charging_active=True, prev_soc_changed_at=dt(23, 0))
    _, decision = reduce(state, _silent_charge(dt(23, 0) + timedelta(minutes=79)))
    assert decision.request_refresh is False


# --- the pure core ----------------------------------------------------------

PURE_MODULES = [
    "models.py", "logic.py", "session.py", "rate_model.py", "source.py",
    "store.py", "aux_battery.py", "prompt.py", "const.py",
]


@pytest.mark.parametrize("module", PURE_MODULES)
def test_pure_core_imports_only_stdlib_and_itself(module):
    """AGENTS.md: these modules import nothing from homeassistant and
    nothing third-party, at any scope -- that is what lets the decision
    core run under plain pytest. Checked statically so it fails here, not
    only when someone runs tier 1 without HA installed."""
    tree = ast.parse((PKG / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: only to another pure module
                assert f"{node.module}.py" in PURE_MODULES, (
                    f"{module} imports .{node.module}, outside the pure core"
                )
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            assert top in sys.stdlib_module_names or top == "__future__", (
                f"{module} imports {name!r}"
            )
