"""Pin the Layer 1 quantile-regression pricer.

Apr 25 2026 — replaces parametric error distribution with non-parametric
quantile regression. This file pins:
  1. Feature vector shape and stability
  2. Climatology lookup fallback chain
  3. Quantile prediction CDF interpolation
  4. End-to-end training on synthetic data
  5. Conformal widening calibration
"""
from __future__ import annotations

import os
import tempfile
from datetime import date

import numpy as np
import pytest

from polymarket_strat.domain.weather.features import (
    FEATURE_NAMES,
    N_FEATURES,
    ClimatologyLookup,
    build_feature_vector,
    day_of_year,
    get_climatology,
    reset_climatology_for_tests,
    reset_model_skill_for_tests,
)
from polymarket_strat.domain.weather.quantile_pricing import (
    TAU_GRID,
    QuantileBracketPricer,
    QuantileModelArtifact,
    QuantilePrediction,
    train_quantile_models_for_bucket,
)


class TestFeatureVector:
    def test_feature_count_matches_constant(self):
        assert N_FEATURES == len(FEATURE_NAMES)
        assert N_FEATURES == 17

    def test_basic_feature_extraction(self):
        reset_climatology_for_tests()
        reset_model_skill_for_tests()
        feat = build_feature_vector(
            city="nyc", model="gfs",
            forecast_high_f=72.0,
            obs_date=date(2026, 4, 25),
            lead_hours=24, regime="stable_high",
            ensemble_spread_f=2.0,
        )
        assert len(feat) == N_FEATURES
        assert all(np.isfinite(feat))

    def test_month_feature(self):
        reset_climatology_for_tests()
        reset_model_skill_for_tests()
        feat = build_feature_vector(
            city="nyc", model="gfs",
            forecast_high_f=72.0,
            obs_date=date(2026, 7, 15),
            lead_hours=24, regime="stable_high",
        )
        assert feat[FEATURE_NAMES.index("month")] == 7.0

    def test_regime_one_hot(self):
        reset_climatology_for_tests()
        reset_model_skill_for_tests()
        feat = build_feature_vector(
            city="nyc", model="gfs",
            forecast_high_f=72.0,
            obs_date=date(2026, 4, 25),
            lead_hours=24, regime="frontal_passage",
        )
        idx_stable = FEATURE_NAMES.index("regime_stable_high")
        idx_frontal = FEATURE_NAMES.index("regime_frontal_passage")
        idx_transit = FEATURE_NAMES.index("regime_transition")
        assert feat[idx_stable] == 0.0
        assert feat[idx_frontal] == 1.0
        assert feat[idx_transit] == 0.0

    def test_model_one_hot(self):
        reset_climatology_for_tests()
        reset_model_skill_for_tests()
        feat = build_feature_vector(
            city="nyc", model="ECMWF",  # mixed case
            forecast_high_f=72.0,
            obs_date=date(2026, 4, 25),
            lead_hours=24, regime="stable_high",
        )
        idx_gfs = FEATURE_NAMES.index("is_gfs")
        idx_ecmwf = FEATURE_NAMES.index("is_ecmwf")
        assert feat[idx_gfs] == 0.0
        assert feat[idx_ecmwf] == 1.0


class TestClimatologyLookup:
    def test_missing_file_returns_empty_lookup(self):
        c = ClimatologyLookup.from_json("/tmp/nonexistent-climo-xyz.json")
        # Should return defaults
        mean, std = c.lookup("nyc", 100)
        assert mean == 60.0
        assert std == 15.0

    def test_exact_doy_lookup(self):
        c = ClimatologyLookup(
            by_city_doy={"nyc": {115: {"mean": 65.0, "std": 8.0}}},
            by_city_yearmean={},
        )
        mean, std = c.lookup("nyc", 115)
        assert mean == 65.0
        assert std == 8.0

    def test_neighbor_doy_smoothing(self):
        """When exact DOY is missing, ±3 day window provides smoothing."""
        c = ClimatologyLookup(
            by_city_doy={"nyc": {117: {"mean": 67.0, "std": 9.0}}},
            by_city_yearmean={},
        )
        # Exact 115 missing — should find 117 (within ±3)
        mean, std = c.lookup("nyc", 115)
        assert mean == 67.0
        assert std == 9.0

    def test_year_round_fallback(self):
        c = ClimatologyLookup(
            by_city_doy={"nyc": {}},  # empty
            by_city_yearmean={"nyc": {"mean": 55.0, "std": 20.0}},
        )
        mean, std = c.lookup("nyc", 115)
        assert mean == 55.0
        assert std == 20.0


