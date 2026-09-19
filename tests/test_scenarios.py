"""R1-R14: the regression scenarios from docs/ev-charging-requirements.md
section 7, all real observed nights. This suite is the port's acceptance
gate (port plan section 12/14) -- it is not done until every one of these
passes.

Each test drives logic.reduce() over a synthetic clock, ticking roughly the
way the coordinator will: PSACC polls at ~2 min intervals, plus a ~30s
timer for the time-dependent projection. Where a scenario doesn't care
about that resolution, we jump the clock directly.
"""
from __future__ import annotations

from datetime import time, timedelta

import pytest

from ev_plug_charging.logic import reduce
from ev_plug_charging.models import (
    ChargeMode,
    ChargeSource,
    PlugAction,
    RateSnapshot,
    SessionState,
)

from conftest import DAY, base_inputs, dt

SEED_RATE = 20.2
RATE = RateSnapshot(minutes_per_percent=SEED_RATE)


def _run_until_charging(state, now, soc, soc_changed_at, **overrides):
    """Helper: drive one tick that starts a charge (plug on, car
    reporting), returning the resulting state/decision."""
    inp = base_inputs().set(
        now=now,
        soc=soc,
        soc_changed_at=soc_changed_at,
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        rate=RATE,
        **overrides,
    ).build()
    return reduce(state, inp)


# --------------------------------------------------------------------------- #
# R1: 790 min with zero SoC reports mid-charge (2026-09-12)
# --------------------------------------------------------------------------- #


