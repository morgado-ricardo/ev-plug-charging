"""rate_model.py: the one-directional guarantee is the point of this file.

test_effective_rate_never_below_seed is the direct regression test for a
real defect: capping only the session-cap term at max(learned, seed) while
letting the READING term use a fast-biased learned rate on its own let the
charge stop up to 26 percentage points short on the R1 blackout scenario.
"""
from __future__ import annotations

import random

import pytest

from ev_plug_charging.const import (
    EFFICIENCY_MAX_GAIN,
    EFFICIENCY_MAX_PLAUSIBLE,
    EFFICIENCY_MIN_PLAUSIBLE,
    EFFICIENCY_MIN_SAMPLES,
    MEASURED_POWER_MIN_SAMPLES,
    RATE_MODEL_CLAMP_HIGH,
    RATE_MODEL_CLAMP_LOW,
    RATE_MODEL_MIN_SAMPLES,
)
from ev_plug_charging.rate_model import (
    CompletedSession,
    accept_session,
    clamp,
    effective_rate,
    learned_rate,
    measured_ac_power_p90,
    record_efficiency_sample,
    reset_samples,
    seed_rate,
    seed_rate_calibrated,
    seed_rate_from_amps,
    solve_anchor_correction,
    working_efficiency,
)
from ev_plug_charging.models import RateSnapshot, SessionState

SEED = seed_rate(capacity_kwh=50.8, power_kw=1.84, efficiency=0.82)


def test_seed_is_about_twenty_minutes_per_percent():
    # A 50.8 kWh pack on a 1.84 kW granny cable at 0.82 efficiency: the
    # arithmetic should land near 20 min/%. A seed far from this means the
    # formula changed, and every projection moves with it.
    assert 20.0 <= SEED <= 20.5


def test_amps_conversion_matches_the_legacy_kw_figure():
    """8A at 230V is exactly the 1.84kW default this integration has always
    used -- seed_rate_from_amps() must reproduce seed_rate() bit for bit
    for the value everyone's already running, not just approximately."""
    assert seed_rate_from_amps(50.8, 8.0, 230.0, 0.82) == seed_rate(50.8, 1.84, 0.82)


def test_amps_conversion_scales_with_current():
    """A 16A EVSE delivers twice the power of 8A, so half the minutes per
    percent -- the estimator should track that whether it's told the amps
    or the equivalent kW."""
    double_current = seed_rate_from_amps(50.8, 16.0, 230.0, 0.82)
    assert double_current == pytest.approx(SEED / 2.0)


def test_fewer_than_min_samples_uses_seed_only():
    for n in range(RATE_MODEL_MIN_SAMPLES):
        samples = tuple([10.0] * n)  # a wildly fast, wrong sample sequence
        result = effective_rate(samples, SEED)
        assert result.minutes_per_percent == SEED


@pytest.mark.parametrize("bias", [0.3, 0.5, 0.6, 0.7, 0.85, 1.0, 1.5, 3.0])
def test_effective_rate_never_below_seed(bias: float):
    """THE regression test. No accepted sample sequence -- however
    fast-biased -- may push the effective (i.e. used-for-both-terms) rate
    below the seed. This is what makes learning safe: it can only push the
    projection, and therefore the stop, later than the seed would."""
    random.seed(42)
    samples = tuple(SEED * bias * random.uniform(0.9, 1.1) for _ in range(20))
    result = effective_rate(samples, SEED)
    assert result.minutes_per_percent >= SEED


def test_effective_rate_can_exceed_seed_when_learned_is_slower():
    slow_samples = tuple([SEED * 1.4] * 5)
    result = effective_rate(slow_samples, SEED)
    assert result.minutes_per_percent > SEED


def test_r1_blackout_does_not_truncate_even_with_fast_learned_samples():
    """Direct reproduction of the design review's finding: replay R1's
    790-minute blackout and confirm the reading-projection term (which is
    what actually decides the stop on a real night) credits no MORE charge
    than reality allows, using the effective (never-below-seed) rate."""
    fast_samples = tuple([SEED * 0.6] * 5)  # what the FIRST draft allowed
    naive_rate = RateSnapshot(minutes_per_percent=SEED * 0.6)
    safe_rate = effective_rate(fast_samples, SEED)

    silence_minutes = 790
    naive_credited = silence_minutes / naive_rate.minutes_per_percent
    safe_credited = silence_minutes / safe_rate.minutes_per_percent
    real_gain = silence_minutes / SEED

    # The naive (first-draft) approach overcredits badly -- this asserts
    # the defect existed, as a guardrail against silently reintroducing it.
    assert naive_credited - real_gain > 20.0
    # The fixed model must never overcredit relative to the seed-based
    # calculation, because effective_rate is bounded below by SEED.
    assert safe_credited <= real_gain + 0.01


