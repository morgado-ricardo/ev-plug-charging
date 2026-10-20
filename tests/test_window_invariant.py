"""The window invariant: inside the charging window the plug is ON, or
being switched on, unless one of a short, named list of reasons holds.

A plug that is off inside the window means no charge is possible at all --
the one failure the user always notices the next morning and never
notices in time. R17 (tests/test_scenarios.py) is the night it happened
for real: a stale "already completed" guard left the plug off for a whole
window with nothing anywhere saying why. That fix pins the one road it
took; this pins every road, by driving reduce() over an exhaustive grid of
window shapes, clock positions, plug states, readings, prior completions,
ownership and human overrides, and failing on any in-window "off" that is
not on the list below.

The allowed reasons, each a deliberate choice documented in AGENTS.md /
README.md, and each judged from the INPUTS and the resulting state -- not
from decision.reason, which is the very thing under test:

- the master switch is off (observe only, never actuate);
- the plug is overheating (cut for safety);
- a person switched the plug off during this window (respected until it
  closes);
- Smart, and the battery is known to be at or past target;
- Smart, and a charge already completed inside THIS window occurrence
  (the stale reading would otherwise flap the plug on and off);
- Smart, and there is no reading at all, or the only reading predates the
  last completed charge (fail closed -- and push, see R6/R16).

Everything else -- Timed mode, any Smart below-target reading, a reachable
or an unreachable source, a completion in any EARLIER window -- must leave
the plug on.
"""
from __future__ import annotations

import itertools
from datetime import datetime, time, timedelta

import pytest

from ev_plug_charging.logic import in_window, reduce, window_instance_id, window_open_at
from ev_plug_charging.models import ChargeMode, ChargingStatus, Owner, PlugAction, SessionState

from conftest import DAY, base_inputs, dt

TARGET = 80.0
WINDOWS = [
    (time(22, 0), time(8, 0)),  # wraps midnight -- the real setup
    (time(9, 0), time(17, 0)),  # does not wrap
]


def _clock_positions(start: time, end: time):
    """(now, open_edge) pairs covering the window: the exact open
    callback, the callback landing a hair early (R12), just after open,
    the middle of the night, and the last minute."""
    open_at = datetime.combine(DAY, start, tzinfo=dt(0).tzinfo)
    close_at = datetime.combine(DAY, end, tzinfo=open_at.tzinfo)
    if close_at <= open_at:
        close_at += timedelta(days=1)
    mid = open_at + (close_at - open_at) / 2
    return [
        (open_at, True),
        (open_at - timedelta(milliseconds=200), True),
        (open_at + timedelta(minutes=1), False),
        (mid, False),
        (close_at - timedelta(minutes=1), False),
    ]


def _completions(now: datetime, start: time, end: time):
    """charge_completed_at values that matter: none; last window's close
    edge (R17); between windows; and inside the current window."""
    opened = window_open_at(window_instance_id(now, start, end), start, now.tzinfo)
    prev_close = datetime.combine(opened.date(), end, tzinfo=now.tzinfo)
    if prev_close > opened:
        prev_close -= timedelta(days=1)
    out = [None, prev_close + timedelta(milliseconds=205), opened - timedelta(hours=1)]
    if now - opened > timedelta(minutes=10):
        out.append(opened + timedelta(minutes=5))
    return out


def _grid():
    for (start, end) in WINDOWS:
        for now, open_edge in _clock_positions(start, end):
            for completed_at in _completions(now, start, end):
                yield from itertools.product(
                    [(start, end, now, open_edge, completed_at)],
                    [ChargeMode.SMART, ChargeMode.TIMED],
                    [False, True],  # plug switch on
                    [False, True],  # plug was on last tick
                    [0.0, 1800.0],  # plug power
                    [None, 10.0, 50.0, 80.0],  # soc (10 is below the emergency floor)
                    [timedelta(minutes=30), timedelta(hours=26)],  # reading age
                    [True, False],  # source reachable
                    [Owner.US, Owner.EXTERNAL, Owner.UNKNOWN],
                    [False, True],  # a manual-off latch already running
                )


def _exception(new_state, inp, completed_at, opened) -> str | None:
    """Which allowed reason, if any, explains an in-window "off"."""
    if not inp.enabled:
        return "disabled"
    if inp.plug_switch_on and inp.overheat_protection and (inp.plug_temp_c or 0) > inp.temp_limit_c:
        return "overheat"
    if new_state.manual_off_until is not None and inp.now < new_state.manual_off_until:
        return "manual_off"
    if inp.mode != ChargeMode.SMART:
        return None
    if inp.soc is not None and inp.soc >= inp.target_soc:
        return "at_target"
    if (
        new_state.complete_notified
        and new_state.charge_completed_at is not None
        and new_state.charge_completed_at >= opened
    ):
        return "completed_this_window"
    if inp.soc is None:
        return "no_reading"
    if (
        not inp.source_reachable
        and completed_at is not None
        and inp.soc_changed_at is not None
        and inp.soc_changed_at <= completed_at
    ):
        return "reading_predates_last_charge"
    return None


