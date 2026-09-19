# AGENTS.md — EV Plug Charging

This file is the source of truth for any agent (or human) making changes in
this repository. It is self-contained: nothing in this repository links to
files outside it, and this document does not either. Where it names the
upstream design record or the YAML package this integration was ported from,
that is a name in prose, not a path to follow — those files live in a
different repository and will not be present here.

## What this is

A Home Assistant custom integration for scheduled EV charging through an
ordinary switched plug — a smart relay/plug like a Shelly, TP-Link Kasa or
similar, not a wallbox. It charges to a target state of charge, during a
chosen window, from a pluggable **telemetry source**, and it does this
**without polling or waking the vehicle**, stopping on time even when the
telemetry feed goes quiet for hours. See `README.md` for the user-facing
description of what it does; this file is about what must not break while
changing how it does it.

## The core objective — the thing not to break

The problem this integration solves is not "turn a switch on at 22:00 and
off at 80%". It is that **state of charge is expensive to fetch and arrives
in bursts** — a vehicle API cannot be polled every few seconds without
draining the car's 12 V auxiliary battery over its cellular link, so
readings show up rarely and unpredictably. That means you cannot simply poll
until the target is reached: you have to **project** where the state of
charge will be by a given time, and you have to keep projecting *correctly*
when the feed goes silent for hours. Every piece of this codebase that looks
more complicated than it "should" be — the anchor/correction dance in
`session.py`, the dual reading/session projection in `logic.py`, the
one-directional rate model — exists to serve that single property. See
`README.md`'s "Why not just poll the car?" and "Telemetry sources" sections,
which argue this at more length.

## Provenance, and the two failure directions

This integration is a port of a ~2,150-line hand-written Home Assistant YAML
package. Its full requirements (`FR-*`), design decisions (`D1`–`D10`) and
regression scenarios (`R1`–`R14`) are recorded in
`docs/ev-charging-requirements.md`, **in the upstream repository this was
ported from** — that document does not travel with this repository, and
nothing here links to it. Docstrings throughout this codebase cite those
decision/scenario IDs (e.g. "D8", "R6") as a compact way to say *why*, without
reproducing the record. Keep that citation habit; it is the mechanism that
keeps the rationale attached to the code once the design record is out of
reach.

The judgement call behind almost every default in this repository comes down
to one asymmetry, stated here because it should be legible without the design
record:

> **Stopping a charge short of the requested target is a user-visible
> failure. Overshooting the target slightly is not. Waking the vehicle costs
> 12 V battery charge; failing to wake it when nothing was actually wrong
> costs nothing but a slightly staler reading.**

When a change has to pick a direction under uncertainty, pick the side of
that asymmetry the existing code picks — it is not arbitrary.

## Repository map

| Path | Owns | HA/third-party imports |
|---|---|---|
| `custom_components/ev_plug_charging/models.py` | Frozen dataclasses: `SessionState`, `Inputs`, `Decision`, `TelemetrySnapshot`, etc. | None |
| `custom_components/ev_plug_charging/logic.py` | The reducer: `reduce(prev_state, inputs) -> (new_state, Decision)`. The decision core. | None |
| `custom_components/ev_plug_charging/session.py` | New-session detection and the matched-pair anchor capture/correction. | None |
| `custom_components/ev_plug_charging/rate_model.py` | The learned minutes-per-percent rate, one-directionally safe. | None |
| `custom_components/ev_plug_charging/source.py` | `TelemetrySnapshot`, and the shared freshness clock (`derive_freshness`) every source delegates to. | None |
| `custom_components/ev_plug_charging/store.py` | Persisted state across restarts (`Store`-backed, but the persistence shape itself is plain data). | None |
| `custom_components/ev_plug_charging/sources/` | `TelemetrySource` ABC (`base.py`), the registry (`__init__.py`), and each concrete source (`psacc.py`). | Deferred (see below) |
| `custom_components/ev_plug_charging/coordinator.py` | `DataUpdateCoordinator` subclass: gathers inputs, calls `reduce()` once per tick, actuates, persists, dispatches events. The funnel — not where decisions are made. | Yes |
| `custom_components/ev_plug_charging/config_flow.py` | Setup wizard (source → source settings → plug → advanced) and the options flow. | Yes |
| `custom_components/ev_plug_charging/{sensor,binary_sensor,switch,number,select,time,button}.py` | Entity platforms. Thin: format `Decision`/coordinator state for display, or push a setting change via `request_settings_update()`. | Yes |
| `custom_components/ev_plug_charging/{notify,repairs,diagnostics,services}.py` | Event dispatch, Repairs issues, the diagnostics download, the three services. | Yes |
| `custom_components/ev_plug_charging/__init__.py` | `async_setup_entry`, `async_migrate_entry`, platform wiring. | Deferred (see below) |
| `tests/test_scenarios.py` | R1–R14: the regression-scenario acceptance gate. | None |
| `tests/test_*.py` (most others) | Unit tests for the pure-core modules above. | None |
| `tests/test_integration_setup.py` | Config flow, coordinator, entity platforms against real Home Assistant. | Yes (`pytest-homeassistant-custom-component`) |
| `tests/test_dashboard_examples.py` | Parses `dashboard*.yaml` and the platform modules; fails if a dashboard entity ID doesn't match an entity this integration actually creates. | None |
| `dashboard.yaml`, `dashboard-mushroom.yaml` | Copy-paste starter dashboards. | — |

