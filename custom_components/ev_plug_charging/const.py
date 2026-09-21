"""Constants for the EV Plug Charging integration.

Kept deliberately brand-neutral: nothing here names a car manufacturer.
"PSA Car Controller" (PSACC) appears only as one labelled *source type* in
the config flow, because that is the API this integration speaks -- not
because the logic assumes any particular vehicle.
"""
from __future__ import annotations

from datetime import time

DOMAIN = "ev_plug_charging"
PLATFORMS = [
    "select",
    "switch",
    "number",
    "time",
    "sensor",
    "binary_sensor",
    "button",
]

# v2 added CONF_SOURCE_TYPE; entries created before that are all PSACC,
# and async_migrate_entry backfills them.
# v3 replaced CONF_NOTIFY_SERVICE (one scalar target) with CONF_NOTIFY_TARGETS
# (a list) -- async_migrate_entry wraps the old value into a one-element list.
# v4 replaced CONF_CHARGE_POWER_KW (a kW figure nobody actually knows) with
# CONF_CHARGE_CURRENT_A (amps, what's printed on the EVSE/granny cable), and
# hid CONF_CHARGE_EFFICIENCY behind CONF_EFFICIENCY_PRIOR -- no longer asked
# for, since nobody knows it either. async_migrate_entry converts the old kW
# figure to the equivalent amps at 230V and carries the old efficiency value
# forward unchanged as the new prior.
CONFIG_VERSION = 4

# --- Telemetry sources -------------------------------------------------------
# The state-of-charge provider is pluggable: `sources/` holds one module per
# source type, all behind sources.base.TelemetrySource. PSACC is the only one
# implemented today, but nothing outside sources/ knows that -- the
# coordinator, and all of logic.py/session.py/rate_model.py, only ever see a
# normalised TelemetrySnapshot. See sources/__init__.py for how to add one.
CONF_SOURCE_TYPE = "source_type"
SOURCE_TYPE_PSACC = "psacc"

# --- Config flow: PSACC source step -----------------------------------------
CONF_PSACC_URL = "psacc_url"
CONF_VIN = "vin"

DEFAULT_PSACC_URL = "http://homeassistant.local:5000"

# The two charging-status strings used to be config-flow fields
# (charging_state_string / charge_finished_state_string), on the (wrong)
# assumption that different PSACC deployments might spell them differently.
# They don't: Stellantis' Connected Car API defines charging.status as a
# closed, Swagger-generated enum with exactly five values, identical for
# every vehicle brand and every deployment. Now hardcoded in
# sources/psacc.py, the one file that is allowed to know this.

# --- Config flow: actuator step (source-agnostic) ---------------------------
CONF_PLUG_SWITCH = "plug_switch_entity_id"
CONF_PLUG_POWER_SENSOR = "plug_power_sensor_entity_id"

# --- Config flow: advanced step ----------------------------------------------
# Required (not Optional) as of v4: it is the one thing that makes the
# efficiency prior calibratable, and asking for it up front is cheaper than
# discovering its absence later. An entry that predates this still loads
# without one -- see coordinator.py.
CONF_PLUG_ENERGY_SENSOR = "plug_energy_sensor_entity_id"
CONF_PLUG_TEMP_SENSOR = "plug_temperature_sensor_entity_id"
CONF_TEMP_LIMIT = "temperature_limit_c"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
# v4-retired: a kW figure nobody actually knows. Read only by
# async_migrate_entry now -- nothing else should reference this key.
CONF_CHARGE_POWER_KW = "charge_power_kw"
# v4+: amps, what's printed on the EVSE or granny cable. seed_rate_from_amps()
# (rate_model.py) converts this to the same effective kW CONF_CHARGE_POWER_KW
# used to supply, at a fixed 230V (the socket's own power sensor supersedes
# this arithmetic once real telemetry is available -- see coordinator.py).
CONF_CHARGE_CURRENT_A = "charge_current_a"
# v4-retired: nobody knows their charge efficiency either, and unlike the
# amps it was never something a user could look up and enter honestly. Read
# only by async_migrate_entry now.
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
# v4+: replaces CONF_CHARGE_EFFICIENCY as a fixed, hidden prior -- not asked
# for in any form. Self-calibration against measured telemetry is future
# work; until then this is just the seed's efficiency factor under another
# name.
CONF_EFFICIENCY_PRIOR = "efficiency_prior"
CONF_POLL_INTERVAL = "poll_interval_seconds"

