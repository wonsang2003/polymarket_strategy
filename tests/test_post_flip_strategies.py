"""Pin Strategies 1, 3, 7 — the post-paradigm-flip refactor.

After -$687 cumulative loss demonstrated that "model > market = edge"
is wrong, we shipped three strategies that respect efficient markets:
  S1 — coherence arbitrage (pure arithmetic violations)
  S3 — ensemble-confidence-weighted blending
  S7 — Claude API qualitative gate

This file pins constants and the core decision boundaries.
"""
from __future__ import annotations

from datetime import date as ddate
from unittest.mock import patch

import pytest

from polymarket_strat.domain.weather.coherence import (
    CoherenceOpportunity,
    detect_coherence_violations,
)
from polymarket_strat.domain.weather.models import BracketContract
from polymarket_strat.domain.weather.strategy import (
    _S3_MIN_BLENDED_EDGE,
    _S3_MIN_CONFIDENCE,
    _S3_SIGMA_DECAY,
)


# ===========================================================================
# Strategy 1 — coherence arbitrage
# ===========================================================================


def _mk_bracket(
    *,
    city: str = "nyc",
    target_date: ddate = ddate(2026, 4, 25),
    lower_f: float = 60.0,
    upper_f: float = 200.0,
    market_price: float = 0.30,
) -> BracketContract:
    """Minimal BracketContract for coherence tests."""
    return BracketContract(
        market_id=f"mkt_{lower_f}_{upper_f}",
        question=f"Highest temperature in {city} ≥ {lower_f}°F?",
        city=city,
        target_date=target_date,
        lower_f=lower_f,
        upper_f=upper_f,
        token_id_yes=f"tk_yes_{lower_f}",
        token_id_no=f"tk_no_{lower_f}",
        market_price_yes=market_price,
        best_ask_yes=market_price,
        best_bid_yes=market_price - 0.01,
        spread=0.02,
        liquidity=1000.0,
    )


