"""source.py: the freshness clock, which is SHARED by every telemetry
source and must behave identically regardless of which one produced the
reading. It is the highest-risk code in the integration: if soc_changed_at
tracks poll time instead of value change, every staleness and projection
decision silently degrades while looking perfectly healthy.

PSACC's own JSON parsing is tested separately in test_source_psacc.py; here
we exercise `derive_freshness` directly, so a future source that gets the
clock wrong fails against the same rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ev_plug_charging.source import (
    TelemetrySnapshot,
    derive_freshness,
    restored_snapshot,
    unreachable_snapshot,
)

T0 = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)


def _snap(soc, soc_changed_at):
    return TelemetrySnapshot(
        soc=soc,
        soc_changed_at=soc_changed_at,
        charging_status=None,
        car_charging=False,
        plugged=None,
        polled_at=T0,
        source_reachable=True,
    )


def test_first_reading_is_fresh():
    changed_at, used_payload = derive_freshness(None, 50.0, None, T0)
    assert changed_at == T0
    assert used_payload is False


def test_repeated_identical_value_does_not_advance_clock():
    """The load-bearing case: a poll re-delivering the SAME cached SoC has
    learned nothing, so it must NOT look fresh."""
    prev = _snap(50.0, T0)
    later = T0 + timedelta(minutes=30)
    changed_at, _ = derive_freshness(prev, 50.0, None, later)
    assert changed_at == T0  # unchanged -- NOT `later`


def test_changed_value_advances_clock():
    prev = _snap(50.0, T0)
    later = T0 + timedelta(minutes=30)
    changed_at, _ = derive_freshness(prev, 51.0, None, later)
    assert changed_at == later


def test_payload_timestamp_preferred_when_present():
    """A source-supplied timestamp wins outright, and makes the clock
    restart-safe without needing a previous snapshot at all."""
    prev = _snap(50.0, T0)
    payload_ts = T0 - timedelta(minutes=12)
    later = T0 + timedelta(minutes=30)
    changed_at, used_payload = derive_freshness(prev, 50.0, payload_ts, later)
    assert changed_at == payload_ts
    assert used_payload is True


def test_missing_soc_carries_forward_previous_clock():
    prev = _snap(50.0, T0)
    later = T0 + timedelta(minutes=10)
    changed_at, _ = derive_freshness(prev, None, None, later)
    assert changed_at == T0  # not reset to `later`


def test_missing_soc_with_no_previous_is_none():
    changed_at, _ = derive_freshness(None, None, None, T0)
    assert changed_at is None


def test_unreachable_snapshot_carries_soc_and_clock_but_flags_unreachable():
    """A failed poll must not itself manufacture staleness -- only
    source_reachable=False should change what is_trustworthy_soc sees."""
    prev = _snap(50.0, T0)
    later = T0 + timedelta(minutes=5)
    snap = unreachable_snapshot(later, prev)
    assert snap.soc == 50.0
    assert snap.soc_changed_at == T0
    assert snap.source_reachable is False
    assert snap.car_charging is False


def test_restored_snapshot_survives_a_restart():
    """R4/R8: store.py persists prev_soc/prev_soc_changed_at, and
    restored_snapshot() rebuilds the `prev` the first post-restart fetch
    compares against. A restart between the 18:00 drive and the 22:00
    plug-in must not make the 18:00 reading look fresh at 22:00."""
    drove_at = T0 - timedelta(hours=4)
    restart_at = T0
    prev = restored_snapshot(prev_soc=45.0, prev_soc_changed_at=drove_at, now=restart_at)

    changed_at, _ = derive_freshness(prev, 45.0, None, restart_at)
    assert changed_at == drove_at  # still 4h stale, not restart_at


def test_without_restored_state_the_first_reading_cannot_avoid_looking_fresh():
    """The limitation restored_snapshot exists to avoid: with no prev at
    all, there is nothing to compare against."""
    changed_at, _ = derive_freshness(None, 45.0, None, T0)
    assert changed_at == T0