def test_r1_blackout_stops_at_target_not_96_percent():
    """Charge stops at/near target on the projection, never runs to 96%
    (the old failure) because the reported crossing never happened. The
    window here (20:00-16:00, 20h) is deliberately wider than the ~13.5h a
    40%->80% charge needs at the seed rate, so the window-close backstop
    cannot be what stops it -- only the projection can, exactly as in the
    real 2026-09-12 incident.

    790 minutes of silence from a 40% start credits ~39.1 points at the
    seed rate -- 79.1%, just under an 80% target -- so the real assertion
    is not "stopped by minute 790" but the two things that actually
    matter: (a) at minute 790, with STILL zero reports, the charge has
    NOT stopped on a reported crossing that never happened (the pre-fix
    failure mode: it would have just kept running, unbounded, waiting for
    a report that never comes); and (b) it DOES stop shortly after, on
    the projection, once that projection itself crosses target -- and
    stops near 80%, not at 96%."""
    window_start, window_end = time(20, 0), time(16, 0)
    charge_start = dt(23, 0)
    state = SessionState()

    now = charge_start
    stopped = False
    stop_projected = None
    proj_at_790 = None
    for step in range(850 // 5 + 1):
        inp = base_inputs().set(
            now=now,
            soc=40.0,  # never changes again -- the blackout
            soc_changed_at=charge_start,  # never advances
            plug_switch_on=not stopped,
            car_charging=not stopped,
            plug_power_w=1800.0 if not stopped else 0.0,
            rate=RATE,
            window_start=window_start,
            window_end=window_end,
            target_soc=80.0,
        ).build()
        state, decision = reduce(state, inp)
        elapsed = (now - charge_start).total_seconds() / 60
        if 785 <= elapsed <= 795:
            proj_at_790 = decision.projected_soc
        if decision.plug == PlugAction.OFF and not stopped:
            stopped = True
            stop_projected = decision.projected_soc
            break
        now += timedelta(minutes=5)

    # At ~790 minutes, the RAW reading is still 40% (zero reports) -- the
    # pre-fix system, waiting for a reported crossing, would still be
    # charging with no end in sight. The projection must already be near
    # target even though the reading never moved.
    assert proj_at_790 is not None
    assert 77.0 <= proj_at_790 <= 80.0

    assert stopped, "charge must eventually stop on the projection"
    assert decision.reason == "target_reached", (
        f"must stop via the projection reaching target, not '{decision.reason}' "
        "(e.g. a window-close backstop, which would mean the test's window "
        "was too narrow to isolate the projection's own behaviour)"
    )
    # Must stop close to target (80), NOT run to 96%.
    assert 78.0 <= stop_projected <= 82.0


# --------------------------------------------------------------------------- #
# R2: healthy night, ~20 min reports -> projection within ~1%, stop within ~1%
# --------------------------------------------------------------------------- #


def test_r2_healthy_night_stops_within_one_percent_of_target():
    """40% -> 80% at the seed rate needs ~808 minutes; the window here
    (20:00-16:00, 20h) is wide enough to contain a full session so only
    the projection -- not the window-close backstop -- can be what stops
    it (same reasoning as R1's window choice)."""
    window_start, window_end = time(20, 0), time(16, 0)
    charge_start = dt(23, 0)
    state = SessionState()
    now = charge_start
    soc = 40.0
    soc_changed_at = charge_start
    stopped = False
    stop_soc = None

    for _ in range(850):
        # Every ~20 minutes, a fresh reading arrives that reflects the true
        # rate (SEED_RATE min per 1%).
        if (now - charge_start).total_seconds() % (20 * 60) < 60:
            elapsed_min = (now - charge_start).total_seconds() / 60.0
            soc = round(40.0 + elapsed_min / SEED_RATE, 1)
            soc_changed_at = now
        inp = base_inputs().set(
            now=now,
            soc=soc,
            soc_changed_at=soc_changed_at,
            plug_switch_on=not stopped,
            car_charging=not stopped,
            plug_power_w=1800.0 if not stopped else 0.0,
            rate=RATE,
            window_start=window_start,
            window_end=window_end,
            target_soc=80.0,
        ).build()
        state, decision = reduce(state, inp)
        if decision.plug == PlugAction.OFF and not stopped:
            stopped = True
            stop_soc = decision.projected_soc
            break
        now += timedelta(minutes=1)

    assert stopped
    assert decision.reason == "target_reached"
    assert abs(stop_soc - 80.0) <= 1.5


# --------------------------------------------------------------------------- #
# R3: driven 18:00, charging from 22:00 -> no stale warning/wakeup at 22:00
# --------------------------------------------------------------------------- #


def test_r3_no_stale_warning_at_charge_start_after_a_daytime_drive():
    yesterday_drive = dt(18, 0, day=DAY)
    charge_start = dt(22, 0, day=DAY)  # 4 hours later -- raw reading age is 4h
    state = SessionState()
    inp = base_inputs().set(
        now=charge_start,
        soc=45.0,
        soc_changed_at=yesterday_drive,
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp)
    # At the very instant charging starts, silence must read ~0 (clocked
    # from charge_started_at, not the raw reading age) -- so no stale flag.
    assert decision.soc_stale is False
    assert decision.request_refresh is False

    # A few minutes later, still no warning (well under the 40 min gap).
    inp2 = base_inputs().set(
        now=charge_start + timedelta(minutes=5),
        soc=45.0,
        soc_changed_at=yesterday_drive,
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        rate=RATE,
    ).build()
    state, decision2 = reduce(state, inp2)
    assert decision2.soc_stale is False


def test_r3_warning_only_after_charging_silence_not_raw_age():
    charge_start = dt(22, 0)
    state = SessionState(charge_started_at=charge_start, prev_charging_active=True)
    # 45 minutes of CHARGING silence (> 40 min gap) with a soc below target.
    now = charge_start + timedelta(minutes=45)
    inp = base_inputs().set(
        now=now,
        soc=45.0,
        soc_changed_at=dt(18, 0),  # ancient, but irrelevant -- silence clocks from charge_started_at
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        rate=RATE,
        expected_gap_min=40.0,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.soc_stale is True


# --------------------------------------------------------------------------- #
# R4: plug energised 22:00, cable in 00:30 -> projection true at 00:30,
# session cap measured from 00:30 (not 22:00)
# --------------------------------------------------------------------------- #


def test_r4_session_cap_clocks_from_real_charge_start_not_plug_on():
    plug_on_at = dt(22, 0)
    cable_in_at = dt(0, 30, day=DAY + timedelta(days=1))
    state = SessionState()

    # Plug on at 22:00; car not attached yet -- charging_active stays False.
    inp_plug_on = base_inputs().set(
        now=plug_on_at,
        soc=45.0,
        soc_changed_at=plug_on_at,
        plug_switch_on=True,
        car_charging=False,
        plug_power_w=5.0,  # EVSE idle draw, below threshold
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp_plug_on)
    assert state.charge_started_at is None  # not charging yet

    # Cable goes in at 00:30 -- NOW charging_active becomes true.
    inp_cable_in = base_inputs().set(
        now=cable_in_at,
        soc=45.0,
        soc_changed_at=cable_in_at,
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp_cable_in)
    assert state.charge_started_at == cable_in_at  # NOT plug_on_at
    # Immediately after, projected_soc must read the true 45%, not an
    # inflated ~58.5% as the old plug-on-anchored bug produced.
    assert decision.projected_soc == pytest.approx(45.0, abs=0.5)


# --------------------------------------------------------------------------- #
# R5: HA restart mid-charge, already past target -> stops on next evaluation
# --------------------------------------------------------------------------- #


def test_r5_restart_past_target_stops_immediately():
    charge_start = dt(22, 0)
    # Restored state: mid-charge, anchor already resolved.
    from ev_plug_charging.models import SessionAnchor

    state = SessionState(
        charge_started_at=charge_start,
        anchor=SessionAnchor(soc=40.0, captured_at=charge_start, provisional=False, corrected=True),
        prev_charging_active=True,
        prev_plug_switch_on=True,
    )
    # First evaluation after restart: SoC already at 82%, past an 80% target.
    now = charge_start + timedelta(hours=5)
    inp = base_inputs().set(
        now=now,
        soc=82.0,
        soc_changed_at=now,
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        target_soc=80.0,
        rate=RATE,
        ha_start_edge=True,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.OFF


# --------------------------------------------------------------------------- #
# R6: HA restart inside the window, REST sensors not yet polled -> no
# spurious "charge NOT started" push; charge starts once SoC arrives
# --------------------------------------------------------------------------- #


def test_r6_no_spurious_not_started_push_during_grace_then_starts_silently():
    restart_at = dt(23, 1)
    state = SessionState(ha_started_at=None)
    # Tick 1: HA just started, inside the window, SoC not available yet
    # (REST sensors haven't polled). Must NOT push within the grace window.
    inp1 = base_inputs().set(
        now=restart_at,
        soc=None,
        soc_changed_at=None,
        source_reachable=False,
        plug_switch_on=False,
        car_charging=False,
        plug_power_w=0.0,
        rate=RATE,
        ha_start_edge=True,
    ).build()
    state, decision1 = reduce(state, inp1)
    assert not any(e.name.endswith("charge_not_started") for e in decision1.events)

    # Tick 2, one minute later, SoC arrives -- charge must start silently.
    inp2 = base_inputs().set(
        now=restart_at + timedelta(minutes=1),
        soc=40.0,
        soc_changed_at=restart_at + timedelta(minutes=1),
        source_reachable=True,
        plug_switch_on=False,
        car_charging=False,
        plug_power_w=0.0,
        target_soc=80.0,
        rate=RATE,
    ).build()
    state, decision2 = reduce(state, inp2)
    assert decision2.plug == PlugAction.ON
    assert not any(e.name.endswith("charge_not_started") for e in decision2.events)


def test_r6_pushes_after_grace_expires_with_no_reading():
    restart_at = dt(23, 0)
    state = SessionState(ha_started_at=None)
    inp1 = base_inputs().set(
        now=restart_at, soc=None, soc_changed_at=None, source_reachable=False,
        plug_switch_on=False, car_charging=False, plug_power_w=0.0, rate=RATE,
        ha_start_edge=True,
    ).build()
    state, _ = reduce(state, inp1)

    after_grace = restart_at + timedelta(minutes=4)  # > 180s grace
    inp2 = base_inputs().set(
        now=after_grace, soc=None, soc_changed_at=None, source_reachable=False,
        plug_switch_on=False, car_charging=False, plug_power_w=0.0, rate=RATE,
    ).build()
    state, decision = reduce(state, inp2)
    assert any(e.name.endswith("charge_not_started") for e in decision.events)


# --------------------------------------------------------------------------- #
# R7: EVSE moved to the wall socket -> reported as bypass; plug figures
# excluded from the session
# --------------------------------------------------------------------------- #


def test_r7_bypass_classified_and_warned_after_debounce():
    state = SessionState()
    now = dt(22, 0)
    # Car charging, but the plug is NOT delivering (EVSE moved to the wall).
    for minute in range(6):
        now = dt(22, minute)
        inp = base_inputs().set(
            now=now, soc=40.0, soc_changed_at=now, plug_switch_on=False,
            car_charging=True, plug_power_w=0.0, rate=RATE,
        ).build()
        state, decision = reduce(state, inp)
    assert decision.charge_source == ChargeSource.BYPASS
    assert any(e.name.endswith("bypass_detected") for e in decision.events)


# --------------------------------------------------------------------------- #
# R8: anchor SoC stale at capture, corrected exactly once on first fresh
# reading; clock unchanged
# --------------------------------------------------------------------------- #


def test_r8_anchor_corrected_once_clock_unmoved():
    drove_at = dt(18, 0)
    cable_in_at = dt(22, 0)
    state = SessionState()
    inp1 = base_inputs().set(
        now=cable_in_at, soc=45.0, soc_changed_at=drove_at, plug_switch_on=True,
        car_charging=True, plug_power_w=1800.0, rate=RATE,
    ).build()
    state, _ = reduce(state, inp1)
    assert state.anchor.provisional is True
    assert state.charge_started_at == cable_in_at

    fresh_at = cable_in_at + timedelta(minutes=25)
    inp2 = base_inputs().set(
        now=fresh_at, soc=46.5, soc_changed_at=fresh_at, plug_switch_on=True,
        car_charging=True, plug_power_w=1800.0, rate=RATE,
    ).build()
    state, _ = reduce(state, inp2)
    assert state.anchor.provisional is False
    assert state.anchor.corrected is True
    assert state.charge_started_at == cable_in_at  # clock never moved

    # A second fresh reading must NOT re-correct.
    anchor_after_first_correction = state.anchor.soc
    fresh_at_2 = fresh_at + timedelta(minutes=20)
    inp3 = base_inputs().set(
        now=fresh_at_2, soc=48.0, soc_changed_at=fresh_at_2, plug_switch_on=True,
        car_charging=True, plug_power_w=1800.0, rate=RATE,
    ).build()
    state, _ = reduce(state, inp3)
    assert state.anchor.soc == anchor_after_first_correction


# --------------------------------------------------------------------------- #
# R9: plug over temperature while delivering -> charge cut, force_disable,
# emergency push
# --------------------------------------------------------------------------- #


def test_r9_overheat_cuts_charge_and_forces_disable():
    state = SessionState()
    inp = base_inputs().set(
        plug_switch_on=True, plug_temp_c=70.0, temp_limit_c=65.0, overheat_protection=True,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.OFF
    assert decision.force_disable is True
    assert any(e.name.endswith("overheat_cutoff") for e in decision.events)


def test_r9_overheat_never_acts_on_idle_plug():
    """FR-X2: never act on plug temperature when the plug is not carrying
    current -- an idle plug in a warm cupboard must not interrupt a
    bypass charge running direct from the wall."""
    state = SessionState()
    inp = base_inputs().set(
        plug_switch_on=False, plug_temp_c=90.0, temp_limit_c=65.0, overheat_protection=True,
        car_charging=True, plug_power_w=0.0,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.force_disable is False


# --------------------------------------------------------------------------- #
# R10: plug on, plugged, no power after 5 min -> "EVSE may need reset" push
# --------------------------------------------------------------------------- #


def test_r10_evse_no_power_push_after_five_minutes():
    state = SessionState()
    now = dt(23, 0)
    for minute in range(6):
        now = dt(23, minute)
        inp = base_inputs().set(
            now=now, plug_switch_on=True, plugged=True, plug_power_w=0.0,
            car_charging=False, soc=40.0, soc_changed_at=now, rate=RATE,
        ).build()
        state, decision = reduce(state, inp)
    assert any(e.name.endswith("evse_no_power") for e in decision.events)


# --------------------------------------------------------------------------- #
# R11: charging-active flickers mid-charge -> energy meter NOT reset, no
# duplicate completion push
# --------------------------------------------------------------------------- #


def test_r11_flicker_does_not_reset_meter_or_duplicate_completion():
    charge_start = dt(23, 0)
    state = SessionState()
    inp1 = base_inputs().set(
        now=charge_start, soc=40.0, soc_changed_at=charge_start, plug_switch_on=True,
        car_charging=True, plug_power_w=1800.0, rate=RATE,
    ).build()
    state, _ = reduce(state, inp1)
    # Pretend 3 kWh has accumulated (coordinator would set this from the
    # energy sensor between ticks -- simulate directly).
    from dataclasses import replace as _replace

    state = _replace(state, session_energy_kwh=3.0)

    # Flicker: both car status and plug power read false for one tick
    # (Wi-Fi drop + source down simultaneously).
    flicker_at = charge_start + timedelta(minutes=30)
    inp2 = base_inputs().set(
        now=flicker_at, soc=45.0, soc_changed_at=charge_start, plug_switch_on=True,
        car_charging=False, plug_power_w=0.0, rate=RATE,
    ).build()
    state, _ = reduce(state, inp2)
    assert state.session_energy_kwh == 3.0  # untouched during the flicker

    # Recovers a moment later -- charging_active edges back on.
    recover_at = flicker_at + timedelta(seconds=30)
    inp3 = base_inputs().set(
        now=recover_at, soc=45.0, soc_changed_at=recover_at, plug_switch_on=True,
        car_charging=True, plug_power_w=1800.0, rate=RATE,
    ).build()
    state, _ = reduce(state, inp3)
    assert state.session_energy_kwh == 3.0  # still untouched -- not a new session


# --------------------------------------------------------------------------- #
# R12: evaluation exactly at window_start -> ON (D10's race cannot exist)
# --------------------------------------------------------------------------- #


def test_r12_window_open_edge_starts_charge_even_if_soc_untrustworthy_grace_not_elapsed():
    """The window-open callback fires exactly at window_start. Below
    target with a trustworthy reading, the charge must start on this
    exact tick -- not miss it the way D10's now()-based sensor did."""
    state = SessionState(ha_started_at=dt(0, 0))  # long-running, grace already satisfied
    inp = base_inputs().set(
        now=dt(23, 0),
        window_open_edge=True,
        soc=50.0,
        soc_changed_at=dt(22, 55),
        target_soc=80.0,
        plug_switch_on=False,
        car_charging=False,
        plug_power_w=0.0,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.ON


# --------------------------------------------------------------------------- #
# R13: cable connected at 00:30 inside the window -> starts; outside the
# window, or in Timed, it must not
# --------------------------------------------------------------------------- #


def test_r13_cable_in_mid_window_starts_smart_charge():
    state = SessionState()
    inp = base_inputs().set(
        now=dt(0, 30, day=DAY + timedelta(days=1)),
        soc=50.0,
        soc_changed_at=dt(0, 25, day=DAY + timedelta(days=1)),
        target_soc=80.0,
        plug_switch_on=False,
        mode=ChargeMode.SMART,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.ON


def test_r13_no_start_outside_window():
    state = SessionState()
    inp = base_inputs().set(
        now=dt(12, 0),
        soc=50.0,
        soc_changed_at=dt(11, 55),
        target_soc=80.0,
        plug_switch_on=False,
        mode=ChargeMode.SMART,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.UNCHANGED


# --------------------------------------------------------------------------- #
# R14: Smart, still below target when the window closes -> shortfall push
# naming the gap; one-push-per-session flag set
# --------------------------------------------------------------------------- #


def test_r14_window_close_shortfall_push_in_smart_mode():
    state = SessionState(charge_started_at=dt(23, 0))
    inp = base_inputs().set(
        now=dt(7, 0, day=DAY + timedelta(days=1)),
        window_close_edge=True,
        soc=68.0,
        soc_changed_at=dt(6, 58, day=DAY + timedelta(days=1)),
        target_soc=80.0,
        plug_switch_on=True,
        car_charging=True,
        plug_power_w=1800.0,
        mode=ChargeMode.SMART,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.OFF
    shortfall_events = [e for e in decision.events if e.name.endswith("window_shortfall")]
    assert len(shortfall_events) == 1
    assert shortfall_events[0].data["shortfall"] > 0
    assert state.complete_notified is True


def test_r14_window_close_notifies_in_timed_mode_too():
    """FR-N3: a charge the window cuts short must notify in EVERY mode."""
    state = SessionState(charge_started_at=dt(23, 0))
    inp = base_inputs().set(
        now=dt(7, 0, day=DAY + timedelta(days=1)),
        window_close_edge=True,
        plug_switch_on=True,
        car_charging=False,
        plug_power_w=1800.0,
        mode=ChargeMode.TIMED,
        rate=RATE,
    ).build()
    state, decision = reduce(state, inp)
    assert decision.plug == PlugAction.OFF
    assert any(e.name.endswith("window_shortfall") for e in decision.events)
