---
name: adding-a-telemetry-source
description: Load before adding support for a new vehicle/telemetry provider. Explains the closed three-step procedure, the TelemetrySource contract, why the freshness clock must not be reimplemented per source, and what a new source must not touch.
---

# Adding a telemetry source

## It's meant to be a closed job

`sources/__init__.py`'s module docstring already states the procedure:

1. Write `sources/<name>.py` with a class implementing `TelemetrySource`
   (`sources/base.py`).
2. Add a constant in `const.py` and one line each to the `SOURCES` and
   `SOURCE_LABELS` registries in `sources/__init__.py`.
3. Add a config-flow step for the source's own settings, plus its strings in
   `strings.json` and `translations/{en,pt}.json`.

## What must NOT change

`logic.py`, `session.py`, `rate_model.py`, `coordinator.py`, and every
entity platform. If adding a source seems to require touching one of these,
the normalisation belongs in the adapter instead — everything above the
source layer works off a *normalised* `TelemetrySnapshot`
(`source.py`), and knows nothing about where it came from. This is verified
mechanically: `tests/test_source_psacc.py`'s
`test_every_registered_source_implements_the_contract` walks the `SOURCES`
registry and checks every entry is a `TelemetrySource` subclass with a
matching `source_type` and a `SOURCE_LABELS` entry — a new source
automatically inherits that check.

## The contract (`sources/base.py`)

- `source_type: str` — the registry key, and the value stored in config
  entries. Never renamed once shipped (it's persisted data).
- `supports_refresh: bool` — `False` by default. If your source has no way
  to request a fresh reading on demand, leave it `False`; the rescue-wakeup
  gate then quietly never fires for this source rather than erroring.
- `from_config(session, config) -> TelemetrySource` — build an instance from
  the merged `entry.data`/`entry.options` dict.
- `async_fetch(now, prev) -> TelemetrySnapshot` — the routine poll. Must
  never itself risk waking the vehicle (see below).
- `async_request_refresh() -> bool` — only if `supports_refresh`; the one
  deliberate wakeup.
- `async_validate(hass, user_input) -> dict` — one real call during config
  flow setup, so a wrong URL/ID is caught during setup rather than three
  silent retries into the first poll.
- `diagnostics() -> dict` — see identity redaction below.
- Four error types (`SourceConnectionError`, `SourceResponseError`,
  `SourceNoVehicleData`, and the base `SourceError`) that the coordinator and
  config flow already know how to handle; raise the specific one that
  matches the failure rather than a bare exception.

## The freshness clock is not yours to reimplement

Every source's `async_fetch()` must derive `soc_changed_at` by calling
`source.derive_freshness(prev, soc, payload_timestamp, now)`, never by
inventing its own logic. Its semantics (`source.py`) intentionally reproduce
Home Assistant's own `last_changed`: a re-delivered value **identical** to
the previous one is *not* fresh, even if the poll that delivered it happened
just now. Only an actual value change (or an explicit payload timestamp, if
the source has one) advances the clock.

This is called out as the single highest-risk line in the whole port,
because getting it wrong doesn't crash anything — it silently degrades
every staleness and projection decision while the integration continues to
look perfectly healthy. `tests/test_source_freshness.py` tests
`derive_freshness()` directly and applies to any source; a new source's own
tests (parsing, URL-building) belong in a `test_source_<name>.py`
alongside it, following `tests/test_source_psacc.py`'s shape.

## Diagnostics must not leak identity

`PsaccSource.diagnostics()` (`sources/psacc.py`) deliberately omits the base
URL and VIN — both identify the vehicle and, by extension, its owner. A new
source's `diagnostics()` should include its `source_type` and any
non-identifying config (state-string mappings, thresholds) and omit
anything that identifies the account, vehicle, or location.

## The cost model to think through before implementing `async_fetch`

PSACC is unusually well-behaved: reading (`?from_cache=1`) and refreshing
(`/wakeup`) are **separate acts**, so a routine poll costs the vehicle
nothing (`sources/psacc.py`'s module docstring explains this at length).
Most vehicle APIs conflate the two — a plain "give me current status" call
*is* a live poll and *does* wake the car. If your source works that way,
**the poll interval becomes the primary lever for protecting the 12 V
battery**, not the rescue-refresh gate (which only bounds the one
*deliberate* extra wakeup on top of routine polling). Document which
behaviour your source has in its module docstring, the way `psacc.py` does.

## A minimal skeleton

```python
"""<Provider> as a telemetry source. <One sentence on its cost model —
does async_fetch ever wake the vehicle, or is it free like PSACC?>"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..const import SOURCE_TYPE_<NAME>
from ..source import TelemetrySnapshot, derive_freshness
from .base import SourceConnectionError, TelemetrySource


class <Name>Source(TelemetrySource):
    source_type = SOURCE_TYPE_<NAME>
    supports_refresh = False  # or True, if there's a real wakeup call

    @classmethod
    def from_config(cls, session, config: dict[str, Any]) -> "<Name>Source":
        ...

    async def async_fetch(self, now: datetime, prev: Optional[TelemetrySnapshot]) -> TelemetrySnapshot:
        raw = await self._get_json(...)  # import aiohttp inside this method
        soc = ...  # parse
        payload_ts = ...  # parse, or None
        soc_changed_at, used_payload_ts = derive_freshness(prev, soc, payload_ts, now)
        return TelemetrySnapshot(
            soc=soc,
            soc_changed_at=soc_changed_at,
            charging_status=...,
            car_charging=...,
            plugged=...,
            polled_at=now,
            source_reachable=True,
            used_payload_timestamp=used_payload_ts,
        )

    @staticmethod
    async def async_validate(hass, user_input: dict[str, Any]) -> dict[str, Any]:
        ...

    def diagnostics(self) -> dict[str, Any]:
        return {"source_type": self.source_type}  # no URL, no vehicle ID
```

Then: `const.py` gets `SOURCE_TYPE_<NAME> = "<name>"`; `sources/__init__.py`
gets `SOURCE_TYPE_<NAME>: <Name>Source` in `SOURCES` and a human-readable
label in `SOURCE_LABELS`; the config flow gets one new step keyed on the
source-selection step choosing `<name>`.

Tests to write: parsing tests for the raw payload shape (mirror
`tests/test_source_psacc.py`), and nothing else new for freshness or the
registry contract — those are inherited automatically once the source is
registered.
