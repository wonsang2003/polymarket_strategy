"""Pin the Apr 24 2026 edge-gate and city-blocklist contracts.

Why this test exists:
  Two surgical changes landed Apr 24 2026 after a live paper audit of 32
  settled trades showed ~2x model overconfidence on low-p brackets and
  bracket-parsing artifacts at three cities (§14 priority 4, §15 Apr 24
  strategy-review bullet):

    (1) `BracketProbabilityCalculator.edge()` now applies a p-scaled
        min-edge:
          required = max(min_edge, low_p_min_edge) if p_model < 0.50
          required = min_edge                      otherwise
        Defaults: 5¢ at p ≥ 0.50, 10¢ at p < 0.50. Rationale: at 2x
        overconfidence a flat 5¢ floor has negative EV for p < 0.50.

    (2) `_BLOCKED_CITIES = frozenset({"la", "seattle", "mexico_city"})`
        is checked at the top of `_analyze_weather_brackets` before any
        forecast fetch. All three showed demonstrably unphysical edge
        distributions in the audit window (avg_p=1.0, avg_edge=+0.52,
        avg_edge=+0.47 respectively).

  This file locks in both contracts so the next refactor is deliberate,
  not accidental.
"""
from __future__ import annotations

import pytest

from polymarket_strat.domain.weather.forecast import BracketProbabilityCalculator
from polymarket_strat.domain.weather.strategy import _BLOCKED_CITIES


# ---------------------------------------------------------------------------
# _BLOCKED_CITIES — contents + type
# ---------------------------------------------------------------------------


def test_blocked_cities_is_empty_post_deprecation() -> None:
    """As of Apr 25 2026 LATE, _BLOCKED_CITIES is intentionally empty.

    Why this changed:
      The blocklist's original justifications were based on losses from
      a now-fixed pipeline (leaky 1-yr climatology) and were duplicative
      of newer safety layers:
        - Per-city ECE shrinkage (#52, #76) auto-shrinks size for
          poorly-calibrated cities.
        - Plan B p>0.85+edge>0.20 cap (#50) catches the artifact pattern
          that produced most of the -$687 loss tape.
        - BucketBlocklist (#53) re-blocks (city, lead, regime) buckets
          nightly based on REALIZED P&L, not synthetic.
        - 48h hard lead cap kills the Mexico City D+2 pathology.

      Today's fine-bin ECE audit also showed the previously-blocked
      cities are NOT obviously worse calibrated than the unblocked ones
      (sao_paulo / munich / amsterdam are the BEST calibrated cities in
      the dataset; dubai at 13.27% is unblocked but worst).

    Reinstatement path:
      Use this variable for STRUCTURAL bugs only (e.g. parser failure
      that emits garbage bracket bounds). For data-driven blocks based
      on realized losses, rely on BucketBlocklist + nightly fit_*.py
      cron jobs.

    Changing the contents requires explicit evidence in the
    _BLOCKED_CITIES docstring above the assignment, and an update to
    this test.
    """
    assert _BLOCKED_CITIES == frozenset()


def test_blocked_cities_is_frozenset() -> None:
    """frozenset, not set — blocklist is a module-level constant and
    must not be mutable at runtime."""
    assert isinstance(_BLOCKED_CITIES, frozenset)


# ---------------------------------------------------------------------------
# BracketProbabilityCalculator.edge() — p-scaled min-edge
# ---------------------------------------------------------------------------


class TestEdgeHighP:
    """p_model ≥ 0.50: flat 5¢ min-edge gate (high-p bucket)."""

    def test_accepts_comfortable_edge(self) -> None:
        """p=0.60, market=0.30 → raw 30¢, adjusted ~29.6¢ → tradeable."""
        raw, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.60, market_prob=0.30
        )
        assert raw == pytest.approx(0.30)
        assert tradeable is True
        assert adjusted > 0.05

    def test_accepts_marginal_edge_at_threshold(self) -> None:
        """p=0.55, market=0.47 → raw 8¢, adjusted ~7.4¢ → passes 5¢."""
        raw, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.55, market_prob=0.47
        )
        assert tradeable is True
        assert adjusted >= 0.05

    def test_rejects_below_flat_5c(self) -> None:
        """p=0.55, market=0.52 → raw 3¢, below 5¢ flat → reject."""
        _, _, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.55, market_prob=0.52
        )
        assert tradeable is False