@pytest.mark.parametrize(
    "learned,seed,expected_low,expected_high",
    [
        (10.0, 20.0, 20.0 * RATE_MODEL_CLAMP_LOW, 20.0 * RATE_MODEL_CLAMP_HIGH),
        (100.0, 20.0, 20.0 * RATE_MODEL_CLAMP_LOW, 20.0 * RATE_MODEL_CLAMP_HIGH),
    ],
)
def test_clamp_bounds(learned, seed, expected_low, expected_high):
    result = clamp(learned, seed)
    assert expected_low <= result <= expected_high


def test_learned_rate_uses_median_of_recent_window():
    # 7 samples; window keeps the most recent 5.
    samples = (10.0, 10.0, 20.0, 20.0, 20.0, 30.0, 40.0)
    result = learned_rate(samples)
    # most recent 5: 20, 20, 30, 40 ... wait window is last 5 of the tuple:
    # (20.0, 20.0, 30.0, 40.0) -- only 4 remain after the first 3 are
    # dropped; recompute explicitly to avoid a brittle hand-picked expectation.
    from statistics import median

    assert result == median(samples[-5:])


# -- Sample acceptance -------------------------------------------------- #


def _session(**overrides) -> CompletedSession:
    base = dict(
        duration_minutes=60.0,
        soc_gained=15.0,
        stayed_on_plug=True,
        ran_above_target=False,
        anchor_provisional_unresolved=False,
        stop_reason="target_reached",
    )
    base.update(overrides)
    return CompletedSession(**base)


def test_healthy_session_accepted():
    assert accept_session(_session()) is True


def test_rejects_provisional_anchor():
    assert accept_session(_session(anchor_provisional_unresolved=True)) is False


def test_rejects_short_session():
    assert accept_session(_session(duration_minutes=10.0)) is False


def test_rejects_small_soc_gain():
    assert accept_session(_session(soc_gained=3.0)) is False


def test_rejects_session_that_ran_above_target():
    """The linear model is invalid in the constant-voltage taper -- a
    session that crossed into it must not contaminate the rate."""
    assert accept_session(_session(ran_above_target=True)) is False


def test_rejects_bypass_session():
    assert accept_session(_session(stayed_on_plug=False)) is False


def test_rejects_window_close_stop():
    assert accept_session(_session(stop_reason="window_close")) is False


def test_rejects_fault_stop():
    assert accept_session(_session(stop_reason="fault")) is False


def test_accepts_power_drop_stop():
    assert accept_session(_session(stop_reason="power_drop")) is True


# -- Anchor correction uses the effective rate, consistently -------------- #


def test_solve_anchor_correction_matches_projection_margin():
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)
    now = start + timedelta(minutes=120)
    rate = RateSnapshot(minutes_per_percent=20.0)
    corrected = solve_anchor_correction(60.0, start, now, rate)
    expected = 60.0 - 120 / (20.0 * 1.15)
    assert corrected == pytest.approx(expected, abs=0.01)


def test_solve_anchor_correction_is_clamped():
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)
    now = start + timedelta(minutes=100000)  # absurdly long, forces < -30
    rate = RateSnapshot(minutes_per_percent=20.0)
    corrected = solve_anchor_correction(60.0, start, now, rate)
    assert corrected == -30.0


# -- Efficiency self-calibration (Phase 2) -------------------------------- #

CAPACITY = 50.0
CURRENT_A = 8.0
VOLTAGE = 230.0
PRIOR = 0.75
CONFIGURED_KW = CURRENT_A * VOLTAGE / 1000.0  # 1.84 kW
CONFIGURED_EFFECTIVE_KW = CONFIGURED_KW * PRIOR


def test_seed_calibrated_matches_configured_when_efficiency_equals_prior():
    """No measurement, no calibration movement -- the calibrated seed must
    reduce to exactly the same figure the amps-only seed already gives."""
    seed = seed_rate_calibrated(CAPACITY, CURRENT_A, VOLTAGE, PRIOR, None, PRIOR)
    assert seed == seed_rate_from_amps(CAPACITY, CURRENT_A, VOLTAGE, PRIOR)


