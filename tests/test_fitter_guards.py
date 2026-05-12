"""Pin the Apr 24 2026 fitter guards in ErrorDistributionFitter.fit.

Why this test exists:
  Two guards were added to calibration.py after the external PDF review
  (§14 priority 7 / Apr 24 strategy review):

    Guard A — sample-size floor (n < 30):
        3-parameter fits (skew_normal, student_t) on 8-20 samples produce
        parameters dominated by noise. Example from the audit:
            NYC ECMWF 48h frontal_passage: μ=-15°F, σ=13.5, n=9
        A 15°F "bias estimate" from 9 samples is statistically meaningless.
        Guard forces Normal(empirical μ, empirical σ) when n < 30.

    Guard B — skew_normal shape blow-up:
        scipy's skewnorm.fit sometimes hits optimizer bounds and returns
        |shape| of 1e6 or -7e7. Not a skew parameter — that's the optimizer
        dying silently. Real skewness in temperature errors rarely exceeds
        3. Guard rejects |shape| > 10 and falls back to Student-t (or
        Normal if that also fails).

  These guards are conservative: they prefer a slightly-less-optimal
  family on clean data over a catastrophic fit on dirty data.
"""
from __future__ import annotations

import numpy as np
import pytest

from polymarket_strat.domain.weather.calibration import (
    ErrorDistributionFitter,
    _MAX_SKEW_SHAPE,
    _MIN_SAMPLES_FOR_PARAMETRIC_FIT,
)
from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    SynopticRegime,
    WeatherModel,
)


@pytest.fixture
def fitter() -> ErrorDistributionFitter:
    return ErrorDistributionFitter()


# ---------------------------------------------------------------------------
# Guard A — sample-size floor
# ---------------------------------------------------------------------------


class TestSampleSizeFloor:
    """n < 30 → force Normal family regardless of shape of the residuals."""

    def test_thirty_samples_is_the_cutoff(self) -> None:
        """Sanity-check that the constant we're testing against is 30."""
        assert _MIN_SAMPLES_FOR_PARAMETRIC_FIT == 30

    def test_small_n_highly_skewed_still_returns_normal(self, fitter: ErrorDistributionFitter) -> None:
        """Even highly-skewed residuals with n<30 get Normal, not Skew-Normal.
        This is the whole point of the guard — don't fit 3-param families
        on thin data."""
        rng = np.random.default_rng(42)
        # n=15 with strong right-skew (lognormal shift)
        errors = list(rng.lognormal(mean=0.0, sigma=1.0, size=15) - 1.0)
        dist = fitter.fit(errors, city="test", lead_hours=24)
        assert dist.family == DistributionFamily.NORMAL
        assert dist.n_samples == 15

    def test_small_n_fat_tails_still_returns_normal(self, fitter: ErrorDistributionFitter) -> None:
        """Kurtotic residuals with n<30 also get Normal, not Student-t."""
        rng = np.random.default_rng(42)
        errors = list(rng.standard_t(df=3.0, size=20))
        dist = fitter.fit(errors, city="test", lead_hours=24)
        assert dist.family == DistributionFamily.NORMAL
        assert dist.n_samples == 20

    def test_small_n_uses_empirical_moments(self, fitter: ErrorDistributionFitter) -> None:
        """The μ and σ on the guarded Normal should match the sample mean
        and sample stdev exactly (no fancy MLE)."""
        errors = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        dist = fitter.fit(errors, city="test", lead_hours=24)
        assert dist.family == DistributionFamily.NORMAL
        assert dist.mu == pytest.approx(5.5)
        expected_sigma = float(np.std(errors, ddof=1))
        assert dist.sigma == pytest.approx(expected_sigma)

    def test_thirty_or_more_samples_allows_fancy_fits(self, fitter: ErrorDistributionFitter) -> None:
        """At n >= 30 the fitter is allowed to pick skew_normal or student_t
        based on moment tests. Verify with strongly-skewed data."""
        rng = np.random.default_rng(42)
        errors = list(rng.lognormal(mean=0.0, sigma=1.0, size=60) - 1.0)
        dist = fitter.fit(errors, city="test", lead_hours=24)
        # With strong skew at n=60 we expect either SKEW_NORMAL (if shape
        # guard allows) or STUDENT_T — but not NORMAL.
        assert dist.family in {DistributionFamily.SKEW_NORMAL, DistributionFamily.STUDENT_T}
        assert dist.n_samples == 60


