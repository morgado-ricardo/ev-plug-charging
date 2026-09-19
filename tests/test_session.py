"""advance_session(): new-session detection, anchor capture, and the
one-time anchor correction. R8 and R11 both hinge on this file."""
from __future__ import annotations

from datetime import timedelta

from ev_plug_charging.models import RateSnapshot, SessionAnchor, SessionState
from ev_plug_charging.session import advance_session

from conftest import DAY, base_inputs, dt

RATE = RateSnapshot(minutes_per_percent=20.2)


def test_charging_active_edge_captures_matched_pair_anchor():
    state = SessionState()
    inp = base_inputs().set(now=dt(22, 30), soc=55.0, soc_changed_at=dt(22, 29)).build()
    new_state = advance_session(
        state,
        inp,
        charging_active=True,
        plug_on_edge=False,
        charging_active_edge=True,
        fresh_reading_edge=True,
    )
    assert new_state.charge_started_at == dt(22, 30)
    assert new_state.anchor.soc == 55.0
    assert new_state.anchor.captured_at == dt(22, 30)
    assert new_state.anchor.provisional is False  # reading was 1 min old


def test_anchor_provisional_when_reading_is_stale_at_capture():
    """Plug energised hours before the cable goes in -- the reading at
    the instant charging_active fires may already be old, so the anchor
    is captured provisionally and corrected later."""
    state = SessionState()
    inp = base_inputs().set(
        now=dt(0, 30),
        soc=45.0,
        soc_changed_at=dt(18, 0, day=DAY - timedelta(days=1)),
        expected_gap_min=40.0,
    ).build()
    new_state = advance_session(
        state,
        inp,
        charging_active=True,
        plug_on_edge=False,
        charging_active_edge=True,
        fresh_reading_edge=False,
    )
    assert new_state.anchor.provisional is True


def test_anchor_correction_moves_value_not_clock():
    """The correction must reproduce the fresh reading NOW without moving
    charge_started_at -- the value shifts, the clock does not, so the
    session cap still bounds the same real duration."""
    charge_started = dt(22, 0)
    state = SessionState(
        charge_started_at=charge_started,
        anchor=SessionAnchor(soc=100.0, captured_at=charge_started, provisional=True),
    )
    now = charge_started + timedelta(minutes=60)
    inp = base_inputs().set(now=now, soc=53.0, soc_changed_at=now, rate=RATE).build()
    new_state = advance_session(
        state,
        inp,
        charging_active=True,
        plug_on_edge=False,
        charging_active_edge=False,
        fresh_reading_edge=True,
    )
    assert new_state.charge_started_at == charge_started  # clock unchanged
    assert new_state.anchor.provisional is False
    assert new_state.anchor.corrected is True
    # solved = 53 - 60/(20.2*1.15) ~= 53 - 2.58 ~= 50.4
    assert 50.0 <= new_state.anchor.soc <= 51.0


def test_correction_runs_at_most_once():
    charge_started = dt(22, 0)
    state = SessionState(
        charge_started_at=charge_started,
        anchor=SessionAnchor(soc=50.0, captured_at=charge_started, provisional=False, corrected=True),
    )
    now = charge_started + timedelta(minutes=90)
    inp = base_inputs().set(now=now, soc=60.0, soc_changed_at=now, rate=RATE).build()
    new_state = advance_session(
        state,
        inp,
        charging_active=True,
        plug_on_edge=False,
        charging_active_edge=False,
        fresh_reading_edge=True,
    )
    # already corrected -> anchor soc must NOT change again
    assert new_state.anchor.soc == 50.0


def test_plug_on_always_starts_new_session():
    state = SessionState(complete_notified=False, session_energy_kwh=12.3)
    inp = base_inputs().set(now=dt(22, 0)).build()
    new_state = advance_session(
        state, inp, charging_active=False, plug_on_edge=True, charging_active_edge=False,
        fresh_reading_edge=False,
    )
    assert new_state.session_energy_kwh == 0.0
    assert new_state.complete_notified is False


def test_charging_active_flicker_does_not_reset_meter_r11():
    """R11: charging_active flickers off and on mid-charge (plug drops
    Wi-Fi AND the source is down simultaneously). Session energy must
    NOT reset, because complete_notified is still False (the previous
    completion hasn't happened) and there was no new plug_on_edge."""
    state = SessionState(complete_notified=False, session_energy_kwh=8.4)
    inp = base_inputs().set(now=dt(2, 0)).build()
    new_state = advance_session(
        state,
        inp,
        charging_active=True,
        plug_on_edge=False,
        charging_active_edge=True,  # the flicker's "back on" edge
        fresh_reading_edge=False,
    )
    assert new_state.session_energy_kwh == 8.4  # untouched
    assert new_state.complete_notified is False


def test_new_charging_active_edge_after_prior_completion_is_a_new_session():
    """The other half of new_session's definition: if the PREVIOUS
    session already completed (complete_notified True) and charging_active
    rises again without an intervening plug_on (e.g. car resumed via a
    wall-socket bypass), it IS a new session."""
    state = SessionState(complete_notified=True, session_energy_kwh=5.0)
    inp = base_inputs().set(now=dt(3, 0)).build()
    new_state = advance_session(
        state, inp, charging_active=True, plug_on_edge=False, charging_active_edge=True,
        fresh_reading_edge=False,
    )
    assert new_state.session_energy_kwh == 0.0
    assert new_state.complete_notified is False
