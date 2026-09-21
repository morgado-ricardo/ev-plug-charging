"""Session-anchor bookkeeping: new-session detection, the matched-pair
anchor capture, and its one-time backwards correction.

Split out from logic.py because getting the anchor wrong silently corrupts
everything downstream of it: R8 and R11 both hinge entirely on this file,
and the session-cap projection term is meaningless without it.

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
    # -- new-session detection. Level-based, so "did either signal edge
    # this tick" is two booleans rather than a race between two
    # simultaneous triggers. --
    if plug_on_edge or charging_active_edge:
        # A plug-on always starts a new session; a charging-active edge
        # only does if the PREVIOUS session already finished
        # (complete_notified).
        # This is exactly what stops a mid-session flicker (R11)
        # from wiping the night's kWh and re-arming a second completion
        # push -- charging_active going off and back on with the plug still
        # on is NOT plug_on_edge, and complete_notified is still False from
        # the ongoing session, so new_session is False and nothing resets.
        new_session = plug_on_edge or state.complete_notified
        if new_session:
            state = replace(
                state,
                session_energy_kwh=0.0,
                session_saw_power=False,
                complete_notified=False,
            )

    # -- anchor capture: only on the instant current actually starts
    # flowing, never on plug-on -- a cable can sit connected for an hour
    # first. The staleness half deliberately uses the RAW age of the
    # reading, NOT the silence clock: the silence clock reads zero by
    # construction the moment charging starts, so it would answer "fresh"
    # every single time and the provisional path would never fire. --
    if charging_active_edge:
        anchor_soc = inp.soc if inp.soc is not None else 100.0  # fail closed
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

    # -- anchor correction: exactly once per session, on the first
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