# ---------------------------------------------------------------------------
# Guard B — skew_normal shape blow-up detection
# ---------------------------------------------------------------------------


class TestSkewShapeGuard:
    """|shape| > 10 → reject skew_normal fit, fall back to Student-t."""

    def test_shape_constant(self) -> None:
        """The max shape constant is 10 — pinned so future changes are
        intentional."""
        assert _MAX_SKEW_SHAPE == 10.0

    def test_valid_skew_passes_through(self, fitter: ErrorDistributionFitter) -> None:
        """Residuals with real (moderate) skewness should produce a valid
        skew_normal fit with |shape| < 10."""
        rng = np.random.default_rng(123)
        from scipy import stats as sp_stats
        # Generate from a skew_normal with shape=3 (moderate, realistic)
        errors = list(sp_stats.skewnorm.rvs(a=3.0, loc=0.0, scale=1.0, size=200, random_state=rng))
        dist = fitter.fit(errors, city="test", lead_hours=24)
        # Should successfully fit as skew_normal with |shape| < 10
        assert dist.family == DistributionFamily.SKEW_NORMAL
        assert abs(dist.shape) < 10.0
        assert abs(dist.shape) > 0.5  # real skew, not near-zero


# ---------------------------------------------------------------------------
# Guard doesn't break well-behaved data
# ---------------------------------------------------------------------------


class TestGuardDoesntBreakNormalCase:
    """Well-conditioned, well-sampled data must still get the "right"
    family. Regression protection for the guards being too aggressive."""

    def test_clean_normal_data_gets_normal(self, fitter: ErrorDistributionFitter) -> None:
        """Symmetric, thin-tailed, large-sample → Normal."""
        rng = np.random.default_rng(0)
        errors = list(rng.normal(loc=-1.0, scale=2.5, size=150))
        dist = fitter.fit(errors, city="nyc", model=WeatherModel.GFS, lead_hours=24)
        assert dist.family == DistributionFamily.NORMAL
        assert dist.mu == pytest.approx(-1.0, abs=0.5)
        assert dist.sigma == pytest.approx(2.5, abs=0.3)

    def test_clean_fat_tail_gets_student_t(self, fitter: ErrorDistributionFitter) -> None:
        """Symmetric but kurtotic → Student-t."""
        rng = np.random.default_rng(7)
        errors = list(rng.standard_t(df=4.0, size=150))
        dist = fitter.fit(errors, city="nyc", model=WeatherModel.GFS, lead_hours=24)
        assert dist.family == DistributionFamily.STUDENT_T
        assert dist.nu > 2.0


# ---------------------------------------------------------------------------
# Minimum sample handling (existing behavior should still hold)
# ---------------------------------------------------------------------------


class TestMinimumSamples:
    """n < 5 still raises — guard does not replace the hard minimum."""

    def test_raises_on_fewer_than_five_samples(self, fitter: ErrorDistributionFitter) -> None:
        with pytest.raises(ValueError, match="Need >= 5"):
            fitter.fit([1.0, 2.0, 3.0], city="test", lead_hours=24)

    def test_five_samples_returns_normal(self, fitter: ErrorDistributionFitter) -> None:
        """Minimum viable sample size — Normal family by n-floor guard."""
        dist = fitter.fit([1.0, 2.0, 3.0, 4.0, 5.0], city="test", lead_hours=24)
        assert dist.family == DistributionFamily.NORMAL
        assert dist.n_samples == 5
