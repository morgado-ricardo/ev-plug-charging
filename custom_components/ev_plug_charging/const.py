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
CONFIG_VERSION = 2

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
CONF_CHARGING_STATE_STRING = "charging_state_string"
# The car's own terminal "charge finished" status string -- a SEPARATE
# signal from CONF_CHARGING_STATE_STRING above. It is the only completion
# path that works no matter how the car was charged (plug, EVSE straight
# into the wall, or a public charger with no telemetry of its own) --
# ported from opel_charge_complete (packages/opel.yaml:913-950), whose own
# comment calls this out explicitly.
CONF_CHARGE_FINISHED_STATE_STRING = "charge_finished_state_string"

DEFAULT_PSACC_URL = "http://homeassistant.local:5000"
DEFAULT_CHARGING_STATE_STRING = "InProgress"
DEFAULT_CHARGE_FINISHED_STATE_STRING = "Finished"

# --- Config flow: actuator step (source-agnostic) ---------------------------
CONF_PLUG_SWITCH = "plug_switch_entity_id"
CONF_PLUG_POWER_SENSOR = "plug_power_sensor_entity_id"

# --- Config flow: advanced step ----------------------------------------------
CONF_PLUG_ENERGY_SENSOR = "plug_energy_sensor_entity_id"
CONF_PLUG_TEMP_SENSOR = "plug_temperature_sensor_entity_id"
CONF_TEMP_LIMIT = "temperature_limit_c"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_CHARGE_POWER_KW = "charge_power_kw"
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
CONF_POLL_INTERVAL = "poll_interval_seconds"

DEFAULT_TEMP_LIMIT_C = 65
DEFAULT_BATTERY_CAPACITY_KWH = 50.0
DEFAULT_CHARGE_POWER_KW = 1.84
DEFAULT_CHARGE_EFFICIENCY = 0.82
DEFAULT_POLL_INTERVAL_SECONDS = 120
MIN_POLL_INTERVAL_SECONDS = 60
MAX_POLL_INTERVAL_SECONDS = 600

# --- Options flow -------------------------------------------------------------
CONF_NOTIFY_SERVICE = "notify_service"
CONF_MUTED_EVENTS = "muted_events"
CONF_RESCUE_REFRESH_ENABLED = "rescue_refresh_enabled"

# --- Runtime-tunable defaults (exposed as entities, not config) -------------
DEFAULT_TARGET_SOC = 80
DEFAULT_MIN_SOC_OVERRIDE = 15
DEFAULT_EXPECTED_GAP_MINUTES = 40
DEFAULT_POWER_THRESHOLD_W = 50
DEFAULT_WINDOW_START: time = time(23, 0)
DEFAULT_WINDOW_END: time = time(7, 0)
# Daily wakeup (D5's rescue refresh only runs DURING a session; between
# sessions the feed can go stale for as long as the gap between one
# session's end and the next window's open -- ported from
# opel_daily_wakeup, packages/opel.yaml:884-898). Off by default: unlike
# the rescue refresh, this is a second, independent budget line the user
# opts into per source.
DEFAULT_DAILY_WAKEUP_ENABLED = False
DEFAULT_DAILY_WAKEUP_TIME: time = time(6, 0)

# --- Timing constants (ported from packages/ev_charging.yaml) ---------------
# Session-cap margin over the (possibly learned) rate. Measured: at 1.0x the
# cap landed +2/+8/+19 min from the real crossing on three logged healthy
# sessions; at 1.15x the slack is +56/+75/+41 min. See docs D2.
SESSION_CAP_MARGIN = 1.15

# Window-start restart settle wait (FR-S6): REST sensors may not have polled
# yet right after a restart during the window.
WINDOW_START_GRACE_SECONDS = 180

# Dwell/debounce durations, all ported verbatim from the YAML.
CHARGE_STARTED_NOTIFY_DELAY_SECONDS = 120  # ev_charge_started_notify
TIMED_START_DWELL_SECONDS = 30  # ev_timed_charge_started_notify
POWER_DROP_COMPLETE_DWELL_SECONDS = 300  # ev_charge_power_drop_complete
BYPASS_DEBOUNCE_SECONDS = 300  # binary_sensor.ev_shelly_bypassed delay_on
EVSE_NO_POWER_DWELL_SECONDS = 300  # ev_charge_resume_check

# Rescue wakeup: only past 2x the expected reporting gap (D5).
RESCUE_WAKEUP_GAP_MULTIPLE = 2.0

# Overheat cutoff hysteresis, to avoid oscillating re-notify once cut.
OVERHEAT_HYSTERESIS_C = 5.0

# Rate-model guardrails (see docs section 5 of the port plan).
RATE_MODEL_MIN_SAMPLES = 3
RATE_MODEL_SAMPLE_WINDOW = 5
RATE_MODEL_MIN_SESSION_MINUTES = 45
RATE_MODEL_MIN_SOC_GAIN = 10.0
RATE_MODEL_CLAMP_LOW = 0.85
RATE_MODEL_CLAMP_HIGH = 2.0

# 12V auxiliary-battery health (aux_battery.py). Bands and thresholds are
# the YAML's proven ones (packages/opel.yaml:481-503), just re-homed onto a
# simpler 4-entity model -- see aux_battery.py's module docstring.
AUX_BATTERY_HEALTHY_THRESHOLD = 70.0
AUX_BATTERY_LOW_THRESHOLD = 50.0
AUX_BATTERY_CRITICAL_THRESHOLD = 30.0
AUX_BATTERY_LOW_DWELL_SECONDS = 6 * 3600  # opel_12v_low's 6h "for"
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
