# Migrating from `packages/ev_charging.yaml`

This integration was ported from a hand-written YAML package (see the
source repository's `docs/ev-charging-requirements.md` for the full design
record). The YAML package is **not modified or removed** by installing this
integration — they can run side by side, which is the recommended way to
gain confidence before cutting over.

## Recommended cutover sequence

1. Install this integration alongside the YAML package.
2. Leave `switch.*_enabled` **off** for the first few nights. The
   integration will still poll PSACC, project SoC, and classify the charge
   source — compare its `sensor.*_projected_soc` against the YAML's
   `sensor.ev_projected_soc`, and its `sensor.*_charge_source` against
   `sensor.ev_charge_source`, over two or three real sessions.
3. Once the numbers agree, disarm the YAML's `input_boolean.ev_auto_window_start`
   and `input_boolean.ev_auto_target_reached` switches for one night and
   enable this integration instead. Confirm the stop lands within about 1%
   of target.
4. Only then consider removing `packages/ev_charging.yaml` — see "What
   still points at the YAML" below first.

## What still points at the YAML package

`packages/opel.yaml`'s `opel_charge_complete` automation was originally a
fifth completion path — the car reporting `Finished` — that the YAML needed
because none of `ev_charging.yaml`'s own automations could see a bypass
session (car charging straight from the wall, no Shelly involved).

**This integration now detects that signal natively**, via the
`charge_finished_state_string` PSACC setting (default `Finished`, set during
setup or in Options): a `charging → finished` edge fires
`ev_plug_charging_charge_complete` with `reason: car_confirmed`, through the
same one-push-per-session latch every other completion path shares, so it
dedupes automatically against target-reached, window-close, and the
power-drop path. `sensor.<name>_last_charge_source` is the sticky companion
`opel_charge_complete`'s message needed (`sensor.ev_last_charge_source` in
the YAML) — still correct once the live `charge_source` has fallen back to
"none".

That means `opel_charge_complete` can simply be **disabled** once you've
cut over — nothing further needs bridging. The service
(`ev_plug_charging.mark_completion_notified`) and
`binary_sensor.<name>_completion_notified` are still there if you have a
*different* external automation that wants to observe or claim the latch
for some other reason.

`opel_daily_wakeup` is the other `opel.yaml` automation worth revisiting:
this integration now has its own `switch.<name>_daily_wakeup` +
`time.<name>_daily_wakeup_time`, budget-gated (skipped while a session is
active, while the reading is already fresh, or while the source is
unreachable) in a way the plain `rest_command.opel_wakeup` call never was.
It is **off by default**, so cutting over means *enabling* it, not
disabling anything — until you do, `opel_daily_wakeup` is still the only
thing keeping the feed fresh between sessions.

The 12V health tracker (`opel_12v_critical`, `opel_12v_low`,
`opel_12v_declining_trend`, and the `sensor.opel_12v_*` / `_days_since_*`
sensors behind them) has a native equivalent too — see README's
[12V auxiliary battery](README.md#12v-auxiliary-battery) section. It's
simpler than the YAML's seven sensors (four instead, the 30-day baseline
folded into an attribute rather than its own entity) and doesn't cover
`days_since_driven`, which needs an ignition signal this integration has
no source-agnostic way to ask for. Once you've compared the two for a
week or so, `opel_12v_critical`/`opel_12v_low`/`opel_12v_declining_trend`
can be disabled; `opel_track_last_driven`/`opel_track_last_charge` should
stay, since nothing here replaces `days_since_driven`.

## Replacing `opel.yaml`'s `rest_command:` block

