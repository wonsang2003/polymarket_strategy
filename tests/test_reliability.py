"""Pin the CityReliability multiplier formula and fallback behavior.

Why this test exists:
  Apr 24 2026 — Tier 2b of Apr 24 dev plan. The reliability multiplier
  replaces the binary _BLOCKED_CITIES blocklist with a smooth shrinkage
  for cities whose walk-forward Brier exceeds the Tier-A target (0.12)
  or whose real-forecast sample count is below 50. Formula:

      multiplier = min(1, 0.12/brier) × min(1, n_samples/50)

  This file pins:
    1. Formula correctness across representative (brier, n) pairs.
    2. Graceful fallback (multiplier=1.0) when JSON is missing or city
       is unknown — ensures pre-reliability code behavior is preserved
       on day 1 of deploy before reliability.json is written.
    3. Lead-fallback when a city has one lead's data but not the other.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from polymarket_strat.domain.weather.reliability import (
    CityReliability,
    reset_reliability_cache_for_tests,
)


@pytest.fixture
def reliability_json():
    """Write a hand-crafted reliability JSON with known brier/n values."""
    payload = {
        "fit_at_utc": "2026-04-24T17:00:00+00:00",
        "per_city": {
            # Tier-A: Wellington — already at target, full sizing
            "wellington": {
                "24": {"brier": 0.098, "n_samples": 200},
                "48": {"brier": 0.115, "n_samples": 200},
            },
            # Tier-B: NYC — slightly above target Brier, full samples
            "nyc": {
                "24": {"brier": 0.149, "n_samples": 1500},
                "48": {"brier": 0.196, "n_samples": 1500},
            },
            # Small-sample city: hypothetical new addition
            "new_city": {
                "24": {"brier": 0.10, "n_samples": 25},
            },
            # City with only one lead (48h coverage only)
            "half_city": {
                "48": {"brier": 0.13, "n_samples": 100},
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Formula — core correctness
# ---------------------------------------------------------------------------


class TestMultiplierFormula:
    def test_tier_a_city_gets_full_size(self, reliability_json: str) -> None:
        """Wellington Brier 0.098 (below 0.12 target), n=200 → mult = 1.0."""
        r = CityReliability(json_path=reliability_json)
        mult, diag = r.multiplier(city="wellington", lead_hours=24)
        assert mult == pytest.approx(1.0)
        assert diag["brier_mult"] == pytest.approx(1.0)
        assert diag["samples_mult"] == pytest.approx(1.0)
        assert diag["fallback"] is False

    def test_mid_brier_city_gets_shrunk(self, reliability_json: str) -> None:
        """NYC Brier 0.149 → brier_mult = 0.12/0.149 ≈ 0.805."""
        r = CityReliability(json_path=reliability_json)
        mult, diag = r.multiplier(city="nyc", lead_hours=24)
        # brier_mult = 0.12 / 0.149 ≈ 0.805, samples_mult = 1.0
        assert diag["brier_mult"] == pytest.approx(0.12 / 0.149, abs=1e-3)
        assert diag["samples_mult"] == pytest.approx(1.0)
        assert mult == pytest.approx(0.12 / 0.149, abs=1e-3)

    def test_small_sample_city_gets_shrunk_by_samples(self, reliability_json: str) -> None:
        """new_city: Brier 0.10 (above target) but n=25 < 50 → samples_mult = 0.5."""
        r = CityReliability(json_path=reliability_json)
        mult, diag = r.multiplier(city="new_city", lead_hours=24)
        assert diag["brier_mult"] == pytest.approx(1.0)  # Brier better than target
        assert diag["samples_mult"] == pytest.approx(0.5)  # 25/50
        assert mult == pytest.approx(0.5)

    def test_48h_fallback_when_only_24h_unavailable(self, reliability_json: str) -> None:
        """half_city has 48h only — a 24h query should fall back to 48h."""
        r = CityReliability(json_path=reliability_json)
        mult, diag = r.multiplier(city="half_city", lead_hours=24)
        # Uses the 48h fit
        assert mult == pytest.approx(0.12 / 0.13, abs=1e-3)
        assert diag["fallback"] is False  # we had data, just from different lead

    def test_worst_case_48h_shrinkage(self, reliability_json: str) -> None:
        """NYC 48h Brier 0.196 → brier_mult = 0.12/0.196 ≈ 0.61."""
        r = CityReliability(json_path=reliability_json)
        mult, _ = r.multiplier(city="nyc", lead_hours=48)
        assert mult == pytest.approx(0.12 / 0.196, abs=1e-3)
        assert mult < 0.65  # notably shrunk — this city gets HALF position at 48h


# ---------------------------------------------------------------------------
# Fallback behavior — safety on day-1 deploy
# ---------------------------------------------------------------------------


class TestFallback:
    def test_missing_file_returns_one(self) -> None:
        """No JSON → multiplier 1.0 unconditionally. This is how reliability
        rolls out safely: until a JSON exists, nothing changes."""
        r = CityReliability(json_path="/tmp/does-not-exist-xyz-xyz.json")
        mult, diag = r.multiplier(city="nyc", lead_hours=24)
        assert mult == pytest.approx(1.0)
        assert diag["fallback"] is True
        assert diag["reason"] == "no_file"

    def test_unknown_city_returns_one(self, reliability_json: str) -> None:
        """Unknown city → 1.0, fallback=True."""
        r = CityReliability(json_path=reliability_json)
        mult, diag = r.multiplier(city="atlantis", lead_hours=24)
        assert mult == pytest.approx(1.0)
        assert diag["fallback"] is True
        assert diag["reason"] == "unknown_city"

    def test_malformed_json_returns_one(self) -> None:
        """Corrupt JSON → behaves like no-file (identity)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{garbled}")
            path = f.name
        try:
            r = CityReliability(json_path=path)
            mult, diag = r.multiplier(city="nyc", lead_hours=24)
            assert mult == pytest.approx(1.0)
            assert diag["fallback"] is True
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Integration sanity — the real file, if it's there
# ---------------------------------------------------------------------------


