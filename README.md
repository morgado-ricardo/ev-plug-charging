# EV Plug Charging

**Stop your EV charging at 80% using an ordinary smart plug.**

No wallbox, no OCPP, no charger integration. A cheap smart plug — Shelly,
TP-Link Kasa, anything exposing a `switch` and a power `sensor` — sits
between the wall and your granny cable, and this integration decides when to
switch it off.

It charges to a target state of charge, inside a window you choose, and
stops on time **without polling or waking the vehicle**.

## The problem this actually solves

A smart plug can cut power. It cannot tell you the battery is at 80%.

And you cannot simply ask the car, because asking costs something. Every
remote query to most vehicle APIs wakes the car over its cellular link and
drains the 12 V auxiliary battery — and a flat 12 V battery immobilises the
car completely. So state of charge arrives **rarely, in bursts, and
unpredictably**: you might get a reading at 22:04, then nothing until 01:30.

Polling until you see 80% is therefore not an option. Neither is a plain
timer: charge for "about four hours" and you will overshoot on a warm night
and stop at 61% on a cold one.

**So this integration projects.** It takes the last real reading, anchors it
to a known moment, and extrapolates forward from measured charging rate —
continuously, correctly, through hours of silence — then cuts power when the
*projected* state of charge reaches your target. When a fresh reading lands,
it re-anchors and carries on.

That projection is the whole point of this integration. Everything else —
the telemetry source, the entities, the notifications — exists to feed it or
report on it.

It also stops on a **time** limit, for when you have no telemetry at all, or
the plug is occasionally used by a different car.

## Telemetry is an input, not the product