DEFAULT_TEMP_LIMIT_C = 65
DEFAULT_BATTERY_CAPACITY_KWH = 50.0
DEFAULT_CHARGE_POWER_KW = 1.84
DEFAULT_CHARGE_CURRENT_A = 8.0
DEFAULT_CHARGE_EFFICIENCY = 0.82
# Deliberately LOWER than DEFAULT_CHARGE_EFFICIENCY (0.82) was -- this is
# now safe to ship pessimistic, because the self-calibration in
# rate_model.py exists to correct it back up (bounded by EFFICIENCY_MAX_GAIN)
# for anyone whose real setup beats it. A migrated entry is NOT affected:
# async_migrate_entry carries its OLD tuned 0.82 forward into
# CONF_EFFICIENCY_PRIOR unchanged -- this constant is the fresh-install
# fallback only.
DEFAULT_EFFICIENCY_PRIOR = 0.75
SUPPLY_VOLTAGE_V = 230.0
DEFAULT_POLL_INTERVAL_SECONDS = 120
MIN_POLL_INTERVAL_SECONDS = 60
MAX_POLL_INTERVAL_SECONDS = 600

# --- Options flow -------------------------------------------------------------
# Legacy (pre-v3): a single scalar target, e.g. "notify.mobile_app_phone" or
# "mobile_app_phone". Read only by async_migrate_entry now -- nothing else
# should reference this key.
CONF_NOTIFY_SERVICE = "notify_service"
# v3+: a list of targets, each either a legacy notify.<service> name or a
# notify.* entity id. See notify.py for why both forms have to be supported:
# only the legacy form can carry the critical-alert payload.
CONF_NOTIFY_TARGETS = "notify_targets"
CONF_MUTED_EVENTS = "muted_events"
CONF_RESCUE_REFRESH_ENABLED = "rescue_refresh_enabled"

# --- Runtime-tunable defaults (exposed as entities, not config) -------------
DEFAULT_TARGET_SOC = 80
DEFAULT_MIN_SOC_OVERRIDE = 15
DEFAULT_EXPECTED_GAP_MINUTES = 40
DEFAULT_POWER_THRESHOLD_W = 50
DEFAULT_WINDOW_START: time = time(23, 0)
DEFAULT_WINDOW_END: time = time(7, 0)
# Daily wakeup. The rescue refresh only runs DURING a session, so between
# sessions the feed can go stale for as long as the gap from one session's
# end to the next window's open -- which makes the first projection of the
# night the worst one, exactly when it matters most. Off by default: unlike
# the rescue refresh, this is a second, independent 12V budget line, and
# the user opts into it.
DEFAULT_DAILY_WAKEUP_ENABLED = False
DEFAULT_DAILY_WAKEUP_TIME: time = time(6, 0)

# --- Timing constants --------------------------------------------------------
# Session-cap margin over the (possibly learned) rate. Measured: at 1.0x the
# cap landed +2/+8/+19 min from the real crossing on three logged healthy
# sessions; at 1.15x the slack is +56/+75/+41 min. 1.0x is too tight to
# absorb a slow night without cutting the charge short.
SESSION_CAP_MARGIN = 1.15

# Window-start restart settle wait: source sensors may not have polled yet
# right after a restart that lands inside the window.
WINDOW_START_GRACE_SECONDS = 180

# Dwell/debounce durations. Each exists because the underlying signal is
# noisy on a timescale shorter than this and acting on the noise is worse
# than acting late.
CHARGE_STARTED_NOTIFY_DELAY_SECONDS = 120
TIMED_START_DWELL_SECONDS = 30
POWER_DROP_COMPLETE_DWELL_SECONDS = 300
BYPASS_DEBOUNCE_SECONDS = 300
EVSE_NO_POWER_DWELL_SECONDS = 300

# How long after WE command the plug off we still recognise the observed
# off as ours rather than a human's. The plug's state change normally
# arrives within a second (a state listener), so this is generous; it is
# bounded at all only so a command that silently failed can't make us
# ignore a real human override forever.
OWN_OFF_OBSERVATION_GRACE_SECONDS = 300

# Rescue wakeup: only past 2x the expected reporting gap.
RESCUE_WAKEUP_GAP_MULTIPLE = 2.0

