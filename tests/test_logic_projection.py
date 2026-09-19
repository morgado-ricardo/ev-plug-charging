"""projected_soc(): max(reading_proj, session_proj), D1-D3."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ev_plug_charging.logic import projected_soc
from ev_plug_charging.models import RateSnapshot, SessionAnchor

RATE = RateSnapshot(minutes_per_percent=20.2)
T0 = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)


def test_not_charging_returns_current_reading():
    assert (
        projected_soc(T0, 55.0, True, False, None, T0, SessionAnchor(), RATE) == 55.0
    )


def test_untrustworthy_soc_returns_none():
    """D8: a missing/untrustworthy SoC must not silently become a number
    a caller could compare against target (see R6)."""
    assert projected_soc(T0, None, False, True, T0, T0, SessionAnchor(), RATE) is None
    assert projected_soc(T0, 55.0, False, True, T0, T0, SessionAnchor(), RATE) is None


def test_reading_projection_extrapolates_from_last_reading():
    charge_started = T0
    last_reading_at = T0 + timedelta(minutes=10)
    now = T0 + timedelta(minutes=30)  # 20 min since the last reading
    anchor = SessionAnchor(soc=55.0, captured_at=charge_started, provisional=False)
    proj = projected_soc(now, 60.0, True, True, charge_started, last_reading_at, anchor, RATE)
    # reading_proj = 60 + 20/20.2 ~= 60.99; session_proj (from 55%, margin
    # applied) is smaller over 30 min -- reading term should win.
    assert proj is not None
    assert 60.9 <= proj <= 61.1


def test_session_cap_wins_when_reading_understates():
    """D2: 'the session cap must not be argued down by a reading
    projection that is still optimistic [i.e. too low]'. Construct the
    case directly: a fresh-but-low reading arrives after a long charge, so
    the reading term barely extrapolates, while the session term -- which
    has been counting from the start SoC the whole time -- is already much
    higher. max() must pick the session term, not the reading term."""
    charge_started = T0
    now = T0 + timedelta(minutes=400)
    fresh_low_reading_at = now - timedelta(minutes=5)
    anchor = SessionAnchor(soc=50.0, captured_at=charge_started, provisional=False)
    proj = projected_soc(now, 52.0, True, True, charge_started, fresh_low_reading_at, anchor, RATE)
    reading_proj = 52.0 + 5 / 20.2
    session_proj = 50.0 + 400 / (20.2 * 1.15)
    assert session_proj > reading_proj  # sanity: the scenario is set up right
    assert proj == round(max(reading_proj, session_proj), 1)
    assert proj == round(session_proj, 1)
