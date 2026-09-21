"""Self-calibrating charge-rate model, with the stop protected against a
fast-biased learned rate.

The projection that stops the charge (logic.projected_soc) is
`max(reading_proj, session_proj)`, and both terms use the SAME rate. That
means whichever rate is fed in decides the stop on a healthy night -- if
learning is allowed to make that rate faster than reality, the charge stops
*early*. Quantified against R1 (790 minutes of silence): a learned rate at
a naive 0.6x-of-seed clamp floor stops the charge 26 percentage points
short. That is not a hypothetical; it happened.

The fix is structural, not a tighter clamp: `effective_rate(...)` always
returns max(learned, seed), so learning can only ever push the projection
-- and therefore the stop -- LATER, never earlier. The clamp band still
exists as a belt-and-braces bound against a wild sample, but the safety
property does not depend on it.

## Efficiency self-calibration (Phase 2)

`seed_rate_from_amps()`'s efficiency argument used to be a number the user
typed in; now it is CONF_EFFICIENCY_PRIOR, a fixed, pessimistic starting
point (see const.DEFAULT_EFFICIENCY_PRIOR), corrected upward by
`working_efficiency()` from real telemetry. That correction points in the
DANGEROUS direction the rest of this module exists to guard against:
raising efficiency shrinks the seed, which makes the projection -- and
therefore the stop -- run EARLIER.

So it gets its own, independent bound, structural in the same way
`effective_rate`'s is: `seed_rate_calibrated()` never returns a seed
implying more than EFFICIENCY_MAX_GAIN times the AS-CONFIGURED effective
power, regardless of what the telemetry claims. Concretely, the stop can
never land more than `1 - 1/1.20 = 16.7%` earlier than the numbers the user
actually entered. Downward movement -- a LOWER calibrated efficiency, a
SLOWER projection -- is unbounded and free, the same asymmetry
`effective_rate` already trusts: it can only ever make the charge safer.

Two independent, cooperating measurements feed this, both gated on the
SAME session-acceptance rule as the rate sample (`accept_session()`) so
there is one gate vocabulary, not two:

- `record_efficiency_sample()`: capacity x (SoC gained) / (energy
  delivered), from a MATCHED PAIR of readings (never session totals --
  see its docstring for why that distinction is load-bearing on a source
  that reports SoC in bursts).
- Measured AC power (`coordinator.py`'s per-tick sampling,
  `measured_ac_power_p90()` here): the plug's own power sensor, which can
  supersede the configured amps once available, independent of the
  efficiency question.

Zero Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .const import (
    EFFICIENCY_DERATE,
    EFFICIENCY_MAX_GAIN,
    EFFICIENCY_MAX_PLAUSIBLE,
    EFFICIENCY_MIN_ENERGY_KWH,
    EFFICIENCY_MIN_PLAUSIBLE,
    EFFICIENCY_MIN_SAMPLES,
    EFFICIENCY_SAMPLE_WINDOW,
    MEASURED_POWER_MIN_SAMPLES,
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
    """Minutes per 1% SoC from the configured capacity/power/efficiency.

    This is also the permanent FLOOR under the learned rate (see
    effective_rate below), so a fresh install with zero sessions behaves
    exactly as if learning were switched off.
    """
    if power_kw <= 0 or efficiency <= 0:
        raise ValueError("power_kw and efficiency must be positive")
    return (capacity_kwh / 100.0) / (power_kw * efficiency) * 60.0


def seed_rate_from_amps(
    capacity_kwh: float, current_a: float, voltage_v: float, efficiency: float
) -> float:
    """Same formula as seed_rate(), starting from what a user actually
    knows (amps) rather than an effective kW figure nobody does. A new
    function, not a change to seed_rate()'s signature -- kept stable for
    the config-entry migration path, which still has old kW/efficiency
    pairs to convert, and for existing callers/tests."""
    return seed_rate(capacity_kwh, current_a * voltage_v / 1000.0, efficiency)


def seed_rate_calibrated(
    capacity_kwh: float,
    configured_current_a: float,
    voltage_v: float,
    prior_efficiency: float,
    measured_power_kw: float | None,
    calibrated_efficiency: float,
) -> float:
    """The full Phase-2 seed: measured AC power (falling back to the
    configured amps when no measurement is available yet) times the
    calibrated efficiency -- bounded to never imply more than
    EFFICIENCY_MAX_GAIN times the AS-CONFIGURED effective power. See the
    module docstring's Phase-2 section for the full argument; this
    function is where that bound is actually enforced, structurally, the
    same way effective_rate() enforces max(learned, seed) rather than
    trusting every caller to apply it.

    `calibrated_efficiency` is expected to be working_efficiency()'s
    output, but this function does not call it directly -- keeping the
    "what is efficiency right now" question (working_efficiency) and the
    "how much can that possibly move the seed" question (this function)
    separate is what makes both independently testable.
    """
    configured_power_kw = configured_current_a * voltage_v / 1000.0
    configured_effective_kw = configured_power_kw * prior_efficiency
    ac_power_kw = (
        measured_power_kw
        if measured_power_kw is not None and measured_power_kw > 0
        else configured_power_kw
    )
    effective_kw = min(
        ac_power_kw * calibrated_efficiency,
        configured_effective_kw * EFFICIENCY_MAX_GAIN,
    )
    return seed_rate(capacity_kwh, effective_kw, 1.0)


def _median(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def learned_rate(samples: tuple[float, ...]) -> float | None:
    """Median of the most recent accepted samples, or None if there are not
    enough yet to trust (RATE_MODEL_MIN_SAMPLES)."""
    if len(samples) < RATE_MODEL_MIN_SAMPLES:
        return None
    return _median(samples[-RATE_MODEL_SAMPLE_WINDOW:])


def clamp(rate: float, seed: float) -> float:
    """Belt-and-braces bound against a wild stored sample. Not the
    mechanism that keeps the stop safe -- effective_rate's max(learned,
    seed) is -- but bounds how far a single bad accepted sample can drift
    the *reading* projection before the next healthy sample corrects it."""
    return min(max(rate, seed * RATE_MODEL_CLAMP_LOW), seed * RATE_MODEL_CLAMP_HIGH)


def effective_rate(samples: tuple[float, ...], seed: float) -> RateSnapshot:
    """The rate used for BOTH projection terms. Always
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
    """Back-solve the virtual start SoC that reproduces `fresh_soc` at
    `now`, given the elapsed time so far -- moves the anchor VALUE without
    moving the CLOCK, so the session cap still bounds the same real
    duration. Clamped -30..130 to keep a wild reading from producing a
    nonsensical anchor.
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
    """Sample acceptance rules. A session contributes a
    rate sample only if all of these hold -- each guards against a specific
    way a bad sample would corrupt the stop:

    - non-provisional/corrected anchor: an unresolved provisional anchor
      means we do not actually know what the SoC was when charging
      started, so the gain is guesswork.
    - >= 45 min: short sessions are dominated by measurement noise.
    - >= 10 points of SoC gain: same reason, for the numerator.
    - never ran above target: the linear rate model is invalid in the
      constant-voltage taper above roughly 80%; learning from a taper
      session would bias the rate slow for the normal below-target range
      it is actually used for -- the opposite failure from the one this
      module exists to prevent, but still wrong.
    - stayed on the plug throughout: a bypass session's energy figures
      describe the wall socket, not what the plug delivered.
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
    would give a clamped-but-wrong learned rate and a discontinuous cap.

    Clears the efficiency buffer too, not just rate_samples -- a capacity
    change invalidates a stored efficiency sample the same way it
    invalidates a rate sample (capacity is in both formulas' numerator).
    The in-flight calibration pair is left alone: it belongs to whatever
    session is currently open, not to the history this button/reconfigure
    is meant to discard."""
    return replace(state, rate_samples=(), efficiency_samples=())


def record_efficiency_sample(
    state: SessionState, session: CompletedSession, capacity_kwh: float
) -> SessionState:
    """Append an efficiency sample if the session qualifies; otherwise
    return state unchanged. Reuses accept_session() VERBATIM -- one
    acceptance vocabulary for both calibrations, not two -- plus two gates
    specific to a paired measurement: the pair must span a real SoC gain
    and a real energy delta, or sensor quantisation dominates the result.

    Deliberately does NOT use session.soc_gained (the anchor-based figure
    record_session() uses) -- it uses the MATCHED (soc, energy) pair in
    `state.calib_soc_first/last` instead. See coordinator.py's per-tick
    snapshot hook and this module's docstring for why: SoC arrives in
    bursts, so pairing against the session's start/end anchor rather than
    the two readings actually measured between would silently mix in
    energy delivered before or after the readings that bound the
    measurement -- exactly wrong for computing efficiency, even though
    it's exactly right for the rate sample, which cares about a duration,
    not an energy total.
    """
    if not accept_session(session):
        return state
    if state.calib_soc_first is None or state.calib_soc_last is None:
        return state
    if state.calib_energy_first is None or state.calib_energy_last is None:
        return state
    soc_gain = state.calib_soc_last - state.calib_soc_first
    energy_gain = state.calib_energy_last - state.calib_energy_first
    if soc_gain < RATE_MODEL_MIN_SOC_GAIN or energy_gain < EFFICIENCY_MIN_ENERGY_KWH:
        return state
    measured = (capacity_kwh * soc_gain / 100.0) / energy_gain
    # Reject outright, never clip -- clipping a mis-scaled sensor's
    # reading to 0.95 would silently accept it as "maximally efficient"
    # instead of discarding the bad data point it actually is.
    if not (EFFICIENCY_MIN_PLAUSIBLE <= measured <= EFFICIENCY_MAX_PLAUSIBLE):
        return state
    samples = state.efficiency_samples + (measured,)
    samples = samples[-(EFFICIENCY_SAMPLE_WINDOW * 4) :]
    return replace(state, efficiency_samples=samples)


def working_efficiency(samples: tuple[float, ...], prior: float) -> float:
    """The efficiency actually used for the seed (via
    seed_rate_calibrated()). Below EFFICIENCY_MIN_SAMPLES accepted
    measurements, always the prior -- self-calibration needs corroborating
    evidence before it is allowed to move anything.

    Above that: the median of the recent window, derated for
    conservatism. No floor at the prior here -- unlike effective_rate()'s
    max(learned, seed), a LOWER working efficiency is the SAFE direction
    (it shrinks the seed's implied power, which makes the projection
    slower), so it is left free to move down as far as the plausibility
    band allows. Only the upward direction is bounded, and it is bounded
    at the seed level (seed_rate_calibrated's EFFICIENCY_MAX_GAIN cap),
    not here -- this function's job is "what does the evidence say",
    not "how far is the evidence allowed to move things".
    """
    if len(samples) < EFFICIENCY_MIN_SAMPLES:
        return prior
    recent = samples[-EFFICIENCY_SAMPLE_WINDOW:]
    return _median(recent) * EFFICIENCY_DERATE


def measured_ac_power_p90(samples: tuple[float, ...]) -> float | None:
    """The 90th percentile of a session's power-sensor readings, or None
    if there are not enough qualifying samples yet (MEASURED_POWER_MIN_SAMPLES).
    p90 rather than an instant or a mean: resistant to a single noisy
    reading (a brief inrush spike) without needing a second smoothing
    mechanism, and without being dragged down the way a mean would be by
    whatever taper the caller's own gating didn't fully exclude.

    Nearest-rank, no interpolation -- simple, and exactness is not
    load-bearing here: seed_rate_calibrated()'s EFFICIENCY_MAX_GAIN bound
    is what keeps this value's influence on the stop within a fixed
    margin regardless of exactly which sample lands on the p90 rank.
    """
    if len(samples) < MEASURED_POWER_MIN_SAMPLES:
        return None
    ordered = sorted(samples)
    idx = min(int(len(ordered) * 0.90), len(ordered) - 1)
    return ordered[idx]
