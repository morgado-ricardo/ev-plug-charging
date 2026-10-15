"""No vehicle telemetry at all: the state of charge comes from a person.

For a car with no API this integration can read, or an owner who would
rather not connect one. Every Smart charge then starts the same way the
confirm prompt starts one when PSACC is unreachable: the window opens, the
"charge NOT started -- no reading" push asks for the state of charge, and
the answer (push reply, number.*_current_soc, or the set_soc service)
anchors the session. From there the projection, the stop at target and the
window-close backstop are exactly the ones a telemetry source gets -- they
never needed more than a start value and a clock.

What does NOT carry over, because each needs the car itself to report:
bypass detection, car-confirmed completion, 12V tracking, the rescue and
daily wakeups, and rate/efficiency learning (no end reading, so the seed
rate is the only rate). Timed mode needs no state of charge at all.

Costs the vehicle nothing: there is nothing to poll.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..const import SOURCE_TYPE_NONE
from ..source import TelemetrySnapshot, derive_freshness
from .base import TelemetrySource


class NoTelemetrySource(TelemetrySource):
    source_type = SOURCE_TYPE_NONE
    reports_live = False

    @classmethod
    def from_config(cls, session: Any, config: dict[str, Any]) -> "NoTelemetrySource":
        return cls()

    async def async_fetch(
        self, now: datetime, prev: Optional[TelemetrySnapshot]
    ) -> TelemetrySnapshot:
        # Reachable, with nothing to say: an unreachable snapshot would
        # word the not-started push as "source unreachable" every night,
        # when the truth is "there is no reading -- give me one".
        soc_changed_at, used_payload_timestamp = derive_freshness(prev, None, None, now)
        return TelemetrySnapshot(
            soc=None,
            soc_changed_at=soc_changed_at,
            charging_status=None,
            car_charging=False,
            plugged=None,
            polled_at=now,
            source_reachable=True,
            used_payload_timestamp=used_payload_timestamp,
        )

    @staticmethod
    async def async_validate(hass: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        return {}