`packages/opel.yaml:9-46` declares nine `rest_command:` entries —
`opel_wakeup`, `opel_lock`, `opel_unlock`, `opel_horn`,
`opel_lights_flash`, `opel_charge_start`/`_stop`,
`opel_preconditioning_on`/`_off` — each redeclaring the PSACC host and VIN
in plaintext YAML. This integration's `ev_plug_charging.vehicle_command`
service (see README's [Services](README.md#services)) is the exact same
nine calls, ported byte-for-byte, through the transport this integration
already owns instead of a second, independently-configured one.

Once you've confirmed the service works (call it once for each command
you actually use, e.g. from Developer Tools → Actions), the
`rest_command:` block can be deleted and the automations/scripts/dashboard
buttons that called `rest_command.opel_*` switched to
`ev_plug_charging.vehicle_command` with the matching `command:` value
(`opel_wakeup` → `wake`, `opel_lock` → `lock`, and so on — see README for
the full list). `opel_horn`'s `{{ count | default(2) }}` and
`opel_lights_flash`'s `{{ duration | default(10) }}` become
`params: {count: ...}` / `params: {duration: ...}`.

**One real behavioural difference to know about first**: `rest_command`
has no dependency on anything besides network reachability.
`ev_plug_charging.vehicle_command` depends on this integration's config
entry being loaded — if it's ever unloaded or fails to set up, the service
disappears and any automation calling it will error, where the
`rest_command` would still have fired. If that matters for something
safety-relevant in your automations, keep the `rest_command` for that one
case rather than migrating it.

## Entity mapping

| Domain in YAML | Purpose | This integration |
|---|---|---|
| `input_select.ev_charge_mode` | Smart / Timed / **Off (manual)** | `select.*_charge_mode` (Smart / Timed only) + `switch.*_enabled` for Off |
| `input_number.ev_target_soc` | Target SoC | `number.*_target_soc` |
| `input_number.ev_min_soc_override` | Emergency threshold | `number.*_minimum_soc_for_immediate_charge` |
| `input_number.ev_soc_step_minutes` | Expected reporting gap | `number.*_expected_minutes_per_1%_soc_report` |
| `input_number.ev_shelly_temp_limit` | Overheat limit | Options flow: "Plug temperature limit" |
| `input_number.ev_charge_efficiency` | Rate model input | Options flow: "Charge efficiency" (now also self-calibrating — see README) |
| `input_number.ev_cost_per_kwh` | Tariff | **Dropped** — no cost tracking; see README |
| `input_datetime.ev_charge_start_time` / `_end_time` | Window | `time.*_window_start` / `*_window_end` |
| `input_boolean.ev_shelly_overheat_protection` | Overheat master toggle | `switch.*_overheat_protection` |
| `input_boolean.ev_auto_*` (11 switches) | Per-automation arm/disarm | **Dropped** — one `switch.*_enabled` does the job |
| `input_boolean.ev_helpers_seeded`, `ev_charge_complete_notified`, `ev_session_anchor_provisional`, `ev_soc_wakeup_attempted` | Internal bookkeeping | Persisted internally; surfaced in diagnostics, not as entities |
| `sensor.ev_projected_soc` | The control-input sensor | `sensor.*_projected_soc` |
| `sensor.ev_charging_cost_session/_monthly/_total` | Cost | **Dropped** |
| `utility_meter.ev_charge_total_energy`, `ev_energy_monthly` | Energy | `sensor.*_session_energy`, `*_monthly_energy`, `*_total_energy` |
| everything else in `sensor:`/`binary_sensor:` | Display | Equivalent `sensor.*`/`binary_sensor.*` — see README's entity table |

## Behavioural differences worth knowing about

- **The stop can re-assert.** The YAML's automations only acted on their own
  triggers, so a manual "off" during the window stuck until the next
  trigger. This integration re-evaluates continuously; if you switch the
  plug off by hand while it's mid-charge and below target, it detects that
  and will not turn it back on for the rest of that window occurrence — but
  this is new logic (not present in the YAML) built specifically to restore
  that "manual off sticks" behaviour. Report anything that feels off.
- **`Off (manual)` is gone.** Use the `enabled` switch instead. The 100%
  completion push that mode used to send still fires while disabled.
- **The rate model self-calibrates** (see README) instead of being a fixed
  number you tune by hand.
- **No cost tracking.** Pair the energy sensors with Home Assistant's Energy
  dashboard if you want cost.