def test_seed_calibrated_falls_back_to_configured_power_without_a_measurement():
    """Efficiency corrected upward but no measured AC power yet -- the
    configured amps still supply the power term."""
    seed = seed_rate_calibrated(CAPACITY, CURRENT_A, VOLTAGE, PRIOR, None, PRIOR * 1.1)
    expected = seed_rate(CAPACITY, CONFIGURED_KW * PRIOR * 1.1, 1.0)
    assert seed == pytest.approx(expected)


def test_seed_calibrated_uses_measured_power_over_configured():
    """Once a measured AC power is available, it (not the configured amps)
    supplies the power term -- confirmed by it changing the result even
    with efficiency held at the prior."""
    seed_no_measurement = seed_rate_calibrated(CAPACITY, CURRENT_A, VOLTAGE, PRIOR, None, PRIOR)
    seed_with_measurement = seed_rate_calibrated(
        CAPACITY, CURRENT_A, VOLTAGE, PRIOR, CONFIGURED_KW * 1.05, PRIOR
    )
    assert seed_with_measurement != seed_no_measurement


@pytest.mark.parametrize(
    "measured_kw,calibrated_efficiency",
    [
        (None, 0.95),  # efficiency alone tries to push far past the cap
        (CONFIGURED_KW * 3.0, PRIOR),  # measured power alone tries to
        (CONFIGURED_KW * 3.0, 0.95),  # both at once, worst case
    ],
)
def test_seed_never_runs_more_than_gain_cap_fast(measured_kw, calibrated_efficiency):
    """THE headline safety property for Phase 2, the calibration
    equivalent of test_effective_rate_never_below_seed above: however
    aggressive the measured power or the calibrated efficiency, the
    calibrated seed may never imply more than EFFICIENCY_MAX_GAIN times
    the as-configured effective power -- i.e. it may never fall below
    seed_configured / EFFICIENCY_MAX_GAIN."""
    seed_configured = seed_rate_from_amps(CAPACITY, CURRENT_A, VOLTAGE, PRIOR)
    seed = seed_rate_calibrated(
        CAPACITY, CURRENT_A, VOLTAGE, PRIOR, measured_kw, calibrated_efficiency
    )
    assert seed >= seed_configured / EFFICIENCY_MAX_GAIN - 1e-9


@pytest.mark.parametrize("bias", [0.9, 1.0, 1.05, 1.1, 1.2, 1.5, 2.0])
def test_seed_never_runs_more_than_gain_cap_fast_over_random_inputs(bias: float):
    """Same property, swept over a range of measured-power/efficiency
    combinations rather than just the boundary cases above."""
    seed_configured = seed_rate_from_amps(CAPACITY, CURRENT_A, VOLTAGE, PRIOR)
    for eff in (0.50, 0.65, 0.75, 0.85, 0.95):
        seed = seed_rate_calibrated(
            CAPACITY, CURRENT_A, VOLTAGE, PRIOR, CONFIGURED_KW * bias, eff
        )
        assert seed >= seed_configured / EFFICIENCY_MAX_GAIN - 1e-9


def test_seed_calibrated_moves_freely_downward():
    """Downward movement (a lower calibrated efficiency, or a measured
    power below configured) is unbounded and free -- only the upward
    direction is capped."""
    seed_configured = seed_rate_from_amps(CAPACITY, CURRENT_A, VOLTAGE, PRIOR)
    seed = seed_rate_calibrated(CAPACITY, CURRENT_A, VOLTAGE, PRIOR, CONFIGURED_KW * 0.3, 0.50)
    assert seed > seed_configured  # slower (higher minutes/%) is safe and unbounded


def test_reset_samples_clears_efficiency_samples_too():
    state = SessionState(rate_samples=(20.0, 21.0), efficiency_samples=(0.7, 0.75))
    new_state = reset_samples(state)
    assert new_state.rate_samples == ()
    assert new_state.efficiency_samples == ()


def test_reset_samples_leaves_the_in_flight_pair_alone():
    """A capacity change/reset button invalidates the HISTORY, not the
    pair belonging to whatever session is currently open."""
    state = SessionState(
        efficiency_samples=(0.7,), calib_soc_first=40.0, calib_energy_first=0.5
    )
    new_state = reset_samples(state)
    assert new_state.calib_soc_first == 40.0
    assert new_state.calib_energy_first == 0.5


def _eff_session(**overrides) -> CompletedSession:
    base = dict(
        duration_minutes=60.0,
        soc_gained=15.0,
        stayed_on_plug=True,
        ran_above_target=False,
        anchor_provisional_unresolved=False,
        stop_reason="power_drop",
    )
    base.update(overrides)
    return CompletedSession(**base)