def test_inside_the_window_the_plug_is_on_unless_a_named_reason_holds():
    seen_exceptions: set[str] = set()
    plug_on_count = 0
    checked = 0
    failures = []
    for combo in _grid():
        (
            (start, end, now, open_edge, completed_at),
            mode, switch_on, was_on, power, soc, age, reachable, owner, latched,
        ) = combo
        assert in_window(now, start, end, open_edge)
        opened = window_open_at(window_instance_id(now, start, end), start, now.tzinfo)
        state = SessionState(
            ha_started_at=now - timedelta(days=1),
            complete_notified=completed_at is not None,
            charge_completed_at=completed_at,
            prev_plug_switch_on=was_on,
            prev_charging_active=was_on and power > 0,
            # A charge already flowing always has its start recorded
            # (session.py sets it on the charging edge); leaving it None
            # would clock the projection from a day-old reading.
            charge_started_at=(now - timedelta(minutes=5)) if was_on and power > 0 else None,
            plug_turned_on_by=owner,
            manual_off_until=(now + timedelta(hours=1)) if latched else None,
        )
        inp = base_inputs().set(
            now=now,
            window_start=start,
            window_end=end,
            window_open_edge=open_edge,
            mode=mode,
            target_soc=TARGET,
            soc=soc,
            soc_changed_at=None if soc is None else now - age,
            source_reachable=reachable,
            plug_switch_on=switch_on,
            plug_power_w=power if switch_on else 0.0,
            car_charging=False,
            plugged=None,
        ).build()
        new_state, decision = reduce(state, inp)
        checked += 1
        # Whatever the plug does, the dashboard must say why in words --
        # inside the window, the vague catch-all is never the answer.
        if decision.status == ChargingStatus.IDLE:
            failures.append(f"{now.isoformat()} soc={soc} reason={decision.reason} -> status idle")

        plug_ends_on = decision.plug == PlugAction.ON or (
            inp.plug_switch_on and decision.plug != PlugAction.OFF
        )
        if plug_ends_on:
            plug_on_count += 1
            continue
        why = _exception(new_state, inp, completed_at, opened)
        if why is None:
            failures.append(
                f"{now.isoformat()} window {start}-{end} mode={mode.value} "
                f"switch_on={switch_on} was_on={was_on} power={power} soc={soc} age={age} "
                f"reachable={reachable} owner={owner.value} latched={latched} "
                f"completed_at={completed_at} -> plug={decision.plug.value} "
                f"reason={decision.reason} status={decision.status.value}"
            )
        else:
            seen_exceptions.add(why)

    assert not failures, (
        f"{len(failures)} in-window states leave the plug off for no allowed reason; "
        "first few:\n" + "\n".join(failures[:10])
    )
    # The grid must actually reach the cases it claims to cover, or a
    # broken generator would pass vacuously.
    assert checked > 10_000
    assert plug_on_count > checked // 3
    assert {"manual_off", "at_target", "completed_this_window", "no_reading",
            "reading_predates_last_charge"} <= seen_exceptions


@pytest.mark.parametrize(
    "temp_c, enabled, expected",
    [(90.0, True, PlugAction.OFF), (30.0, False, PlugAction.UNCHANGED)],
)
def test_the_two_safety_exceptions_hold_the_plug_off(temp_c, enabled, expected):
    """Overheat and the master switch are the exceptions the grid above
    never exercises (it keeps the plug cool and the integration enabled),
    so they are pinned here instead."""
    inp = base_inputs().set(
        now=dt(23, 30), soc=50.0, plug_switch_on=True, plug_power_w=1800.0,
        plug_temp_c=temp_c, enabled=enabled,
    ).build()
    _, decision = reduce(SessionState(prev_plug_switch_on=True), inp)
    assert decision.plug == expected


@pytest.mark.parametrize("soc", [None, 10.0, 50.0, 95.0])
@pytest.mark.parametrize("reachable", [True, False])
def test_timed_mode_has_no_exception_but_a_human_off(soc, reachable):
    """Timed charges for the whole window whatever the reading says, even
    after an earlier completion inside this same window."""
    state = SessionState(complete_notified=True, charge_completed_at=dt(23, 10))
    inp = base_inputs().set(
        now=dt(23, 30), mode=ChargeMode.TIMED, soc=soc, source_reachable=reachable,
        plug_switch_on=False, plug_power_w=0.0, car_charging=False,
    ).build()
    _, decision = reduce(state, inp)
    assert decision.plug == PlugAction.ON
