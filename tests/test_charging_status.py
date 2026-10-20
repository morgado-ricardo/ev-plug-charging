"""sensor.*_charging_status: logic.charging_status() turns the decision
reason plus what is physically observed into one human-readable state.

What this guards: a person looking at the dashboard can always tell WHY
the plug is in the state it is -- "plug on but the car is drawing
nothing" must not read the same as "charging", a safety cut must not read
as "idle", and no reason reduce() can produce falls through to the
catch-all.
"""
from __future__ import annotations

import ast
import json
from datetime import time, timedelta
from pathlib import Path

import pytest

from ev_plug_charging.logic import charging_status, reduce
from ev_plug_charging.models import (
    ChargeMode,
    ChargeSource,
    ChargingStatus,
    Owner,
    PlugAction,
    SessionState,
)

from conftest import DAY, base_inputs, dt

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "ev_plug_charging"


def _reason_literals() -> set[str]:
    """Every string reduce() can put in Decision.reason: `reason = "..."`
    assignments and `reason="..."` keywords in logic.py."""
    tree = ast.parse((PKG / "logic.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "reason" for t in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found.add(node.value.value)
        if isinstance(node, ast.keyword) and node.arg == "reason":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found.add(node.value.value)
    return found


# Reasons reduce() only produces while commanding the plug on or leaving
# an already-on plug alone.
ON_REASONS = {"below_target", "below_target_unreachable", "emergency_low_soc"}


def _status(**overrides) -> ChargingStatus:
    kwargs = dict(
        reason="in_window_no_action",
        plug=PlugAction.UNCHANGED,
        plug_switch_on=False,
        plug_delivering=False,
        plugged=None,
        charge_source=ChargeSource.NONE,
        complete_notified=False,
        session_saw_power=False,
        manual_off_active=False,
        completed_this_window=False,
        soc=60.0,
        target_soc=80.0,
    )
    kwargs.update(overrides)
    return charging_status(**kwargs)


def test_reason_extraction_found_the_known_reasons():
    """Self-guard: if logic.py changes shape and the extractor finds
    nothing, the coverage test below would pass vacuously."""
    reasons = _reason_literals()
    assert {"below_target", "target_reached", "overheat", "timed_window",
            "in_window_no_action", "outside_window_not_ours"} <= reasons
    assert len(reasons) >= 14


@pytest.mark.parametrize("reason", sorted(_reason_literals()))
def test_every_reason_reads_as_something_specific(reason):
    """No reason falls through to IDLE in the states it is actually
    produced in: commanded on, already on and delivering, or off."""
    if reason == "in_window_no_action":
        # Only produced with the plug off when a guard holds -- each guard
        # has its own status (the invariant test proves no other road).
        assert _status(reason=reason, manual_off_active=True) == ChargingStatus.MANUAL_OFF
        assert _status(reason=reason, completed_this_window=True) == ChargingStatus.CHARGED_THIS_WINDOW
        assert _status(reason=reason, soc=85.0) == ChargingStatus.AT_TARGET
        return
    if reason == "timed_window":
        assert _status(reason=reason, manual_off_active=True) == ChargingStatus.MANUAL_OFF
    for plug, switch_on, delivering in [
        (PlugAction.ON, False, False),
        (PlugAction.UNCHANGED, True, True),
        (PlugAction.OFF, True, True),
        (PlugAction.UNCHANGED, False, False),
    ]:
        status = _status(
            reason=reason, plug=plug, plug_switch_on=switch_on,
            plug_delivering=delivering, manual_off_active=reason == "timed_window",
        )
        if reason in ON_REASONS and (plug == PlugAction.OFF or (plug == PlugAction.UNCHANGED and not switch_on)):
            continue  # these reasons only ever come with the plug on or switching on
        assert status != ChargingStatus.IDLE, (reason, plug, switch_on, delivering)


@pytest.mark.parametrize(
    "plugged, complete, saw_power, expected",
    [
        (None, False, False, ChargingStatus.PLUG_ON_NO_POWER),
        (False, False, False, ChargingStatus.PLUG_ON_NO_POWER),
        (True, False, False, ChargingStatus.PLUG_ON_CAR_NOT_DRAWING),
        (None, True, True, ChargingStatus.CAR_FINISHED),
    ],
)
def test_plug_on_without_power_says_why(plugged, complete, saw_power, expected):
    """The case a reason code alone hides: the logic wants the plug on and
    it IS on, but nothing flows."""
    assert _status(
        reason="below_target", plug_switch_on=True, plugged=plugged,
        complete_notified=complete, session_saw_power=saw_power,
    ) == expected


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("below_target", ChargingStatus.CHARGING),
        ("timed_window", ChargingStatus.CHARGING_TIMED),
        ("below_target_unreachable", ChargingStatus.CHARGING_UNCONFIRMED_SOC),
        ("outside_window_not_ours", ChargingStatus.OUTSIDE_WINDOW_MANUAL),
    ],
)
def test_charging_says_how(reason, expected):
    assert _status(reason=reason, plug_switch_on=True, plug_delivering=True) == expected


