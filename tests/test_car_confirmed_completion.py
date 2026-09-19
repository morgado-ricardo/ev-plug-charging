"""The car-confirmed completion edge (logic.reduce()'s car_finished_edge)
and its sticky companion, SessionState.last_charge_source.

This is the ONE completion signal that works no matter how the car was
charged -- plug, EVSE straight into the wall (a bypass session), or a
public charger this integration never sees. Ported from
opel_charge_complete (packages/opel.yaml:913-950); see AGENTS.md and
docs/ev-charging-requirements.md (in the source repo) for why every
completion path shares one dedup latch (complete_notified).
"""
from __future__ import annotations


from ev_plug_charging.logic import reduce
from ev_plug_charging.models import ChargeSource, PlugAction, SessionState
from ev_plug_charging.store import from_dict, to_dict

from conftest import base_inputs, dt


def _tick(state, **overrides):
    inp = base_inputs().set(**overrides).build()
    return reduce(state, inp)


def _names(decision):
    return [e.name for e in decision.events]


def test_car_confirmed_completion_fires_on_the_charging_to_finished_edge():
    """The car reports charging, then reports finished: one charge_complete
    event, and it must not touch the plug -- it's a report, not a stop."""
    state = SessionState()
    # Tick 1: car charging (bypass -- plug delivers nothing).
    state, d1 = _tick(
        state,
        now=dt(22, 0),
        car_charging=True,
        plug_power_w=0.0,
        plug_switch_on=False,
        soc=55.0,
        soc_changed_at=dt(22, 0),
    )
    assert d1.charge_source == ChargeSource.BYPASS
    assert "ev_plug_charging_charge_complete" not in _names(d1)

    # Tick 2: car now reports finished. car_charging itself drops to False
    # (that's what "finished" means), but car_charge_finished is True.
    state, d2 = _tick(
        state,
        now=dt(22, 30),
        car_charging=False,
        car_charge_finished=True,
        plug_power_w=0.0,
        plug_switch_on=False,
        soc=80.0,
        soc_changed_at=dt(22, 30),
    )
    assert "ev_plug_charging_charge_complete" in _names(d2)
    complete_events = [e for e in d2.events if e.name == "ev_plug_charging_charge_complete"]
    assert len(complete_events) == 1
    assert complete_events[0].data["reason"] == "car_confirmed"
    assert complete_events[0].data["confirmed"] is True
    assert complete_events[0].data["last_charge_source"] == ChargeSource.BYPASS.value
    # A report, not a stop: no PlugAction was ever going to apply here (the
    # plug was never on in this bypass session), but the decision reason
    # must not be attributed to the completion edge either.
    assert d2.plug != PlugAction.ON
    assert state.complete_notified is True


def test_car_confirmed_completion_requires_a_prior_charging_tick():
    """No car_finished_edge without state.prev_car_charging having been
    True first -- e.g. a stale/garbage 'Finished' status on a car that was
    never observed charging must not fire a false completion."""
    state = SessionState()
    state, d = _tick(
        state,
        now=dt(22, 0),
        car_charging=False,
        car_charge_finished=True,
        plug_power_w=0.0,
        plug_switch_on=False,
        soc=55.0,
        soc_changed_at=dt(22, 0),
    )
    assert "ev_plug_charging_charge_complete" not in _names(d)


def test_car_confirmed_completion_dedupes_against_a_plug_side_completion():
    """Plug-side completion (target reached) fires first and sets
    complete_notified; the car-confirmed edge on a later tick must not fire
    a second notification for the same session."""
    state = SessionState()
    # Plug-side: SMART mode, INSIDE the (default 23:00-07:00) window, plug
    # on, projection at/above target.
    state, d1 = _tick(
        state,
        now=dt(23, 30),
        car_charging=True,
        plug_switch_on=True,
        plug_power_w=1800.0,
        soc=80.0,
        soc_changed_at=dt(23, 30),
        target_soc=80.0,
    )
    assert "ev_plug_charging_charge_complete" in _names(d1)
    assert d1.plug == PlugAction.OFF
    assert state.complete_notified is True

    # Later: the car itself also reports finished. Whichever signal wins
    # the race, exactly one notification -- this one must be silent.
    state, d2 = _tick(
        state,
        now=dt(23, 35),
        car_charging=False,
        car_charge_finished=True,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=80.0,
        soc_changed_at=dt(23, 35),
        target_soc=80.0,
    )
    assert "ev_plug_charging_charge_complete" not in _names(d2)


def test_car_confirmed_completion_fires_even_when_disabled():
    """Observation is explicitly what a disabled integration still does
    (priority rung 2 in logic.reduce()) -- the car-confirmed edge must not
    be gated behind `enabled`."""
    state = SessionState()
    state, _ = _tick(
        state,
        now=dt(22, 0),
        enabled=False,
        car_charging=True,
        plug_power_w=0.0,
        plug_switch_on=False,
        soc=55.0,
        soc_changed_at=dt(22, 0),
    )
    state, d = _tick(
        state,
        now=dt(22, 30),
        enabled=False,
        car_charging=False,
        car_charge_finished=True,
        plug_power_w=0.0,
        plug_switch_on=False,
        soc=80.0,
        soc_changed_at=dt(22, 30),
    )
    assert "ev_plug_charging_charge_complete" in _names(d)
    assert d.plug == PlugAction.UNCHANGED  # disabled: observes, never actuates


def test_last_charge_source_survives_charge_source_falling_back_to_none():
    """The whole reason last_charge_source exists: by the time a session
    ends, the LIVE charge_source has already fallen back to NONE, but the
    sticky field must still hold what actually powered the session."""
    state = SessionState()
    assert state.last_charge_source == ChargeSource.NONE

    state, d1 = _tick(
        state,
        now=dt(22, 0),
        car_charging=True,
        plug_switch_on=True,
        plug_power_w=1800.0,
        soc=55.0,
        soc_changed_at=dt(22, 0),
    )
    assert d1.charge_source == ChargeSource.PLUG
    assert state.last_charge_source == ChargeSource.PLUG

    # Charging stops; live charge_source falls back to NONE, but the sticky
    # field must still say PLUG.
    state, d2 = _tick(
        state,
        now=dt(22, 30),
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=56.0,
        soc_changed_at=dt(22, 30),
    )
    assert d2.charge_source == ChargeSource.NONE
    assert state.last_charge_source == ChargeSource.PLUG


def test_last_charge_source_tracks_bypass_sessions_too():
    state = SessionState()
    state, d = _tick(
        state,
        now=dt(22, 0),
        car_charging=True,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(22, 0),
    )
    assert d.charge_source == ChargeSource.BYPASS
    assert state.last_charge_source == ChargeSource.BYPASS


def test_new_fields_round_trip_through_the_store_codec():
    """prev_car_charging and last_charge_source must survive a restart --
    store.py is the single persistence codec (see its module docstring),
    so a field only living in SessionState and never touching to_dict/
    from_dict would silently reset to its default on every restart."""
    state = SessionState(prev_car_charging=True, last_charge_source=ChargeSource.BYPASS)
    restored = from_dict(to_dict(state))
    assert restored.prev_car_charging is True
    assert restored.last_charge_source == ChargeSource.BYPASS


def test_store_codec_defaults_are_safe_for_missing_data():
    restored = from_dict(None)
    assert restored.prev_car_charging is False
    assert restored.last_charge_source == ChargeSource.NONE

    restored2 = from_dict({})
    assert restored2.prev_car_charging is False
    assert restored2.last_charge_source == ChargeSource.NONE
