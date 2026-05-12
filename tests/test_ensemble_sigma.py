"""Pin the ensemble-aware σ widening (Tier 3a).

Why this test exists:
  Apr 24 2026 — `ensemble_aware_sigma` combines historical MLE σ with
  today's ensemble member disagreement to produce a forward-looking σ
  estimate. Core invariants:
    1. When ensemble_std = 0, returns MLE σ unchanged (no-op).
    2. Widening is in quadrature (σ_eff² = σ_mle² + (α × σ_ens)²).
    3. σ_effective >= σ_mle always (never shrinks).
    4. ensemble_bracket_probability threads the widening through from
       forecast.ensemble_spread_f.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

import pytest

from polymarket_strat.domain.weather.forecast import (
    BracketProbabilityCalculator,
    ensemble_aware_sigma,
)
from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    TemperatureForecast,
    WeatherModel,
)


class TestEnsembleAwareSigma:
    def test_zero_ensemble_std_returns_mle_unchanged(self) -> None:
        """No ensemble data → pure MLE σ. This is the safety fallback."""
        assert ensemble_aware_sigma(2.5, 0.0) == pytest.approx(2.5)
        assert ensemble_aware_sigma(2.5, 0.0, alpha=2.0) == pytest.approx(2.5)

    def test_negative_ensemble_std_returns_mle(self) -> None:
        """Defensive: negative ensemble_std is invalid input, return MLE."""
        assert ensemble_aware_sigma(2.5, -1.0) == pytest.approx(2.5)

    def test_nan_ensemble_std_returns_mle(self) -> None:
        """NaN input → MLE σ unchanged."""
        assert ensemble_aware_sigma(2.5, float("nan")) == pytest.approx(2.5)

    def test_quadrature_widening(self) -> None:
        """σ_eff = sqrt(σ_mle² + (α × σ_ens)²) with α=1."""
        # σ_mle=3, σ_ens=4 → sqrt(9+16) = 5
        assert ensemble_aware_sigma(3.0, 4.0, alpha=1.0) == pytest.approx(5.0)
        # σ_mle=2.5, σ_ens=2.5 → sqrt(6.25+6.25) = sqrt(12.5)
        assert ensemble_aware_sigma(2.5, 2.5, alpha=1.0) == pytest.approx(math.sqrt(12.5))

    def test_alpha_scales_ensemble_contribution(self) -> None:
        """α is the weight on the ensemble contribution. α=0 collapses to MLE."""
        assert ensemble_aware_sigma(2.5, 4.0, alpha=0.0) == pytest.approx(2.5)
        # α=0.5 → sqrt(6.25 + (0.5*4)^2) = sqrt(6.25+4) = sqrt(10.25)
        assert ensemble_aware_sigma(2.5, 4.0, alpha=0.5) == pytest.approx(math.sqrt(10.25))

    def test_never_shrinks(self) -> None:
        """σ_eff >= σ_mle for any non-negative ensemble_std and alpha."""
        for mle in (0.5, 1.0, 2.5, 5.0):
            for ens in (0.0, 0.5, 1.0, 5.0, 10.0):
                for alpha in (0.0, 0.5, 1.0, 2.0):
                    out = ensemble_aware_sigma(mle, ens, alpha=alpha)
                    assert out >= mle - 1e-9

    def test_zero_mle_with_ensemble(self) -> None:
        """Degenerate MLE σ=0 falls back to ensemble contribution."""
        result = ensemble_aware_sigma(0.0, 3.0, alpha=1.0)
        assert result == pytest.approx(3.0)


class TestBracketProbEnsembleThread:
    """ensemble_bracket_probability must thread the spread through so the
    σ widens on high-disagreement days."""

    def _mk(self, *, spread_f: float, sigma_mle: float) -> tuple[list, list]:
        t0 = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
        fc = TemperatureForecast(
            city="nyc",
            model=WeatherModel.GFS,
            init_time=t0,
            valid_time=t0 + timedelta(hours=24),
            lead_hours=24,
            forecast_high_f=70.0,
            ensemble_spread_f=spread_f,
        )
        dist = ErrorDistribution(
            city="nyc",
            model=WeatherModel.GFS,
            regime=SynopticRegime.STABLE_HIGH,
            lead_hours=24,
            family=DistributionFamily.NORMAL,
            mu=0.0,
            sigma=sigma_mle,
            n_samples=100,
        )
        return [fc], [dist]

    def test_high_spread_widens_probability(self) -> None:
        """On a high-ensemble-disagreement day, a narrow bracket should
        read LESS confident (probability closer to bracket-width/range).

        Forecast 70°F, bracket 68-72°F (narrow 4°F-window):
          - Calm day (spread=0): tight σ → P close to ~0.8+ (covers the
            forecast with room to spare)
          - Stormy day (spread=12°F → ens_std=3°F): widened σ → lower P
            because tail mass pushes outside the narrow bracket
        """
        calc = BracketProbabilityCalculator()
        # No ensemble spread
        fcs_calm, dists = self._mk(spread_f=0.0, sigma_mle=2.0)
        p_calm, _ = calc.ensemble_bracket_probability(
            forecasts=fcs_calm, error_dists=dists,
            lower_f=68.0, upper_f=72.0,
            apply_isotonic=False,
        )
        # Strong ensemble disagreement (12°F range → std≈3°F)
        fcs_storm, _ = self._mk(spread_f=12.0, sigma_mle=2.0)
        p_storm, _ = calc.ensemble_bracket_probability(
            forecasts=fcs_storm, error_dists=dists,
            lower_f=68.0, upper_f=72.0,
            apply_isotonic=False,
        )
        # Storm day σ widens → narrow bracket gets LESS of the mass
        assert p_storm < p_calm
        # Sanity: both are valid probabilities
        assert 0.0 <= p_calm <= 1.0
        assert 0.0 <= p_storm <= 1.0

    def test_zero_spread_no_behavior_change(self) -> None:
        """Spread=0 on the forecast → exactly the same p as if we didn't
        have ensemble awareness. Tier-3a is strictly additive."""
        calc = BracketProbabilityCalculator()
        fcs, dists = self._mk(spread_f=0.0, sigma_mle=2.5)
        p_with_thread, _ = calc.ensemble_bracket_probability(
            forecasts=fcs, error_dists=dists,
            lower_f=68.0, upper_f=72.0,
            apply_isotonic=False,
        )
        # Direct call with ensemble_std_f=0 should match
        p_direct = calc.bracket_probability(
            forecast_f=70.0, error_dist=dists[0],
            lower_f=68.0, upper_f=72.0,
            ensemble_std_f=0.0,
        )
        assert p_with_thread == pytest.approx(p_direct)