def test_bypass_is_named_whatever_the_plug_is_doing():
    assert _status(reason="outside_window_not_ours", charge_source=ChargeSource.BYPASS) == ChargingStatus.BYPASS
    assert _status(reason="below_target", plug_switch_on=True, charge_source=ChargeSource.BYPASS) == ChargingStatus.BYPASS


# --- through reduce(), the way the sensor actually gets it ----------------


def test_overheat_cut_reads_as_overheat():
    inp = base_inputs().set(now=dt(23, 30), plug_temp_c=90.0).build()
    _, decision = reduce(SessionState(prev_plug_switch_on=True), inp)
    assert decision.status == ChargingStatus.OVERHEAT


def test_human_off_in_window_reads_as_manual_off():
    state = SessionState(prev_plug_switch_on=True, plug_turned_on_by=Owner.US)
    inp = base_inputs().set(
        now=dt(23, 30), soc=50.0, plug_switch_on=False, plug_power_w=0.0, car_charging=False,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug != PlugAction.ON
    assert decision.status == ChargingStatus.MANUAL_OFF


def test_r17_state_reads_as_switching_on_not_idle():
    """The diagnostics state of 2026-10-20 now switches on, and says so."""
    evening = DAY + timedelta(days=1)
    state = SessionState(
        ha_started_at=dt(12, 0), complete_notified=True,
        charge_completed_at=dt(8, 0, day=evening) + timedelta(milliseconds=205),
    )
    inp = base_inputs().set(
        now=dt(22, 57, day=evening), window_start=time(22, 0), window_end=time(8, 0), soc=69.0, soc_changed_at=dt(18, 29, day=evening),
        plug_switch_on=False, plug_power_w=0.0, car_charging=False,
    ).build()
    _, decision = reduce(state, inp)
    assert decision.status == ChargingStatus.SWITCHING_ON


def test_outside_window_reads_as_waiting_for_window():
    inp = base_inputs().set(
        now=dt(15, 0), plug_switch_on=False, plug_power_w=0.0, car_charging=False,
        mode=ChargeMode.SMART,
    ).build()
    _, decision = reduce(SessionState(), inp)
    assert decision.status == ChargingStatus.WAITING_FOR_WINDOW


def test_disabled_reads_as_disabled():
    inp = base_inputs().set(now=dt(23, 30), enabled=False).build()
    _, decision = reduce(SessionState(), inp)
    assert decision.status == ChargingStatus.DISABLED


# --- translations ------------------------------------------------------------


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json", "translations/pt.json"])
def test_every_status_has_a_state_string(path):
    """An ENUM sensor state with no translation renders as the raw code --
    exactly the unreadable output this sensor exists to replace."""
    data = json.loads((PKG / path).read_text())
    states = data["entity"]["sensor"]["charging_status"]["state"]
    assert set(states) == {s.value for s in ChargingStatus}
    assert all(isinstance(v, str) and v.strip() for v in states.values())
