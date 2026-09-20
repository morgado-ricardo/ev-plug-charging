# AGENTS.md — EV Plug Charging

Source of truth for any agent or contributor changing this repository.
`README.md` says what the integration does; this says what must not break
while changing how it does it.

## The core objective — the thing not to break

The problem is not "turn a switch on at 22:00 and off at 80%". It is that
**state of charge is expensive to fetch and arrives in bursts** — most
vehicle APIs wake the car to answer, draining its 12 V battery, so readings
land rarely and unpredictably.

So you cannot poll until the target is reached. You have to **project**
where the state of charge will be at a given moment, and keep projecting
*correctly* through hours of silence.

Every piece of this codebase that looks more complicated than it "should"
be — the anchor/correction dance in `session.py`, the dual
reading/session projection in `logic.py`, the one-directional rate model —
exists to serve that one property.

## The asymmetry behind every default

> **Stopping a charge short of the requested target is a user-visible
> failure. Overshooting slightly is not. Waking the vehicle costs 12 V
> battery; not waking it costs nothing but a staler reading.**

When a change has to pick a direction under uncertainty, pick the side the
existing code picks. It is not arbitrary.

## Repository map

| Path | Owns | HA/third-party imports |
|---|---|---|
| `custom_components/ev_plug_charging/models.py` | Frozen dataclasses: `SessionState`, `Inputs`, `Decision`, `TelemetrySnapshot`, etc. | None |
| `custom_components/ev_plug_charging/logic.py` | The reducer: `reduce(prev_state, inputs) -> (new_state, Decision)`. The decision core. | None |
| `custom_components/ev_plug_charging/session.py` | New-session detection and the matched-pair anchor capture/correction. | None |
| `custom_components/ev_plug_charging/rate_model.py` | The learned minutes-per-percent rate, one-directionally safe. | None |
| `custom_components/ev_plug_charging/source.py` | `TelemetrySnapshot`, and the shared freshness clock (`derive_freshness`) every source delegates to. | None |
| `custom_components/ev_plug_charging/store.py` | Persisted state across restarts (`Store`-backed, but the shape itself is plain data). | None |
| `custom_components/ev_plug_charging/aux_battery.py` | The 12 V resting-sample ring buffer and health bands. | None |
| `custom_components/ev_plug_charging/sources/` | `TelemetrySource` ABC (`base.py`), the registry (`__init__.py`), each concrete source (`psacc.py`). | Deferred |
| `custom_components/ev_plug_charging/coordinator.py` | `DataUpdateCoordinator` subclass: gathers inputs, calls `reduce()` once per tick, actuates, persists, dispatches. A funnel — not where decisions are made. | Yes |
| `custom_components/ev_plug_charging/config_flow.py` | Setup wizard (source → source settings → plug → advanced) and options flow. | Yes |
| `custom_components/ev_plug_charging/{sensor,binary_sensor,switch,number,select,time,button}.py` | Entity platforms. Thin: format `Decision`/coordinator state for display, or push a setting via `request_settings_update()`. | Yes |
| `custom_components/ev_plug_charging/{notify,repairs,diagnostics,services}.py` | Event dispatch, Repairs, the diagnostics download, the services. | Yes |
| `custom_components/ev_plug_charging/logbook.py` | Describes `EVENT_PLUG_COMMANDED` for Home Assistant's logbook -- an integration platform, discovered via `manifest.json`'s `after_dependencies`, not an entity platform. | Yes |
| `custom_components/ev_plug_charging/__init__.py` | `async_setup_entry`, `async_migrate_entry`, platform wiring. | Deferred |
| `tests/test_scenarios.py` | R1–R14: the regression acceptance gate. | None |
| `tests/test_*.py` (most others) | Unit tests for the pure core. | None |
| `tests/test_integration_setup.py` | Config flow, coordinator, entity platforms against real Home Assistant. | Yes |
| `tests/test_dashboard_examples.py` | Parses `dashboard*.yaml` and the platform modules; fails if a dashboard entity ID doesn't match an entity this integration actually creates. | None |

**"None" above is a tested property, not a description.** `models.py`,
`logic.py`, `session.py`, `rate_model.py`, `source.py`, `store.py` and
`aux_battery.py` must import nothing from `homeassistant` and nothing
third-party. That is what lets the decision core be unit-tested with Home
Assistant not even installed. `sources/psacc.py` and `__init__.py` need
HA/`aiohttp` types but defer those imports into function bodies (with
`TYPE_CHECKING` for annotations) for the same reason.

## The rules that hold everywhere

- **The pure-core zero-import property above.** Don't "tidy" a deferred
  import up to module scope.
- **A routine read must never be able to wake the vehicle.** For PSACC that
  means `?from_cache=1` is never dropped from a poll.
- **The deliberate wakeup is spent at most once per session**, and only when
  every gate passes: the feed silent past a multiple of the expected
  reporting gap, a session actually in progress, and the source reachable.
  Don't add a second path that can also spend it.
- **Fail closed, but never fail silent.** A missing or untrustworthy reading
  must never be treated as good enough to stop on — but the user must always
  be told why nothing happened.
- **Never gate an actuation on a `now()`-derived display sensor.** Two code
  paths computing "are we in the window" independently (the tick vs. a
  boundary callback) can momentarily disagree. `reduce()` always re-derives
  the window fresh from timestamps.
- **The learned rate may only push the stop later, never earlier:**
  `effective_rate = max(learned, seed)`. An unclamped fast-biased rate stops
  a charge short of what was asked. This has happened — a session stopped 26
  points short of target — and the clamp is why it can't again.
- **`tests/test_scenarios.py` (R1–R14) is the acceptance gate** for any
  change to `logic.py`, `session.py` or `rate_model.py`. Every scenario in
  it is a real failure that already happened once. A scenario is never
  edited to make a change pass; if one is genuinely wrong, that's a
  conversation, not a diff.

## House style

Docstrings and comments explain *why*, not just *what*, and name the
regression scenario they encode when one exists (`R6`, `R11` — these resolve
to real tests in `tests/test_scenarios.py`). That habit is what keeps the
rationale attached to the code.

Cite only things that exist in this repository. A reference to a file,
document or section a reader here cannot open is worse than no reference at
all: it looks like provenance and delivers nothing. If a piece of reasoning
is worth keeping, write the reasoning down — don't point at something that
isn't here.

## Where to go next

- **`.agents/skills/architecture/SKILL.md`** — before changing `logic.py`,
  `session.py`, `rate_model.py` or the coordinator.
- **`.agents/skills/testing/SKILL.md`** — before adding or changing a test,
  or before claiming a change is verified.
- **`.agents/skills/adding-a-telemetry-source/SKILL.md`** — before adding a
  new vehicle or telemetry provider.
- **`.agents/skills/home-assistant-conventions/SKILL.md`** — before touching
  an entity platform, the config flow, diagnostics, services or the
  manifest.
