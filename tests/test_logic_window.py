"""in_window(): half-open interval, midnight wrap, and the D10/R12 edge
override."""
from __future__ import annotations

from datetime import datetime, time, timezone

from ev_plug_charging.logic import in_window


def _at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 12, h, m, tzinfo=timezone.utc)


def test_simple_non_wrapping_window():
    start, end = time(9, 0), time(17, 0)
    assert in_window(_at(8, 59), start, end) is False
    assert in_window(_at(9, 0), start, end) is True
    assert in_window(_at(16, 59), start, end) is True
    assert in_window(_at(17, 0), start, end) is False  # half-open at the end


def test_midnight_wrap():
    start, end = time(23, 0), time(7, 0)
    assert in_window(_at(22, 59), start, end) is False
    assert in_window(_at(23, 0), start, end) is True
    assert in_window(_at(0, 30), start, end) is True
    assert in_window(_at(6, 59), start, end) is True
    assert in_window(_at(7, 0), start, end) is False
    assert in_window(_at(12, 0), start, end) is False


def test_zero_width_window_never_in():
    start = end = time(23, 0)
    assert in_window(_at(23, 0), start, end) is False
    assert in_window(_at(12, 0), start, end) is False


def test_window_open_edge_wins_even_if_clock_disagrees():
    """R12 (2026-08-11 22:00:00): the exact point-in-time callback fires,
    but its own `now()` lands a hair before `start` due to scheduling
    jitter -- in_window must still say True on that edge, matching D10's
    intent that the moment of the callback is authoritative, not a
    recomputation from the clock."""
    start, end = time(22, 0), time(6, 0)
    one_second_early = _at(21, 59)
    # (helper only supports minute resolution; simulate "a hair before"
    # by using 21:59 exactly, one minute early -- still must be overridden)
    assert in_window(one_second_early, start, end, open_edge=True) is True


def test_window_close_edge_wins_even_if_clock_disagrees():
    start, end = time(22, 0), time(6, 0)
    assert in_window(_at(5, 59), start, end, close_edge=True) is False