def test_record_efficiency_sample_needs_a_matched_pair():
    state = SessionState()  # no calib pair at all
    new_state = record_efficiency_sample(state, _eff_session(), CAPACITY)
    assert new_state.efficiency_samples == ()


def test_record_efficiency_sample_rejects_small_soc_gain():
    """Below RATE_MODEL_MIN_SOC_GAIN -- the same bar the rate sample uses
    -- sensor quantisation dominates the paired measurement."""
    state = SessionState(
        calib_soc_first=50.0, calib_energy_first=1.0, calib_soc_last=55.0, calib_energy_last=3.0
    )
    new_state = record_efficiency_sample(state, _eff_session(), CAPACITY)
    assert new_state.efficiency_samples == ()


def test_record_efficiency_sample_rejects_small_energy_gain():
    state = SessionState(
        calib_soc_first=50.0, calib_energy_first=1.0, calib_soc_last=65.0, calib_energy_last=1.5
    )
    new_state = record_efficiency_sample(state, _eff_session(), CAPACITY)
    assert new_state.efficiency_samples == ()


def test_record_efficiency_sample_accepts_a_healthy_pair():
    # 15 points of gain on a 50 kWh pack = 7.5 kWh "should have" flowed;
    # 10 kWh actually flowed -> measured efficiency = 0.75, plausible.
    state = SessionState(
        calib_soc_first=50.0, calib_energy_first=1.0, calib_soc_last=65.0, calib_energy_last=11.0
    )
    new_state = record_efficiency_sample(state, _eff_session(), CAPACITY)
    assert len(new_state.efficiency_samples) == 1
    assert new_state.efficiency_samples[0] == pytest.approx(0.75)


@pytest.mark.parametrize("measured", [0.30, EFFICIENCY_MIN_PLAUSIBLE - 0.01])
def test_efficiency_rejects_implausibly_low_sample(measured):
    """A mis-scaled sensor claiming near-zero efficiency is rejected
    outright, not clipped into the plausible band."""
    # capacity * soc_gain/100 / energy_gain == measured
    energy_gain = CAPACITY * 15.0 / 100.0 / measured
    state = SessionState(
        calib_soc_first=50.0,
        calib_energy_first=1.0,
        calib_soc_last=65.0,
        calib_energy_last=1.0 + energy_gain,
    )
    new_state = record_efficiency_sample(state, _eff_session(), CAPACITY)
    assert new_state.efficiency_samples == ()


@pytest.mark.parametrize("measured", [1.4, EFFICIENCY_MAX_PLAUSIBLE + 0.01])
def test_efficiency_rejects_implausibly_high_sample(measured):
    """A mis-scaled sensor claiming a better-than-physical conversion is
    rejected outright, never clamped in as 'maximally efficient'."""
    energy_gain = CAPACITY * 15.0 / 100.0 / measured
    state = SessionState(
        calib_soc_first=50.0,
        calib_energy_first=1.0,
        calib_soc_last=65.0,
        calib_energy_last=1.0 + energy_gain,
    )
    new_state = record_efficiency_sample(state, _eff_session(), CAPACITY)
    assert new_state.efficiency_samples == ()


def test_fewer_than_min_efficiency_samples_uses_prior():
    for n in range(EFFICIENCY_MIN_SAMPLES):
        samples = tuple([0.95] * n)  # a wildly optimistic sample sequence
        assert working_efficiency(samples, PRIOR) == PRIOR


def test_working_efficiency_derates_the_median():
    from ev_plug_charging.const import EFFICIENCY_DERATE

    samples = (0.70, 0.72, 0.74)
    result = working_efficiency(samples, PRIOR)
    assert result == pytest.approx(0.72 * EFFICIENCY_DERATE)


def test_working_efficiency_has_no_floor_at_the_prior():
    """Unlike effective_rate's max(learned, seed), a LOWER working
    efficiency (the safe direction -- it shrinks the seed's implied
    power) is free to move below the prior."""
    samples = (0.55, 0.56, 0.57)
    result = working_efficiency(samples, PRIOR)
    assert result < PRIOR


def test_measured_ac_power_p90_needs_min_samples():
    samples = tuple([2.0] * (MEASURED_POWER_MIN_SAMPLES - 1))
    assert measured_ac_power_p90(samples) is None


def test_measured_ac_power_p90_nearest_rank():
    samples = tuple(float(i) for i in range(1, 11))  # 1..10
    result = measured_ac_power_p90(samples)
    # nearest-rank, 0-indexed: idx = int(10 * 0.90) = 9 -> ordered[9] == 10.0
    assert result == 10.0
