"""Plug ownership (plan section 3.3): who turned the plug on, and what
that implies for the level-based re-assert. This is the one place the
port is behaviourally DIFFERENT from the YAML by construction (a
level-based decision re-asserts every tick; the YAML's edge-triggered
automations only acted on their triggers) -- these tests pin the
mitigation down.
"""
from __future__ import annotations

from datetime import timedelta

from ev_plug_charging.logic import reduce
from ev_plug_charging.models import ChargeMode, Owner, PlugAction, SessionState

from conftest import base_inputs, dt


def test_smart_mode_turns_plug_on_and_claims_ownership():
    state = SessionState()
    inp = base_inputs().set(
        now=dt(23, 0),
        window_open_edge=True,
        soc=50.0,
        target_soc=80.0,
        plug_switch_on=False,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.ON
    assert state.plug_turned_on_by == Owner.US


def test_human_switching_owned_plug_off_mid_window_sticks():
    """A human presses the physical off button on a plug we turned on.
    The very next tick must NOT turn it back on, even though the
    below-target condition is still true."""
    state = SessionState(plug_turned_on_by=Owner.US, prev_plug_switch_on=True)
    inp = base_inputs().set(
        now=dt(23, 5), soc=50.0, target_soc=80.0, plug_switch_on=False,  # human turned it off
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.UNCHANGED
    assert state.manual_off_until is not None

    # A later tick, still well inside the window, must still not re-assert.
    inp2 = base_inputs().set(
        now=dt(23, 10), soc=50.0, target_soc=80.0, plug_switch_on=False
    ).build()
    state, decision2 = reduce(state, inp2)
    assert decision2.plug == PlugAction.UNCHANGED


def test_manual_off_expires_after_window_close():
    # The human's off happened hours ago -- by the next window's opening
    # tick, prev_plug_switch_on is already False (equilibrium reached over
    # many ticks in between, as in a real coordinator loop), so this tick
    # must NOT be mistaken for a fresh off-edge that re-arms suppression.
    state = SessionState(
        plug_turned_on_by=Owner.UNKNOWN,
        prev_plug_switch_on=False,
        manual_off_until=dt(7, 0),
    )
    inp = base_inputs().set(
        now=dt(23, 0, day=dt(23, 0).date() + timedelta(days=1)),
        window_open_edge=True,
        soc=50.0,
        target_soc=80.0,
        plug_switch_on=False,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.ON


def test_restore_with_unknown_owner_outside_window_fails_closed():
    """R1/D8: a restart loses in-memory ownership. If the plug is ON, we
    are OUTSIDE the window, and ownership restored as UNKNOWN, this must
    NOT read as "leave it running forever" -- that is the R1 failure
    (unbounded charge) reached by a different road. Since a restart
    cannot tell "we own this and forgot" apart from "a human started a
    deliberate manual charge outside the window", it fails closed: cut it
    once, on the very first post-restart evaluation, and say so."""
    state = SessionState(plug_turned_on_by=Owner.UNKNOWN, prev_plug_switch_on=True)
    inp = base_inputs().set(
        now=dt(12, 0),  # well outside a 23:00-07:00 window
        plug_switch_on=True,
        mode=ChargeMode.SMART,
        ha_start_edge=True,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.OFF
    assert decision.reason == "restart_reconcile_unknown_owner"
    assert any(e.name.endswith("restart_reconciled_off") for e in decision.events)


def test_restore_with_unknown_owner_only_reconciled_on_the_start_edge():
    """The reconciliation must fire only on the genuine startup tick, not
    on every subsequent evaluation while ownership happens to still read
    UNKNOWN for some other reason (e.g. mid-window human control)."""
    state = SessionState(plug_turned_on_by=Owner.UNKNOWN, prev_plug_switch_on=True)
    inp = base_inputs().set(
        now=dt(12, 0), plug_switch_on=True, mode=ChargeMode.SMART, ha_start_edge=False
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.UNCHANGED
    assert decision.reason == "outside_window_not_ours"
