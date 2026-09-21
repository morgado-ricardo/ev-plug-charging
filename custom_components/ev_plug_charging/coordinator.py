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
from dataclasses import dataclass, replace
from datetime import datetime, time as time_cls, timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, Event, HomeAssistant, callback
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
    CONF_CHARGE_CURRENT_A,
    CONF_EFFICIENCY_PRIOR,
    CONF_PLUG_ENERGY_SENSOR,
    CONF_PLUG_POWER_SENSOR,
    CONF_PLUG_SWITCH,
    CONF_PLUG_TEMP_SENSOR,
    CONF_POLL_INTERVAL,
    CONF_RESCUE_REFRESH_ENABLED,
    CONF_TEMP_LIMIT,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGE_CURRENT_A,
    DEFAULT_EFFICIENCY_PRIOR,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MEASURED_POWER_DISAGREEMENT_THRESHOLD,
    MEASURED_POWER_SAMPLE_CAP,
    MEASURED_POWER_TAPER_MARGIN_SOC,
    STORE_KEY_TEMPLATE,
    STORE_VERSION,
    SUPPLY_VOLTAGE_V,
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
        # Phase-2 efficiency calibration: the raw per-tick AC power
        # readings feeding this session's measured_ac_power_p90 settlement
        # (coordinator-side transient, same reasoning as
        # _energy_baseline_kwh -- losing an in-progress session's samples
        # on restart just means that one session doesn't update the
        # measured power, not a safety issue).
        self._power_samples: list[float] = []
        # Set at session-completion time (see _maybe_record_session) to the
        # stop reason record_efficiency_sample()'s accept_session() gate
        # needs; cleared once a sample is recorded or the pair is reset by
        # the next session. Coordinator-side and transient like the above
        # -- see _try_record_efficiency_sample's docstring for why a
        # one-shot attempt is not enough here.
        self._pending_efficiency_stop_reason: Optional[str] = None
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
        now = dt_util.now()
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
        """DataUpdateCoordinator's own tick: poll the source, then evaluate."""
        now = dt_util.now()
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
        now = dt_util.now()
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
        new_state = self._track_calibration_pair(prev_state, new_state, inputs)
        self._sample_measured_power(inputs, decision)
        new_state, aux_reading, aux_events = aux_battery.advance_aux_battery(
            new_state,
            now,
            self._telemetry.aux_battery_soc if self._telemetry else None,
            at_rest=not decision.charging_active,
        )
        self._session_state = new_state
        # Retried every tick (cheap no-op when nothing is pending): a late
        # SoC reading can complete the calibration pair on a tick that has
        # nothing else to do with a since-completed session -- see
        # _try_record_efficiency_sample's docstring.
        self._try_record_efficiency_sample()
        self.last_decision = decision
        self.last_aux_battery = aux_reading
        self._log_decision(
            now, inputs, self._session_state, decision, window_open_edge, window_close_edge
        )

        await self._act_on_decision(prev_state, self._session_state, decision, inputs, aux_events)
        await self._store.async_save(store_mod.to_dict(self._session_state))

        return {
            "telemetry": self._telemetry,
            "decision": decision,
            "state": self._session_state,
            "aux_battery": aux_reading,
        }

    def _log_decision(
        self,
        now: datetime,
        inputs: Inputs,
        state,
        decision: logic_mod.Decision,
        window_open_edge: bool,
        window_close_edge: bool,
    ) -> None:
        """One line per tick saying what was decided and what decided it.

        This integration has no automations, so it leaves no traces: when a
        charge does not start there is nothing to open and step through.
        `decision_reason` on sensor.*_projected_soc shows the latest answer
        but keeps no history, and the two things most likely to be
        suppressing a start -- `manual_off_until` and a window that is not
        open when the wall clock says it should be -- are not entities at
        all and were only visible in a diagnostics download.

        So: everything needed to answer "why didn't it charge?" on one line,
        at DEBUG. Actuations also go out at INFO, because a charge starting
        or stopping is worth a line in anyone's log without opting in.
        """
        if decision.plug is not PlugAction.UNCHANGED:
            _LOGGER.info(
                "Plug %s (%s): soc=%s projected=%s target=%s",
                "ON" if decision.plug is PlugAction.ON else "OFF",
                decision.reason,
                inputs.soc,
                decision.projected_soc,
                inputs.target_soc,
            )

        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return

        _LOGGER.debug(
            "tick %s | %s -> plug %s | soc=%s proj=%s target=%s | "
            "window %s-%s in=%s%s | enabled=%s mode=%s plug_on=%s power=%sW | "
            "silence=%.0fmin stale=%s source=%s | manual_off_until=%s",
            now.isoformat(timespec="seconds"),
            decision.reason,
            decision.plug.value,
            inputs.soc,
            decision.projected_soc,
            inputs.target_soc,
            inputs.window_start,
            inputs.window_end,
            # Recomputed with the same call the decision used, edges and
            # all -- a log line that disagrees with the decision it is
            # describing is worse than no log line.
            logic_mod.in_window(
                now,
                inputs.window_start,
                inputs.window_end,
                window_open_edge,
                window_close_edge,
            ),
            " (open edge)" if window_open_edge else (" (close edge)" if window_close_edge else ""),
            inputs.enabled,
            inputs.mode.value,
            inputs.plug_switch_on,
            inputs.plug_power_w,
            decision.silence_minutes,
            decision.soc_stale,
            decision.charge_source.value,
            state.manual_off_until.isoformat(timespec="seconds")
            if state.manual_off_until
            else None,
        )

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
        if decision.plug in (PlugAction.ON, PlugAction.OFF):
            # A FRESH Context per actuation, never reused or cached on
            # self: sharing one across ticks would chain every actuation
            # of this entry's whole lifetime into one causal graph, which
            # is not what "this call did that" is supposed to mean.
            actuation_context = Context()
            action = "on" if decision.plug == PlugAction.ON else "off"

            from .notify import async_fire_plug_actuation

            # Fired BEFORE the switch call, not after -- see
            # async_fire_plug_actuation's docstring for why the ordering
            # is load-bearing, not cosmetic.
            await async_fire_plug_actuation(
                self.hass,
                self._plug_switch_entity_id,
                action,
                decision.reason,
                actuation_context,
            )
            await self.hass.services.async_call(
                "switch",
                f"turn_{action}",
                {"entity_id": self._plug_switch_entity_id},
                blocking=True,
                context=actuation_context,
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
        energy sensor. Never an input to a LIVE decision -- see the note
        on _energy_baseline_kwh in __init__ for why this lives outside
        SessionState/logic.reduce(). No longer "metering only" as of
        Phase 2, though: at session-completion time, under
        accept_session()'s gates, session_energy_kwh contributes one
        bounded efficiency sample (see _try_record_efficiency_sample and
        rate_model.record_efficiency_sample) -- bounded by
        EFFICIENCY_MAX_GAIN, never a direct actuation input."""
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
        # replace() below overwrites it with the real computed value).
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
        if just_reset:
            # The measured-power samples feeding this session's p90 belong
            # to whatever session was open when they were taken -- see
            # _sample_measured_power.
            self._power_samples = []

        session_kwh = max(0.0, raw - self._energy_baseline_kwh)
        delta_since_last = max(0.0, session_kwh - prev_state.session_energy_kwh)

        current_month = dt_util.now().month
        if self._monthly_energy_month != current_month:
            self._monthly_energy_month = current_month
            self._monthly_energy_kwh = 0.0
        self._monthly_energy_kwh += delta_since_last
        self._lifetime_energy_kwh += delta_since_last

        return replace(new_state, session_energy_kwh=session_kwh)

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

        # Efficiency calibration: the pair _track_calibration_pair has
        # captured so far may not be complete -- a late SoC report can
        # land hours after this session ended (see rate_model.py's module
        # docstring). Remember the stop reason so later ticks keep
        # retrying record_efficiency_sample against the pair as it grows,
        # rather than judging it on whatever it holds this one tick.
        self._pending_efficiency_stop_reason = decision.reason
        self._try_record_efficiency_sample()
        self._settle_measured_power()

    def _try_record_efficiency_sample(self) -> None:
        """Attempt to close out the pending efficiency measurement against
        the CURRENT calibration pair.

        Unlike rate-sample recording, this cannot be a one-shot call made
        only at session-completion time: the pair's second reading
        (calib_soc_last) can arrive on a tick well after the session
        completed -- the sparse-telemetry case rate_model.py's module
        docstring documents (anchor captured, car goes silent for hours,
        one reading lands after the charge already stopped). So this is
        called every tick; it is a cheap no-op whenever nothing is
        pending.
        """
        if self._pending_efficiency_stop_reason is None:
            return
        state = self._session_state
        if state.charge_started_at is None or state.anchor.soc is None:
            self._pending_efficiency_stop_reason = None
            return
        if state.calib_soc_first is None:
            # session.py already reset the pair for a new session --
            # nothing left of the old one to retry.
            self._pending_efficiency_stop_reason = None
            return
        if state.calib_soc_last is None:
            return  # still waiting for a second fresh reading

        capacity = self.entry.options.get(
            CONF_BATTERY_CAPACITY_KWH,
            self.entry.data.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
        )
        prior = self.entry.data.get(CONF_EFFICIENCY_PRIOR, DEFAULT_EFFICIENCY_PRIOR)
        working_before = rate_model.working_efficiency(state.efficiency_samples, prior)

        # This CompletedSession is built fresh from persisted SessionState,
        # not the original completion tick's Inputs/Decision -- those
        # aren't available on a later retry tick. soc_gained is recomputed
        # against the anchor the same way _maybe_record_session's own copy
        # is; duration_minutes only has to clear the MIN_SESSION_MINUTES
        # floor, so measuring it against "now" (which only grows on a
        # retry) is safe. record_efficiency_sample() does not use either
        # field for the measurement itself -- see its docstring.
        session = rate_model.CompletedSession(
            duration_minutes=(dt_util.now() - state.charge_started_at).total_seconds() / 60.0,
            soc_gained=state.calib_soc_last - state.anchor.soc,
            stayed_on_plug=state.session_stayed_on_plug,
            ran_above_target=state.session_ran_above_target,
            anchor_provisional_unresolved=state.anchor.provisional,
            stop_reason=self._pending_efficiency_stop_reason,
        )
        before_count = len(state.efficiency_samples)
        new_state = rate_model.record_efficiency_sample(state, session, capacity)
        self._session_state = new_state
        if len(new_state.efficiency_samples) > before_count:
            self._pending_efficiency_stop_reason = None
            working_after = rate_model.working_efficiency(new_state.efficiency_samples, prior)
            if working_after != working_before:
                _LOGGER.info(
                    "Working charge efficiency changed: %.3f -> %.3f (%d samples)",
                    working_before,
                    working_after,
                    len(new_state.efficiency_samples),
                )

    def _settle_measured_power(self) -> None:
        """Called once, at session-completion time: settle this session's
        p90 measured AC power and check it against the configured amps.
        Unlike the efficiency sample, this never needs a retry --
        _sample_measured_power only appends while charging_active, which
        is already false by the time a session completes."""
        p90 = rate_model.measured_ac_power_p90(tuple(self._power_samples))
        if p90 is None:
            return
        self._session_state = replace(self._session_state, measured_ac_power_kw=p90)

        current_a = self.entry.options.get(
            CONF_CHARGE_CURRENT_A, self.entry.data.get(CONF_CHARGE_CURRENT_A, DEFAULT_CHARGE_CURRENT_A)
        )
        configured_kw = current_a * SUPPLY_VOLTAGE_V / 1000.0
        if configured_kw <= 0:
            return
        disagreement = abs(p90 - configured_kw) / configured_kw
        if disagreement > MEASURED_POWER_DISAGREEMENT_THRESHOLD:
            self._async_raise_power_mismatch_repair(current_a, configured_kw, p90)
        else:
            self._async_clear_power_mismatch_repair()

    def _power_mismatch_issue_id(self) -> str:
        # Same shape as notify.py's _repair_id: <entry_id>_<translation_key>.
        return f"{self.entry.entry_id}_{DOMAIN}_power_mismatch"

    def _async_raise_power_mismatch_repair(
        self, configured_a: float, configured_kw: float, measured_kw: float
    ) -> None:
        try:
            from homeassistant.helpers import issue_registry as ir
        except ImportError:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._power_mismatch_issue_id(),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=f"{DOMAIN}_power_mismatch",
            translation_placeholders={
                "configured_a": f"{configured_a:.1f}",
                "configured_kw": f"{configured_kw:.2f}",
                "measured_kw": f"{measured_kw:.2f}",
                "measured_a": f"{measured_kw * 1000.0 / SUPPLY_VOLTAGE_V:.1f}",
            },
        )

    def _async_clear_power_mismatch_repair(self) -> None:
        try:
            from homeassistant.helpers import issue_registry as ir
        except ImportError:
            return
        ir.async_delete_issue(self.hass, DOMAIN, self._power_mismatch_issue_id())

    def _sample_measured_power(self, inputs: Inputs, decision: logic_mod.Decision) -> None:
        """Accumulate this tick's plug power reading toward the session's
        p90 measured AC power (settled at completion -- see
        _settle_measured_power), while it's actually representative of the
        configured current: only while charging_active, above the same
        delivering-power floor logic.py uses to call power "flowing" at
        all, and comfortably below target. The constant-voltage taper
        tapers power off for reasons unrelated to the configured current
        and would otherwise bias the estimate low."""
        if not decision.charging_active:
            return
        if inputs.plug_power_w is None or inputs.plug_power_w <= inputs.power_threshold_w:
            return
        if (
            inputs.soc is not None
            and inputs.soc >= inputs.target_soc - MEASURED_POWER_TAPER_MARGIN_SOC
        ):
            return
        self._power_samples.append(inputs.plug_power_w / 1000.0)
        if len(self._power_samples) > MEASURED_POWER_SAMPLE_CAP:
            del self._power_samples[: len(self._power_samples) - MEASURED_POWER_SAMPLE_CAP]

    def _track_calibration_pair(self, prev_state, new_state, inputs: Inputs):
        """Snapshot (soc, session_energy_kwh) on each genuinely fresh SoC
        reading -- the matched pair record_efficiency_sample() measures
        between. Same freshness test session.py's anchor correction uses
        (inp.soc_changed_at != the PRE-reduce state's prev_soc_changed_at)
        -- not a second freshness rule.

        Keeps updating calib_soc_last/calib_energy_last even after the
        session has completed: session_energy_kwh has already stopped
        moving by then, so a reading that lands hours later still pairs
        correctly against the full session's energy -- see rate_model.py's
        module docstring, the sparse-telemetry case that justifies this
        over session totals. Only session.py's new_session reset, on the
        NEXT session's plug_on_edge, ends this session's window.
        """
        if new_state.charge_started_at is None:
            return new_state
        if inputs.soc is None or inputs.soc_changed_at == prev_state.prev_soc_changed_at:
            return new_state
        if new_state.calib_soc_first is None:
            return replace(
                new_state, calib_soc_first=inputs.soc, calib_energy_first=new_state.session_energy_kwh
            )
        return replace(
            new_state, calib_soc_last=inputs.soc, calib_energy_last=new_state.session_energy_kwh
        )

    def _effective_rate(self) -> RateSnapshot:
        capacity = self.entry.options.get(
            CONF_BATTERY_CAPACITY_KWH,
            self.entry.data.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
        )
        current_a = self.entry.options.get(
            CONF_CHARGE_CURRENT_A,
            self.entry.data.get(CONF_CHARGE_CURRENT_A, DEFAULT_CHARGE_CURRENT_A),
        )
        # The prior is a fixed constant in v4 -- not read from options, since
        # there is no form field to write one there. entry.data is still
        # checked, since that is where async_migrate_entry writes a
        # migrated entry's carried-forward value.
        prior = self.entry.data.get(CONF_EFFICIENCY_PRIOR, DEFAULT_EFFICIENCY_PRIOR)
        working = rate_model.working_efficiency(self._session_state.efficiency_samples, prior)
        seed = rate_model.seed_rate_calibrated(
            capacity,
            current_a,
            SUPPLY_VOLTAGE_V,
            prior,
            self._session_state.measured_ac_power_kw,
            working,
        )
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
