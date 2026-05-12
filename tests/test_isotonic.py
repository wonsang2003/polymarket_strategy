"""Pin the isotonic post-hoc calibration contract.

Why this test exists:
  Apr 24 2026 — isotonic regression was added as the final step of
  ensemble_bracket_probability to correct systematic overconfidence in
  low-p buckets (§14 priority 7c). This is the single biggest accuracy
  leverage fix in the Apr 24 dev plan.

  This test file pins three things:
    1. IsotonicCalibrator gracefully handles missing/malformed JSON
       (fails closed to identity — never hurts inference).
    2. The per-city → global → identity lookup order is correct.
    3. ensemble_bracket_probability applies the calibration when
       apply_isotonic=True (the default) and skips it when False (for
       walk-forward, where we must avoid training-on-self).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from polymarket_strat.domain.weather.forecast import (
    BracketProbabilityCalculator,
    IsotonicCalibrator,
    _reset_isotonic_cache_for_tests,
)
from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    TemperatureForecast,
    WeatherModel,
)


# ---------------------------------------------------------------------------
# IsotonicCalibrator — loading / fallback
# ---------------------------------------------------------------------------


class TestCalibratorLoad:
    def test_missing_file_is_identity(self) -> None:
        """No JSON → every call returns input unchanged."""
        c = IsotonicCalibrator(json_path="/tmp/does-not-exist-xyz.json")
        assert c.loaded is False
        assert c.calibrate(0.37, city="nyc", lead_hours=24) == pytest.approx(0.37)
        assert c.calibrate(0.0, city="nyc", lead_hours=24) == 0.0
        assert c.calibrate(1.0, city="nyc", lead_hours=24) == 1.0

    def test_malformed_json_is_identity(self) -> None:
        """Corrupt JSON → identity, no crash."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            path = f.name
        try:
            c = IsotonicCalibrator(json_path=path)
            assert c.loaded is False
            assert c.calibrate(0.4, city="nyc", lead_hours=24) == pytest.approx(0.4)
        finally:
            os.unlink(path)

    def test_out_of_bounds_inputs_pass_through(self) -> None:
        """NaN or out-of-[0,1] inputs should return as-is (caller clamps)."""
        c = IsotonicCalibrator(json_path="/tmp/does-not-exist-xyz.json")
        assert c.calibrate(float("nan")) != c.calibrate(float("nan"))  # NaN != NaN
        assert c.calibrate(-0.1) == pytest.approx(-0.1)
        assert c.calibrate(1.5) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Lookup order — per-city → global → identity
# ---------------------------------------------------------------------------


