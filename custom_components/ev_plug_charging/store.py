"""Versioned codec for `SessionState`, the whole of what must persist across
a Home Assistant restart.

This exists as one place, not `RestoreEntity` scattered across nine platform
files, so the state that R5/R6/R8/R11 depend on is durable and migratable
as a single unit. The coordinator wraps `to_dict`/`from_dict` with
`homeassistant.helpers.storage.Store`; this module itself has zero HA
imports and is unit-tested as plain JSON round-tripping.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .const import STORE_VERSION
from .models import ChargeMode, ChargeSource, Owner, SessionAnchor, SessionState


def _dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def to_dict(state: SessionState) -> dict[str, Any]:
    """SessionState -> a plain JSON-serialisable dict."""
    return {
        "version": STORE_VERSION,
        "complete_notified": state.complete_notified,
        "anchor": {
            "soc": state.anchor.soc,
            "captured_at": _dt(state.anchor.captured_at),
            "provisional": state.anchor.provisional,
            "corrected": state.anchor.corrected,
        },
        "charge_started_at": _dt(state.charge_started_at),
        "session_energy_kwh": state.session_energy_kwh,
        "plug_turned_on_by": state.plug_turned_on_by.value,
        "manual_off_until": _dt(state.manual_off_until),
        "not_started_pushed_for": state.not_started_pushed_for,
        "rescue_wakeup_used": state.rescue_wakeup_used,
        "overheat_latched": state.overheat_latched,
        "charge_started_notified": state.charge_started_notified,
        "timed_start_notified": state.timed_start_notified,
        "soc_stale_notified": state.soc_stale_notified,
        "soc_full_notified": state.soc_full_notified,
        "bypass_notified": state.bypass_notified,
        "evse_no_power_notified": state.evse_no_power_notified,
        "car_charging_since": _dt(state.car_charging_since),
        "power_delivering_since": _dt(state.power_delivering_since),
        "power_absent_since": _dt(state.power_absent_since),
        "bypass_since": _dt(state.bypass_since),
        "plug_on_since": _dt(state.plug_on_since),
        "plug_on_no_power_since": _dt(state.plug_on_no_power_since),
        "ha_started_at": _dt(state.ha_started_at),
        "rate_samples": list(state.rate_samples),
        "last_daily_wakeup_at": _dt(state.last_daily_wakeup_at),
        "charge_completed_at": _dt(state.charge_completed_at),
        "aux_battery_samples": [list(pair) for pair in state.aux_battery_samples],
        "aux_battery_low_since": _dt(state.aux_battery_low_since),
        "aux_battery_low_notified": state.aux_battery_low_notified,
        "aux_battery_critical_notified": state.aux_battery_critical_notified,
        "prev_plug_switch_on": state.prev_plug_switch_on,
        "prev_charging_active": state.prev_charging_active,
        "prev_car_charging": state.prev_car_charging,
        "prev_soc": state.prev_soc,
        "prev_soc_changed_at": _dt(state.prev_soc_changed_at),
        "prev_mode": state.prev_mode.value if state.prev_mode is not None else None,
        "last_charge_source": state.last_charge_source.value,
        "session_ran_above_target": state.session_ran_above_target,
        "session_stayed_on_plug": state.session_stayed_on_plug,
    }


def from_dict(data: dict[str, Any] | None) -> SessionState:
    """A plain dict (as produced by to_dict, or missing/corrupt) ->
    SessionState. Unknown or missing fields fall back to SessionState's own
    defaults, which are all chosen to be the fail-closed / safe-restart
    value -- losing a debounce timestamp only restarts that clock."""
    if not data:
        return SessionState()

    anchor_data = data.get("anchor") or {}
    anchor = SessionAnchor(
        soc=anchor_data.get("soc"),
        captured_at=_parse_dt(anchor_data.get("captured_at")),
        provisional=bool(anchor_data.get("provisional", False)),
        corrected=bool(anchor_data.get("corrected", False)),
    )

    plug_owner_raw = data.get("plug_turned_on_by", Owner.UNKNOWN.value)
    try:
        plug_owner = Owner(plug_owner_raw)
    except ValueError:
        plug_owner = Owner.UNKNOWN

    prev_mode_raw = data.get("prev_mode")
    prev_mode = None
    if prev_mode_raw is not None:
        try:
            prev_mode = ChargeMode(prev_mode_raw)
        except ValueError:
            prev_mode = None

    last_charge_source_raw = data.get("last_charge_source", ChargeSource.NONE.value)
    try:
        last_charge_source = ChargeSource(last_charge_source_raw)
    except ValueError:
        last_charge_source = ChargeSource.NONE

    return SessionState(
        complete_notified=bool(data.get("complete_notified", False)),
        anchor=anchor,
        charge_started_at=_parse_dt(data.get("charge_started_at")),
        session_energy_kwh=float(data.get("session_energy_kwh", 0.0)),
        plug_turned_on_by=plug_owner,
        manual_off_until=_parse_dt(data.get("manual_off_until")),
        not_started_pushed_for=data.get("not_started_pushed_for"),
        rescue_wakeup_used=bool(data.get("rescue_wakeup_used", False)),
        overheat_latched=bool(data.get("overheat_latched", False)),
        charge_started_notified=bool(data.get("charge_started_notified", False)),
        timed_start_notified=bool(data.get("timed_start_notified", False)),
        soc_stale_notified=bool(data.get("soc_stale_notified", False)),
        soc_full_notified=bool(data.get("soc_full_notified", False)),
        bypass_notified=bool(data.get("bypass_notified", False)),
        evse_no_power_notified=bool(data.get("evse_no_power_notified", False)),
        car_charging_since=_parse_dt(data.get("car_charging_since")),
        power_delivering_since=_parse_dt(data.get("power_delivering_since")),
        power_absent_since=_parse_dt(data.get("power_absent_since")),
        bypass_since=_parse_dt(data.get("bypass_since")),
        plug_on_since=_parse_dt(data.get("plug_on_since")),
        plug_on_no_power_since=_parse_dt(data.get("plug_on_no_power_since")),
        ha_started_at=_parse_dt(data.get("ha_started_at")),
        rate_samples=tuple(data.get("rate_samples", [])),
        last_daily_wakeup_at=_parse_dt(data.get("last_daily_wakeup_at")),
        charge_completed_at=_parse_dt(data.get("charge_completed_at")),
        aux_battery_samples=tuple(
            (str(day), float(value)) for day, value in data.get("aux_battery_samples", [])
        ),
        aux_battery_low_since=_parse_dt(data.get("aux_battery_low_since")),
        aux_battery_low_notified=bool(data.get("aux_battery_low_notified", False)),
        aux_battery_critical_notified=bool(data.get("aux_battery_critical_notified", False)),
        prev_plug_switch_on=bool(data.get("prev_plug_switch_on", False)),
        prev_charging_active=bool(data.get("prev_charging_active", False)),
        prev_car_charging=bool(data.get("prev_car_charging", False)),
        prev_soc=data.get("prev_soc"),
        prev_soc_changed_at=_parse_dt(data.get("prev_soc_changed_at")),
        prev_mode=prev_mode,
        last_charge_source=last_charge_source,
        session_ran_above_target=bool(data.get("session_ran_above_target", False)),
        session_stayed_on_plug=bool(data.get("session_stayed_on_plug", True)),
    )