State of charge comes from a pluggable **telemetry source**. Today there is
one: [PSA Car Controller](https://github.com/flobz/psa_car_controller)
(PSACC), which covers Peugeot, Citroën, DS, Opel/Vauxhall and the
PSA-platform Fiat/Jeep models.

PSACC is a good fit and it is what exists now — but it is not what makes
this useful. Everything above the source works on a *normalised snapshot*: a
number, a "is it charging" flag, and a timestamp. Nothing in the projection,
the stop, the staleness detection or the rate model knows or cares where
those came from.

Which means the source can be almost anything:

| Source | Status |
|---|---|
| **PSA Car Controller (PSACC)** | Supported |
| Another vehicle API (Kia/Hyundai, Tesla, …) | Not yet — the interface is ready for one |
| **You, typing in the number** | Not yet — see below |

That last one is worth stating plainly, because it shows how little this
depends on a vehicle API at all. If you tell it "the car was at 42% when I
plugged in", the projection has everything it needs: an anchor, a timestamp,
and the plug's own power reading. No cloud, no vehicle account, no 12 V
drain. That source is not implemented yet, but the architecture is built for
it and it would need no change to the decision logic.

For vehicle APIs generally, the projection matters *more* than it does with
PSACC, not less. PSACC is unusually well-behaved: reading and refreshing are
separate acts, so routine reads cost the vehicle nothing. Most APIs conflate
them. The Kia/Hyundai HA integration defaults to a 10-minute poll
specifically to limit 12 V drain, and many owners stretch it to an hour — at
which point projecting between readings is the only thing that can work.

## Installation

Via [HACS](https://hacs.xyz): add this repository as a custom repository
(category: Integration), install "EV Plug Charging", restart Home Assistant.

Manually: copy `custom_components/ev_plug_charging` into your config's
`custom_components/` directory and restart.

## Setup

**Settings → Devices & Services → Add Integration → EV Plug Charging.**

**Step 1 — telemetry source.** Where the battery level comes from. Today the
only option is PSA Car Controller.

**Step 2 — the source's settings.** For PSACC:

| Field | What it is |
|---|---|
| PSACC base URL | e.g. `http://192.168.1.10:5000` |
| Vehicle ID (VIN) | as PSACC knows it |

The connection is tested before you can continue — one real call, showing
the state of charge and status it found — so a wrong address or vehicle ID
is caught here rather than three retries into the first poll tonight.

**Step 3 — the plug:**

| Field | What it is |
|---|---|
| Plug switch | the `switch.*` that controls power to the EVSE |
| Plug power sensor | the `sensor.*` (device class `power`) reading its draw |

**Step 4 — advanced:**

| Field | Default |
|---|---|
| Plug energy sensor | *(required — see below)* |
| Plug temperature sensor | — |
| Temperature limit | 65 °C |
| Battery capacity | 50 kWh |
| Charging current | 8 A — whatever's printed on your EVSE or granny cable |
| Poll interval | 120 s |

The plug energy sensor is the only field that isn't optional here: it's
what session cost is computed from, and there's nothing sensible to default
it to. Everything else has a working default and can be left as-is.

There's no charge-efficiency field. It's the one number in this table
nobody can actually look up, so it starts from a deliberately pessimistic
internal value rather than a guess dressed up as a setting — and unlike
capacity and current, it doesn't stay fixed: see
[Efficiency self-calibration](#efficiency-self-calibration-and-measured-ac-power)
below for how it corrects itself from real sessions, and why it's still
bounded.

Capacity, current and the poll interval can be changed later from
**Options**, along with notification targets, which events to mute, and
whether the rescue refresh is allowed.

## What it creates

Settings you'd want to change from a dashboard are **entities**, not buried
in config:

| Entity | Purpose |
|---|---|
| `number.*_target_soc` | Stop charging at this %. |
| `time.*_window_start` / `*_window_end` | The charging window (can wrap midnight). |
| `select.*_charge_mode` | `Smart` (stop on state of charge) or `Timed` (stop at window end, ignore state of charge). |
| `switch.*_enabled` | Master switch. Off: observes and reports, never touches the plug. |
| `number.*_minimum_soc_for_immediate_charge` | Below this %, start now and ignore the window. |
| `switch.*_overheat_protection` | Cut the charge if the plug overheats. |
| `number.*_expected_minutes_per_1%_soc_report` | How bursty your car's reporting is. Raise it if the stale warning fires on healthy nights. |
| `number.*_delivering-power_threshold` | Watts above which the plug counts as "delivering". |
| `switch.*_daily_wakeup` | Off by default. A scheduled refresh outside any session, so the first projection of the night isn't working from a days-old reading. Only created if the source can be asked to refresh. |
| `time.*_daily_wakeup_time` | When that fires — default 06:00. Skipped automatically if a session is already running, the reading is already fresh, or the source is unreachable. |

Read-only sensors: `*_battery_soc` (last real reading), `*_projected_soc`
(**the one that gates the stop**), `*_charge_needed`,
`*_estimated_charge_time`, `*_soc_silence`, `*_minutes_per_percent` (the
learned rate), `*_charge_started_at`, `*_charge_source`,
`*_last_charge_source`, `*_days_since_charge`, `*_session_energy`,
`*_monthly_energy`, `*_lifetime_energy`.

Binary sensors: `*_soc_stale`, `*_charging_bypassed`, `*_charging_active`,
`*_plug_delivering_power`, `*_plug_overheating`, `*_in_charge_window`,
`*_source_reachable`, `*_completion_notified`.

Buttons: `*_refresh_source`, `*_reset_rate_learning`.

If your source exposes the vehicle's 12 V battery (PSACC does), four more
sensors and a binary sensor appear — see
[12 V auxiliary battery](#12-v-auxiliary-battery).

> **`binary_sensor.*_in_charge_window` is display-only.** Never gate an
> automation on it. The clock tick and the window-boundary callback are
> different code paths that can momentarily disagree about a boundary
> instant, so the real decision always re-derives the window fresh. Use the
> [events](#events) instead.

## A dashboard to start from

Two ready-made dashboards ship with the repository:

| File | Needs |
|---|---|
| [`dashboard.yaml`](dashboard.yaml) | Nothing — core Home Assistant cards only |
| [`dashboard-mushroom.yaml`](dashboard-mushroom.yaml) | [Mushroom](https://github.com/piitaya/lovelace-mushroom) from HACS. Denser, nicer on a phone |

Both show reported vs. projected state of charge side by side, why the
integration last did what it did, the schedule and target, a manual
override, plug health, energy, and the feed/rate diagnostics.

**Two steps before pasting:**

1. **Rename the device to `EV`.** Settings → Devices & Services → EV Plug
   Charging → the device → pencil → name it `EV`, and say **yes** when Home
   Assistant offers to rename the entity IDs too. That turns
   `sensor.ev_plug_charging_<your_vin>_projected_soc` into
   `sensor.ev_projected_soc`, which is what these files use. (Or skip the
   rename and find-and-replace `ev_` with your own prefix.)
2. **Replace `YOUR_PLUG`.** Four entities belong to *your* plug's
   integration, not this one — the switch and the power, energy and
   temperature sensors. The energy and temperature rows are optional; delete
   them if your plug doesn't report those.

Then: open a dashboard → pencil → ⋮ → **Raw configuration editor** → paste →
Save. That replaces the whole dashboard, so use a new empty one — or paste
just the `cards:` list into a view you already have.

## The rate model, and why it only ever slows down

Projecting needs a rate: minutes of charging per 1% of state of charge. It
starts from what your capacity/power/efficiency settings imply, and
**learns** from your own completed sessions once it has at least three.

The learned rate is only ever allowed to push the stop **later**, never
earlier — the effective rate is always `max(learned, seed)`.

The reason is asymmetry. A rate learned too optimistically stops the charge
short of what you asked for, which you find out about in the morning when
the car isn't ready. A rate biased slow just overshoots by a little. One of
those is a real failure; the other is a rounding error. The clamp makes the
safe direction the only possible one.

## Efficiency self-calibration and measured AC power

The seed itself starts from an intentionally pessimistic efficiency, not a
number you provide, and corrects itself from your own completed sessions —
the same way the learned rate does, but for a different part of the
formula:

- **Measured AC power.** Once your plug's power sensor has enough healthy
  readings from a session (never during the taper near your target), its
  own 90th-percentile draw supersedes the configured amps for that half of
  the seed. If it disagrees with the configured current by more than 25%, a
  **Repairs** entry says so — "configured 8 A, the plug reports ~6.1 A
  equivalent" — so you find out your setting is wrong instead of it being
  silently patched over.
- **Efficiency.** Each session that qualifies (the same acceptance rule the
  rate sample uses) measures efficiency from a matched pair of state-of-
  charge and energy readings, never from session totals — the car's SoC
  reports arrive in bursts, sometimes hours after a charge actually ends,
  and pairing against totals would bias the measurement whenever that
  happens. After at least three accepted sessions, the median of the recent
  ones (with a small conservative derate) replaces the starting value.

Both calibrations only ever feed the same seed the rate model already
treats as a floor, and that seed carries its own bound: the calibrated
effective power can never exceed the as-configured effective power (your
amps × the starting efficiency) by more than 20%. So however optimistic the
telemetry gets, the stop can never land more than 1 − 1/1.20 ≈ 17% earlier
than what you actually configured. Moving *down* — a lower calibrated
efficiency, a slower projection — has no such limit, for the same reason the
rate model's own clamp is one-directional: slower is always safe.

Because state-of-charge reporting is sparse by design, qualifying sessions
are not frequent — expect this to take weeks, not days, to move off the
starting value on a quiet feed. The diagnostics download shows the current
working efficiency, how many samples it's based on, the measured AC power,
and whether the 20% cap is currently the thing holding it back, so "nothing
has changed yet" is visibly different from "something is broken".

## 12 V auxiliary battery

Every "don't wake the car" decision here exists to protect the vehicle's
12 V battery, so it's worth being able to see whether that's working. If
your source exposes it (PSACC does), it's tracked at no extra cost — the
reading rides on the same payload every routine poll already fetches.

| Entity | What |
|---|---|
| `sensor.*_aux_battery` | The raw reading, whenever polled. |
| `sensor.*_aux_battery_resting` | The same reading, but only while the car is at rest. Charging and driving both inflate it, so only resting samples compare meaningfully day to day. |
| `sensor.*_aux_battery_7d` | Rolling mean of one resting sample per day. The health band and the alert are computed from this — the raw reading is too noisy tick to tick to gate anything on. |
| `sensor.*_aux_battery_health` | `healthy` / `watch` / `low` / `unknown`, carrying the 7-day-vs-30-day drift as an attribute. A battery slowly losing ground while the level still looks fine is the most useful thing this can tell you. |

Plus `binary_sensor.*_aux_battery_low` (7-day average under 50% for 6+
hours) and `sensor.*_days_since_charge`. A resting reading below 30% raises
a Home Assistant Repair immediately.

For PSACC specifically: its `battery.voltage` field is **not a voltage** —
it is the 12 V battery's state of charge as a percentage. There is no true
12 V voltage anywhere in that API.

Not tracked: days since driven (needs an ignition signal with no
source-agnostic equivalent) and the 400 V traction battery's state of health
(vehicle telemetry, not charging).

## Events

Every reportable condition fires `ev_plug_charging_<name>` on the event bus,
whether or not any notification target is configured:

`charge_started`, `charge_not_started`, `charge_complete`,
`window_shortfall`, `soc_stale`, `refresh_attempted`, `bypass_detected`,
`overheat_cutoff`, `evse_no_power`, `soc_full`, `restart_reconciled_off`,
`aux_battery_low`, `aux_battery_critical`.

`charge_complete` can fire from four different signals — target reached,
window closed with the plug on, a sustained power drop, or the car's own
"finished" report — whichever gets there first. A shared one-per-session
latch means you get exactly one notification no matter how many end up true.
The car's own report is what catches a charge this integration never
actuated: an EVSE plugged straight into the wall, or a public charger.

`refresh_attempted` carries a `trigger` field, `"rescue"` or `"daily"`.

Persistent conditions (overheat, bypass, EVSE-no-power, charge-not-started,
restart-reconciled, both 12 V alerts) also raise a Home Assistant **Repair**.

### Notification targets

**Options → Notification targets** takes a list, reconfigurable at any
time without removing and re-adding the integration — add or remove a
phone and it takes effect on the next event, no restart needed.

Pick from a `notify.<device>` **service** (what most companion-app
integrations register) or a `notify.*` **entity** (what some platforms
register instead). The picker labels each option so the difference is
visible before you pick, because it isn't cosmetic:

| | Can carry `title`/`message` | Can carry the critical-alert channel |
|---|---|---|
| `notify.<device>` service | ✅ | ✅ |
| `notify.*` entity | ✅ | ❌ |

A notify **entity**'s `send_message` action only accepts `message` and
`title` — Home Assistant's own schema for it has no `data` field at all, so
passing one doesn't degrade gracefully, it fails. That means a notify
entity **cannot** carry the critical/time-sensitive channel this
integration uses for `overheat_cutoff` and `aux_battery_critical` — the
two alerts most worth breaking through a silenced phone for.

If a configured target turns out to be an entity with no matching legacy
service, the integration falls back to `send_message` automatically rather
than dropping the notification — but logs a warning once, and the two
critical events will arrive without their critical-channel data on that
target. Prefer the `notify.<device>` service for whichever phone you want
overheat and low-12V alerts to reach no matter what.

Each target fails independently: one target being unavailable (a phone off
the network, a renamed entity) never stops the others from receiving the
push.

## Services

- **`ev_plug_charging.refresh_source`** — manually request a fresh reading,
  the same act as the automatic rescue wakeup.
- **`ev_plug_charging.reset_rate_learning`** — clear the learned-rate
  samples. Use after a vehicle or capacity change.
- **`ev_plug_charging.mark_completion_notified`** — claim the
  one-per-session completion latch from outside, so an external automation
  can own the notification without this integration also sending one.
- **`ev_plug_charging.vehicle_command`** — an escape hatch, not a feature:
  no entity, no dashboard card, no toggle. It exists because the integration
  already holds an authenticated, timed, error-handled connection to your
  source, and lock/unlock/horn/lights/preconditioning are just more calls
  over it. Takes `command` (`wake`, `lock`, `unlock`, `horn`,
  `flash_lights`, `precondition_start`, `precondition_stop`, `charge_start`,
  `charge_stop` — capability names, never brand names) and optional
  `params`. Fails cleanly if your source doesn't support the command; PSACC
  supports all nine.

  Two caveats. `charge_start`/`charge_stop` are **not** wired into the
  charging decisions — calling them doesn't change what the scheduler does.
  And every service here disappears if the config entry fails to load, so
  don't make this your only way to unlock the car.

## Diagnostics

Settings → Devices & Services → EV Plug Charging → ⋮ → **Download
diagnostics** gives you the full anchor, projection and rate-model state
plus the last decision's reason.

## Attribution in the logbook

Every actuation is fired with its own Home Assistant `Context`, shared with
an `ev_plug_charging_plug_commanded` event fired immediately before the
`switch.turn_on`/`turn_off` call. Two consequences:

- Home Assistant's **Logbook** panel shows *"EV Plug Charging: turned the
  plug on (below_target)"* rather than an anonymous state change — the
  message names the [decision reason](#debugging-a-charge-that-didnt-start)
  that triggered it.
- The plug's **own** history/more-info logbook card gets the same line,
  via a `logbook_entry` event carrying the plug's `entity_id` — a
  describable domain event alone doesn't reach that view, because Home
  Assistant's logbook only looks at event types owned by the same
  integration as the entities being viewed, and the plug belongs to
  whichever integration created it, not this one.

This needs the `logbook` integration enabled (on by default). It only
covers actuations this integration makes — flipping the plug by hand, or
through an automation, is attributed to whatever did that, same as always.

## Debugging a charge that didn't start

There are no automations here, so there are no traces to step through. One
`reduce()` call per tick decides everything, and it says why.

**Start with the reason.** Developer Tools → States →
`sensor.*_projected_soc`, attribute `decision_reason`:

| `decision_reason` | Means |
|---|---|
| `below_target` | It **is** charging — the plug should be on |
| `in_window_no_action` | In the window, but something is suppressing a start |
| `outside_window_not_ours` | Outside the window. Check the window entities, and check the clock |
| `soc_untrustworthy` | No reading, or the source is unreachable. Fails closed |
| `disabled` | `switch.*_enabled` is off |
| `target_reached` | Already at or over target |
| `timed_window` | Timed mode, ignoring state of charge |

**Then turn on the per-tick log.** In `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.ev_plug_charging: debug
```

One line per tick, with everything the decision was made from:

```
tick 2026-09-19T22:45:00+01:00 | below_target -> plug on | soc=69.0 proj=69.0
target=80.0 | window 22:40:00-08:00:00 in=True | enabled=True mode=smart
plug_on=False power=1800.0W | silence=0min stale=False source=plug_idle |
manual_off_until=None
```

Read it left to right: the reason, what it did about it, the numbers behind
the target comparison, whether the window was open *at that wall-clock
time*, and the two things most likely to be suppressing a start —
`manual_off_until`, and `enabled`.

Plug actuations are also logged at INFO, so a charge starting or stopping
shows up without opting into debug.

**`manual_off_until`** is worth knowing about. If you switch off a plug this
integration turned on, it takes that as an override and stands down for the
rest of that window rather than switching it straight back on. It only does
this for an off *inside* the window, and never for an off it commanded
itself. If you see a future timestamp there and you didn't expect it, that
is why nothing is happening.

**Download diagnostics** (above) if the log doesn't answer it — that has the
anchor, the rate model and the full persisted state.

## Scope and limitations

- **One car, one plug per config entry.** Add a second entry for a second
  pairing.
- **Keep your target at or below ~80%.** The rate model is linear and is not
  valid in the constant-voltage taper above roughly that point. The
  integration refuses to learn from any session that ran above it.
- **No cost or tariff tracking.** Session, monthly and lifetime energy (kWh)
  are tracked; pair those with Home Assistant's own Energy dashboard and
  your supplier's pricing. Currencies and tariffs have nothing to do with
  deciding when to charge.
- **Monthly and lifetime energy don't survive a restart** — only the current
  session does. If you need durable cumulative figures, point a
  `utility_meter` at your plug's own energy sensor.
- **`Timed` mode has no state-of-charge awareness at all.** It exists for a
  plug that's sometimes used by a car with no telemetry.

## Development

```
pip install -r requirements-test.txt
pytest -q
ruff check custom_components/
```

The decision core (`logic.py`, `session.py`, `rate_model.py`, `source.py`,
`store.py`) has **zero Home Assistant imports** and is fully testable with
plain `pytest`, with Home Assistant not installed. That's a tested property,
not a convention.

`tests/test_scenarios.py` holds fourteen regression scenarios (R1–R14), each
one a real failure that happened once. It is the acceptance gate for any
change to the decision logic.

`tests/test_integration_setup.py` exercises the Home Assistant-facing layer
(config flow, coordinator, entity platforms) using
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).
It pulls in most of Home Assistant core, so those tests skip automatically
if it isn't installed — a green `pytest -q` without it does **not** mean the
Home Assistant layer works.

Contributing, or making a change with an agent?
**[`AGENTS.md`](AGENTS.md)** has the layer boundaries, the invariants a
change must not regress, and four topic skills under `.agents/skills/`.
