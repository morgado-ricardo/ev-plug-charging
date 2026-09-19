"""The daily wakeup: a restart-safe scheduled-time crossing
(logic._daily_wakeup_due) plus the three budget gates reduce() applies
before actually spending it (session active, already fresh, source
unreachable). Ported from opel_daily_wakeup
(packages/opel.yaml:884-898), but budget-aware where the YAML was not --
see AGENTS.md's core-objective section on why that matters.
"""
from __future__ import annotations

from datetime import time, timedelta

from ev_plug_charging.logic import _daily_wakeup_due, reduce
from ev_plug_charging.models import SessionState

from conftest import DAY, base_inputs, dt

TARGET = time(6, 0)


# -- _daily_wakeup_due: the pure crossing test ---------------------------- #


def test_not_due_before_the_target_time():
    assert _daily_wakeup_due(dt(5, 59), TARGET, None) is False


def test_due_at_the_target_time_with_no_prior_wakeup():
    assert _daily_wakeup_due(dt(6, 0), TARGET, None) is True


def test_due_after_the_target_time_with_no_prior_wakeup():
    assert _daily_wakeup_due(dt(9, 0), TARGET, None) is True


def test_not_due_again_the_same_day_once_served():
    served_at = dt(6, 3)
    assert _daily_wakeup_due(dt(6, 5), TARGET, served_at) is False
    assert _daily_wakeup_due(dt(23, 0), TARGET, served_at) is False


def test_due_again_the_next_day_after_being_served():
    served_yesterday = dt(6, 3, day=DAY - timedelta(days=1))
    assert _daily_wakeup_due(dt(6, 1), TARGET, served_yesterday) is True


def test_restart_at_0559_does_not_lose_the_0600_fire():
    """A restart just before the target time: the next tick after 06:00
    must still see it as due."""
    assert _daily_wakeup_due(dt(6, 1), TARGET, None) is True


def test_restart_at_0601_after_already_firing_does_not_double_fire():
    """A restart just after a wakeup already fired this morning must not
    re-fire just because in-memory state was lost -- last_wakeup_at
    persisted through store.py is exactly what prevents this."""
    already_served = dt(6, 0, 30)
    assert _daily_wakeup_due(dt(6, 1), TARGET, already_served) is False


# -- reduce()-level budget gates ------------------------------------------ #


def _tick(state, **overrides):
    inp = base_inputs().set(**overrides).build()
    return reduce(state, inp)


def _refresh_events(decision):
    return [e for e in decision.events if e.name == "ev_plug_charging_refresh_attempted"]


def test_daily_wakeup_requests_a_refresh_when_due_and_all_gates_pass():
    state = SessionState()
    state, d = _tick(
        state,
        now=dt(6, 5),
        daily_wakeup_enabled=True,
        daily_wakeup_time=TARGET,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(4, 0),  # 125 min silence > the 40 min default gap
        rescue_refresh_enabled=False,  # isolate: only the daily gate is exercised
    )
    assert d.request_refresh is True
    daily_events = [e for e in _refresh_events(d) if e.data.get("trigger") == "daily"]
    assert len(daily_events) == 1
    assert state.last_daily_wakeup_at == dt(6, 5)


def test_daily_wakeup_skipped_when_disabled():
    state = SessionState()
    _, d = _tick(
        state,
        now=dt(6, 5),
        daily_wakeup_enabled=False,
        daily_wakeup_time=TARGET,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(4, 0),
        rescue_refresh_enabled=False,
    )
    assert d.request_refresh is False


def test_daily_wakeup_skipped_while_a_session_is_active():
    """The rescue gate already owns refreshing during a session -- a
    second, independent wakeup here would be pure waste."""
    state = SessionState()
    _, d = _tick(
        state,
        now=dt(6, 5),
        daily_wakeup_enabled=True,
        daily_wakeup_time=TARGET,
        car_charging=True,
        plug_switch_on=True,
        plug_power_w=1800.0,
        soc=55.0,
        soc_changed_at=dt(4, 0),
        rescue_refresh_enabled=False,
    )
    assert d.request_refresh is False


def test_daily_wakeup_skipped_when_soc_already_fresh():
    """A forced wakeup that would buy nothing -- the feed is already
    fresher than the expected reporting gap."""
    state = SessionState()
    _, d = _tick(
        state,
        now=dt(6, 5),
        daily_wakeup_enabled=True,
        daily_wakeup_time=TARGET,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(6, 0),  # 5 min silence, well under the 40 min gap
        rescue_refresh_enabled=False,
    )
    assert d.request_refresh is False


def test_daily_wakeup_skipped_when_source_unreachable():
    state = SessionState()
    _, d = _tick(
        state,
        now=dt(6, 5),
        daily_wakeup_enabled=True,
        daily_wakeup_time=TARGET,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(4, 0),
        source_reachable=False,
        rescue_refresh_enabled=False,
    )
    assert d.request_refresh is False


def test_daily_wakeup_does_not_fire_twice_across_ticks_the_same_day():
    state = SessionState()
    state, d1 = _tick(
        state,
        now=dt(6, 5),
        daily_wakeup_enabled=True,
        daily_wakeup_time=TARGET,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(4, 0),
        rescue_refresh_enabled=False,
    )
    assert d1.request_refresh is True

    state, d2 = _tick(
        state,
        now=dt(6, 10),
        daily_wakeup_enabled=True,
        daily_wakeup_time=TARGET,
        car_charging=False,
        plug_switch_on=False,
        plug_power_w=0.0,
        soc=55.0,
        soc_changed_at=dt(4, 0),  # still stale; only the day-bucket should stop a re-fire
        rescue_refresh_enabled=False,
    )
    assert d2.request_refresh is False


def test_last_daily_wakeup_at_round_trips_through_the_store_codec():
    from ev_plug_charging.store import from_dict, to_dict

    state = SessionState(last_daily_wakeup_at=dt(6, 0))
    restored = from_dict(to_dict(state))
    assert restored.last_daily_wakeup_at == dt(6, 0)

    assert from_dict(None).last_daily_wakeup_at is None
    assert from_dict({}).last_daily_wakeup_at is None