class TestCoherenceLowerBoundFamily:
    """Lower-bound brackets: 'X or higher'. P should DESCEND as
    threshold ASCENDS. Buy the cheaper bracket on violation."""

    def test_no_violation_passes_through(self):
        """Properly priced ladder: P descends as threshold ascends."""
        contracts = [
            _mk_bracket(lower_f=60, market_price=0.80),  # easy
            _mk_bracket(lower_f=70, market_price=0.50),  # medium
            _mk_bracket(lower_f=80, market_price=0.20),  # hard
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 0

    def test_clear_violation_detected(self):
        """≥75°F priced 0.45 but ≥73°F priced 0.40 — buy 73°F bracket."""
        contracts = [
            _mk_bracket(lower_f=73, market_price=0.40),
            _mk_bracket(lower_f=75, market_price=0.45),  # violation
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 1
        assert opps[0].family == "lower_bound"
        assert opps[0].long_bracket.lower_f == 73   # cheap easier bracket
        assert opps[0].short_bracket.lower_f == 75
        assert opps[0].violation_magnitude == pytest.approx(0.05, abs=0.001)

    def test_tiny_violation_below_threshold_skipped(self):
        """1¢ violations are below the 3¢ minimum — likely
        microstructure noise."""
        contracts = [
            _mk_bracket(lower_f=70, market_price=0.40),
            _mk_bracket(lower_f=72, market_price=0.41),  # 1¢ inversion
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 0

    def test_penny_artifact_long_skipped(self):
        """If long-side price is below 0.10, penny-artifact territory —
        unrealistic fills, skip."""
        contracts = [
            _mk_bracket(lower_f=85, market_price=0.05),  # penny
            _mk_bracket(lower_f=87, market_price=0.20),  # 15¢ violation
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 0  # long is too cheap, skip


class TestCoherenceUpperBoundFamily:
    """Upper-bound brackets: 'X or lower'. P should ASCEND as threshold
    ASCENDS (looser ceiling = easier). Buy the looser bracket on
    violation."""

    def test_clear_upper_bound_violation(self):
        """≤55°F priced 0.50 but ≤53°F priced 0.55 — buy 55°F bracket."""
        contracts = [
            _mk_bracket(lower_f=-50, upper_f=53, market_price=0.55),  # tight, but expensive
            _mk_bracket(lower_f=-50, upper_f=55, market_price=0.50),  # looser, cheaper — buy this
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 1
        assert opps[0].family == "upper_bound"
        assert opps[0].long_bracket.upper_f == 55  # buy looser
        assert opps[0].short_bracket.upper_f == 53

    def test_correctly_ordered_upper_bound_no_violation(self):
        """≤55°F properly priced higher than ≤50°F — monotonic, no arb."""
        contracts = [
            _mk_bracket(lower_f=-50, upper_f=50, market_price=0.30),
            _mk_bracket(lower_f=-50, upper_f=55, market_price=0.50),
            _mk_bracket(lower_f=-50, upper_f=60, market_price=0.70),
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 0


class TestCoherenceMixedFamilies:
    """Range brackets and cross-family pairs are skipped."""

    def test_range_bracket_ignored(self):
        """Brackets with both finite bounds (range) aren't evaluated —
        they have legitimately non-monotonic prices."""
        contracts = [
            _mk_bracket(lower_f=70, upper_f=72, market_price=0.30),
            _mk_bracket(lower_f=72, upper_f=75, market_price=0.40),
            _mk_bracket(lower_f=75, upper_f=78, market_price=0.20),
        ]
        opps = detect_coherence_violations(contracts)
        assert len(opps) == 0  # no opps — these are range brackets


# ===========================================================================
# Strategy 3 — ensemble confidence blend
# ===========================================================================


class TestStrategy3Constants:
    def test_sigma_decay_is_3F(self):
        """3°F decay: σ=3°F gives confidence ≈ exp(-1) ≈ 0.37."""
        assert _S3_SIGMA_DECAY == 3.0

    def test_min_confidence_threshold(self):
        """Must clear 50% confidence to trade. At 50%, w=0.25 → market gets 75% weight."""
        assert _S3_MIN_CONFIDENCE == 0.50

    def test_blended_edge_threshold(self):
        """8¢ — between the standard 5¢/10¢ floors. Honest middle."""
        assert _S3_MIN_BLENDED_EDGE == 0.08

    def test_blend_formula_calm_day(self):
        """σ=0 (perfect agreement) → confidence=1, blended = pure model."""
        import math
        sigma = 0.0
        confidence = math.exp(-sigma / _S3_SIGMA_DECAY)
        w = confidence ** 2
        # p_blended = w * p_model + (1-w) * p_market
        # at w=1, p_blended = p_model
        assert w == pytest.approx(1.0)
        p_model = 0.7
        p_market = 0.5
        p_blended = w * p_model + (1 - w) * p_market
        assert p_blended == pytest.approx(0.7)

    def test_blend_formula_storm_day(self):
        """σ=12°F (chaos) → confidence ≈ exp(-4) ≈ 0.018, w² ≈ 0.0003.
        Blended ≈ pure market — defer entirely."""
        import math
        sigma = 12.0
        confidence = math.exp(-sigma / _S3_SIGMA_DECAY)
        w = confidence ** 2
        assert confidence < 0.05
        assert w < 0.005
        p_model = 0.97  # the artifact value from our Apr 25 audit
        p_market = 0.67
        p_blended = w * p_model + (1 - w) * p_market
        # Should be very close to market, killing the artifact
        assert p_blended == pytest.approx(p_market, abs=0.01)


# ===========================================================================
# Strategy 7 — Claude API gate
# ===========================================================================


class TestClaudeGate:
    def test_disabled_without_api_key_passes_all(self):
        """No ANTHROPIC_API_KEY → every trade is auto-approved (no-op)."""
        from polymarket_strat.notifications.claude_gate import ClaudeTradeReviewer
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False):
            reviewer = ClaudeTradeReviewer(api_key="")
            assert not reviewer.enabled
            review = reviewer.review_trade({
                "city": "nyc", "target_date": "2026-04-25",
                "model_prob": 0.99, "market_prob": 0.30,
                "edge_after_fees": 0.65,
            })
            assert review.approved is True
            assert "disabled" in review.reasoning

    def test_threshold_default(self):
        """Approval requires confidence ≥ 0.6."""
        from polymarket_strat.notifications.claude_gate import (
            ClaudeTradeReviewer,
            _CLAUDE_APPROVAL_THRESHOLD,
        )
        assert _CLAUDE_APPROVAL_THRESHOLD == 0.60

    def test_user_prompt_includes_all_context(self):
        """The user prompt must surface every artifact-detection signal
        Claude needs: model_prob, market_prob, ensemble spread,
        per-model forecasts, lead time, regime."""
        from polymarket_strat.notifications.claude_gate import build_user_prompt
        ctx = {
            "city": "toronto",
            "target_date": "2026-04-25",
            "question": "Will the highest temperature in Toronto be 50°F or higher?",
            "bracket_lower_f": 50.0,
            "bracket_upper_f": 200.0,
            "model_prob": 1.00,  # the artifact pattern
            "market_prob": 0.67,
            "edge_after_fees": 0.32,
            "ensemble_spread_f": 0.5,
            "forecast_high_f_per_model": {"gfs": 55.0, "ecmwf": 55.5},
            "regime": "stable_high",
            "lead_hours": 18.0,
            "season": 1,
            "strategy_subtype": None,
        }
        prompt = build_user_prompt(ctx)
        # Sanity checks — the artifact-relevant features are visible
        assert "1.000" in prompt or "1.00" in prompt
        assert "0.670" in prompt or "0.67" in prompt
        assert "toronto" in prompt
        assert "ensemble" in prompt.lower()
        assert "gfs" in prompt.lower()
        assert "ecmwf" in prompt.lower()

    def test_fail_open_on_api_error(self):
        """When the API call raises, gate fails OPEN (passes the trade).
        Conservative would be fail-closed but we never want a transient
        outage to halt all trading — quant pipeline already approved."""
        from polymarket_strat.notifications.claude_gate import ClaudeTradeReviewer

        reviewer = ClaudeTradeReviewer(api_key="fake-key-for-test")
        # Don't actually call API — patch it to raise
        class MockClient:
            def __init__(self):
                self.messages = self
            def create(self, **kw):
                raise RuntimeError("simulated API outage")
        reviewer._client = MockClient()
        reviewer._enabled = True

        review = reviewer.review_trade({
            "city": "nyc", "model_prob": 0.5, "market_prob": 0.5,
        })
        # Fail-open
        assert review.approved is True
        assert "API error" in review.reasoning or "API call failed" in review.reasoning.lower() or "auto-approve" in review.reasoning
