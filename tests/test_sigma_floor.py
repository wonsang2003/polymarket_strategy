"""Pin the conditional σ-floor contract in forecast._effective_sigma_floor.

Why this test exists:
  The σ floor was a single constant (2.5°F) protecting against reanalysis-tight
  σ during the pre-real-forecast era. After the Apr 21 2026 regime backfill the
  24h walk-forward showed the OPPOSITE problem — under-confidence in middle
  bins, over-confidence at tails — driven by the 2.5°F floor inflating σ on
  buckets that already have enough real-forecast data to trust their MLE fit.

  The relaxed 2.0°F floor only applies when BOTH conditions hold:
    * lead_hours is in _RELAXED_FLOOR_LEADS (currently {24})
    * n_samples >= _N_REAL_FORECAST_THRESHOLD (currently 60)

  This test locks in the table so a future `n_samples` threshold change or
  lead-set expansion is intentional, not accidental.
"""
from __future__ import annotations

import pytest

from polymarket_strat.domain.weather.forecast import (
    BracketProbabilityCalculator,
    _N_REAL_FORECAST_THRESHOLD,
    _N_REAL_FORECAST_THRESHOLD_ULTRA,
    _RELAXED_FLOOR_LEADS,
    _SIGMA_FLOOR_F_REAL,
    _SIGMA_FLOOR_F_REANALYSIS,
    _SIGMA_FLOOR_F_ULTRA,
    _effective_sigma_floor,
)
from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    WeatherModel,
)


def _dist(*, lead_hours: int, n_samples: int, sigma: float = 1.2) -> ErrorDistribution:
    """Minimal ErrorDistribution for floor-selection tests."""
    return ErrorDistribution(
        city="nyc",
        model=WeatherModel.GFS,
        regime=SynopticRegime.STABLE_HIGH,
        lead_hours=lead_hours,
        family=DistributionFamily.NORMAL,
        mu=0.0,
        sigma=sigma,
        n_samples=n_samples,
    )


# ---------------------------------------------------------------------------
# _effective_sigma_floor — table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lead_hours, n_samples, expected_floor",
    [
        # 24h + ULTRA samples (>=150) → 1.5°F ultra floor (Apr 24 Citadel fix #4)
        (24, 150, _SIGMA_FLOOR_F_ULTRA),
        (24, 500, _SIGMA_FLOOR_F_ULTRA),
        (24, 10_000, _SIGMA_FLOOR_F_ULTRA),
        # 24h + REAL samples (60-149) → 2.0°F real floor
        (24, 60, _SIGMA_FLOOR_F_REAL),
        (24, 91, _SIGMA_FLOOR_F_REAL),
        (24, 149, _SIGMA_FLOOR_F_REAL),
        # 24h but not enough samples → 2.5°F guardrail
        (24, 0, _SIGMA_FLOOR_F_REANALYSIS),
        (24, 59, _SIGMA_FLOOR_F_REANALYSIS),
        # 48h (not in relaxed set) → always 2.5°F even with plenty of samples
        (48, 60, _SIGMA_FLOOR_F_REANALYSIS),
        (48, 500, _SIGMA_FLOOR_F_REANALYSIS),
        (48, 10_000, _SIGMA_FLOOR_F_REANALYSIS),
        # 72h and other long leads → always 2.5°F
        (72, 1000, _SIGMA_FLOOR_F_REANALYSIS),
        # 6h / 12h short-leads not in the earned set yet → 2.5°F
        (6, 1000, _SIGMA_FLOOR_F_REANALYSIS),
        (12, 1000, _SIGMA_FLOOR_F_REANALYSIS),
    ],
)
def test_effective_sigma_floor_table(lead_hours, n_samples, expected_floor):
    dist = _dist(lead_hours=lead_hours, n_samples=n_samples)
    assert _effective_sigma_floor(dist) == pytest.approx(expected_floor)


def test_constants_are_strictly_ordered():
    """Three-tier floor must be strictly monotonic: ULTRA < REAL < REANALYSIS.
    Otherwise the tiering does nothing and the whole patch is a no-op.
    """
    assert _SIGMA_FLOOR_F_ULTRA < _SIGMA_FLOOR_F_REAL < _SIGMA_FLOOR_F_REANALYSIS


def test_sample_thresholds_are_strictly_ordered():
    """ULTRA threshold must be strictly greater than REAL threshold — the
    tier with the most-relaxed floor must require the most evidence."""
    assert _N_REAL_FORECAST_THRESHOLD_ULTRA > _N_REAL_FORECAST_THRESHOLD


def test_threshold_is_conservative():
    """Sanity check the configured threshold against the real-forecast window.
    91 days of previous-runs data exists per (city, model, 24h); requiring at
    least 60 samples means ~65% coverage before relaxing — a defensible floor
    on the floor.
    """
    assert 30 <= _N_REAL_FORECAST_THRESHOLD <= 120


def test_only_24h_in_relaxed_set_today():
    """Today only 24h qualifies for relaxation. Expanding to 48h requires
    evidence from the 48h walk-forward after more real-forecast coverage —
    that's a deliberate change, not a drift-in default.
    """
    assert _RELAXED_FLOOR_LEADS == frozenset({24})


# ---------------------------------------------------------------------------
# bracket_probability — confirm the floor is actually applied
# ---------------------------------------------------------------------------

def test_bracket_probability_applies_relaxed_floor_at_24h_when_earned():
    """At 24h with enough samples, bracket_probability must use σ=2.0 when MLE
    σ is below the relaxed floor. Specifically: σ=1.0 + floor=2.0 → narrower
    bracket prob than σ=1.0 + floor=2.5 (pre-patch behavior)."""
    calc = BracketProbabilityCalculator()
    dist = _dist(lead_hours=24, n_samples=100, sigma=1.0)

    # Forecast exactly at bracket midpoint; bracket width 3°F.
    # With σ=2.0 the bracket captures less mass than with σ=2.5.
    p = calc.bracket_probability(
        forecast_f=70.0, error_dist=dist, lower_f=68.5, upper_f=71.5,
    )

    # Sanity: not a degenerate 0 or 1.
    assert 0.0 < p < 1.0

    # Pin the value against regression. With σ=2.0, N(0, 2.0), bracket
    # width ±1.5°F about the forecast → p ≈ Phi(0.75) - Phi(-0.75) ≈ 0.547.
    # (With old σ=2.5 floor, the same bracket would give ≈ 0.451.)
    assert 0.54 < p < 0.56


def test_bracket_probability_keeps_guardrail_at_48h():
    """At 48h the floor must stay 2.5°F regardless of n_samples. Same MLE σ
    and forecast should produce the tighter pre-patch bracket prob."""
    calc = BracketProbabilityCalculator()
    dist = _dist(lead_hours=48, n_samples=1000, sigma=1.0)

    p = calc.bracket_probability(
        forecast_f=70.0, error_dist=dist, lower_f=68.5, upper_f=71.5,
    )

    # With σ=2.5, ±1.5°F → Phi(0.6) - Phi(-0.6) ≈ 0.451.
    assert 0.44 < p < 0.46


def test_bracket_probability_keeps_guardrail_at_24h_thin_bucket():
    """24h but n_samples=0 should still use the 2.5°F floor — the buckets
    with no real-forecast data haven't earned the relaxation."""
    calc = BracketProbabilityCalculator()
    dist = _dist(lead_hours=24, n_samples=0, sigma=1.0)

    p = calc.bracket_probability(
        forecast_f=70.0, error_dist=dist, lower_f=68.5, upper_f=71.5,
    )

    assert 0.44 < p < 0.46