class TestQuantilePrediction:
    def test_cdf_interpolation_within_range(self):
        """Linear interpolation between adjacent quantile predictions."""
        pred = QuantilePrediction(
            quantiles={0.25: -2.0, 0.50: 0.0, 0.75: 2.0},
            feature_vec=[],
        )
        # x=1.0 is halfway between Q50=0 and Q75=2, so CDF≈0.625
        cdf = pred.cdf(1.0)
        assert 0.5 <= cdf <= 0.75
        # Roughly the linear interp value
        assert 0.6 <= cdf <= 0.65

    def test_cdf_above_range_caps(self):
        pred = QuantilePrediction(
            quantiles={0.25: -2.0, 0.50: 0.0, 0.75: 2.0},
            feature_vec=[],
        )
        # Above max quantile, should approach but not equal 1
        cdf = pred.cdf(100.0)
        assert 0.75 < cdf <= 1.0

    def test_empty_quantiles_returns_half(self):
        pred = QuantilePrediction(quantiles={}, feature_vec=[])
        assert pred.cdf(0.0) == 0.5


class TestQuantileTraining:
    """Train on synthetic data and verify the model recovers known truth."""

    def _generate_synthetic_data(self, n=300, sigma=2.0, seed=42):
        """Generate synthetic forecast errors with known distribution."""
        rng = np.random.default_rng(seed)
        # Synthetic features (random but reasonable)
        X = np.random.RandomState(seed).randn(n, N_FEATURES)
        # Errors are Gaussian with known sigma
        y = rng.normal(loc=0.0, scale=sigma, size=n)
        return X, y

    def test_train_basic_bucket(self):
        X, y = self._generate_synthetic_data(n=300, sigma=2.0)
        artifact = train_quantile_models_for_bucket(
            city="testcity", lead_hours=24,
            feature_matrix=X, error_targets=y,
            max_iter=50,  # fast for tests
        )
        assert artifact.city == "testcity"
        assert artifact.lead_hours == 24
        assert len(artifact.models) == len(TAU_GRID)
        # Pinball loss should be sane (not NaN, positive, not absurd)
        assert artifact.holdout_pinball_loss > 0
        assert artifact.holdout_pinball_loss < 5.0
        # Conformal widening should be reasonable for sigma=2
        assert 0 < artifact.conformal_widening < 6.0

    def test_predict_recovers_distribution(self):
        """For Gaussian errors, the trained quantile predictions should
        approximately match the true Gaussian quantiles."""
        X, y = self._generate_synthetic_data(n=500, sigma=2.0)
        artifact = train_quantile_models_for_bucket(
            city="test", lead_hours=24,
            feature_matrix=X, error_targets=y,
            max_iter=80,
        )
        # Predict on a fresh feature vector
        test_feat = X[0]
        pred = artifact.predict(test_feat)
        # Quantiles should be roughly monotonic and centered near 0
        median_pred = pred.quantiles.get(0.50, 0.0)
        assert abs(median_pred) < 1.5  # close to true median (0)
        # 0.95 - 0.05 quantile gap should be roughly 2 * 1.645 * sigma ≈ 6.6
        q05 = pred.quantiles.get(0.05, -10)
        q95 = pred.quantiles.get(0.95, 10)
        gap = q95 - q05
        assert 3.0 < gap < 12.0  # generous bounds for finite-sample estimate

    def test_save_load_roundtrip(self):
        X, y = self._generate_synthetic_data(n=200)
        artifact = train_quantile_models_for_bucket(
            city="test", lead_hours=24,
            feature_matrix=X, error_targets=y,
            max_iter=30,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pkl")
            artifact.save(path)
            assert os.path.exists(path)
            loaded = QuantileModelArtifact.load(path)
            assert loaded.city == "test"
            assert loaded.lead_hours == 24
            assert len(loaded.models) == len(TAU_GRID)
            # Predictions should match
            test_x = X[0]
            p1 = artifact.predict(test_x)
            p2 = loaded.predict(test_x)
            for tau in TAU_GRID:
                assert abs(p1.quantiles[tau] - p2.quantiles[tau]) < 1e-6

    def test_too_few_samples_raises(self):
        X, y = self._generate_synthetic_data(n=20)  # below 50 minimum
        with pytest.raises(ValueError, match="Need >= 50"):
            train_quantile_models_for_bucket(
                city="test", lead_hours=24,
                feature_matrix=X, error_targets=y,
            )


class TestQuantileBracketPricer:
    def test_no_models_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pricer = QuantileBracketPricer(models_dir=tmpdir)
            assert not pricer.loaded
            assert pricer.n_artifacts == 0
            assert not pricer.has_model("nyc", 24)

    def test_loaded_pricer_returns_probability(self):
        # Train + save one model, then load and predict
        rng = np.random.RandomState(42)
        X = rng.randn(200, N_FEATURES).astype(np.float64)
        y = rng.normal(0, 2.5, 200)
        artifact = train_quantile_models_for_bucket(
            city="nyc", lead_hours=24,
            feature_matrix=X, error_targets=y,
            max_iter=30,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact.save(os.path.join(tmpdir, "nyc_24h.pkl"))
            pricer = QuantileBracketPricer(models_dir=tmpdir)
            assert pricer.has_model("nyc", 24)

            # Predict a wide bracket — should get high probability
            p = pricer.bracket_probability(
                city="nyc", model="gfs",
                forecast_high_f=70.0,
                obs_date=date(2026, 4, 25),
                lead_hours=24, regime="stable_high",
                lower_f=60.0, upper_f=80.0,
                ensemble_spread_f=2.0,
                apply_conformal=False,  # raw probability
            )
            assert p is not None
            assert 0.0 <= p <= 1.0
            # Wide bracket centered on forecast → high probability
            assert p > 0.5

    def test_conformal_widening_more_conservative(self):
        """Conformal-wrapped probability should be ≤ raw (more conservative)."""
        rng = np.random.RandomState(0)
        X = rng.randn(200, N_FEATURES).astype(np.float64)
        y = rng.normal(0, 2.0, 200)
        artifact = train_quantile_models_for_bucket(
            city="test", lead_hours=24,
            feature_matrix=X, error_targets=y,
            max_iter=30,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact.save(os.path.join(tmpdir, "test_24h.pkl"))
            pricer = QuantileBracketPricer(models_dir=tmpdir)
            p_raw = pricer.bracket_probability(
                city="test", model="gfs", forecast_high_f=70.0,
                obs_date=date(2026, 4, 25), lead_hours=24,
                regime="stable_high", lower_f=68.0, upper_f=72.0,
                apply_conformal=False,
            )
            p_conf = pricer.bracket_probability(
                city="test", model="gfs", forecast_high_f=70.0,
                obs_date=date(2026, 4, 25), lead_hours=24,
                regime="stable_high", lower_f=68.0, upper_f=72.0,
                apply_conformal=True,
            )
            # Conformal widening should make narrow brackets LESS likely
            # (probability mass is spread over a wider effective range).
            assert p_conf <= p_raw + 0.001  # tolerance for float math
