"""The normalised telemetry snapshot, and the freshness clock.

Everything here is SOURCE-AGNOSTIC. Each concrete provider lives in
`sources/` and is responsible only for turning its own wire format into a
`TelemetrySnapshot` -- it calls `derive_freshness()` below rather than
inventing its own clock, because that clock is the subtlest and highest-risk
piece of the whole integration and must behave identically no matter where
the reading came from.

Home Assistant's `last_changed` on the old template sensors moved only when
the RENDERED VALUE changed, not on every poll -- so a 120s REST poll
re-delivering an identical cached SoC correctly counted as stale
(packages/ev_charging.yaml:878-882 said so explicitly, and D3/D4 both
depend on it). `derive_freshness` reproduces that on purpose:

  1. If the source supplies a timestamp for the reading itself, prefer it
     unconditionally -- it makes the clock restart-safe for free, because it
     does not depend on remembering the previous snapshot.
  2. Otherwise, advance only when the numeric SoC differs from the previous
     snapshot. NEVER on poll time -- getting this wrong makes every
     staleness and projection decision degrade silently while looking
     healthy (R4, R8).

Zero Home Assistant imports; zero decision logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class TelemetrySnapshot:
    """What every source must produce, whatever it speaks underneath.

    `car_charging` is normalised by the source (each provider spells
    "charging" differently), while `charging_status` keeps the raw value for
    display and diagnostics.
    """

    soc: Optional[float]
    soc_changed_at: Optional[datetime]
    charging_status: Optional[str]
    car_charging: bool
    plugged: Optional[bool]
    polled_at: datetime
    source_reachable: bool
    used_payload_timestamp: bool = False
    # The car's own terminal "charging finished" report -- normalised by
    # the source the same way car_charging is, against a SEPARATE
    # configured state string (a provider's "done" value is not
    # necessarily its "in progress" value's opposite). See logic.reduce()'s
    # car-confirmed completion edge, and the module docstring above for why
    # this must not be reimplemented per source.
    car_charge_finished: bool = False
    # The 12V auxiliary battery's own state of charge, as a PERCENTAGE --
    # not a voltage, whatever the source's underlying field is named (PSACC
    # calls it "voltage"; see sources/psacc.py). None for a source that
    # doesn't expose this at all (TelemetrySource.supports_aux_battery).
    aux_battery_soc: Optional[float] = None


def derive_freshness(
    prev: Optional[TelemetrySnapshot],
    soc: Optional[float],
    payload_timestamp: Optional[datetime],
    now: datetime,
) -> tuple[Optional[datetime], bool]:
    """The shared clock rule. Returns (soc_changed_at, used_payload_timestamp).

    Sources call this instead of computing a timestamp themselves -- see the
    module docstring for why this must not vary by source.
    """
    if payload_timestamp is not None:
        return payload_timestamp, True
    if soc is None:
        return (prev.soc_changed_at if prev else None), False
    if prev is not None and prev.soc == soc and prev.soc_changed_at is not None:
        return prev.soc_changed_at, False  # unchanged value: NOT fresh
    return now, False  # first reading, or the value actually moved


def restored_snapshot(
    prev_soc: Optional[float],
    prev_soc_changed_at: Optional[datetime],
    now: datetime,
) -> TelemetrySnapshot:
    """Reconstructs a `prev` snapshot from the two fields store.py persisted,
    for the coordinator to seed the FIRST fetch after a restart.

    Without this, that first poll has no `prev` to compare against, so
    `derive_freshness` would treat it as unconditionally fresh even when the
    underlying value has not actually changed since before the restart --
    precisely the R4/R8 failure the clock exists to prevent.
    """
    return TelemetrySnapshot(
        soc=prev_soc,
        soc_changed_at=prev_soc_changed_at,
        charging_status=None,
        car_charging=False,
        plugged=None,
        polled_at=now,
        source_reachable=True,
    )


def unreachable_snapshot(now: datetime, prev: Optional[TelemetrySnapshot]) -> TelemetrySnapshot:
    """A failed fetch. SoC and its freshness clock are carried forward
    unchanged -- a network hiccup must not itself manufacture staleness, only
    source_reachable=False does (that is what FR-S4 tests, via
    logic.is_trustworthy_soc)."""
    return TelemetrySnapshot(
        soc=prev.soc if prev else None,
        soc_changed_at=prev.soc_changed_at if prev else None,
        charging_status=prev.charging_status if prev else None,
        car_charging=False,
        plugged=prev.plugged if prev else None,
        polled_at=now,
        source_reachable=False,
    )