@pytest.fixture
def cal_json_path():
    """Write a hand-crafted calibration JSON with known shrinkage curves
    so the test is deterministic. Cleans up the temp file after."""
    payload = {
        "fit_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"24": "test", "48": "test"},
        "min_samples_per_city": 150,
        "global": {
            # Global 24h: identity (for easy sanity checks)
            "24": {"x": [0.0, 1.0], "y": [0.0, 1.0], "n": 10000},
            # Global 48h: uniform shrinkage (y = 0.5 * x). Any input halves.
            "48": {"x": [0.0, 1.0], "y": [0.0, 0.5], "n": 10000},
        },
        "per_city": {
            # NYC 24h: aggressive shrinkage on high-p (y=x/2 above 0.5)
            # — simulates the "NYC 0.66 → 0.48" measured pattern.
            "nyc": {
                "24": {"x": [0.0, 0.5, 1.0], "y": [0.0, 0.5, 0.5], "n": 1500},
            },
            # Seoul 24h: boost on low-p (y=2x below 0.5, clip at 0.5)
            "seoul": {
                "24": {"x": [0.0, 0.5, 1.0], "y": [0.0, 1.0, 1.0], "n": 1500},
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    yield path
    os.unlink(path)


class TestLookupOrder:
    def test_per_city_takes_precedence(self, cal_json_path: str) -> None:
        """When a per-city curve exists for (nyc, 24), use it (not global)."""
        c = IsotonicCalibrator(json_path=cal_json_path)
        assert c.loaded is True
        # NYC 24h at 0.70 — per-city curve clamps above 0.5 to 0.5
        assert c.calibrate(0.70, city="nyc", lead_hours=24) == pytest.approx(0.5)

    def test_unknown_city_falls_through_to_global(self, cal_json_path: str) -> None:
        """Unknown city + known lead → global curve kicks in."""
        c = IsotonicCalibrator(json_path=cal_json_path)
        # Unknown "foo" city at 24h → global 24h (identity in fixture)
        assert c.calibrate(0.42, city="foo", lead_hours=24) == pytest.approx(0.42)
        # Unknown "foo" city at 48h → global 48h (half-shrinkage)
        assert c.calibrate(0.80, city="foo", lead_hours=48) == pytest.approx(0.40)

    def test_unknown_city_unknown_lead_identity(self, cal_json_path: str) -> None:
        """(unknown city, unknown lead-bucket)-identity fallback when no
        per-city AND no global curve is applicable. Our JSON doesn't have
        a 72h or 12h bucket so nothing to fall back to → identity.

        But note: lead bucketing in calibrate() maps every lead to either
        24 or 48 (via `bucket_lead = 24 if lead_hours < 36 else 48`), so
        we always hit a known bucket if global has entries. To test pure
        identity, we need a calibrator with EMPTY global AND no per-city.
        """
        import json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            _json.dump({"global": {}, "per_city": {}}, f)
            empty_path = f.name
        try:
            c = IsotonicCalibrator(json_path=empty_path)
            # loaded is True (JSON parsed) but both maps are empty →
            # every lookup falls to identity.
            assert c.calibrate(0.42, city="foo", lead_hours=24) == pytest.approx(0.42)
            assert c.calibrate(0.80, city="foo", lead_hours=48) == pytest.approx(0.80)
        finally:
            os.unlink(empty_path)

    def test_lead_bucketing_rounds_to_24_or_48(self, cal_json_path: str) -> None:
        """Lead < 36h → bucket 24. Lead >= 36h → bucket 48. Beyond 48
        still falls into the 48 bucket (not extrapolated)."""
        c = IsotonicCalibrator(json_path=cal_json_path)
        # 6h lead → bucket 24 → global 24 (identity)
        assert c.calibrate(0.30, city="foo", lead_hours=6) == pytest.approx(0.30)
        # 35h lead → bucket 24 → global 24 (identity)
        assert c.calibrate(0.30, city="foo", lead_hours=35) == pytest.approx(0.30)
        # 36h lead → bucket 48 → global 48 (half)
        assert c.calibrate(0.80, city="foo", lead_hours=36) == pytest.approx(0.40)
        # 72h lead → still bucket 48 (not extrapolated to some 72h curve)
        assert c.calibrate(0.80, city="foo", lead_hours=72) == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# Integration — ensemble_bracket_probability applies isotonic by default
# ---------------------------------------------------------------------------


def _mk_forecast(city: str, lead_hours: int, temp_f: float, model: WeatherModel) -> TemperatureForecast:
    t0 = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    return TemperatureForecast(
        city=city,
        model=model,
        init_time=t0,
        valid_time=t0 + timedelta(hours=lead_hours),
        lead_hours=lead_hours,
        forecast_high_f=temp_f,
    )


def _mk_dist(sigma: float = 2.5) -> ErrorDistribution:
    return ErrorDistribution(
        city="nyc",
        model=WeatherModel.GFS,
        regime=SynopticRegime.STABLE_HIGH,
        lead_hours=24,
        family=DistributionFamily.NORMAL,
        mu=0.0,
        sigma=sigma,
        n_samples=100,
    )


class TestEnsembleAppliesIsotonic:
    def test_apply_isotonic_true_shrinks_nyc_24h(self, cal_json_path: str, monkeypatch) -> None:
        """With the fixture calibrator (nyc 24h clamps above 0.5 to 0.5),
        a forecast that gives raw p > 0.5 should come out at 0.5 after
        calibration."""
        # Force the module-level singleton to load our fixture JSON.
        monkeypatch.setattr(
            "polymarket_strat.domain.weather.forecast._DEFAULT_ISOTONIC_JSON",
            cal_json_path,
        )
        _reset_isotonic_cache_for_tests()

        calc = BracketProbabilityCalculator()
        fcs = [_mk_forecast("nyc", 24, 75.0, WeatherModel.GFS)]
        dists = [_mk_dist(sigma=1.0)]
        # A forecast of 68°F with σ=1 and bracket [66, ∞) gives ~97% raw
        raw_prob, _ = calc.ensemble_bracket_probability(
            forecasts=fcs, error_dists=dists,
            lower_f=66.0, upper_f=200.0,
            apply_isotonic=False,
        )
        cal_prob, _ = calc.ensemble_bracket_probability(
            forecasts=fcs, error_dists=dists,
            lower_f=66.0, upper_f=200.0,
            apply_isotonic=True,
        )
        assert raw_prob > 0.9  # sanity
        assert cal_prob == pytest.approx(0.5)  # clamped by fixture curve

    def test_apply_isotonic_false_returns_raw(self, cal_json_path: str, monkeypatch) -> None:
        """Regression safety: walk-forward backtests pass apply_isotonic=False
        and must get the uncalibrated value. This keeps the isotonic
        training from being polluted by its own output."""
        monkeypatch.setattr(
            "polymarket_strat.domain.weather.forecast._DEFAULT_ISOTONIC_JSON",
            cal_json_path,
        )
        _reset_isotonic_cache_for_tests()

        calc = BracketProbabilityCalculator()
        fcs = [_mk_forecast("nyc", 24, 75.0, WeatherModel.GFS)]
        dists = [_mk_dist(sigma=1.0)]

        p1, _ = calc.ensemble_bracket_probability(
            forecasts=fcs, error_dists=dists,
            lower_f=66.0, upper_f=200.0,
            apply_isotonic=False,
        )
        p2, _ = calc.ensemble_bracket_probability(
            forecasts=fcs, error_dists=dists,
            lower_f=66.0, upper_f=200.0,
            apply_isotonic=False,
        )
        # Both should be identical and > 0.9 (not clamped by calibration)
        assert p1 == pytest.approx(p2)
        assert p1 > 0.9


# ---------------------------------------------------------------------------
# Real-file smoke test (skipped if the file isn't around)
# ---------------------------------------------------------------------------


def test_real_calibration_file_loads_if_present() -> None:
    """If data/weather/isotonic_calibration.json exists (produced by
    scripts/fit_isotonic.py), sanity-check that it loads and returns
    plausible remappings. Skipped in CI where the file may not exist."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    real_path = repo_root / "data" / "weather" / "isotonic_calibration.json"
    if not real_path.exists():
        pytest.skip("no real calibration JSON to smoke-test against")
    c = IsotonicCalibrator(json_path=str(real_path))
    assert c.loaded
    # Just confirm every output is in [0,1] and preserves monotonicity at
    # a coarse grid.
    prev = -0.01
    for p in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        out = c.calibrate(p, city="nyc", lead_hours=24)
        assert 0.0 <= out <= 1.0
        assert out >= prev  # monotonic in the input
        prev = out
