"""Self-calibrating charge-rate model, with the stop protected against a
fast-biased learned rate.

The projection that stops the charge (logic.projected_soc) is
`max(reading_proj, session_proj)`, and both terms use the SAME rate. That
means whichever rate is fed in decides the stop on a healthy night -- if
learning is allowed to make that rate faster than reality, the charge stops
*early*. Quantified against R1 (790 minutes of silence): a learned rate at
a naive 0.6x-of-seed clamp floor stops the charge 26 percentage points
short. See the port plan section 5 for the full derivation.

The fix is structural, not a tighter clamp: `effective_rate(...)` always
returns max(learned, seed), so learning can only ever push the projection
-- and therefore the stop -- LATER, never earlier. The clamp band still
exists as a belt-and-braces bound against a wild sample, but the safety
property does not depend on it.

Zero Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .const import (
    RATE_MODEL_CLAMP_HIGH,
    RATE_MODEL_CLAMP_LOW,
    RATE_MODEL_MIN_SAMPLES,
    RATE_MODEL_MIN_SESSION_MINUTES,
    RATE_MODEL_MIN_SOC_GAIN,
    RATE_MODEL_SAMPLE_WINDOW,
    SESSION_CAP_MARGIN,
)
from .models import RateSnapshot, SessionState


def seed_rate(capacity_kwh: float, power_kw: float, efficiency: float) -> float:
    """Minutes per 1% SoC from configured (or observed-average) numbers.
    Ported verbatim from sensor.ev_minutes_per_percent
    (packages/ev_charging.yaml:484-495): the seed is also the permanent
    FLOOR under the learned rate (see effective_rate below), so a fresh
    install with zero sessions behaves exactly like the YAML's
    float(20.2) default.
    """
    if power_kw <= 0 or efficiency <= 0:
        raise ValueError("power_kw and efficiency must be positive")
    return (capacity_kwh / 100.0) / (power_kw * efficiency) * 60.0


def learned_rate(samples: tuple[float, ...]) -> float | None:
    """Median of the most recent accepted samples, or None if there are not
    enough yet to trust (RATE_MODEL_MIN_SAMPLES)."""
    if len(samples) < RATE_MODEL_MIN_SAMPLES:
        return None
    recent = samples[-RATE_MODEL_SAMPLE_WINDOW:]
    ordered = sorted(recent)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def clamp(rate: float, seed: float) -> float:
    """Belt-and-braces bound against a wild stored sample. Not the
    mechanism that keeps the stop safe -- effective_rate's max(learned,
    seed) is -- but bounds how far a single bad accepted sample can drift
    the *reading* projection before the next healthy sample corrects it."""
    return min(max(rate, seed * RATE_MODEL_CLAMP_LOW), seed * RATE_MODEL_CLAMP_HIGH)


def effective_rate(samples: tuple[float, ...], seed: float) -> RateSnapshot:
    """The rate used for BOTH projection terms (plan section 5). Always
    >= seed: learning can only make the projection -- and therefore the
    stop -- run later, never earlier. This property is asserted directly
    in tests/test_rate_model.py for every accepted sample sequence.
    """
    learned = learned_rate(samples)
    if learned is None:
        return RateSnapshot(minutes_per_percent=seed)
    return RateSnapshot(minutes_per_percent=max(clamp(learned, seed), seed))


def solve_anchor_correction(
    fresh_soc: float,
    charge_started_at: datetime,
    now: datetime,
    rate: RateSnapshot,
) -> float:
    """D4: back-solve the virtual start SoC that reproduces `fresh_soc` at
    `now`, given the elapsed time so far -- moves the anchor VALUE without
    moving the CLOCK. Clamped -30..130 (packages/ev_charging.yaml:185-187).
    Uses the same cap_rate (rate x margin) as logic.projected_soc's session
    term, or the correction would not reproduce the reading there.
    """
    cap_rate = rate.minutes_per_percent * SESSION_CAP_MARGIN
    elapsed_min = (now - charge_started_at).total_seconds() / 60.0
    solved = fresh_soc - elapsed_min / cap_rate
    return min(max(solved, -30.0), 130.0)


@dataclass(frozen=True)
class CompletedSession:
    """What the coordinator hands to accept_session() when a session ends."""

    duration_minutes: float
    soc_gained: float
    stayed_on_plug: bool
    ran_above_target: bool
    anchor_provisional_unresolved: bool
    stop_reason: str  # "target_reached" | "power_drop" | "window_close" | "fault"


def accept_session(session: CompletedSession) -> bool:
    """Sample acceptance rules (plan section 5). A session contributes a
    rate sample only if all of these hold -- each guards against a specific
    way a bad sample would corrupt the stop:

    - non-provisional/corrected anchor: an unresolved provisional anchor
      means we do not actually know when charging started (D4).
    - >= 45 min: short sessions are dominated by measurement noise.
    - >= 10 points of SoC gain: same reason, for the numerator.
    - never ran above target: the linear rate model is invalid in the
      constant-voltage taper (docs section 6.4); learning from a taper
      session would bias the rate slow for the normal below-target range
      it is actually used for -- the opposite failure from the one this
      module exists to prevent, but still wrong.
    - stayed on the plug throughout: a bypass session's energy figures
      describe the wall socket, not what the plug delivered (D7).
    - ended on target-reached or power-drop, not window-close or a fault:
      those endings don't tell you the car's real rate, just where the
      clock or a safety cutoff happened to land.
    """
    if session.anchor_provisional_unresolved:
        return False
    if session.duration_minutes < RATE_MODEL_MIN_SESSION_MINUTES:
        return False
    if session.soc_gained < RATE_MODEL_MIN_SOC_GAIN:
        return False
    if session.ran_above_target:
        return False
    if not session.stayed_on_plug:
        return False
    if session.stop_reason not in ("target_reached", "power_drop"):
        return False
    return True


def sample_rate(session: CompletedSession) -> float:
    """The observed minutes-per-percent for one accepted session."""
    return session.duration_minutes / session.soc_gained


def record_session(state: SessionState, session: CompletedSession) -> SessionState:
    """Append a sample if the session qualifies; otherwise return state
    unchanged. Called by the coordinator when it detects the session has
    ended (complete_notified went from False to True with a real stop
    reason)."""
    if not accept_session(session):
        return state
    sample = sample_rate(session)
    samples = state.rate_samples + (sample,)
    # Keep only what learned_rate() will ever look at, plus a little slack,
    # so the buffer does not grow unboundedly over a car's lifetime.
    samples = samples[-(RATE_MODEL_SAMPLE_WINDOW * 4) :]
    return replace(state, rate_samples=samples)


def reset_samples(state: SessionState) -> SessionState:
    """Called on a capacity-config change (almost always a different car)
    or from the "reset rate learning" button. Stale samples plus a new seed
    would give a clamped-but-wrong learned rate and a discontinuous cap."""
    return replace(state, rate_samples=())
