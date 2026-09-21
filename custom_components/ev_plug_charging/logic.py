"""Pure decision logic for EV Plug Charging.

This module has ZERO Home Assistant imports. It is a reducer:

    new_state, decision = reduce(prev_state, inputs)

`SessionState` (models.py) is everything that must survive a restart or a
coordinator tick -- session bookkeeping, ownership, debounce timers,
one-shot latches. `Inputs` is a snapshot of the world right now. `Decision`
says what to do with the plug and what happened.

Everything here is arithmetic over timestamps, never an edge-triggered
callback. Edge triggers lose their state across a restart; timestamps do
not, and this module has to keep projecting correctly through both a
restart and hours of telemetry silence.

tests/test_scenarios.py (R1-R14) is the acceptance gate for this module.
Each scenario is a real failure that happened once.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Optional

from .const import (
    BYPASS_DEBOUNCE_SECONDS,
    CHARGE_STARTED_NOTIFY_DELAY_SECONDS,
    EVENT_BYPASS_DETECTED,
    EVENT_CHARGE_COMPLETE,
    EVENT_CHARGE_NOT_STARTED,
    EVENT_CHARGE_STARTED,
    EVENT_EVSE_NO_POWER,
    EVENT_OVERHEAT_CUTOFF,
    EVENT_REFRESH_ATTEMPTED,
    EVENT_RESTART_RECONCILED_OFF,
    EVENT_SOC_FULL,
    EVENT_SOC_STALE,
    EVENT_WINDOW_SHORTFALL,
    EVSE_NO_POWER_DWELL_SECONDS,
    OVERHEAT_HYSTERESIS_C,
    OWN_OFF_OBSERVATION_GRACE_SECONDS,
    POWER_DROP_COMPLETE_DWELL_SECONDS,
    RESCUE_WAKEUP_GAP_MULTIPLE,
    SESSION_CAP_MARGIN,
    TIMED_START_DWELL_SECONDS,
    WINDOW_START_GRACE_SECONDS,
)
from .session import advance_session
from .models import (
    ChargeMode,
    ChargeSource,
    Decision,
    Event,
    Inputs,
    Owner,
    PlugAction,
    RateSnapshot,
    SessionAnchor,
    SessionState,
)

__all__ = [
    "in_window",
    "window_instance_id",
    "is_trustworthy_soc",
    "silence_minutes",
    "projected_soc",
    "classify_source",
    "reduce",
]

# --------------------------------------------------------------------------- #
# Sub-functions -- each independently unit-tested
# --------------------------------------------------------------------------- #


def in_window(
    now: datetime,
    start: time,
    end: time,
    open_edge: bool = False,
    close_edge: bool = False,
) -> bool:
    """Half-open [start, end), with midnight wrap when start > end.

    `open_edge`/`close_edge` are passed explicitly by the coordinator's exact
    point-in-time callbacks rather than re-derived, because a callback can
    land a moment early or late and a recomputation would then disagree with
    the clock that is supposed to be authoritative at that instant. R12 is
    the regression test.
    """
    if open_edge:
        return True
    if close_edge:
        return False
    now_t = now.time()
    if start == end:
        return False
    if start > end:
        return now_t >= start or now_t < end
    return start <= now_t < end


def window_instance_id(now: datetime, start: time, end: time) -> str:
    """A stable identifier for "which window occurrence is this". Used only
    to deduplicate the once-per-window "charge not started" push across
    restarts, not for timing."""
    today = now.date()
    if start > end and now.time() < end:
        open_date = today - timedelta(days=1)
    else:
        open_date = today
    return open_date.isoformat()


def is_trustworthy_soc(soc: Optional[float], source_reachable: bool) -> bool:
    """A valid-looking number from a dead source is as dangerous as no
    number at all."""
    return soc is not None and source_reachable


def silence_minutes(
    now: datetime,
    soc_changed_at: Optional[datetime],
    charge_started_at: Optional[datetime],
) -> float:
    """Measured from the LATER of the last reading and the actual charge
    start.

    Never from the raw age of the reading: a car parked for three days has
    a three-day-old reading and nothing is wrong with it. Never from
    plug-on either: a cable can sit connected for an hour before current
    flows. Only silence DURING a charge means the feed has gone quiet."""
    if soc_changed_at is None:
        return 0.0  # fail closed: "not stale"
    anchor = soc_changed_at
    if charge_started_at is not None and charge_started_at > anchor:
        anchor = charge_started_at
    return max(0.0, (now - anchor).total_seconds() / 60.0)


def projected_soc(
    now: datetime,
    current_soc: Optional[float],
    soc_trustworthy: bool,
    charging_active: bool,
    charge_started_at: Optional[datetime],
    soc_changed_at: Optional[datetime],
    anchor: SessionAnchor,
    rate: RateSnapshot,
) -> Optional[float]:
    """The projection that gates the stop.

    max(reading_proj, session_proj), both clocked on charge_started_at,
    never on plug-on: a cable can sit plugged in for an hour before current
    flows. Returns None when the SoC itself is untrustworthy, so callers
    must handle "no answer" rather than reading a fabricated one.
    """
    if not soc_trustworthy or current_soc is None:
        return None
    if not charging_active:
        return current_soc

    rate_min = rate.minutes_per_percent
    lc_ts = soc_changed_at or charge_started_at or now
    cs_ts = charge_started_at or lc_ts
    reading_anchor = max(lc_ts, cs_ts)
    reading_proj = current_soc + (now - reading_anchor).total_seconds() / 60.0 / rate_min

    start_soc = anchor.soc if anchor.soc is not None else current_soc
    session_anchor_ts = charge_started_at or now
    cap_rate = rate_min * SESSION_CAP_MARGIN
    session_proj = start_soc + (now - session_anchor_ts).total_seconds() / 60.0 / cap_rate

    return round(max(reading_proj, session_proj), 1)


def classify_source(car_charging: bool, plug_delivering: bool) -> ChargeSource:
    """The car's own status is the authority on WHETHER it is charging;
    the plug's power draw is the authority on WHAT is delivering it. Keeping
    those two questions separate is what makes a bypass charge -- car
    charging, plug idle -- detectable at all."""
    if car_charging and plug_delivering:
        return ChargeSource.PLUG
    if car_charging:
        return ChargeSource.BYPASS
    if plug_delivering:
        return ChargeSource.PLUG_IDLE
    return ChargeSource.NONE


def _daily_wakeup_due(
    now: datetime,
    target_time: time,
    last_wakeup_at: Optional[datetime],
) -> bool:
    """A restart-safe "has today's scheduled instant already been served"
    test, in the same spirit as in_window()'s open/close edges and
    window_instance_id() -- timestamp arithmetic, not an edge callback, so
    a missed tick around the target time (a restart at 05:59, a slow poll
    at 06:01) neither loses the day's wakeup nor fires it twice.

    Being due is not enough to spend it -- see reduce()'s daily-wakeup
    block for the gates that decide whether a due wakeup buys anything.
    """
    scheduled = datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)
    if now < scheduled:
        return False
    return last_wakeup_at is None or last_wakeup_at < scheduled


def _dwell(
    condition: bool,
    since: Optional[datetime],
    now: datetime,
) -> tuple[Optional[datetime], float]:
    """Update a "condition has been continuously true since T" timestamp and
    return (new_since, minutes_held)."""
    if not condition:
        return None, 0.0
    if since is None:
        since = now
    return since, (now - since).total_seconds() / 60.0


def _mark_complete(state: SessionState, now: datetime) -> SessionState:
    """The single choke point every completion branch (target reached,
    window close, power drop, car-confirmed) goes through: sets the shared
    one-push-per-session latch AND records when it happened, so
    sensor.*_days_since_charge has one source of truth instead of four
    branches each deciding independently whether to update it."""
    return replace(state, complete_notified=True, charge_completed_at=now)


# --------------------------------------------------------------------------- #
# The reducer
# --------------------------------------------------------------------------- #


def reduce(prev: SessionState, inp: Inputs) -> tuple[SessionState, Decision]:
    """The whole decision, in one call.

    The branch order below is by safety, not readability: see the comments
    on each step. session.py holds the session sub-reducer this calls first.
    """
    events: list[Event] = []
    state = prev

    plug_delivering = (inp.plug_power_w or 0.0) > inp.power_threshold_w
    # With from_cache=1, a car-status of "charging" can be a cached value
    # that has not been refreshed in a while. Trusting it indefinitely would
    # let a stale "charging" misclassify charge_source (as BYPASS) and
    # keep the charge-started notification asserted long after the car
    # actually stopped. Gate it on the SAME freshness clock and threshold
    # the rescue wakeup uses (2x the expected reporting gap): past that
    # point the feed is already known to be unhealthy, and a cached
    # "charging" that old is no more trustworthy than no reading at all.
    # Below it, trust it.
    car_status_fresh = inp.soc_changed_at is not None and (
        inp.now - inp.soc_changed_at
    ) <= timedelta(minutes=inp.expected_gap_min * RESCUE_WAKEUP_GAP_MULTIPLE)
    car_charging = inp.car_charging and car_status_fresh
    # OUR plug's power draw is the sole authority on whether a charge is
    # happening on OUR plug -- it gates session start (session.py's
    # charging_active_edge), the projection (projected_soc), and every
    # completion path below. The car's own reported status is real
    # evidence of a charge (used for charge_source/bypass detection and
    # the charge-started/car-finished notifications, both below) but it is
    # NOT evidence about our plug specifically: an API that latches
    # "InProgress" with nothing plugged into our socket must never drive
    # our projection to a false target_reached, or our own plug-on into a
    # false "power dropped, charge complete". Both happened for real: a
    # charge window opened, the car's cached status alone made the plug
    # look "active" with zero current flowing, and 5 minutes later the
    # power-drop branch below reported the (non-existent) charge complete.
    charging_active = plug_delivering
    charge_source = classify_source(car_charging, plug_delivering)

    plug_on_edge = inp.plug_switch_on and not state.prev_plug_switch_on
    plug_off_edge = (not inp.plug_switch_on) and state.prev_plug_switch_on
    charging_active_edge = charging_active and not state.prev_charging_active
    fresh_reading_edge = (
        inp.soc_changed_at is not None and inp.soc_changed_at != state.prev_soc_changed_at
    )
    prev_owner = state.plug_turned_on_by

    # Narrower than charging_active_edge (see SessionState.prev_car_charging):
    # fires only on "the car itself was reporting charging, and now reports
    # finished" -- the ONE completion signal that works no matter how the
    # car was charged (plug, EVSE straight into the wall, or a public
    # charger this integration never sees).
    car_finished_edge = inp.car_charge_finished and state.prev_car_charging

    # The sticky companion to charge_source: captured whenever there is
    # an actual source to capture, so it still holds the right answer once
    # charge_source itself has fallen back to NONE -- which is exactly the
    # state it's in by the time a car-confirmed completion can fire.
    if charge_source != ChargeSource.NONE:
        state = replace(state, last_charge_source=charge_source)

    if inp.ha_start_edge and state.ha_started_at is None:
        state = replace(state, ha_started_at=inp.now)

    # ---- session bookkeeping (must run before the plug decision) ----
    state = advance_session(
        state,
        inp,
        charging_active=charging_active,
        plug_on_edge=plug_on_edge,
        charging_active_edge=charging_active_edge,
        fresh_reading_edge=fresh_reading_edge,
    )
    # Sticky-OR within the session: once true, stays true until
    # advance_session's new_session branch resets it. Runs AFTER
    # advance_session so a session that starts with power already flowing
    # (e.g. a restart mid-charge) is not left crediting a reset it just
    # received.
    if plug_delivering:
        state = replace(state, session_saw_power=True)

    if plug_on_edge:
        state = replace(
            state,
            plug_turned_on_by=Owner.EXTERNAL,
            plug_on_since=inp.now,
            own_off_commanded_at=None,
        )
    if plug_off_edge:
        # A human switching a plug WE own off: this must take
        # effect on the SAME tick's plug decision below, not just be
        # recorded for next time -- otherwise the below-target branch
        # would immediately turn it back on within this very reduce()
        # call, before the human's action was ever honoured. Suppress
        # re-assertion for the rest of this window occurrence.
        #
        # ONLY while a window is actually open. Off the clock there is no
        # "rest of this window occurrence" to suppress, and latching then
        # sets the deadline to the close of the NEXT window -- which
        # swallows that window whole, so the evening's charge silently
        # never starts. Turning the plug off at 20:00 says nothing about
        # whether you want it charging at 23:00.
        #
        # And only when a HUMAN did it. The plug's observed state lags our
        # own command by a tick, so our every stop -- an overheat cutoff,
        # most of all -- used to come back around and look like someone
        # overriding us, suppressing the resume we would otherwise make
        # once the plug cooled.
        own_off = state.own_off_commanded_at is not None and (
            inp.now - state.own_off_commanded_at
        ) <= timedelta(seconds=OWN_OFF_OBSERVATION_GRACE_SECONDS)

        manual_off_until = state.manual_off_until
        if not own_off and prev_owner == Owner.US and in_window(
            inp.now,
            inp.window_start,
            inp.window_end,
            inp.window_open_edge,
            inp.window_close_edge,
        ):
            window_close_dt = datetime.combine(
                inp.now.date(), inp.window_end, tzinfo=inp.now.tzinfo
            )
            if window_close_dt <= inp.now:
                window_close_dt += timedelta(days=1)
            manual_off_until = window_close_dt
        else:
            manual_off_until = None
        state = replace(
            state,
            plug_turned_on_by=Owner.UNKNOWN,
            plug_on_since=None,
            evse_no_power_notified=False,
            manual_off_until=manual_off_until,
            own_off_commanded_at=None,  # consumed
        )

    # ---- health signals (computed regardless of branch) ----
    soc_trustworthy = is_trustworthy_soc(inp.soc, inp.source_reachable)
    silence = silence_minutes(inp.now, inp.soc_changed_at, state.charge_started_at)
    proj = projected_soc(
        inp.now,
        inp.soc,
        soc_trustworthy,
        charging_active,
        state.charge_started_at,
        inp.soc_changed_at,
        state.anchor,
        inp.rate,
    )

    soc_stale = (
        inp.soc is not None
        and charging_active
        and inp.soc < inp.target_soc
        and silence > inp.expected_gap_min
    )
    if soc_stale and not state.soc_stale_notified:
        events.append(
            Event(EVENT_SOC_STALE, {"silence_minutes": round(silence, 1), "soc": inp.soc})
        )
        state = replace(state, soc_stale_notified=True)
    elif not soc_stale and state.soc_stale_notified:
        state = replace(state, soc_stale_notified=False)

    request_refresh = False
    if (
        inp.rescue_refresh_enabled
        and inp.mode == ChargeMode.SMART
        and inp.plug_switch_on
        and soc_stale
        and inp.source_reachable
        and silence >= inp.expected_gap_min * RESCUE_WAKEUP_GAP_MULTIPLE
        and not state.rescue_wakeup_used
    ):
        request_refresh = True
        state = replace(state, rescue_wakeup_used=True)
        events.append(Event(EVENT_REFRESH_ATTEMPTED, {"trigger": "rescue"}))

    # Daily wakeup: unlike the rescue refresh above (which only exists
    # DURING a session and is gated on plug_switch_on), this runs BETWEEN
    # sessions, when the feed would otherwise go stale for as long as the
    # gap to the next window -- see _daily_wakeup_due's docstring. Three
    # independent gates keep it from spending 12V budget for nothing:
    # a session already in progress (the rescue gate above owns that
    # case), a reading that is already fresher than the expected gap (a
    # forced wakeup would buy nothing), and the source being unreachable
    # (nothing to wake).
    if (
        inp.daily_wakeup_enabled
        and inp.source_reachable
        and not charging_active
        and silence > inp.expected_gap_min
        and _daily_wakeup_due(inp.now, inp.daily_wakeup_time, state.last_daily_wakeup_at)
    ):
        request_refresh = True
        state = replace(state, last_daily_wakeup_at=inp.now)
        events.append(Event(EVENT_REFRESH_ATTEMPTED, {"trigger": "daily"}))

    # ---- dwell-timed notifications (edge-shaped behaviour, level-based) ----
    since, held = _dwell(car_charging, state.car_charging_since, inp.now)
    state = replace(state, car_charging_since=since)
    if car_charging and held * 60 >= CHARGE_STARTED_NOTIFY_DELAY_SECONDS:
        if not state.charge_started_notified:
            events.append(
                Event(
                    EVENT_CHARGE_STARTED,
                    {"source": charge_source.value, "soc": inp.soc, "mode": inp.mode.value},
                )
            )
            state = replace(state, charge_started_notified=True)
    elif not car_charging and state.charge_started_notified:
        state = replace(state, charge_started_notified=False)

    since, held = _dwell(plug_delivering, state.power_delivering_since, inp.now)
    state = replace(state, power_delivering_since=since)
    if (
        inp.mode == ChargeMode.TIMED
        and plug_delivering
        and held * 60 >= TIMED_START_DWELL_SECONDS
    ):
        if not state.timed_start_notified:
            events.append(Event(EVENT_CHARGE_STARTED, {"source": "plug", "mode": "timed"}))
            state = replace(state, timed_start_notified=True)
    elif not plug_delivering and state.timed_start_notified:
        state = replace(state, timed_start_notified=False)

    since, held = _dwell(
        inp.plug_switch_on and not plug_delivering, state.power_absent_since, inp.now
    )
    state = replace(state, power_absent_since=since)
    if (
        inp.plug_switch_on
        and not plug_delivering
        and held * 60 >= POWER_DROP_COMPLETE_DWELL_SECONDS
        and not state.complete_notified
        # "Power stopped" is only evidence of a finished charge if power
        # actually started -- without this, turning the plug on with
        # nothing plugged in reports a completed charge 5 minutes later
        # (session_saw_power stays False the whole time; see its
        # docstring in models.py for the incident this guards against).
        and state.session_saw_power
    ):
        events.append(
            Event(
                EVENT_CHARGE_COMPLETE,
                {
                    "confirmed": True,
                    "reason": "power_drop",
                    "mode": inp.mode.value,
                    "energy_kwh": state.session_energy_kwh,
                },
            )
        )
        state = _mark_complete(state, inp.now)

    # Car-confirmed completion (see car_finished_edge above): a REPORT, not
    # a stop -- it never sets `plug`. The plug-side branches below already
    # own stopping; a car reporting its own "Finished" is not evidence OUR
    # target was reached (it may have its own limit, or none at all). Runs
    # here, before the enabled check further down, because observation is
    # explicitly what a disabled integration still does -- and it shares
    # complete_notified with every other completion path above/below, so
    # whichever one wins the race, exactly one notification goes out.
    if car_finished_edge and not state.complete_notified:
        events.append(
            Event(
                EVENT_CHARGE_COMPLETE,
                {
                    "confirmed": True,
                    "reason": "car_confirmed",
                    "mode": inp.mode.value,
                    "energy_kwh": state.session_energy_kwh,
                    "last_charge_source": state.last_charge_source.value,
                },
            )
        )
        state = _mark_complete(state, inp.now)

    since, held = _dwell(charge_source == ChargeSource.BYPASS, state.bypass_since, inp.now)
    state = replace(state, bypass_since=since)
    bypassed = charge_source == ChargeSource.BYPASS and held * 60 >= BYPASS_DEBOUNCE_SECONDS
    if bypassed and not state.bypass_notified:
        events.append(Event(EVENT_BYPASS_DETECTED, {}))
        state = replace(state, bypass_notified=True)
    elif not bypassed and state.bypass_notified:
        state = replace(state, bypass_notified=False)

    plugged_no_power = inp.plug_switch_on and inp.plugged is True and not plug_delivering
    since, held = _dwell(plugged_no_power, state.plug_on_no_power_since, inp.now)
    state = replace(state, plug_on_no_power_since=since)
    if plugged_no_power and held * 60 >= EVSE_NO_POWER_DWELL_SECONDS:
        if not state.evse_no_power_notified:
            events.append(Event(EVENT_EVSE_NO_POWER, {}))
            state = replace(state, evse_no_power_notified=True)
    elif not plugged_no_power:
        state = replace(state, evse_no_power_notified=False)

    if inp.soc is not None and inp.soc >= 99.9:
        if not state.soc_full_notified:
            events.append(Event(EVENT_SOC_FULL, {"soc": inp.soc}))
            state = replace(state, soc_full_notified=True)
    else:
        state = replace(state, soc_full_notified=False)

    # ---- 1. overheat: highest priority, latched ----
    overheating_now = (
        inp.plug_switch_on
        and inp.overheat_protection
        and inp.plug_temp_c is not None
        and inp.plug_temp_c > inp.temp_limit_c
    )
    if overheating_now:
        if not state.overheat_latched:
            events.append(Event(EVENT_OVERHEAT_CUTOFF, {"temp_c": inp.plug_temp_c}))
        state = replace(state, overheat_latched=True)
        return _finalize(state, inp, charging_active, car_charging, PlugAction.OFF), Decision(
            plug=PlugAction.OFF,
            force_disable=True,
            events=tuple(events),
            projected_soc=proj,
            silence_minutes=silence,
            charge_source=charge_source,
            charging_active=charging_active,
            plug_delivering=plug_delivering,
            soc_stale=soc_stale,
            request_refresh=False,
            reason="overheat",
        )
    if state.overheat_latched and (
        inp.plug_temp_c is None or inp.plug_temp_c < inp.temp_limit_c - OVERHEAT_HYSTERESIS_C
    ):
        state = replace(state, overheat_latched=False)

    # ---- 2. not enabled: observe only, never actuate ----
    if not inp.enabled:
        return _finalize(
            state, inp, charging_active, car_charging, PlugAction.UNCHANGED
        ), Decision(
            plug=PlugAction.UNCHANGED,
            force_disable=False,
            events=tuple(events),
            projected_soc=proj,
            silence_minutes=silence,
            charge_source=charge_source,
            charging_active=charging_active,
            plug_delivering=plug_delivering,
            soc_stale=soc_stale,
            request_refresh=request_refresh,
            reason="disabled",
        )

    # ---- 3. emergency low SoC: Smart only, ignores the window ----
    if (
        inp.mode == ChargeMode.SMART
        and soc_trustworthy
        and inp.soc is not None
        and inp.soc < inp.min_soc_override
        and not inp.plug_switch_on
    ):
        state = replace(state, plug_turned_on_by=Owner.US)
        return _finalize(
            state, inp, charging_active, car_charging, PlugAction.ON
        ), Decision(
            plug=PlugAction.ON,
            force_disable=False,
            events=tuple(events) + (Event("emergency_low_soc", {"soc": inp.soc}),),
            projected_soc=proj,
            silence_minutes=silence,
            charge_source=charge_source,
            charging_active=charging_active,
            plug_delivering=plug_delivering,
            soc_stale=soc_stale,
            request_refresh=request_refresh,
            reason="emergency_low_soc",
        )

    window_open = in_window(
        inp.now, inp.window_start, inp.window_end, inp.window_open_edge, inp.window_close_edge
    )
    manual_off_active = state.manual_off_until is not None and inp.now < state.manual_off_until

    # Did THIS window occurrence already complete?
    #
    # Stopping at target is not self-sustaining: projected_soc collapses
    # back to the last real reading the moment charging_active goes false
    # (see projected_soc's early return), and that reading is hours old by
    # construction -- it still says 69 when we just stopped at a projected
    # 80. So the below-target branch would turn the plug straight back on,
    # stop again, and oscillate every tick until a fresh reading landed.
    #
    # Until now the only thing preventing that was our own stop being
    # misread as a human override -- accidental, and it took the overheat
    # resume down with it. This is the same guard stated on purpose, and it
    # is derived from persisted state (charge_completed_at) rather than a
    # latch, so it survives a restart and expires on its own when the next
    # window occurrence opens. A genuinely new window, or a new session,
    # charges normally.
    completed_this_window = (
        state.complete_notified
        and state.charge_completed_at is not None
        and window_instance_id(state.charge_completed_at, inp.window_start, inp.window_end)
        == window_instance_id(inp.now, inp.window_start, inp.window_end)
    )

    # ---- 4. in window ----
    if window_open:
        if inp.mode == ChargeMode.SMART and not soc_trustworthy:
            # Fail closed, but never fail silent, and only once per
            # window occurrence.
            wid = window_instance_id(inp.now, inp.window_start, inp.window_end)
            grace_ok = state.ha_started_at is not None and (
                inp.now - state.ha_started_at
            ).total_seconds() >= WINDOW_START_GRACE_SECONDS
            if (
                not charging_active
                and not inp.plug_switch_on
                and grace_ok
                and state.not_started_pushed_for != wid
            ):
                events.append(
                    Event(
                        EVENT_CHARGE_NOT_STARTED,
                        {
                            "reason": "no_reading" if inp.soc is None else "source_unreachable",
                            "last_soc": inp.soc,
                        },
                    )
                )
                state = replace(state, not_started_pushed_for=wid)
            plug = PlugAction.UNCHANGED
            reason = "soc_untrustworthy"
        elif inp.mode == ChargeMode.TIMED:
            if manual_off_active:
                plug = PlugAction.UNCHANGED
            elif inp.plug_switch_on:
                plug = PlugAction.UNCHANGED
            else:
                plug = PlugAction.ON
                state = replace(state, plug_turned_on_by=Owner.US)
            reason = "timed_window"
        elif (
            inp.mode == ChargeMode.SMART
            and proj is not None
            and proj >= inp.target_soc
            and inp.plug_switch_on
        ):
            if not state.complete_notified:
                events.append(
                    Event(
                        EVENT_CHARGE_COMPLETE,
                        {
                            "confirmed": inp.soc is not None
                            and round(proj, 1) == round(inp.soc, 1),
                            "reason": "target_reached",
                            "projected_soc": proj,
                            "target_soc": inp.target_soc,
                            "energy_kwh": state.session_energy_kwh,
                        },
                    )
                )
                state = _mark_complete(state, inp.now)
            plug = PlugAction.OFF
            reason = "target_reached"
        elif (
            inp.mode == ChargeMode.SMART
            and inp.soc is not None
            and inp.soc < inp.target_soc
            and not manual_off_active
            and not completed_this_window
        ):
            plug = PlugAction.ON
            state = replace(state, plug_turned_on_by=Owner.US)
            reason = "below_target"
        else:
            plug = PlugAction.UNCHANGED
            reason = "in_window_no_action"
    else:
        # ---- 5. outside window ----
        if inp.ha_start_edge and inp.plug_switch_on and state.plug_turned_on_by == Owner.UNKNOWN:
            # A restart loses in-memory ownership. Outside the
            # window, a plug that is on with unknown ownership might be a
            # charge WE started that would otherwise run unbounded -- the
            # exact failure R1 exists to prevent, reached by a different
            # road. Cannot tell that apart from a human's deliberate
            # outside-window manual charge, so fail closed rather than
            # silently trust it: cut it and say so, once, on this first
            # post-restart evaluation only.
            events.append(Event(EVENT_RESTART_RECONCILED_OFF, {}))
            state = replace(state, plug_turned_on_by=Owner.UNKNOWN)
            plug = PlugAction.OFF
            reason = "restart_reconcile_unknown_owner"
        elif inp.window_close_edge and inp.plug_switch_on:
            if not state.complete_notified:
                shortfall = None
                if inp.mode == ChargeMode.SMART and proj is not None:
                    shortfall = max(0.0, inp.target_soc - proj)
                events.append(
                    Event(
                        EVENT_WINDOW_SHORTFALL,
                        {
                            "mode": inp.mode.value,
                            "projected_soc": proj,
                            "target_soc": inp.target_soc,
                            "shortfall": shortfall,
                            "energy_kwh": state.session_energy_kwh,
                        },
                    )
                )
                state = _mark_complete(state, inp.now)
            plug = PlugAction.OFF
            reason = "window_close"
        elif state.plug_turned_on_by == Owner.US and inp.plug_switch_on:
            plug = PlugAction.OFF
            reason = "outside_window_we_own_it"
        else:
            plug = PlugAction.UNCHANGED
            reason = "outside_window_not_ours"

    return _finalize(state, inp, charging_active, car_charging, plug), Decision(
        plug=plug,
        force_disable=False,
        events=tuple(events),
        projected_soc=proj,
        silence_minutes=silence,
        charge_source=charge_source,
        charging_active=charging_active,
        plug_delivering=plug_delivering,
        soc_stale=soc_stale,
        request_refresh=request_refresh,
        reason=reason,
    )


def _finalize(
    state: SessionState,
    inp: Inputs,
    charging_active: bool,
    car_charging: bool,
    plug: PlugAction = PlugAction.UNCHANGED,
) -> SessionState:
    """Record this tick's raw values for next call's edge detection.

    That includes `plug`: the plug's observed state lags the command by a
    tick, so without recording that WE asked for the off, the next tick
    sees an off on a plug we owned and cannot tell it from a human's.
    """
    return replace(
        state,
        own_off_commanded_at=(
            inp.now if plug is PlugAction.OFF else state.own_off_commanded_at
        ),
        prev_plug_switch_on=inp.plug_switch_on,
        prev_charging_active=charging_active,
        prev_car_charging=car_charging,
        prev_soc=inp.soc,
        prev_soc_changed_at=inp.soc_changed_at,
        prev_mode=inp.mode,
    )
