"""aux_battery.py: the 12V auxiliary-battery tracker. PSA's field is named
`battery.voltage` but is actually the 12V's own state of charge as a
percentage -- see test_source_psacc.py for that parsing; here we exercise
the pure resting-sample/rolling-mean/health-band/event logic directly, the
same split test_source_freshness.py has from test_source_psacc.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ev_plug_charging.aux_battery import (
    advance_aux_battery,
    health_band,
    record_resting_sample,
    rolling_mean,
)
from ev_plug_charging.models import SessionState

T0 = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


def dt(day_offset=0, hour=6):
    return T0 + timedelta(days=day_offset, hours=hour - 6)


# -- record_resting_sample: one entry per day, capped ---------------------- #


def test_first_sample_is_appended():
    samples = record_resting_sample((), date(2026, 9, 1), 80.0)
    assert samples == (("2026-09-01", 80.0),)


def test_same_day_sample_overwrites_not_appends():
    """The LAST resting reading of the day wins -- many polls, one sample."""
    samples = record_resting_sample((), date(2026, 9, 1), 80.0)
    samples = record_resting_sample(samples, date(2026, 9, 1), 78.0)
    assert samples == (("2026-09-01", 78.0),)


def test_new_day_appends_a_new_entry():
    samples = record_resting_sample((), date(2026, 9, 1), 80.0)
    samples = record_resting_sample(samples, date(2026, 9, 2), 79.0)
    assert samples == (("2026-09-01", 80.0), ("2026-09-02", 79.0))


def test_ring_buffer_caps_at_max_entries():
    samples: tuple = ()
    for i in range(1, 36):
        samples = record_resting_sample(
            samples, date(2026, 9, 1) + timedelta(days=i), float(i), max_entries=30
        )
    assert len(samples) == 30
    # the oldest 5 were dropped -- the buffer keeps the most recent 30
    assert samples[0][1] == 6.0
    assert samples[-1][1] == 35.0


# -- rolling_mean ------------------------------------------------------------ #


def test_rolling_mean_of_no_samples_is_none():
    assert rolling_mean((), date(2026, 9, 12), 7) is None


def test_rolling_mean_averages_within_the_window_inclusive_of_today():
    samples = (
        ("2026-09-06", 70.0),  # exactly 6 days before -- inside a 7-day window
        ("2026-09-12", 80.0),  # today
    )
    assert rolling_mean(samples, date(2026, 9, 12), 7) == 75.0


def test_rolling_mean_excludes_samples_outside_the_window():
    samples = (
        ("2026-09-04", 10.0),  # 8 days before -- outside a 7-day window
        ("2026-09-12", 80.0),
    )
    assert rolling_mean(samples, date(2026, 9, 12), 7) == 80.0


# -- health_band -------------------------------------------------------------- #


def test_health_band_unknown_with_no_data():
    assert health_band(None) == "unknown"


def test_health_band_thresholds():
    assert health_band(70.0) == "healthy"
    assert health_band(69.9) == "watch"
    assert health_band(50.0) == "watch"
    assert health_band(49.9) == "low"


# -- advance_aux_battery: the coordinator-facing reducer ---------------------- #


def test_resting_sample_only_recorded_while_at_rest():
    state = SessionState()
    state, reading, _ = advance_aux_battery(state, dt(0), 60.0, at_rest=False)
    assert reading.resting is None
    assert state.aux_battery_samples == ()

    state, reading, _ = advance_aux_battery(state, dt(0), 60.0, at_rest=True)
    assert reading.resting == 60.0
    assert len(state.aux_battery_samples) == 1


def test_current_reading_always_reported_regardless_of_rest():
    state = SessionState()
    _, reading, _ = advance_aux_battery(state, dt(0), 45.0, at_rest=False)
    assert reading.current == 45.0
    assert reading.resting is None


def test_health_and_drift_computed_from_accumulated_samples():
    state = SessionState()
    for day in range(7):
        state, reading, _ = advance_aux_battery(state, dt(day), 75.0, at_rest=True)
    assert reading.avg_7d == 75.0
    assert reading.health == "healthy"


def test_critical_event_fires_once_on_a_resting_reading_below_30():
    state = SessionState()
    state, reading, events = advance_aux_battery(state, dt(0), 25.0, at_rest=True)
    assert reading.resting == 25.0
    names = [e.name for e in events]
    assert "ev_plug_charging_aux_battery_critical" in names
    assert state.aux_battery_critical_notified is True

    # Second tick still critical: must not re-fire (one-shot latch).
    state, _, events2 = advance_aux_battery(
        state, dt(0) + timedelta(minutes=5), 24.0, at_rest=True
    )
    assert "ev_plug_charging_aux_battery_critical" not in [e.name for e in events2]


def test_critical_event_clears_and_can_refire_after_recovering():
    state = SessionState()
    state, _, _ = advance_aux_battery(state, dt(0), 20.0, at_rest=True)
    assert state.aux_battery_critical_notified is True

    state, _, _ = advance_aux_battery(state, dt(1), 80.0, at_rest=True)
    assert state.aux_battery_critical_notified is False

    state, _, events = advance_aux_battery(state, dt(2), 20.0, at_rest=True)
    assert "ev_plug_charging_aux_battery_critical" in [e.name for e in events]


def test_critical_not_evaluated_while_charging_or_driving():
    """Deliberate simplification: while the DC-DC converter is actively
    feeding the 12V, "is it critical right now" isn't meaningful -- only a
    resting reading is trusted for the level check."""
    state = SessionState()
    _, _, events = advance_aux_battery(state, dt(0), 20.0, at_rest=False)
    assert events == ()


def test_low_event_requires_the_full_dwell_not_just_one_bad_reading():
    """A single day under 50% must not fire the low alert immediately --
    it needs to persist for AUX_BATTERY_LOW_DWELL_SECONDS (6h)."""
    state = SessionState()
    state, reading, events = advance_aux_battery(state, dt(0), 40.0, at_rest=True)
    assert reading.avg_7d == 40.0  # genuinely low on the 7d average already
    assert "ev_plug_charging_aux_battery_low" not in [e.name for e in events]

    # 3 hours later, still low -- still within the dwell.
    state, _, events2 = advance_aux_battery(state, dt(0) + timedelta(hours=3), 40.0, at_rest=True)
    assert "ev_plug_charging_aux_battery_low" not in [e.name for e in events2]

    # 6+ hours later, still low -- now it fires.
    state, _, events3 = advance_aux_battery(state, dt(0) + timedelta(hours=6, minutes=1), 40.0, at_rest=True)
    assert "ev_plug_charging_aux_battery_low" in [e.name for e in events3]
    assert state.aux_battery_low_notified is True


def test_low_dwell_resets_if_the_average_recovers_before_the_dwell_completes():
    state = SessionState()
    state, _, _ = advance_aux_battery(state, dt(0), 40.0, at_rest=True)
    # Recovers immediately -- the "since" timer must reset.
    state, _, _ = advance_aux_battery(state, dt(0) + timedelta(hours=1), 90.0, at_rest=True)
    assert state.aux_battery_low_since is None


def test_aux_battery_state_round_trips_through_the_store_codec():
    from ev_plug_charging.store import from_dict, to_dict

    state = SessionState(
        aux_battery_samples=(("2026-09-01", 55.5), ("2026-09-02", 54.0)),
        aux_battery_low_since=dt(0),
        aux_battery_low_notified=True,
        aux_battery_critical_notified=True,
        charge_completed_at=dt(0),
    )
    restored = from_dict(to_dict(state))
    assert restored.aux_battery_samples == (("2026-09-01", 55.5), ("2026-09-02", 54.0))
    assert restored.aux_battery_low_since == dt(0)
    assert restored.aux_battery_low_notified is True
    assert restored.aux_battery_critical_notified is True
    assert restored.charge_completed_at == dt(0)


def test_aux_battery_store_defaults_are_safe_for_missing_data():
    from ev_plug_charging.store import from_dict

    restored = from_dict(None)
    assert restored.aux_battery_samples == ()
    assert restored.aux_battery_low_since is None
    assert restored.aux_battery_low_notified is False
    assert restored.aux_battery_critical_notified is False
    assert restored.charge_completed_at is None
