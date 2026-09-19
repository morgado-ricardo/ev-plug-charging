"""The coordinator: the only place logic.reduce() is called for real.

Owns the telemetry-source poll, the plug-entity listeners, the ~30s
projection timer, the exact window open/close callbacks, persistence via
store.py, and actuation. Every tick funnels through the same path: build
Inputs from the current world -> logic.reduce() -> persist the new
SessionState -> act on the Decision.

One funnel, deliberately. Independent rules that each watch the world and
act on it cannot be reasoned about together, and they race: two of them can
observe the same tick and disagree about what to do with one plug.

It never knows WHICH telemetry source it has: `sources.async_create_source()`
hands it a `TelemetrySource`, and from then on it only asks for a normalised
`TelemetrySnapshot` and, rarely, a refresh.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as time_cls, timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import event as event_helper
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import aux_battery
from . import logic as logic_mod
from . import rate_model
from . import source as source_mod
from . import store as store_mod
from .sources import SourceConnectionError, SourceResponseError, TelemetrySource, async_create_source
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_CHARGE_POWER_KW,
    CONF_PLUG_ENERGY_SENSOR,
    CONF_PLUG_POWER_SENSOR,
    CONF_PLUG_SWITCH,
    CONF_PLUG_TEMP_SENSOR,
    CONF_POLL_INTERVAL,
    CONF_RESCUE_REFRESH_ENABLED,
    CONF_TEMP_LIMIT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    STORE_KEY_TEMPLATE,
    STORE_VERSION,
)
from .models import ChargeMode, Inputs, PlugAction, RateSnapshot

_LOGGER = logging.getLogger(__name__)

# The projection is time-dependent even with no new data, so the stop must
# not wait for a source poll (default 120s) -- see logic.projected_soc.
PROJECTION_TICK_SECONDS = 30


@dataclass
class RuntimeSettings:
    """The runtime-tunable entities (select/switch/number/time platforms),
    held here rather than looked up by guessing entity_ids from
    hass.states.get(): HA derives entity_id from the friendly name/device
    slug, not from unique_id, so that lookup would be fragile. Each
    platform entity is a RestoreEntity that pushes its value here (and
    requests a re-evaluation) on every change, and restores it from HA's
    own state restoration on add, so a setting you changed months ago
    survives a restart.
    """

    mode: ChargeMode = ChargeMode.SMART
    enabled: bool = True
    overheat_protection: bool = True
    target_soc: float = 80.0
    min_soc_override: float = 15.0
    expected_gap_minutes: float = 40.0
    power_threshold_w: float = 50.0
    window_start: time_cls = time_cls(23, 0)
    window_end: time_cls = time_cls(7, 0)
    # Off by default (see const.DEFAULT_DAILY_WAKEUP_ENABLED): a second,
    # independent 12V budget line the user opts into per source, only
    # created as entities at all when the source supports a refresh --
    # see switch.py/time.py.
    daily_wakeup_enabled: bool = False
    daily_wakeup_time: time_cls = time_cls(6, 0)


class EvPlugChargingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One instance per config entry (one car/plug pairing)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.hass = hass
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(
                seconds=entry.options.get(
                    CONF_POLL_INTERVAL,
                    entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS),
                )
            ),
        )
        self._store: Store = Store(
            hass, STORE_VERSION, STORE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
        )
        self._session_state = store_mod.from_dict(None)  # replaced in async_config_entry_first_refresh
        self._telemetry: Optional[source_mod.TelemetrySnapshot] = None
        self._source: Optional[TelemetrySource] = None
        self._unsub_listeners: list[Any] = []
        self._unsub_window_open: Optional[Any] = None
        self._unsub_window_close: Optional[Any] = None
        self._ha_start_edge_pending = True
        self.last_decision: Optional[logic_mod.Decision] = None
        self.last_aux_battery: Optional[aux_battery.AuxBatteryReading] = None
        # Energy tracking (session/monthly/lifetime kWh) is intentionally
        # coordinator-side, not part of SessionState/logic.reduce(): no
        # metering value may gate a control decision, so it can be simpler
        # than the safety-critical state store.py persists. A
        # restart mid-session loses this baseline and the session figure
        # restarts from the plug's raw current reading -- cosmetic, not a
        # safety issue, unlike losing charge_started_at or the anchor.
        self._energy_baseline_kwh: Optional[float] = None
        self._monthly_energy_kwh: float = 0.0
        self._monthly_energy_month: Optional[int] = None
        self._lifetime_energy_kwh: float = 0.0
        self.settings = RuntimeSettings()

    @property
    def source(self) -> Optional[TelemetrySource]:
        """The configured telemetry source. Public because the refresh
        button and the refresh_source service both legitimately need it --
        they are the manual equivalents of the automatic rescue refresh."""
        return self._source

    def request_settings_update(self, **changes: Any) -> None:
        """Called by a settings entity (select/switch/number/time) when the
        user changes it. Updates the coordinator's live settings and
        triggers a re-evaluation on the same tick, rather than waiting for
        the next poll: flipping the charge mode should take effect now,
        not in two minutes."""
        rearm_window = "window_start" in changes or "window_end" in changes
        for key, value in changes.items():
            setattr(self.settings, key, value)
        if rearm_window:
            self._arm_window_callbacks()
        self.hass.async_create_task(self.async_request_refresh())

    # -- lifecycle --------------------------------------------------------

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        self._session_state = store_mod.from_dict(stored)

        self._source = async_create_source(self.hass, self.entry)

        # Restore the freshness clock's `prev` snapshot from what store.py
        # persisted -- without this, the first poll after a restart would
        # have nothing to compare against and would look unconditionally
        # fresh (see source.py's module docstring, R4/R8).
        now = dt_util.utcnow()
        self._telemetry = source_mod.restored_snapshot(
            self._session_state.prev_soc, self._session_state.prev_soc_changed_at, now
        )

        self._unsub_listeners.append(
            event_helper.async_track_state_change_event(
                self.hass, [self._plug_switch_entity_id, self._plug_power_entity_id], self._on_plug_event
            )
        )
        self._unsub_listeners.append(
            event_helper.async_track_time_interval(
                self.hass, self._on_projection_tick, timedelta(seconds=PROJECTION_TICK_SECONDS)
            )
        )
        self._arm_window_callbacks()
        self._unsub_listeners.append(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_ha_started)
        )

        if self.hass.is_running:
            self._ha_start_edge_pending = True

    async def async_shutdown(self) -> None:
        # Defensive: one listener already being gone (a teardown-ordering
        # quirk, not a decision-correctness issue) must never abort the
        # rest of cleanup or leave the entry unload reported as failed.
        for unsub in (*self._unsub_listeners, self._unsub_window_open, self._unsub_window_close):
            if unsub is None:
                continue
            try:
                unsub()
            except (KeyError, ValueError):
                _LOGGER.debug("Listener already removed during shutdown", exc_info=True)
        await super().async_shutdown()

    @callback
    def _on_ha_started(self, _event: Event) -> None:
        self._ha_start_edge_pending = True
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_plug_event(self, _event: Event) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_projection_tick(self, _now: datetime) -> None:
        self.hass.async_create_task(self._async_evaluate_only())

    def _arm_window_callbacks(self) -> None:
        """Exact point-in-time callbacks at window open/close, re-armed
        whenever the window times change. These pass window_open_edge /
        window_close_edge=True explicitly to logic.reduce() rather than
        letting in_window() re-derive from the clock -- see logic.in_window's
        docstring and R12."""
        if self._unsub_window_open:
            self._unsub_window_open()
        if self._unsub_window_close:
            self._unsub_window_close()
        window_start, window_end = self.settings.window_start, self.settings.window_end
        self._unsub_window_open = event_helper.async_track_time_change(
            self.hass,
            self._on_window_open,
            hour=window_start.hour,
            minute=window_start.minute,
            second=window_start.second,
        )
        self._unsub_window_close = event_helper.async_track_time_change(
            self.hass,
            self._on_window_close,
            hour=window_end.hour,
            minute=window_end.minute,
            second=window_end.second,
        )

    @callback
    def _on_window_open(self, _now: datetime) -> None:
        self.hass.async_create_task(self._async_evaluate_only(window_open_edge=True))

    @callback
    def _on_window_close(self, _now: datetime) -> None:
        self.hass.async_create_task(self._async_evaluate_only(window_close_edge=True))

    # -- the funnel ---------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """DataUpdateCoordinator's own tick: poll PSACC, then evaluate."""
        now = dt_util.utcnow()
        try:
            self._telemetry = await self._source.async_fetch(now, self._telemetry)
        except (SourceConnectionError, SourceResponseError) as err:
            _LOGGER.debug("Telemetry source fetch failed: %s", err)
            self._telemetry = source_mod.unreachable_snapshot(now, self._telemetry)
        return await self._async_evaluate(now)

    async def _async_evaluate_only(
        self, window_open_edge: bool = False, window_close_edge: bool = False
    ) -> None:
        """A tick that does NOT poll PSACC (plug event, projection timer,
        window edge) -- re-evaluates against the last known telemetry."""
        now = dt_util.utcnow()
        await self._async_evaluate(now, window_open_edge, window_close_edge)
        self.async_update_listeners()

    async def _async_evaluate(
        self,
        now: datetime,
        window_open_edge: bool = False,
        window_close_edge: bool = False,
    ) -> dict[str, Any]:
        inputs = self._build_inputs(now, window_open_edge, window_close_edge)
        prev_state = self._session_state
        new_state, decision = logic_mod.reduce(prev_state, inputs)
        new_state = self._track_energy(prev_state, new_state)
        new_state, aux_reading, aux_events = aux_battery.advance_aux_battery(
            new_state,
            now,
            self._telemetry.aux_battery_soc if self._telemetry else None,
            at_rest=not decision.charging_active,
        )
        self._session_state = new_state
        self.last_decision = decision
        self.last_aux_battery = aux_reading

        await self._act_on_decision(prev_state, new_state, decision, inputs, aux_events)
        await self._store.async_save(store_mod.to_dict(new_state))

        return {
            "telemetry": self._telemetry,
            "decision": decision,
            "state": new_state,
            "aux_battery": aux_reading,
        }

    def _build_inputs(
        self, now: datetime, window_open_edge: bool, window_close_edge: bool
    ) -> Inputs:
        hass = self.hass
        opt = self.entry.options
        data = self.entry.data
        settings = self.settings

        plug_switch_on = hass.states.is_state(self._plug_switch_entity_id, "on")
        plug_power = self._float_state(self._plug_power_entity_id)
        plug_temp = self._float_state(opt.get(CONF_PLUG_TEMP_SENSOR, data.get(CONF_PLUG_TEMP_SENSOR)))
        plugged = self._bool_state(self._plugged_entity_id) if self._plugged_entity_id else None

        rate = self._effective_rate()

        ha_start_edge = self._ha_start_edge_pending
        self._ha_start_edge_pending = False

        telemetry = self._telemetry
        # Already normalised by the source -- each provider spells "charging"
        # differently, and mapping it is the source's job, not ours.
        car_charging = telemetry is not None and telemetry.car_charging
        car_charge_finished = telemetry is not None and telemetry.car_charge_finished

        return Inputs(
            now=now,
            mode=settings.mode,
            enabled=settings.enabled,
            target_soc=settings.target_soc,
            min_soc_override=settings.min_soc_override,
            window_start=settings.window_start,
            window_end=settings.window_end,
            window_open_edge=window_open_edge,
            window_close_edge=window_close_edge,
            expected_gap_min=settings.expected_gap_minutes,
            power_threshold_w=settings.power_threshold_w,
            overheat_protection=settings.overheat_protection,
            temp_limit_c=opt.get(CONF_TEMP_LIMIT, data.get(CONF_TEMP_LIMIT, 65.0)),
            rescue_refresh_enabled=opt.get(CONF_RESCUE_REFRESH_ENABLED, True),
            soc=telemetry.soc if telemetry else None,
            soc_changed_at=telemetry.soc_changed_at if telemetry else None,
            source_reachable=telemetry.source_reachable if telemetry else False,
            car_charging=car_charging,
            plug_switch_on=plug_switch_on,
            plug_power_w=plug_power,
            plug_temp_c=plug_temp,
            plugged=plugged,
            rate=rate,
            ha_start_edge=ha_start_edge,
            car_charge_finished=car_charge_finished,
            daily_wakeup_enabled=settings.daily_wakeup_enabled,
            daily_wakeup_time=settings.daily_wakeup_time,
        )

    async def _act_on_decision(
        self,
        prev_state,
        new_state,
        decision: logic_mod.Decision,
        inputs: Inputs,
        extra_events: tuple[logic_mod.Event, ...] = (),
    ) -> None:
        if decision.plug == PlugAction.ON:
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": self._plug_switch_entity_id}, blocking=True
            )
        elif decision.plug == PlugAction.OFF:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self._plug_switch_entity_id}, blocking=True
            )

        if decision.force_disable:
            # The enabled switch's displayed state is DERIVED from
            # self.settings.enabled (see switch.py), so setting it here and
            # letting the next async_update_listeners() propagate is enough
            # -- no guessed-entity-id service call needed, unlike the
            # actual plug switch above (real hardware, not our own entity).
            self.settings.enabled = False

        if decision.request_refresh and self._source is not None:
            if self._source.supports_refresh:
                try:
                    await self._source.async_request_refresh()
                except (SourceConnectionError, SourceResponseError) as err:
                    _LOGGER.warning("Rescue refresh request failed: %s", err)
            else:
                _LOGGER.debug(
                    "Source %s cannot request a refresh; skipping",
                    self._source.source_type,
                )

        # Rate-model learning: a session just completed if complete_notified
        # flipped False -> True this tick with a stop reason the model
        # accepts. See rate_model.accept_session for the criteria.
        if new_state.complete_notified and not prev_state.complete_notified:
            self._maybe_record_session(prev_state, new_state, decision, inputs)

        from .notify import async_dispatch_events

        await async_dispatch_events(self.hass, self.entry, decision.events + extra_events)

    def _track_energy(self, prev_state, new_state):
        """Session/monthly/lifetime kWh from the plug's raw cumulative
        energy sensor. Metering only, never an input to a decision -- see
        the note on _energy_baseline_kwh in __init__ for why this lives
        outside SessionState/logic.reduce()."""
        from dataclasses import replace as _replace

        entity_id = self.entry.data.get(CONF_PLUG_ENERGY_SENSOR) or self.entry.options.get(
            CONF_PLUG_ENERGY_SENSOR
        )
        if not entity_id:
            return new_state
        raw = self._float_state(entity_id)
        if raw is None:
            return new_state

        # logic.reduce() (via session.advance_session) already decided
        # WHETHER this tick starts a new session, and that decision is
        # encoded as session_energy_kwh being reset to 0.0 in the state
        # `logic_mod.reduce()` just returned (before this method's own
        # _replace() below overwrites it with the real computed value).
        # Re-deriving "is this a new session?" independently here, e.g.
        # from charge_started_at changing, would drift from that decision
        # -- charge_started_at moves on every charging_active edge, not
        # only on new sessions (R11's mid-session flicker moves neither).
        # So: reset the baseline exactly when logic just zeroed the field.
        # That is what makes an overnight session spanning midnight count
        # as one, and a flicker (R11) not reset it.
        just_reset = new_state.session_energy_kwh == 0.0 and (
            self._energy_baseline_kwh is None or prev_state.session_energy_kwh != 0.0
        )
        if just_reset or self._energy_baseline_kwh is None:
            self._energy_baseline_kwh = raw

        session_kwh = max(0.0, raw - self._energy_baseline_kwh)
        delta_since_last = max(0.0, session_kwh - prev_state.session_energy_kwh)

        current_month = dt_util.utcnow().month
        if self._monthly_energy_month != current_month:
            self._monthly_energy_month = current_month
            self._monthly_energy_kwh = 0.0
        self._monthly_energy_kwh += delta_since_last
        self._lifetime_energy_kwh += delta_since_last

        return _replace(new_state, session_energy_kwh=session_kwh)

    @property
    def monthly_energy_kwh(self) -> float:
        return self._monthly_energy_kwh

    @property
    def lifetime_energy_kwh(self) -> float:
        return self._lifetime_energy_kwh

    def _maybe_record_session(self, prev_state, new_state, decision, inputs: Inputs) -> None:
        if prev_state.charge_started_at is None:
            return
        duration_minutes = (
            inputs.now - prev_state.charge_started_at
        ).total_seconds() / 60.0
        soc_gained = None
        if inputs.soc is not None and prev_state.anchor.soc is not None:
            soc_gained = inputs.soc - prev_state.anchor.soc
        if soc_gained is None:
            return
        session = rate_model.CompletedSession(
            duration_minutes=duration_minutes,
            soc_gained=soc_gained,
            stayed_on_plug=prev_state.session_stayed_on_plug,
            ran_above_target=prev_state.session_ran_above_target,
            anchor_provisional_unresolved=prev_state.anchor.provisional,
            stop_reason=decision.reason,
        )
        self._session_state = rate_model.record_session(self._session_state, session)

    def _effective_rate(self) -> RateSnapshot:
        capacity = self.entry.options.get(
            CONF_BATTERY_CAPACITY_KWH, self.entry.data.get(CONF_BATTERY_CAPACITY_KWH, 50.0)
        )
        power = self.entry.options.get(
            CONF_CHARGE_POWER_KW, self.entry.data.get(CONF_CHARGE_POWER_KW, 1.84)
        )
        efficiency = self.entry.options.get(
            CONF_CHARGE_EFFICIENCY, self.entry.data.get(CONF_CHARGE_EFFICIENCY, 0.82)
        )
        seed = rate_model.seed_rate(capacity, power, efficiency)
        return rate_model.effective_rate(self._session_state.rate_samples, seed)

    # -- small entity/config helpers ---------------------------------------

    @property
    def _plug_switch_entity_id(self) -> str:
        return self.entry.data[CONF_PLUG_SWITCH]

    @property
    def _plug_power_entity_id(self) -> str:
        return self.entry.data[CONF_PLUG_POWER_SENSOR]

    @property
    def _plugged_entity_id(self) -> Optional[str]:
        return self.entry.options.get("plugged_sensor_entity_id")

    def _float_state(self, entity_id: Optional[str]) -> Optional[float]:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _bool_state(self, entity_id: str) -> Optional[bool]:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state.lower() in ("true", "on", "1")
