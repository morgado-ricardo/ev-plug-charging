"""Turning the plug off by hand suppresses re-assertion -- but only for a
window that is actually open.

When someone switches off a plug this integration turned on, `reduce()`
records `manual_off_until` so the very next tick doesn't just switch it
back on again. The deadline is the close of the current window: "you
overrode me, I'll leave it alone tonight."

Outside the window that reasoning does not hold. There is no current
occurrence to suppress, so the deadline lands on the close of the NEXT
window -- and swallows it whole. Switch the plug off at 20:00 and the
23:00 charge silently never starts, with `decision_reason` reading
`in_window_no_action` all night.

Turning a plug off in the evening says nothing about whether you want the
car charged by morning.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from conftest import base_inputs
from ev_plug_charging.logic import reduce
from ev_plug_charging.models import Owner, PlugAction, SessionState

TZ = timezone(timedelta(hours=1))
WINDOW_START, WINDOW_END = time(22, 40), time(8, 0)


def _owned_plug_on() -> SessionState:
    return SessionState(plug_turned_on_by=Owner.US, prev_plug_switch_on=True)


def _switch_off_at(hour: int, minute: int = 0) -> SessionState:
    """Observe the plug going off at a given local time, on a plug we own."""
    inputs = (
        base_inputs()
        .set(
            now=datetime(2026, 9, 19, hour, minute, tzinfo=TZ),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            plug_switch_on=False,
            plug_power_w=0.0,
            car_charging=False,
            soc=69.0,
            soc_changed_at=datetime(2026, 9, 19, hour, minute, tzinfo=TZ) - timedelta(minutes=10),
        )
        .build()
    )
    state, _ = reduce(_owned_plug_on(), inputs)
    return state


def test_off_outside_the_window_does_not_latch():
    """The regression. 20:00 is nowhere near the 22:40 window."""
    assert _switch_off_at(20, 0).manual_off_until is None


def test_off_outside_the_window_still_lets_the_window_start_the_charge():
    """The symptom the user actually sees: nothing happens at 22:40."""
    state = _switch_off_at(20, 0)
    at_open = (
        base_inputs()
        .set(
            now=datetime(2026, 9, 19, 22, 40, tzinfo=TZ),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            window_open_edge=True,
            plug_switch_on=False,
            plug_power_w=0.0,
            car_charging=False,
            soc=69.0,
            soc_changed_at=datetime(2026, 9, 19, 21, 23, tzinfo=TZ),
        )
        .build()
    )
    _, decision = reduce(state, at_open)
    assert decision.plug is PlugAction.ON
    assert decision.reason == "below_target"


def test_off_inside_the_window_still_latches_until_that_window_closes():
    """The behaviour being protected -- an override inside the window is
    honoured for the rest of it, and must not be weakened by the fix."""
    state = _switch_off_at(23, 30)
    assert state.manual_off_until == datetime(2026, 9, 20, 8, 0, tzinfo=TZ)

    later = (
        base_inputs()
        .set(
            now=datetime(2026, 9, 20, 1, 0, tzinfo=TZ),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            plug_switch_on=False,
            plug_power_w=0.0,
            car_charging=False,
            soc=69.0,
            soc_changed_at=datetime(2026, 9, 19, 23, 0, tzinfo=TZ),
        )
        .build()
    )
    _, decision = reduce(state, later)
    assert decision.plug is PlugAction.UNCHANGED
    assert decision.reason == "in_window_no_action"


def test_off_on_a_plug_we_do_not_own_never_latches():
    """Unchanged behaviour: if we didn't turn it on, its going off is not
    an override of anything."""
    inputs = (
        base_inputs()
        .set(
            now=datetime(2026, 9, 19, 23, 30, tzinfo=TZ),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            plug_switch_on=False,
            plug_power_w=0.0,
            car_charging=False,
            soc=69.0,
            soc_changed_at=datetime(2026, 9, 19, 23, 0, tzinfo=TZ),
        )
        .build()
    )
    state, _ = reduce(
        SessionState(plug_turned_on_by=Owner.EXTERNAL, prev_plug_switch_on=True), inputs
    )
    assert state.manual_off_until is None