**"None" above is a tested property, not a description.** `models.py`,
`logic.py`, `session.py`, `rate_model.py`, `source.py` and `store.py` must
import nothing from `homeassistant` and nothing third-party (no `aiohttp`,
etc.). This is what lets the decision core be unit-tested with plain
`pytest`, with Home Assistant not even installed. `sources/psacc.py` and
`__init__.py` need HA/`aiohttp` types, but defer those imports into function
bodies (with `TYPE_CHECKING` for annotations) rather than importing them at
module scope, for the same reason — see `sources/psacc.py`'s `_get_json()`
and `__init__.py`.

## The rules that hold everywhere

- The pure-core zero-import property above. Don't "simplify" a deferred
  import to the top of a core or source-registry module.
- PSACC (or any polling source) never drops the equivalent of `?from_cache=1`
  from a routine read. A routine read must never itself be able to wake the
  vehicle.
- The one deliberate wakeup is spent **at most once per session**, and only
  when all of its gates pass (currently: the feed has been silent past a
  multiple of the expected reporting gap, a session is actually in progress,
  and the source is reachable). Don't add a second path that can also spend
  it.
- **Fail closed, but never fail silent.** An untrustworthy or missing SoC
  reading must never be treated as "good enough to stop on" — but the user
  must always be told why nothing happened.
- **Never gate an actuation decision on a `now()`-derived template/computed
  sensor.** Two code paths computing "are we in the window right now"
  independently (the tick vs. a boundary callback) can momentarily disagree;
  the decision always re-derives the window fresh from timestamps inside
  `reduce()`, never from a cached display sensor.
- The learned charging rate may only ever push the projected stop time
  **later**, never earlier: `effective_rate = max(learned, seed)`. A
  fast-biased learned rate that isn't clamped this way stops a charge short
  of what was asked — this happened once during the port (a session stopped
  26 points short of target) and is exactly the failure mode this rule
  exists to prevent.
- `tests/test_scenarios.py` (R1–R14) is the acceptance gate for any change to
  `logic.py`, `session.py`, or `rate_model.py`. It is not a slow or optional
  suite — every scenario in it is a real incident that already happened
  once. A scenario is never edited to make a change pass; if a scenario is
  genuinely wrong, that's a conversation, not a diff.
- **This repository is self-contained.** No file here links to a path
  outside `ev-plug-charging/`. Cite the source YAML package and the design
  record by name, in prose (`packages/ev_charging.yaml:878-882`,
  `docs/ev-charging-requirements.md D3, D4`), the way the existing code
  already does — never as a markdown link or an `../` relative path.

## Where to go next

- **`.agents/skills/architecture/SKILL.md`** — before changing `logic.py`,
  `session.py`, `rate_model.py`, or the coordinator; the layer boundaries and
  why the reducer's priority ladder is ordered the way it is.
- **`.agents/skills/testing/SKILL.md`** — before adding or changing any test,
  or before claiming a change is verified.
- **`.agents/skills/adding-a-telemetry-source/SKILL.md`** — before adding
  support for a new vehicle/telemetry provider.
- **`.agents/skills/home-assistant-conventions/SKILL.md`** — before touching
  any entity platform, the config flow, diagnostics, services, or the
  manifest.

## House style

Docstrings and comments explain *why*, not just *what*, and cite the design
decision or regression scenario they encode when one exists (`D4`, `R8`,
`packages/ev_charging.yaml:878-882`). This is deliberate and load-bearing —
once the design record is out of reach (as it is for anyone working only in
this extracted repository), those citations are the only trace left of *why*
a piece of logic looks the way it does. Keep writing them that way.
