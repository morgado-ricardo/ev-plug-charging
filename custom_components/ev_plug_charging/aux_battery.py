"""12V auxiliary-battery health, deliberately simpler than the YAML's
seven sensors -- four entities instead: `aux_battery` (the raw reading),
`aux_battery_resting` (the same value, but only comparable samples --
charging and driving both inflate it), `aux_battery_7d` (a rolling mean),
and `aux_battery_health` (a band, with the 7d-vs-30d drift as an
attribute rather than its own entity -- see AGENTS.md's port notes for
why `days_since_driven` and a separate 30d-average entity were left out).

Every "don't wake the car" decision in this integration exists to protect
this battery; before this module, the integration had no way to show
whether that protection was actually working. The data costs nothing to
get: it rides on the same cached payload every routine poll already
fetches (see sources/psacc.py's parse_vehicle_info).

Zero Home Assistant imports, same discipline as logic.py/session.py --
unit-tested with plain pytest.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Optional

from .const import (
    AUX_BATTERY_BASELINE_DAYS,
    AUX_BATTERY_CRITICAL_THRESHOLD,
    AUX_BATTERY_HEALTHY_THRESHOLD,
    AUX_BATTERY_LOW_DWELL_SECONDS,
    AUX_BATTERY_LOW_THRESHOLD,
    AUX_BATTERY_ROLLING_DAYS,
    AUX_BATTERY_SAMPLE_WINDOW_DAYS,
    EVENT_AUX_BATTERY_CRITICAL,
    EVENT_AUX_BATTERY_LOW,
)
from .models import Event, SessionState

__all__ = [
    "AuxBatteryReading",
    "record_resting_sample",
    "rolling_mean",
    "health_band",
    "advance_aux_battery",
]


def record_resting_sample(
    samples: tuple[tuple[str, float], ...],
    day: date,
    value: float,
    max_entries: int = AUX_BATTERY_SAMPLE_WINDOW_DAYS,
) -> tuple[tuple[str, float], ...]:
    """One entry per calendar day -- the LAST resting reading of that day
    wins, so a day with many polls still contributes exactly one sample
    to the rolling mean (matching how a human reading a daily "resting
    SoC" figure would expect it to behave). Capped at `max_entries`,
    oldest dropped first."""
    day_iso = day.isoformat()
    entries = list(samples)
    if entries and entries[-1][0] == day_iso:
        entries[-1] = (day_iso, value)
    else:
        entries.append((day_iso, value))
    return tuple(entries[-max_entries:])


def rolling_mean(
    samples: tuple[tuple[str, float], ...],
    as_of_day: date,
    window_days: int,
) -> Optional[float]:
    """Mean of every sample whose day falls within the last `window_days`
    days of `as_of_day`, inclusive. None with nothing in range -- "no data
    yet", not zero."""
    if not samples:
        return None
    cutoff = as_of_day.toordinal() - window_days
    values = [
        value
        for day_iso, value in samples
        if date.fromisoformat(day_iso).toordinal() > cutoff
        and date.fromisoformat(day_iso).toordinal() <= as_of_day.toordinal()
    ]
    if not values:
        return None
    return sum(values) / len(values)


def health_band(avg_7d: Optional[float]) -> str:
    """healthy / watch / low / unknown -- the YAML's own proven bands
    (packages/opel.yaml:161-176), unchanged."""
    if avg_7d is None:
        return "unknown"
    if avg_7d >= AUX_BATTERY_HEALTHY_THRESHOLD:
        return "healthy"
    if avg_7d >= AUX_BATTERY_LOW_THRESHOLD:
        return "watch"
    return "low"


@dataclass(frozen=True)
class AuxBatteryReading:
    """Display values for one tick. Not itself persisted -- derived fresh
    from SessionState.aux_battery_samples every time, the same relationship
    logic.projected_soc() has to the anchor it's derived from."""

    current: Optional[float]
    resting: Optional[float]
    avg_7d: Optional[float]
    avg_30d: Optional[float]
    drift: Optional[float]
    health: str


def _dwell(condition: bool, since: Optional[datetime], now: datetime) -> tuple[Optional[datetime], float]:
    """Same shape as logic.py's own _dwell -- kept local rather than
    imported so this module has no dependency on logic.py at all."""
    if not condition:
        return None, 0.0
    if since is None:
        since = now
    return since, (now - since).total_seconds() / 60.0


def advance_aux_battery(
    state: SessionState,
    now: datetime,
    aux_battery_soc: Optional[float],
    at_rest: bool,
) -> tuple[SessionState, AuxBatteryReading, tuple[Event, ...]]:
    """One call per coordinator tick, after logic.reduce() -- the same
    "second pure step over the state reduce() just returned" shape
    coordinator._track_energy already uses. `at_rest` is the caller's
    `not decision.charging_active`: charging and driving both inflate the
    12V reading, matching the YAML's own resting-sensor gate
    (packages/opel.yaml:469-481, D7's "actuator truth vs car truth" split
    applied to a different battery).
    """
    events: list[Event] = []

    resting = aux_battery_soc if (at_rest and aux_battery_soc is not None) else None
    samples = state.aux_battery_samples
    if resting is not None:
        samples = record_resting_sample(samples, now.date(), resting)
    state = replace(state, aux_battery_samples=samples)

    avg_7d = rolling_mean(samples, now.date(), AUX_BATTERY_ROLLING_DAYS)
    avg_30d = rolling_mean(samples, now.date(), AUX_BATTERY_BASELINE_DAYS)
    drift = (avg_30d - avg_7d) if (avg_7d is not None and avg_30d is not None) else None
    health = health_band(avg_7d)

    # -- low: the 7-day trend, held for AUX_BATTERY_LOW_DWELL_SECONDS so a
    # single cold-morning dip can't trigger it (opel_12v_low's own reason
    # for its 6h "for") --
    low_now = avg_7d is not None and avg_7d < AUX_BATTERY_LOW_THRESHOLD
    since, held = _dwell(low_now, state.aux_battery_low_since, now)
    state = replace(state, aux_battery_low_since=since)
    low_persistent = low_now and held * 60 >= AUX_BATTERY_LOW_DWELL_SECONDS
    if low_persistent and not state.aux_battery_low_notified:
        events.append(
            Event(EVENT_AUX_BATTERY_LOW, {"avg_7d": avg_7d, "avg_30d": avg_30d, "drift": drift})
        )
        state = replace(state, aux_battery_low_notified=True)
    elif not low_persistent and state.aux_battery_low_notified:
        state = replace(state, aux_battery_low_notified=False)

    # -- critical: the LEVEL right now (on a resting reading), not the
    # trend -- opel_12v_critical's own distinction. Deliberately only
    # evaluated on a resting tick: while the car is charging or driving,
    # the DC-DC converter is actively feeding the 12V, so "is it critical
    # right now" isn't a meaningful question until it's at rest again. --
    critical_now = resting is not None and resting < AUX_BATTERY_CRITICAL_THRESHOLD
    if critical_now and not state.aux_battery_critical_notified:
        events.append(Event(EVENT_AUX_BATTERY_CRITICAL, {"resting_soc": resting}))
        state = replace(state, aux_battery_critical_notified=True)
    elif not critical_now and state.aux_battery_critical_notified:
        state = replace(state, aux_battery_critical_notified=False)

    reading = AuxBatteryReading(
        current=aux_battery_soc,
        resting=resting,
        avg_7d=avg_7d,
        avg_30d=avg_30d,
        drift=drift,
        health=health,
    )
    return state, reading, tuple(events)
