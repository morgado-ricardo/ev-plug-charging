# EV Plug Charging

A Home Assistant integration for scheduled EV charging through a switched
plug — a smart relay/plug like a Shelly, TP-Link Kasa or similar, anything
exposing a `switch` and a power `sensor`. **For people charging from an
ordinary socket, with no wallbox in the circuit.**

It charges to a target state of charge, during a chosen window, **without
polling or waking the vehicle**, and stops on time even when the telemetry
feed goes quiet for hours.

Battery level comes from a pluggable **telemetry source**. Today that means
a [PSA Car Controller](https://github.com/flobz/psa_car_controller) (PSACC)
instance — Peugeot, Citroën, DS, Opel/Vauxhall and the PSA-platform
Fiat/Jeep models — with more sources planned; see
[Telemetry sources](#telemetry-sources). Nothing in the decision logic
assumes a particular manufacturer, or even a particular kind of source.

This integration was extracted from a hand-written Home Assistant YAML
package, following the design record and porting assessment in
`docs/ev-charging-requirements.md` of the source repository. If you're
curious *why* it behaves the way it does — the incidents that shaped each
decision — that document is the fuller story; this README is the "how to use
it" version.

## Why not just poll the car?

Every remote command to a Stellantis-group vehicle wakes it over the
cellular link and drains its 12 V auxiliary battery. A flat 12 V battery
immobilises the car. This integration never polls PSACC in a way that wakes
the vehicle (`from_cache=1` on every read), and spends at most **one**
deliberate wakeup per charging session, only when the SoC feed has been
silent for twice as long as expected and only if PSACC itself is reachable.

## Installation

Via [HACS](https://hacs.xyz): add this repository as a custom repository
(category: Integration), then install "EV Plug Charging" and restart Home
Assistant.

Manually: copy `custom_components/ev_plug_charging` into your Home
Assistant config's `custom_components/` directory and restart.

## Setting it up

**Settings → Devices & Services → Add Integration → EV Plug Charging.**

**Step 1 — telemetry source.** Where the battery level comes from. Today
there is one option, **PSA Car Controller (PSACC)**; more are coming (see
[Telemetry sources](#telemetry-sources) below).

**Step 2 — the source's own settings.** For PSACC:

| Field | What it is |
|---|---|
| PSACC base URL | e.g. `http://192.168.1.10:5000` |
| Vehicle ID (VIN) | as PSACC knows it |
| Charging-state string | the value PSACC reports while charging — default `InProgress` |
| Charge-finished state string | the value PSACC reports once charging ends — default `Finished`. A separate signal from the one above: it's what makes `charge_complete` fire even for a session this integration never actuated, such as a bypass charge straight from the wall. |

The connection is tested before you can continue — one real call, showing
the SoC and status it found — so a wrong address or vehicle ID is caught
here rather than three retries into the first poll tonight.

**Step 3 — the plug** (source-agnostic):

| Field | What it is |
|---|---|
| Plug switch | the `switch.*` entity that controls power to the EVSE |
| Plug power sensor | the `sensor.*` (device class `power`) reading the plug's draw |

**Step 4 — advanced** (optional, sensible defaults):

| Field | Default |
|---|---|
| Plug energy sensor | — |
| Plug temperature sensor | — |
| Temperature limit | 65 °C |
| Battery capacity | 50 kWh |
| Charge power | 1.84 kW (a typical 8 A Schuko EVSE) |
| Charge efficiency | 0.82 |
| Poll interval | 120 s |

These four (capacity/power/efficiency, and the poll interval) can be changed
later from the integration's **Options**, along with a notify service, which
events to mute, and whether the one-per-session rescue refresh is allowed.

## What it creates

Runtime-tunable settings are **entities**, not hidden in config, because
they belong on a dashboard:

| Entity | Purpose |
|---|---|
| `select.*_charge_mode` | `Smart` (SoC-aware) or `Timed` (charge to 100%, ignore SoC — for a different car on the same plug) |
| `switch.*_enabled` | The master switch. Off: observes and reports, never actuates the plug. |
| `switch.*_overheat_protection` | Master toggle for the temperature cutoff. |
| `number.*_target_soc` | Stop charging at this %. |
| `number.*_minimum_soc_for_immediate_charge` | Start immediately, ignoring the window, below this %. |
| `number.*_expected_minutes_per_1%_soc_report` | How bursty your car's reporting is (tune from the diagnostics dump if the stale warning fires often). |
| `number.*_delivering-power_threshold` | Watts above which the plug counts as "delivering" (depends on your EVSE's current). |
| `time.*_window_start` / `*_window_end` | The charging window (can wrap midnight). |
| `switch.*_daily_wakeup` | Off by default. A scheduled refresh outside any charging session, so the first projection of the night isn't working from a feed that's been stale for days. Only created if the source can be asked for a refresh at all. |
| `time.*_daily_wakeup_time` | When the daily wakeup fires — default 06:00. Skipped automatically if a session is already active, the reading is already fresh, or the source is unreachable, so an enabled toggle never spends more than it needs to. |

And read-only sensors: `sensor.*_battery_soc`, `*_projected_soc` (what
actually gates the stop), `*_charge_needed`, `*_estimated_charge_time`,
`*_soc_silence`, `*_minutes_per_percent` (the rate model — see below),
`*_charge_started_at`, `*_charge_source`, `*_last_charge_source` (the
sticky version — still correct once a session has ended and the live
sensor has fallen back to "none"), `*_days_since_charge`, `*_session_energy`,
`*_monthly_energy`, `*_lifetime_energy`; `binary_sensor.*_soc_stale`,
`*_charging_bypassed`, `*_charging_active`, `*_plug_delivering_power`
(just the plug's own contribution to `*_charging_active` — useful when
diagnosing an EVSE that's on but drawing nothing), `*_plug_overheating`,
`*_in_charge_window` (display only — see "Never gate on this" below),
`*_source_reachable`, `*_completion_notified`; and two buttons,
`button.*_refresh_source` and `*_reset_rate_learning`.

If your source supports it (PSACC does), four more sensors and a
binary sensor track the vehicle's 12V auxiliary battery — see
[12V auxiliary battery](#12v-auxiliary-battery) below.

## Telemetry sources

| Source | Status |
|---|---|
| **PSA Car Controller (PSACC)** | Supported |
| Others | Coming — see below |

The state-of-charge provider is pluggable. Everything above the source —
the projection, the stop, the staleness detection, the rate model, every
entity — works on a *normalised snapshot*: a SoC number, a "is it
charging" flag, and a freshness timestamp. None of it knows or cares where
those came from.

That matters because the hard problem this integration solves is not
Stellantis-specific. It exists because **state of charge is expensive to
fetch and arrives in bursts**, so you cannot simply poll until the target
is reached — you have to project, and you have to keep projecting correctly
when the feed goes quiet. That premise holds for several vehicle APIs.
Kia/Hyundai is the closest parallel: its Home Assistant integration
defaults to a 10-minute poll specifically to limit 12 V drain, many owners
stretch it to 60, and Hyundai officially warns against third-party
automation for the same reason. At a 60-minute interval the projection
matters *more* than it does here, not less.

PSACC is implemented first because it is unusually well-behaved: reading
(`?from_cache=1`) and refreshing (`/wakeup`) are **separate acts**, so
routine reads cost the vehicle nothing. Most vehicle APIs conflate them,
which is exactly why the one-per-session rescue refresh is gated so
tightly.

### Adding one

It is meant to be a closed job — see `custom_components/ev_plug_charging/sources/__init__.py`:

1. Write `sources/<name>.py` with a `TelemetrySource` subclass.
2. Add a constant in `const.py` and a line to the `SOURCES` registry.
3. Add a config-flow step and its strings.

Nothing in `logic.py`, `session.py`, `rate_model.py`, `coordinator.py` or
any entity platform changes. A source that has no way to request a fresh
reading just leaves `supports_refresh` alone, and the rescue refresh
quietly never fires.

## A dashboard to start from

Two ready-made dashboards ship with this repository, so you don't have to
assemble one entity at a time:

| File | Needs |
|---|---|
| [`dashboard.yaml`](dashboard.yaml) | Nothing — core Home Assistant cards only |
| [`dashboard-mushroom.yaml`](dashboard-mushroom.yaml) | [Mushroom](https://github.com/piitaya/lovelace-mushroom) from HACS. Denser, nicer on a phone |

Both show the same things: reported vs. projected state of charge side by
side, why the integration last did what it did, the schedule and target,
a manual override, plug health, energy, and the feed/rate diagnostics.

**Two steps before pasting:**

1. **Rename the device to `EV`.** Settings → Devices & Services → EV Smart
   Charging → the device → pencil → name it `EV`, and say **yes** when Home
   Assistant offers to rename the entity IDs too. That turns
   `sensor.ev_plug_charging_<your_vin>_projected_soc` into
   `sensor.ev_projected_soc`, which is what the dashboards use. (If you're
   coming from the YAML package this was ported from, these are the same
   entity IDs it used, so the dashboards drop straight into what you have.)
2. **Replace `YOUR_PLUG`.** Four entities belong to *your* smart plug's
   integration, not this one — the switch, and the power, energy and
   temperature sensors. Find-and-replace `YOUR_PLUG` with your plug's
   entity ID. The energy and temperature rows are optional; delete them if
   your plug doesn't report those.

Then: open a dashboard → pencil (edit) → ⋮ → **Raw configuration editor** →
paste → Save. That replaces the whole dashboard's config, so use a new empty
dashboard — or paste just the `cards:` list into a view you already have.

## The rate model, and why it can only ever be conservative

The projection that decides when to stop charging needs a rate: minutes of
charging per 1% of state of charge. It starts from the number your
capacity/power/efficiency settings imply, and **learns** from your own
completed sessions once it has enough of them (three, minimum).

Critically, the learned rate is only ever allowed to make the stop happen
**later**, never earlier — the effective rate used everywhere is
`max(learned, seed)`. A rate biased fast (learned too optimistically) could
otherwise stop a charge short of what you asked for; a rate biased slow just
costs a little overshoot, which the design accepts as the safer failure
direction. See `docs/ev-charging-requirements.md` in the source repo, and
the port design plan, for the full reasoning and the incident that made this
non-negotiable.

## 12V auxiliary battery

Every "don't wake the car" decision in this integration exists to protect
the vehicle's 12V auxiliary battery — a flat one immobilises the car. Until
now the integration protected it blind, with no way to show whether that
protection was actually working. If your source supports it (PSACC does),
it now tracks it, at no extra cost: the reading rides on the same cached
payload every routine poll already fetches.

For PSACC specifically: its `battery.voltage` field is **not a voltage** —
it is the 12V battery's own state of charge, as a percentage. (An earlier
tool multiplied it by 4 to fake a plausible-looking ~12.8V reading; that was
wrong, and there is no true 12V voltage anywhere in the API.)

Four sensors, deliberately simpler than tracking every figure imaginable:

| Entity | What |
|---|---|
| `sensor.*_aux_battery` | The raw reading, whenever polled. |
| `sensor.*_aux_battery_resting` | The same reading, but only while the car is at rest — charging and driving both inflate it, so only resting samples are comparable day to day. |
| `sensor.*_aux_battery_7d` | A rolling mean of one resting sample per day. This is what the health band and the low-battery alert are actually computed from — the raw reading is too noisy tick to tick to gate anything on directly. |
| `sensor.*_aux_battery_health` | `healthy` / `watch` / `low` / `unknown`. Carries the 7-day-vs-30-day drift as an attribute — a battery that's slowly losing ground even though the level still looks fine is the single most useful thing this tracker can tell you, and it doesn't need its own entity to say it. |

Plus `binary_sensor.*_aux_battery_low` (the 7-day average held below 50%
for 6+ hours) and `sensor.*_days_since_charge`. A critically low resting
reading (below 30%) raises a Home Assistant Repair immediately — see
[Events](#events).

Not tracked: **days since driven** (would need an ignition signal this
integration doesn't have a source-agnostic way to ask for) and the 400V
traction battery's own state-of-health figures (vehicle telemetry, not
charging or 12V protection — out of scope here).

## Never gate anything on `binary_sensor.*_in_charge_window`

It is display-only. HA's clock tick and the exact window-boundary callback
are two different code paths that can momentarily disagree about whether a
boundary instant counts as "in" or "out" — the actual decision always
re-derives the window fresh, never from this sensor's cached value. If
you're building your own automation, use the events below instead of
polling this sensor.

## Events

Every reportable condition fires `ev_plug_charging_<name>` on the event bus,
regardless of whether a notify service is configured:

`charge_started`, `charge_not_started`, `charge_complete`,
`window_shortfall`, `soc_stale`, `refresh_attempted`, `bypass_detected`,
`overheat_cutoff`, `evse_no_power`, `soc_full`, `restart_reconciled_off`,
`aux_battery_low`, `aux_battery_critical`.

`charge_complete` can fire from four different signals — target reached,
window closed with the plug on, a sustained power drop, or the car's own
"finished" report — whichever gets there first; a shared one-push-per-
session latch means only one notification ever goes out no matter how many
of those conditions end up true.

`refresh_attempted` carries a `trigger` field, `"rescue"` or `"daily"`,
telling the two apart.

Persistent conditions (overheat, bypass, EVSE-no-power, charge-not-started,
restart-reconciled, both 12V battery alerts) additionally raise a Home
Assistant **Repair**.

## Services

- `ev_plug_charging.mark_completion_notified` — claims the one-push-per-session
  latch externally. See `MIGRATION.md` if you're moving from the YAML
  package this was ported from.
- `ev_plug_charging.refresh_source` — the manual equivalent of the one
  automatic rescue wakeup.
- `ev_plug_charging.reset_rate_learning` — clears the learned-rate sample
  buffer. Use this after a capacity/vehicle change.
- `ev_plug_charging.vehicle_command` — an abstract vehicle-command API, not
  a feature this integration surfaces anywhere: no entity, no dashboard
  card, no toggle to turn it on. It exists because the integration already
  owns an authenticated, timed, error-handled transport to your source
  (host, VIN, timeout policy), and lock/unlock/horn/lights/preconditioning/
  a car-side charge start-stop are just more calls over that same
  transport. Takes `command` (one of `wake`, `lock`, `unlock`, `horn`,
  `flash_lights`, `precondition_start`, `precondition_stop`,
  `charge_start`, `charge_stop` — capability names, never brand names) and
  optional `params` (e.g. `{count: 3}` for `horn`). Fails if the configured
  source doesn't support the command; PSACC supports all nine. See
  `MIGRATION.md` if you're replacing `packages/opel.yaml`'s `rest_command:`
  block with this. **`charge_start`/`charge_stop` are not wired into this
  integration's own charging decisions** — calling them doesn't change
  what the scheduler does; they're exposed only because the same PSACC
  endpoint exists and some other automation might want it.

## Diagnostics

Settings → Devices & Services → EV Plug Charging → ⋮ → **Download
diagnostics** gives you the full anchor/projection/rate-model state and the
last decision's reason — everything a template editor used to be needed for
in the original YAML package.

## Scope and limitations

- One car, one plug, per config entry. Add a second entry for a second
  car/plug pairing.
- The rate model is linear and is not valid in the constant-voltage taper
  above roughly 80% SoC. Keep your target at or below that, and the
  integration will refuse to learn from any session that ran above it.
- No cost/tariff tracking. Session, monthly and lifetime **energy** (kWh)
  are tracked; pairing that with Home Assistant's own Energy dashboard and
  your utility's pricing gets you cost without this integration needing to
  know your currency or tariff.
- `Timed` mode has no SoC awareness at all — it exists for a plug sometimes
  used by a different vehicle with no telemetry.

## Development

```
pip install -r requirements-test.txt
pytest -q
ruff check custom_components/
```

The pure decision logic (`logic.py`, `session.py`, `rate_model.py`,
`source.py`, `store.py`) has **zero Home Assistant imports** and is fully
testable with plain `pytest` — no Home Assistant installation required.
`tests/test_scenarios.py` reproduces the fourteen real-incident regression
scenarios (R1–R14) from the source repository's design record; that suite
is the acceptance gate for any change to the decision logic.

`tests/test_integration_setup.py` additionally exercises the real Home
Assistant-facing layer (config flow, coordinator, entity platforms) using
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component),
which is a heavier dependency (it pulls in most of Home Assistant core) —
those tests are skipped automatically if it isn't installed.

Making a change here? **[`AGENTS.md`](AGENTS.md)** is the source of truth
for agents (and contributors) working in this repository — the layer
boundaries, the invariants a change must not regress, and four topic skills
under `.agents/skills/`: `architecture`, `testing`,
`adding-a-telemetry-source`, and `home-assistant-conventions`.
