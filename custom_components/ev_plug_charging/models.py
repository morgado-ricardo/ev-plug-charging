"""Shared value types for the pure logic layer (logic.py, session.py,
rate_model.py). Zero Home Assistant imports -- this module, and everything
that imports only from it, is unit-testable with plain pytest.

Split out from logic.py so logic.py, session.py and rate_model.py can share
these types without a circular import between them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any, Mapping, Optional


class ChargeMode(str, Enum):
    """input_select.ev_charge_mode, minus "Off (manual)" -- decision 4."""

    SMART = "smart"
    TIMED = "timed"


class PlugAction(str, Enum):
    ON = "on"
    OFF = "off"
    UNCHANGED = "unchanged"


class ChargeSource(str, Enum):
    """How the current charge is being powered. `bypass` means the car is
    charging but not through our plug, so we cannot stop it."""

    PLUG = "plug"
    BYPASS = "bypass"
    PLUG_IDLE = "plug_idle"
    NONE = "none"


class Owner(str, Enum):
    """Who turned the plug on. UNKNOWN after a restart, which is why a
    restart with the plug already on has to be reconciled rather than
    trusted."""

    US = "us"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RateSnapshot:
    """The rate model's output: already max(learned, seed) -- see
    rate_model.py. logic.py applies the 1.15x session-cap margin on top
    of this value, so both projection terms start from the same rate and
    learning can only ever push the cap later, never earlier."""

    minutes_per_percent: float


@dataclass(frozen=True)
class SessionAnchor:
    """The session-cap anchor: SoC + timestamp as a MATCHED pair. Reading
    one without the other is how a projection ends up extrapolating a
    known value from the wrong moment."""

    soc: Optional[float] = None
    captured_at: Optional[datetime] = None
    provisional: bool = False
    corrected: bool = False


@dataclass(frozen=True)
class SessionState:
    """Everything persisted across coordinator ticks and HA restarts.

    Losing the `*_since` debounce timestamps merely
    restarts each clock -- the safe direction for all of them -- so on
    restore those default to None without a special code path.
    """

    # -- session bookkeeping (session.py) --
    complete_notified: bool = False
    anchor: SessionAnchor = field(default_factory=SessionAnchor)
    charge_started_at: Optional[datetime] = None
    session_energy_kwh: float = 0.0

    # -- plug ownership --
    plug_turned_on_by: Owner = Owner.UNKNOWN
    manual_off_until: Optional[datetime] = None

    # -- once-per-window "charge not started" latch --
    not_started_pushed_for: Optional[str] = None

    # -- rescue wakeup: the one-shot-per-session latch --
    rescue_wakeup_used: bool = False

    # -- overheat cutoff latch --
    overheat_latched: bool = False

    # -- notification one-shot flags (cleared when their trigger clears) --
    charge_started_notified: bool = False
    timed_start_notified: bool = False
    soc_stale_notified: bool = False
    soc_full_notified: bool = False
    bypass_notified: bool = False
    evse_no_power_notified: bool = False

    # -- debounce timestamps: "condition has been continuously true since T" --
    car_charging_since: Optional[datetime] = None
    power_delivering_since: Optional[datetime] = None
    power_absent_since: Optional[datetime] = None
    bypass_since: Optional[datetime] = None
    plug_on_since: Optional[datetime] = None
    plug_on_no_power_since: Optional[datetime] = None
    ha_started_at: Optional[datetime] = None

    # -- rate-learning sample buffer (rate_model.py) --
    rate_samples: tuple[float, ...] = field(default_factory=tuple)

    # -- daily wakeup: the last instant a daily (as opposed to rescue)
    # refresh actually fired, so the crossing test in logic.py stays
    # restart-safe the same way the window-close/anchor timestamps are:
    # compared against, never re-derived from "have we seen this tick
    # before". --
    last_daily_wakeup_at: Optional[datetime] = None

    # -- when the most recent session actually finished (any of the four
    # completion paths). Feeds sensor.*_days_since_charge -- see
    # logic._mark_complete, the one choke point every completion branch
    # goes through. --
    charge_completed_at: Optional[datetime] = None

    # -- 12V auxiliary-battery tracking (aux_battery.py). `samples` is a
    # ring buffer of (ISO date, resting-SoC%) pairs, capped at
    # AUX_BATTERY_SAMPLE_WINDOW_DAYS entries, one per day -- see
    # aux_battery.record_resting_sample. The two `_notified` flags are the
    # same one-shot-per-condition pattern as the notification flags above;
    # `low_since` is the dwell timer for the 7-day-average-below-50 alert,
    # matching logic.py's own `_dwell` helper in spirit. --
    aux_battery_samples: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    aux_battery_low_since: Optional[datetime] = None
    aux_battery_low_notified: bool = False
    aux_battery_critical_notified: bool = False

    # -- previous-tick raw values, purely for edge detection in a pure
    # reducer -- reduce(prev, inputs), not stateless.
    # `prev_soc` + `prev_soc_changed_at` do double duty: persisted through
    # store.py, they are also what the coordinator seeds source.py's
    # `prev` TelemetrySnapshot with on the FIRST poll after a restart --
    # without them, that first poll has nothing to compare the new reading
    # against and would have to treat it as unconditionally fresh, which
    # is exactly the R4/R8 failure the freshness clock exists to prevent.
    # See source.py's module docstring and coordinator.py's startup path.
    prev_plug_switch_on: bool = False
    prev_charging_active: bool = False
    prev_soc: Optional[float] = None
    prev_soc_changed_at: Optional[datetime] = None
    prev_mode: Optional[ChargeMode] = None
    # Narrower than prev_charging_active (which is OR'd with plug power
    # delivery): specifically "was the CAR reporting it was charging, as
    # opposed to the plug drawing power". The car-confirmed completion edge
    # in logic.reduce() needs exactly this narrower signal -- a bypass
    # session (car charging, plug never involved) must still detect
    # "was charging, now finished" without depending on plug telemetry.
    prev_car_charging: bool = False

    # -- the sticky version of charge_source (models.ChargeSource): holds
    # how the session that just ended was actually powered, because the
    # live value has already fallen back to NONE by the time a car-
    # confirmed completion (which can arrive well after charging_active
    # drops) fires. --
    last_charge_source: ChargeSource = ChargeSource.NONE

    # -- fields the rate model needs to recognise a completed, acceptable
    # session (see rate_model.accept_session) --
    session_ran_above_target: bool = False
    session_stayed_on_plug: bool = True


@dataclass(frozen=True)
class Inputs:
    """A snapshot of the world for one reduce() call."""

    now: datetime
    mode: ChargeMode
    enabled: bool
    target_soc: float
    min_soc_override: float
    window_start: time
    window_end: time
    window_open_edge: bool
    window_close_edge: bool
    expected_gap_min: float
    power_threshold_w: float
    overheat_protection: bool
    temp_limit_c: float
    rescue_refresh_enabled: bool

    soc: Optional[float]
    soc_changed_at: Optional[datetime]
    source_reachable: bool
    car_charging: bool  # RAW status match; logic.reduce() gates this on
    # freshness itself (a stale cached "charging" must not latch forever)

    plug_switch_on: bool
    plug_power_w: Optional[float]
    plug_temp_c: Optional[float]
    plugged: Optional[bool]

    rate: RateSnapshot

    # True on the first reduce() call after HA/the config entry starts.
    ha_start_edge: bool = False
    # The car's own terminal "finished" report, source-normalised (see
    # source.TelemetrySnapshot.car_charge_finished). Unlike car_charging,
    # this is not gated on freshness in reduce() -- a terminal status is a
    # discrete report, not a value that can go silently stale the way a
    # cached "still charging" can.
    car_charge_finished: bool = False

    # -- daily wakeup (see logic._daily_wakeup_due) --
    daily_wakeup_enabled: bool = False
    daily_wakeup_time: time = time(6, 0)


@dataclass(frozen=True)
class Event:
    name: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    plug: PlugAction
    force_disable: bool
    events: tuple[Event, ...]
    projected_soc: Optional[float]
    silence_minutes: float
    charge_source: ChargeSource
    charging_active: bool
    # Just the plug's own contribution to charging_active -- car_charging
    # OR plug_delivering makes the composite, but "is the plug itself
    # drawing power" is useful on its own when diagnosing an EVSE that
    # isn't delivering anything (binary_sensor.*_plug_delivering_power).
    plug_delivering: bool
    soc_stale: bool
    request_refresh: bool
    reason: str