def test_real_reliability_file_if_present() -> None:
    """If data/weather/reliability.json exists, sanity-check each city
    produces a multiplier in [0, 1]."""
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "data" / "weather" / "reliability.json"
    if not path.exists():
        pytest.skip("no reliability.json")

    r = CityReliability(json_path=str(path))
    assert r.loaded
    # Spot-check a handful of cities
    for city in ("wellington", "nyc", "tokyo", "seoul"):
        mult, diag = r.multiplier(city=city, lead_hours=24)
        assert 0.0 <= mult <= 1.0
        assert "brier" in diag


# ---------------------------------------------------------------------------
# Per-city ECE multiplier — Apr 25 2026
# ---------------------------------------------------------------------------


@pytest.fixture
def reliability_with_ece_json():
    """Reliability JSON with the new ece_shrunk + n_test fields populated.
    Models the post-compute_per_city_ece.py shape."""
    payload = {
        "fit_at_utc": "2026-04-25T17:00:00+00:00",
        "per_city_ece_meta": {
            "aggregate_ece": 0.0445,
            "shrinkage_k": 50,
        },
        "per_city": {
            # Well-calibrated city (low ECE)
            "london": {
                "24": {
                    "brier": 0.115,
                    "n_samples": 1500,
                    "ece_shrunk": 0.089,
                    "n_test": 126,
                    "ece_raw": 0.106,
                },
            },
            # Mid-calibration city (NYC-like)
            "nyc": {
                "24": {
                    "brier": 0.115,
                    "n_samples": 1500,
                    "ece_shrunk": 0.126,
                    "n_test": 126,
                    "ece_raw": 0.158,
                },
            },
            # Poorly-calibrated city — should hit the floor
            "miami_bad": {
                "24": {
                    "brier": 0.115,
                    "n_samples": 1500,
                    "ece_shrunk": 0.50,  # extreme miscalibration
                    "n_test": 72,
                    "ece_raw": 0.60,
                },
            },
            # ECE field missing — backward compat
            "no_ece_city": {
                "24": {"brier": 0.115, "n_samples": 1500},
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    yield path
    os.unlink(path)


class TestEceMultiplier:
    def test_low_ece_city_close_to_one(self, reliability_with_ece_json: str) -> None:
        """London ece_shrunk=0.089 → ece_mult = 1 - 1.5*0.089 = 0.867."""
        r = CityReliability(json_path=reliability_with_ece_json)
        mult, diag = r.multiplier(city="london", lead_hours=24)
        assert diag["ece_mult"] == pytest.approx(1.0 - 1.5 * 0.089, abs=1e-3)
        assert diag["ece_shrunk"] == pytest.approx(0.089, abs=1e-3)
        # Combined: brier 0.115 < target 0.12 → brier_mult=1, samples_mult=1, ece_mult≈0.867
        assert mult == pytest.approx(0.8665, abs=1e-3)

    def test_mid_ece_city_proportional_shrinkage(
        self, reliability_with_ece_json: str
    ) -> None:
        """NYC ece_shrunk=0.126 → ece_mult = 1 - 1.5*0.126 = 0.811."""
        r = CityReliability(json_path=reliability_with_ece_json)
        mult, diag = r.multiplier(city="nyc", lead_hours=24)
        assert diag["ece_mult"] == pytest.approx(1.0 - 1.5 * 0.126, abs=1e-3)
        assert mult == pytest.approx(0.811, abs=1e-3)

    def test_extreme_ece_hits_floor(self, reliability_with_ece_json: str) -> None:
        """Severe miscalibration (ECE 0.50) → ece_mult floored at 0.40,
        not pushed to 0.25 by the linear formula."""
        r = CityReliability(json_path=reliability_with_ece_json)
        mult, diag = r.multiplier(city="miami_bad", lead_hours=24)
        # 1 - 1.5 × 0.5 = 0.25 BUT floored at 0.40
        assert diag["ece_mult"] == pytest.approx(0.40, abs=1e-6)
        assert mult == pytest.approx(0.40, abs=1e-3)

    def test_missing_ece_falls_back_to_one(
        self, reliability_with_ece_json: str
    ) -> None:
        """ECE field absent → ece_mult=1.0 (backward compat for old JSON files)."""
        r = CityReliability(json_path=reliability_with_ece_json)
        mult, diag = r.multiplier(city="no_ece_city", lead_hours=24)
        assert diag["ece_mult"] == pytest.approx(1.0)
        assert diag["ece_shrunk"] is None
        # Brier 0.115 < target 0.12 → brier_mult=1, samples_mult=1, mult=1.0
        assert mult == pytest.approx(1.0)

    def test_ece_beta_parameter_overridable(
        self, reliability_with_ece_json: str
    ) -> None:
        """β=2.5 makes shrinkage more aggressive."""
        r = CityReliability(json_path=reliability_with_ece_json)
        mult, diag = r.multiplier(city="nyc", lead_hours=24, ece_beta=2.5)
        assert diag["ece_mult"] == pytest.approx(1.0 - 2.5 * 0.126, abs=1e-3)

    def test_ece_floor_parameter_overridable(
        self, reliability_with_ece_json: str
    ) -> None:
        """Override floor lower → no protection from extreme values."""
        r = CityReliability(json_path=reliability_with_ece_json)
        _, diag = r.multiplier(city="miami_bad", lead_hours=24, ece_floor=0.10)
        # 1 - 1.5*0.5 = 0.25, well above the 0.10 floor → ece_mult=0.25
        assert diag["ece_mult"] == pytest.approx(0.25, abs=1e-3)

    def test_existing_brier_test_still_passes_when_ece_added(
        self, reliability_with_ece_json: str
    ) -> None:
        """Verify the new ECE pass doesn't break the diag schema for old callers.
        Old fields (brier, n_samples, brier_mult, samples_mult, fallback) all
        still present after the additive change."""
        r = CityReliability(json_path=reliability_with_ece_json)
        _, diag = r.multiplier(city="nyc", lead_hours=24)
        for k in ("brier", "n_samples", "brier_mult", "samples_mult", "fallback"):
            assert k in diag, f"missing legacy diagnostic key: {k}"


class TestEceShrinkageMath:
    """Pin the Bayesian shrinkage formula in compute_per_city_ece.shrunk_ece."""

    def test_shrinkage_at_50_50_when_n_equals_k(self) -> None:
        from scripts.compute_per_city_ece import shrunk_ece
        out = shrunk_ece(ece_city=0.20, n_test=50, ece_aggregate=0.10, k=50)
        assert out == pytest.approx(0.15, abs=1e-6)

    def test_shrinkage_pulls_strongly_at_low_n(self) -> None:
        from scripts.compute_per_city_ece import shrunk_ece
        # n=10 with k=50 → 10/(60) city + 50/60 aggregate
        out = shrunk_ece(ece_city=0.20, n_test=10, ece_aggregate=0.05, k=50)
        expected = (10 * 0.20 + 50 * 0.05) / 60
        assert out == pytest.approx(expected, abs=1e-6)

    def test_shrinkage_high_n_dominated_by_city(self) -> None:
        from scripts.compute_per_city_ece import shrunk_ece
        # n=1000, k=50: 95% city weight
        out = shrunk_ece(ece_city=0.20, n_test=1000, ece_aggregate=0.05, k=50)
        expected = (1000 * 0.20 + 50 * 0.05) / 1050
        assert out == pytest.approx(expected, abs=1e-6)
        # confirms it's close to 0.20 (city-dominated)
        assert abs(out - 0.20) < 0.01

    def test_shrinkage_zero_n_returns_aggregate(self) -> None:
        from scripts.compute_per_city_ece import shrunk_ece
        out = shrunk_ece(ece_city=0.99, n_test=0, ece_aggregate=0.05, k=50)
        assert out == pytest.approx(0.05, abs=1e-6)


# ---------------------------------------------------------------------------
# BucketBlocklist — Tier 2c
# ---------------------------------------------------------------------------


from polymarket_strat.domain.weather.reliability import BucketBlocklist


@pytest.fixture
def blocklist_json():
    """Hand-crafted blocked_buckets.json with mixed fine + coarse blocks."""
    payload = {
        "fit_at_utc": "2026-04-24T17:00:00+00:00",
        "n_min": 20,
        "ev_threshold": 0.0,
        "blocked_fine": [
            {
                "city": "seoul",
                "lead_hours": 48,
                "regime": "frontal_passage",
                "n_trades": 25,
                "total_pnl": -120.0,
                "total_notional": 1000.0,
                "ev_normalized": -0.12,
            },
        ],
        "blocked_coarse": [
            {
                "city": "toronto",
                "lead_hours": 24,
                "n_trades": 30,
                "total_pnl": -150.0,
                "total_notional": 1200.0,
                "ev_normalized": -0.125,
            },
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    yield path
    os.unlink(path)


class TestBucketBlocklist:
    def test_fine_match_blocks(self, blocklist_json: str) -> None:
        """Exact (city, lead_bucket, regime) match → blocked."""
        b = BucketBlocklist(json_path=blocklist_json)
        blocked, reason = b.is_blocked(
            city="seoul", lead_hours=48, regime="frontal_passage"
        )
        assert blocked is True
        assert "fine:seoul/48h/frontal_passage" in reason

    def test_fine_match_different_regime_not_blocked(self, blocklist_json: str) -> None:
        """Seoul 48h STABLE_HIGH is NOT in the fine list → pass through."""
        b = BucketBlocklist(json_path=blocklist_json)
        blocked, _ = b.is_blocked(
            city="seoul", lead_hours=48, regime="stable_high"
        )
        assert blocked is False

    def test_coarse_match_blocks_all_regimes(self, blocklist_json: str) -> None:
        """Toronto 24h is in coarse list → blocks every regime for that
        (city, lead_bucket)."""
        b = BucketBlocklist(json_path=blocklist_json)
        for regime in ("stable_high", "frontal_passage", "transition"):
            blocked, reason = b.is_blocked(
                city="toronto", lead_hours=24, regime=regime
            )
            assert blocked is True
            assert "coarse:toronto/24h" in reason

    def test_lead_bucketing_24_48(self, blocklist_json: str) -> None:
        """Lead <36h buckets to 24, >=36h buckets to 48."""
        b = BucketBlocklist(json_path=blocklist_json)
        # 6h lead → bucket 24 → Toronto 24h coarse blocks
        blocked, _ = b.is_blocked(city="toronto", lead_hours=6, regime="x")
        assert blocked is True
        # 48h lead → bucket 48 → Toronto 48h not in list → unblocked
        blocked, _ = b.is_blocked(city="toronto", lead_hours=48, regime="x")
        assert blocked is False

    def test_unblocked_city_passes(self, blocklist_json: str) -> None:
        """Unknown city → not blocked."""
        b = BucketBlocklist(json_path=blocklist_json)
        blocked, _ = b.is_blocked(city="tokyo", lead_hours=24, regime="stable_high")
        assert blocked is False

    def test_missing_file_no_blocks(self) -> None:
        """No JSON → no blocks (safe fallback)."""
        b = BucketBlocklist(json_path="/tmp/does-not-exist-xyz-abc.json")
        blocked, _ = b.is_blocked(city="seoul", lead_hours=48, regime="frontal_passage")
        assert blocked is False
        assert b.loaded is False