class TestEdgeLowP:
    """p_model < 0.50: p-scaled 10¢ min-edge gate (low-p bucket)."""

    def test_rejects_5c_edge(self) -> None:
        """p=0.30, market=0.22 → raw 8¢, adjusted ~7.5¢ — passes 5¢
        flat but NOT the 10¢ low-p floor. This is the core new
        behavior vs. the Apr 19 flat-5¢ gate."""
        _, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.30, market_prob=0.22
        )
        # Adjusted is between 5¢ and 10¢ — exactly the range the new
        # gate filters out.
        assert 0.05 <= adjusted < 0.10
        assert tradeable is False

    def test_rejects_9c_edge(self) -> None:
        """Edge just below the 10¢ low-p floor still rejects."""
        _, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.35, market_prob=0.26
        )
        assert adjusted < 0.10
        assert tradeable is False

    def test_accepts_15c_edge(self) -> None:
        """p=0.30, market=0.14 → adjusted ~14¢, above 10¢ floor → trade.
        Market is at 0.15 band floor to stay in-band."""
        _, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.30, market_prob=0.15
        )
        assert adjusted >= 0.10
        assert tradeable is True

    def test_accepts_just_above_floor(self) -> None:
        """Adjusted edge just above 10¢ → tradeable. Sanity-check the
        boundary without requiring exact fp equality — the gate uses
        `adjusted < required_edge`, i.e. strict-less-than, so any
        adjusted ≥ 10¢ passes.

        Market math: adjusted = (p-m) - 0.02 * p * (1-m) = 0.392 - 0.992*m
        at p=0.40. Setting m=0.29 gives adjusted ≈ 0.1043 — a hair
        above 10¢, clearly tradeable.
        """
        _, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.40, market_prob=0.29
        )
        assert adjusted >= 0.10
        assert tradeable is True


class TestEdgeMarketBandStillBinds:
    """Gate 1 (market band 0.15-0.75) still takes precedence over the
    new p-scaled Gate 2. A 30¢ edge in an out-of-band market is still
    rejected — both because penny artifacts dominate below 0.15 and
    because crushed payoffs above 0.75 yield poor Sharpe regardless of
    raw edge."""

    def test_rejects_below_band_even_with_huge_edge(self) -> None:
        _, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.40, market_prob=0.10
        )
        assert adjusted > 0.20  # huge edge
        assert tradeable is False  # but out of band

    def test_rejects_above_band_even_with_big_edge(self) -> None:
        _, adjusted, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.95, market_prob=0.80
        )
        assert adjusted > 0.10
        assert tradeable is False


class TestEdgeCustomKwargs:
    """Custom low_p_threshold / low_p_min_edge overrides must work.
    Lets future calibration work A/B different floors without editing
    the default signature."""

    def test_custom_low_p_threshold_tightens_mid_range(self) -> None:
        """With threshold=0.60, a p=0.55 / edge=7¢ bracket that
        passes under defaults (0.55 ≥ 0.50 → 5¢ floor) should now be
        rejected (0.55 < 0.60 → 10¢ floor)."""
        _, adjusted, tradeable_default = BracketProbabilityCalculator.edge(
            model_prob=0.55, market_prob=0.47
        )
        # Sanity: tradeable under defaults
        assert tradeable_default is True

        _, _, tradeable_tight = BracketProbabilityCalculator.edge(
            model_prob=0.55,
            market_prob=0.47,
            low_p_threshold=0.60,
        )
        assert adjusted < 0.10  # between 5¢ and 10¢
        assert tradeable_tight is False

    def test_custom_low_p_min_edge_loosens_low_bucket(self) -> None:
        """With low_p_min_edge=0.05, the p<0.50 bucket reverts to the
        Apr 19 flat-5¢ behavior. Useful for running back-to-back
        walk-forward sims under the old vs new gate."""
        # p=0.30, market=0.22 → adjusted ~7.5¢ — rejected under the
        # default 10¢ low-p floor, accepted if we loosen back to 5¢.
        _, _, tradeable = BracketProbabilityCalculator.edge(
            model_prob=0.30,
            market_prob=0.22,
            low_p_min_edge=0.05,
        )
        assert tradeable is True


class TestEdgeReturnsTuple:
    """Shape contract — callers unpack (raw, adjusted, tradeable)."""

    def test_returns_three_floats_plus_bool(self) -> None:
        result = BracketProbabilityCalculator.edge(
            model_prob=0.50, market_prob=0.30
        )
        assert isinstance(result, tuple)
        assert len(result) == 3
        raw, adjusted, tradeable = result
        assert isinstance(raw, float)
        assert isinstance(adjusted, float)
        assert isinstance(tradeable, bool)

    def test_adjusted_less_than_raw_for_positive_edge(self) -> None:
        """Fee drag = fee_rate * p * (1-m) is strictly positive when
        p>0 and m<1, so adjusted must be below raw."""
        raw, adjusted, _ = BracketProbabilityCalculator.edge(
            model_prob=0.60, market_prob=0.30
        )
        assert adjusted < raw
