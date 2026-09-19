"""Session-anchor bookkeeping: new-session detection, the matched-pair
anchor capture, and its one-time backwards correction.

Split out from logic.py because this is the single largest omission a first
pass at porting the YAML tends to make: R8 and R11 both hinge entirely on
it, and D2's session-cap term is meaningless without it. See plan section
3.1 and docs/ev-charging-requirements.md D3, D4, D9.

Zero Home Assistant imports, same as logic.py -- this is unit-tested on its
own in tests/test_session.py.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from .rate_model import solve_anchor_correction
from .models import Inputs, SessionAnchor, SessionState


def advance_session(
    state: SessionState,
    inp: Inputs,
    *,
    charging_active: bool,
    plug_on_edge: bool,
    charging_active_edge: bool,
    fresh_reading_edge: bool,
) -> SessionState:
    """Run new-session detection and anchor capture/correction. Must run
    before the plug decision on every reduce() call.
    """
    # -- new-session detection (D9's mode:single race no longer exists: this
    # is level-based, so "did either signal edge this tick" is just two
    # booleans, not a coin toss between simultaneous triggers) --
    if plug_on_edge or charging_active_edge:
        # Ported in spirit from packages/ev_charging.yaml:1400-1401: a
        # plug-on always starts a new session; a charging-active edge only
        # does if the PREVIOUS session already finished (complete_notified).
        # This is exactly what stops an ev_charging_active flicker (R11)
        # from wiping the night's kWh and re-arming a second completion
        # push -- charging_active going off and back on with the plug still
        # on is NOT plug_on_edge, and complete_notified is still False from
        # the ongoing session, so new_session is False and nothing resets.
        new_session = plug_on_edge or state.complete_notified
        if new_session:
            state = replace(state, session_energy_kwh=0.0, complete_notified=False)

    # -- anchor capture (D3, D4): only on the instant current actually
    # starts flowing, never on plug-on. The SoC half is deliberately the
    # RAW age of the reading (D3's warning against "fixing" this to use the
    # silence clock, which reads zero by construction the moment charging
    # starts and would answer "fresh" every time). --
    if charging_active_edge:
        anchor_soc = inp.soc if inp.soc is not None else 100.0  # D8 fail-closed default
        anchor_stale = (
            inp.soc is None
            or inp.soc_changed_at is None
            or (inp.now - inp.soc_changed_at) >= timedelta(minutes=inp.expected_gap_min)
        )
        state = replace(
            state,
            charge_started_at=inp.now,
            anchor=SessionAnchor(
                soc=anchor_soc, captured_at=inp.now, provisional=anchor_stale, corrected=False
            ),
            rescue_wakeup_used=False,
            soc_stale_notified=False,
        )

    # -- anchor correction (D4): exactly once per session, on the first
    # fresh reading AFTER a provisional capture. Moves the VALUE, never the
    # CLOCK, so the session cap still bounds the same real-world duration.
    #
    # Must NOT run on the same tick as the capture above: on session.py's
    # very first-ever call, state.prev_soc_changed_at is None, so ANY
    # reading -- including the same stale one the capture above just used
    # -- looks like a "fresh_reading_edge" (None != anything). Without the
    # `not charging_active_edge` guard, that stale reading would
    # "correct" the anchor against itself in the same tick, satisfying
    # nothing: the correction is only meaningful once a reading that
    # postdates the capture arrives. --
    if (
        not charging_active_edge
        and state.anchor.provisional
        and not state.anchor.corrected
        and charging_active
        and inp.soc is not None
        and fresh_reading_edge
        and state.charge_started_at is not None
    ):
        corrected = solve_anchor_correction(inp.soc, state.charge_started_at, inp.now, inp.rate)
        state = replace(
            state,
            anchor=replace(state.anchor, soc=round(corrected), provisional=False, corrected=True),
        )

    return state
