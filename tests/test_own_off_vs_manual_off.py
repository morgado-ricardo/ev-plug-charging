"""Telling our own stop apart from a human's.

The plug's observed state lags our command by a tick: we call
`switch.turn_off`, and the NEXT `reduce()` sees a plug-off edge on a plug we
owned. With nothing recording that we asked for it, every stop this
integration makes came back around looking like someone reaching for the
switch and overriding us.

Mostly that was invisible, because after a completion there is nothing left
to do anyway. The exception is the overheat cutoff: a safety stop is not a
completion, and the charge is supposed to resume once the plug cools. Being
misread as a manual override suppressed exactly that resume for the rest of
the window.

The catch is that the misreading was also, accidentally, the only thing
stopping an oscillation. `projected_soc` collapses back to the last real
reading the moment `charging_active` goes false -- and that reading is hours
old by construction, so it still says 69 when we have just stopped at a
projected 80. The below-target branch would turn the plug straight back on.

So these tests pin down both halves: our own stop is not a manual off, AND
a completed window stays completed.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from conftest import base_inputs
from ev_plug_charging.logic import reduce
from ev_plug_charging.models import (
    ChargeMode,
    Owner,
    PlugAction,
    SessionAnchor,
    SessionState,
)

TZ = timezone(timedelta(hours=1))
WINDOW_START, WINDOW_END = time(22, 40), time(8, 0)
NIGHT = datetime(2026, 9, 19, 23, 0, tzinfo=TZ)


def _at(minutes: int) -> datetime:
    return NIGHT + timedelta(minutes=minutes)


def _charging_state() -> SessionState:
    """Mid-session: we own the plug, current is flowing, anchored at 69%."""
    return SessionState(
        plug_turned_on_by=Owner.US,
        prev_plug_switch_on=True,
        prev_charging_active=True,
        prev_car_charging=True,
        charge_started_at=NIGHT,
        anchor=SessionAnchor(soc=69.0, captured_at=NIGHT, corrected=True),
        prev_soc=69.0,
        prev_soc_changed_at=NIGHT,
    )


def _tick(state: SessionState, minutes: int, **overrides):
    """One tick. The SoC reading deliberately never moves -- that is the
    normal case, not an edge case: readings arrive hours apart."""
    kwargs = dict(
        now=_at(minutes),
        mode=ChargeMode.SMART,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        target_soc=80.0,
        soc=69.0,
        soc_changed_at=NIGHT,
        car_charging=True,
        plug_switch_on=True,
        plug_power_w=1800.0,
    )
    kwargs.update(overrides)
    return reduce(state, base_inputs().set(**kwargs).build())


def _unplugged(**overrides):
    return dict(car_charging=False, plug_switch_on=False, plug_power_w=0.0, **overrides)


def test_stopping_at_target_is_not_recorded_as_a_manual_override():
    state = _charging_state()
    state, decision = _tick(state, 240)  # 4h in: projection crosses target
    assert decision.plug is PlugAction.OFF
    assert decision.reason == "target_reached"

    # The plug is observed off on the next tick. That is OUR off.
    state, _ = _tick(state, 242, **_unplugged())
    assert state.manual_off_until is None


def test_a_completed_window_does_not_oscillate_back_on():
    """The reading still says 69 against a target of 80, so without a guard
    the below-target branch fires immediately."""
    state = _charging_state()
    state, _ = _tick(state, 240)

    for minute in (242, 244, 250, 300):
        state, decision = _tick(state, minute, **_unplugged())
        assert decision.plug is not PlugAction.ON, f"turned back on at +{minute}min"
        assert decision.reason == "in_window_no_action"


def test_the_next_window_charges_normally():
    """The completion guard must expire on its own -- a car left plugged in
    produces no new plug-on edge to reset anything."""
    state = _charging_state()
    state, _ = _tick(state, 240)
    state, _ = _tick(state, 242, **_unplugged())

    tomorrow = _at(24 * 60)  # same clock time, next night, still in window
    state, decision = reduce(
        state,
        base_inputs()
        .set(
            now=tomorrow,
            mode=ChargeMode.SMART,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            target_soc=80.0,
            soc=55.0,
            soc_changed_at=tomorrow - timedelta(hours=1),
            **_unplugged(),
        )
        .build(),
    )
    assert decision.plug is PlugAction.ON
    assert decision.reason == "below_target"


def test_an_overheat_cutoff_is_not_a_manual_override():
    """The case that was actually broken. A safety stop is not a completion,
    and must not suppress the resume once the plug cools."""
    state = _charging_state()
    state, decision = _tick(state, 10, plug_temp_c=90.0, temp_limit_c=75.0)
    assert decision.plug is PlugAction.OFF
    assert decision.reason == "overheat"

    state, _ = _tick(state, 12, plug_temp_c=90.0, temp_limit_c=75.0, **_unplugged())
    assert state.manual_off_until is None, "an overheat cutoff is not a human override"


def test_a_real_human_off_still_latches():
    """The behaviour being protected. No command from us, so the observed
    off is someone else's and we stand down for the window."""
    state = _charging_state()
    state, _ = _tick(state, 10)  # charging along, we command nothing but ON/unchanged
    assert state.own_off_commanded_at is None

    state, _ = _tick(state, 12, **_unplugged())
    assert state.manual_off_until == datetime(2026, 9, 20, 8, 0, tzinfo=TZ)


def test_a_stale_own_off_marker_does_not_swallow_a_later_human_off():
    """If our turn_off call silently failed, the marker must expire rather
    than make us ignore a real override forever."""
    state = _charging_state()
    state, _ = _tick(state, 240)
    assert state.own_off_commanded_at is not None

    # Plug still on an hour later: the command never took effect. Then a
    # human switches it off for real.
    state, _ = _tick(state, 300)
    state, _ = _tick(state, 360, **_unplugged())
    assert state.manual_off_until is not None