# Overheat cutoff hysteresis, to avoid oscillating re-notify once cut.
OVERHEAT_HYSTERESIS_C = 5.0

# Rate-model guardrails -- see rate_model.py for what each one rules out.
RATE_MODEL_MIN_SAMPLES = 3
RATE_MODEL_SAMPLE_WINDOW = 5
RATE_MODEL_MIN_SESSION_MINUTES = 45
RATE_MODEL_MIN_SOC_GAIN = 10.0
RATE_MODEL_CLAMP_LOW = 0.85
RATE_MODEL_CLAMP_HIGH = 2.0

# Efficiency self-calibration -- see rate_model.py's module docstring for
# the full safety argument. Mirrors the rate-model constants above in
# shape (same MIN_SAMPLES/SAMPLE_WINDOW pattern), deliberately: two
# independent calibrations should not each invent their own vocabulary
# for "how many samples before this is trusted".
EFFICIENCY_MIN_SAMPLES = 3
EFFICIENCY_SAMPLE_WINDOW = 5
# A measurement outside this band is treated as a bad reading (a
# mis-scaled sensor, a wrong capacity), not a real efficiency -- rejected
# outright, never clamped into range. 0.95 is already an implausibly good
# AC->battery conversion for a granny-cable/EVSE setup.
EFFICIENCY_MIN_PLAUSIBLE = 0.50
EFFICIENCY_MAX_PLAUSIBLE = 0.95
# Applied to the aggregated (median) measurement before it's used, on top
# of -- not instead of -- EFFICIENCY_MAX_GAIN below. Two independent
# margins, not one: this one shrinks the INPUT to the seed calculation,
# MAX_GAIN bounds its OUTPUT.
EFFICIENCY_DERATE = 0.95
# The one hard safety bound: the calibrated effective power (measured
# power x calibrated efficiency) may never exceed the AS-CONFIGURED
# effective power (configured amps x the prior) by more than this factor.
# So the stop can never land more than 1 - 1/1.20 = 16.7% earlier than
# what the user actually entered, no matter what the telemetry claims.
EFFICIENCY_MAX_GAIN = 1.20
# Below this paired energy delta, sensor quantisation dominates the
# measurement -- same reasoning as RATE_MODEL_MIN_SOC_GAIN, for the
# denominator instead of the numerator.
EFFICIENCY_MIN_ENERGY_KWH = 1.0

# Measured AC power (superseding the configured amps once available) --
# see coordinator.py's per-tick sampling and rate_model.measured_ac_power_p90.
# A p90 across many ticks, not an instant or a mean: resistant to a single
# noisy reading without being dragged down by the CV taper the SoC gate
# below already excludes.
MEASURED_POWER_MIN_SAMPLES = 5
MEASURED_POWER_SAMPLE_CAP = 200
# "Comfortably below target" -- excludes the constant-voltage taper, where
# power tapers off for reasons that have nothing to do with the configured
# current and would otherwise bias the estimate low.
MEASURED_POWER_TAPER_MARGIN_SOC = 5.0
# Relative disagreement between measured and configured power that's
# worth telling the user about (e.g. "you configured 8A, the plug reports
# ~6.1A equivalent") rather than just quietly correcting for.
MEASURED_POWER_DISAGREEMENT_THRESHOLD = 0.25

# 12V auxiliary-battery health -- see aux_battery.py for the bands.
AUX_BATTERY_HEALTHY_THRESHOLD = 70.0
AUX_BATTERY_LOW_THRESHOLD = 50.0
AUX_BATTERY_CRITICAL_THRESHOLD = 30.0
AUX_BATTERY_LOW_DWELL_SECONDS = 6 * 3600  # so one cold morning isn't an alert
AUX_BATTERY_ROLLING_DAYS = 7
AUX_BATTERY_BASELINE_DAYS = 30
AUX_BATTERY_SAMPLE_WINDOW_DAYS = 30  # ring-buffer cap -- one entry per day

