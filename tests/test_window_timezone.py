"""The charge window is LOCAL wall-clock time, and the whole integration
has to agree about that.

`time.*_window_start` is a Home Assistant `TimeEntity`. When someone types
22:40 into it they mean 22:40 on their own kitchen clock -- the entity
stores a naive `datetime.time` and nothing anywhere converts it. So every
comparison against it has to use a local-time `now`.

Feeding it a UTC `now` instead does not error, does not warn, and is
invisible in every entity state. It just moves the whole window by the
UTC offset: in Europe/Lisbon in summer a 22:40 window opened at 23:40, and
in Europe/Madrid it would be 00:40 the next day. The only symptom is that
the charge does not start and `decision_reason` reads
`outside_window_not_ours` at a time the user can see is inside the window.

These tests pin both halves down: the contract `in_window` is written to,
and the rule that no module may reach for a UTC clock.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from ev_plug_charging.logic import in_window

# Europe/Lisbon in September: WEST, UTC+1. The offset is the whole bug.
LISBON = timezone(timedelta(hours=1))

_PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "ev_plug_charging"


def test_window_is_evaluated_against_local_wall_clock():
    """22:40 local, with a 22:40 window: in the window.

    The same instant expressed in UTC is 21:40, which is NOT in the
    window -- so passing a UTC `now` reverses the answer.
    """
    start, end = time(22, 40), time(8, 0)
    local_now = datetime(2026, 9, 19, 22, 40, 30, tzinfo=LISBON)

    assert in_window(local_now, start, end) is True
    assert in_window(local_now.astimezone(timezone.utc), start, end) is False


def test_a_utc_now_delays_the_window_by_the_offset():
    """Not just an edge case at the boundary -- the window stays wrong for
    a full hour after it should have opened."""
    start, end = time(22, 40), time(8, 0)
    for minutes_in in (1, 15, 45, 59):
        local_now = datetime(2026, 9, 19, 22, 40, tzinfo=LISBON) + timedelta(minutes=minutes_in)
        assert in_window(local_now, start, end) is True
        assert in_window(local_now.astimezone(timezone.utc), start, end) is False


def test_no_module_uses_a_utc_clock():
    """A structural guard, because the defect lives in the CALLER.

    `in_window` is correct for whatever it is handed; the bug was that
    `coordinator.py` handed it `dt_util.utcnow()`. There is no pure-logic
    test that can catch that, and the entity states all look healthy when
    it is wrong -- so this asserts the property directly on the source.

    If a genuine need for a UTC clock ever arises, this test is the place
    to record why, not something to delete quietly.
    """
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\butcnow\b", line):
                offenders.append(f"{path.relative_to(_PACKAGE)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Use dt_util.now() (local) -- the charge window is local wall clock:\n  "
        + "\n  ".join(offenders)
    )