# --- HA events fired on the event bus ----------------------------------------
EVENT_CHARGE_STARTED = f"{DOMAIN}_charge_started"
EVENT_CHARGE_NOT_STARTED = f"{DOMAIN}_charge_not_started"
EVENT_CHARGE_COMPLETE = f"{DOMAIN}_charge_complete"
EVENT_WINDOW_SHORTFALL = f"{DOMAIN}_window_shortfall"
EVENT_SOC_STALE = f"{DOMAIN}_soc_stale"
EVENT_REFRESH_ATTEMPTED = f"{DOMAIN}_refresh_attempted"
EVENT_BYPASS_DETECTED = f"{DOMAIN}_bypass_detected"
EVENT_OVERHEAT_CUTOFF = f"{DOMAIN}_overheat_cutoff"
EVENT_EVSE_NO_POWER = f"{DOMAIN}_evse_no_power"
EVENT_SOC_FULL = f"{DOMAIN}_soc_full"
EVENT_RESTART_RECONCILED_OFF = f"{DOMAIN}_restart_reconciled_off"
EVENT_AUX_BATTERY_LOW = f"{DOMAIN}_aux_battery_low"
EVENT_AUX_BATTERY_CRITICAL = f"{DOMAIN}_aux_battery_critical"

# Fired around every plug actuation, carrying the SAME Context as the
# switch.turn_on/turn_off call it brackets -- see coordinator.py's
# _act_on_decision and notify.py's async_fire_plug_actuation. This is an
# audit trail, not a reportable condition: it is fired directly, never
# through async_dispatch_events, so CONF_MUTED_EVENTS does not apply to it
# and it never triggers a push. Its only job is to give Home Assistant's
# logbook something to attribute the plug's state change to.
EVENT_PLUG_COMMANDED = f"{DOMAIN}_plug_commanded"

# Events that raise a self-clearing Repairs issue in addition to firing.
PERSISTENT_EVENTS = frozenset(
    {
        EVENT_OVERHEAT_CUTOFF,
        EVENT_BYPASS_DETECTED,
        EVENT_EVSE_NO_POWER,
        EVENT_CHARGE_NOT_STARTED,
        EVENT_RESTART_RECONCILED_OFF,
        EVENT_AUX_BATTERY_LOW,
        EVENT_AUX_BATTERY_CRITICAL,
    }
)

# --- Services ------------------------------------------------------------
SERVICE_MARK_COMPLETION_NOTIFIED = "mark_completion_notified"
SERVICE_REFRESH_SOURCE = "refresh_source"
SERVICE_RESET_RATE_LEARNING = "reset_rate_learning"
SERVICE_VEHICLE_COMMAND = "vehicle_command"

# --- Abstract vehicle-command API (sources/base.py's TelemetrySource.
# supported_commands / async_vehicle_command) --------------------------------
# A CAPABILITY vocabulary, never a brand vocabulary -- see AGENTS.md's
# self-containment rule and const.py's own module docstring. These are not
# features of THIS integration; they are a transport this integration
# happens to already own (the same authenticated, timed, error-handled
# call every routine poll and the rescue wakeup already use). No entity,
# no options toggle, no default behaviour is built on top of them here --
# see sources/psacc.py for the one implementation and services.py for the
# one service (ev_plug_charging.vehicle_command) that exposes them.
VEHICLE_COMMAND_WAKE = "wake"
VEHICLE_COMMAND_LOCK = "lock"
VEHICLE_COMMAND_UNLOCK = "unlock"
VEHICLE_COMMAND_HORN = "horn"
VEHICLE_COMMAND_FLASH_LIGHTS = "flash_lights"
VEHICLE_COMMAND_PRECONDITION_START = "precondition_start"
VEHICLE_COMMAND_PRECONDITION_STOP = "precondition_stop"
VEHICLE_COMMAND_CHARGE_START = "charge_start"
VEHICLE_COMMAND_CHARGE_STOP = "charge_stop"

VEHICLE_COMMANDS = frozenset(
    {
        VEHICLE_COMMAND_WAKE,
        VEHICLE_COMMAND_LOCK,
        VEHICLE_COMMAND_UNLOCK,
        VEHICLE_COMMAND_HORN,
        VEHICLE_COMMAND_FLASH_LIGHTS,
        VEHICLE_COMMAND_PRECONDITION_START,
        VEHICLE_COMMAND_PRECONDITION_STOP,
        VEHICLE_COMMAND_CHARGE_START,
        VEHICLE_COMMAND_CHARGE_STOP,
    }
)

# --- Storage -------------------------------------------------------------
STORE_VERSION = 1
STORE_KEY_TEMPLATE = f"{DOMAIN}.{{entry_id}}"
