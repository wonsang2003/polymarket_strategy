"""Tests for the production tail-NO strategy (Apr 26 2026).

Pin behavior of:
  - Edge-distance-based gating (gap_to_upper / gap_to_lower)
  - Position sizing tiers
  - Empirical hit rate calc (using a stub error pool)
  - Correlation-group cap
  - Cooldown / duplicate guard
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from polymarket_strat.config import (
    PortfolioState,
    TradingConstraints,
)
from polymarket_strat.domain.weather.models import (
    BracketContract, CITY_REGISTRY, CorrelationGroup,
)
from polymarket_strat.domain.weather import tail_no_strategy as tns


# ---------------------------------------------------------------------------
# Constant pins — refactor protection
# ---------------------------------------------------------------------------


def test_constants_match_design_decisions() -> None:
    assert tns.EDGE_DISTANCE_MIN_F == 1.0
    assert tns.EDGE_DISTANCE_MAX_F == 5.0
    assert tns.LEAD_HOURS_MIN == 4.0
    assert tns.LEAD_HOURS_MAX == 72.0
    # Apr 28 2026 (afternoon) — REVERTED to original 0.05 / 0.03 after
    # the 36h triple-check audit revealed the morning's tightening was
    # built on selection bias from an unrepresentative 8h losing streak.
    # Counterfactual showed the tighter gates would have cost $130 of
    # realized P&L on the same 36h window. The replacement filter is
    # the wide-bracket low-NO-ask gate below.
    assert tns.NO_ASK_MIN == 0.05
    assert tns.NO_ASK_MAX == 0.95
    assert tns.LIQUIDITY_MIN_USD == 200.0
    assert tns.EDGE_FLOOR_PP == 0.03
    # Apr 28 2026 (afternoon) — wide-bracket low-NO-ask gate. Pins:
    #   width > 10°F (sentinel value indicates one-sided bracket)
    #   AND no_ask < 0.50 → reject
    # Lifetime evidence: 15 wide-NO trades, net −$217.27, both outright
    # settle losses (#83 seoul entry 0.19, #187 milan entry 0.34) at
    # NO entry < 0.40.
    assert tns.WIDE_BRACKET_F == 10.0
    assert tns.WIDE_BRACKET_MIN_NO_ASK == 0.50
    assert tns.POSITION_SIZE_LARGE_USD == 50.0
    assert tns.POSITION_SIZE_MEDIUM_USD == 40.0
    assert tns.POSITION_SIZE_BASE_USD == 30.0
    assert tns.CORRELATION_GROUP_CAP_USD == 200.0
    assert tns.CATEGORY == "weather_tail_no"


# ---------------------------------------------------------------------------
# _ev_per_dollar — Polymarket fee-on-winnings math
# ---------------------------------------------------------------------------


class TestEVPerDollar:
    def test_high_p_low_no_ask(self) -> None:
        # P(NO)=99%, no_ask=$0.10 → win 9.0/dollar net of fee, EV ~ 0.99*8.82 - 0.01 = 8.72
        ev = tns._ev_per_dollar(0.99, 0.10)
        assert 8.5 < ev < 8.9

    def test_breakeven(self) -> None:
        # At p_no = no_ask (market efficient), EV slightly negative due to fee
        ev = tns._ev_per_dollar(0.50, 0.50)
        assert -0.02 < ev < 0.0

    def test_zero_no_ask_returns_zero(self) -> None:
        assert tns._ev_per_dollar(0.99, 0.0) == 0.0

    def test_full_no_ask_returns_zero(self) -> None:
        assert tns._ev_per_dollar(0.99, 1.0) == 0.0


# ---------------------------------------------------------------------------
# Position sizing tiers
# ---------------------------------------------------------------------------


class TestPositionSize:
    def test_large_ev(self) -> None:
        assert tns._position_size_usd(2.0) == 50.0
        assert tns._position_size_usd(1.0) == 50.0

    def test_medium_ev(self) -> None:
        assert tns._position_size_usd(0.5) == 40.0
        assert tns._position_size_usd(0.3) == 40.0

    def test_base_ev(self) -> None:
        assert tns._position_size_usd(0.15) == 30.0
        assert tns._position_size_usd(0.1) == 30.0

    def test_below_floor_skipped(self) -> None:
        assert tns._position_size_usd(0.05) == 0.0
        assert tns._position_size_usd(-0.5) == 0.0


# ---------------------------------------------------------------------------
# EmpiricalHitRate — stub-injected error pool
# ---------------------------------------------------------------------------


def _make_engine_with(errors_24: list[float], errors_48: list[float] | None = None
                     ) -> tns.EmpiricalHitRate:
    """Return an engine whose internal error pool is pre-loaded — bypasses DB."""
    e = tns.EmpiricalHitRate.__new__(tns.EmpiricalHitRate)
    e._db_path = ""
    e._errors_24 = list(errors_24)
    e._errors_48 = list(errors_48 or errors_24)
    e._loaded = True
    return e


class TestEmpiricalHitRate:
    def test_p_no_below_uniform(self) -> None:
        # Errors uniformly distributed in [-5, 5]. For gap_to_upper=2, NO wins
        # iff error < 2 → P = 7/10 = 0.7.
        errors = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]
        engine = _make_engine_with(errors)
        # error < 2 → -5..1 = 7 of 11 = 0.636
        assert engine.p_no_below(24, 2.0) == pytest.approx(7 / 11)

    def test_p_no_above(self) -> None:
        errors = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]
        engine = _make_engine_with(errors)
        # gap_to_lower=2 → NO wins iff error > -2 → -1..5 = 7 of 11
        assert engine.p_no_above(24, 2.0) == pytest.approx(7 / 11)

    def test_lead_routing(self) -> None:
        engine = _make_engine_with([0, 0, 0, 0, 0], [10, 10, 10, 10, 10])
        # 24h-only: error always 0 → P(error < 5) = 1.0
        assert engine.p_no_below(24, 5.0) == pytest.approx(1.0)
        # 48h: error always 10 → P(error < 5) = 0.0
        assert engine.p_no_below(48, 5.0) == pytest.approx(0.0)

    def test_empty_returns_nan(self) -> None:
        engine = _make_engine_with([])
        result = engine.p_no_below(24, 2.0)
        assert result != result  # NaN


# ---------------------------------------------------------------------------
# evaluate_tail_no_bracket — gate-by-gate behavior
# ---------------------------------------------------------------------------


def _stub_contract(
    *,
    city: str = "london",
    target_offset_days: int = 1,
    lower_f: float = 60.0,
    upper_f: float = 62.0,
    best_bid_yes: float = 0.10,  # NO ask = 0.90
    best_ask_yes: float = 0.12,
    liquidity: float = 1000.0,
    token_id_no: str = "no_token_123",
) -> BracketContract:
    today = datetime.now(timezone.utc).date()
    target = today + timedelta(days=target_offset_days)
    return BracketContract(
        market_id="test_market",
        city=city,
        target_date=target,
        lower_f=lower_f,
        upper_f=upper_f,
        question="Will the highest temperature in london be in [60, 62]F",
        market_price_yes=best_bid_yes,
        best_ask_yes=best_ask_yes,
        best_bid_yes=best_bid_yes,
        spread=best_ask_yes - best_bid_yes,
        liquidity=liquidity,
        token_id_yes="yes_token_123",
        token_id_no=token_id_no,
    )


def _stub_constraints() -> TradingConstraints:
    return TradingConstraints(bankroll=1000.0)


def _stub_portfolio() -> PortfolioState:
    return PortfolioState.default(_stub_constraints())


# Errors centered at 0 with σ ~ 2.5°F (matching real 24h forecast skill).
_realistic_errors = [
    -7, -5, -4, -3, -3, -2, -2, -1, -1, -1, 0, 0, 0, 0, 0, 0,
    1, 1, 1, 2, 2, 3, 3, 4, 5, 7,
]


class TestEvaluateGates:
    def test_passes_when_all_gates_satisfied_below(self) -> None:
        # Bracket [60, 62] with forecast 65 → BELOW by 3°F (gap_to_upper=3).
        # NO wins iff error < 3 → from realistic_errors, 22/26 = 0.846
        # Use best_bid_yes=0.31 → no_ask=0.69 (below FLIP_NO_TO_YES_THRESHOLD
        # of 0.70 so we don't trigger the flip experiment).
        # Edge_pp = 0.846 - 0.69 = +0.156 (well above 3pp floor).
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0, best_bid_yes=0.31,
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct,
            forecast_high_f=65.0,  # gap_to_upper = 3
            hit_rate_engine=engine,
            constraints=_stub_constraints(),
            portfolio_state=_stub_portfolio(),
        )
        assert plan is not None, f"expected pass, rejected with: {diag.reject_reason}"
        assert diag.passed is True
        assert diag.direction == "BELOW"
        assert diag.edge_distance_f == pytest.approx(3.0)
        assert plan.outcome == "NO"
        assert plan.category == "weather_tail_no"

    def test_flip_when_no_ask_above_threshold(self) -> None:
        """Apr 28 2026 PM: no_ask >= 0.70 → buy YES instead of NO.

        Same selection logic; only the final endpoint flips. Tagged with
        category=weather_tail_no_flipped for A/B audit. Token_id is the
        YES token, side/outcome is YES, and metadata.flipped_from_no_ask
        records the original NO ask for the 30-day comparison.
        """
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0,
            best_bid_yes=0.20,   # no_ask = 0.80 (above flip threshold)
            best_ask_yes=0.22,
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=66.0,  # gap=4, p_no_emp=0.885
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is not None, f"expected flip pass, rejected with: {diag.reject_reason}"
        assert plan.outcome == "YES"
        assert plan.side == "YES"
        assert plan.category == "weather_tail_no_flipped"
        assert plan.token_id == "yes_token_123"  # not the NO token
        assert plan.reference_price == pytest.approx(0.22)  # yes_ask
        assert plan.metadata["token_side"] == "YES"
        assert plan.metadata["flipped_from_no_ask"] == pytest.approx(0.80)
        # Edge from YES side is mathematically negative-by-construction
        assert plan.metadata["edge_after_fees"] < 0

    def test_no_flip_at_threshold_boundary(self) -> None:
        """At no_ask = 0.69 (just below 0.70), still buy NO."""
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0,
            best_bid_yes=0.31,   # no_ask = 0.69 (just below threshold)
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is not None
        assert plan.outcome == "NO"
        assert plan.category == "weather_tail_no"

    def test_flip_rejects_when_yes_token_missing(self) -> None:
        """Flip-eligible position with no YES token → reject (don't fall back to NO)."""
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0,
            best_bid_yes=0.20,   # no_ask = 0.80 → flip-eligible
            best_ask_yes=0.22,
        )
        # Override token_id_yes to empty
        ct = BracketContract(
            market_id=ct.market_id, city=ct.city, target_date=ct.target_date,
            lower_f=ct.lower_f, upper_f=ct.upper_f, question=ct.question,
            market_price_yes=ct.market_price_yes,
            best_ask_yes=ct.best_ask_yes, best_bid_yes=ct.best_bid_yes,
            spread=ct.spread, liquidity=ct.liquidity,
            token_id_yes="",   # ← missing YES token
            token_id_no=ct.token_id_no,
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=66.0,  # gap=4 so we clear edge floor
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert "flip_yes_token_missing" in diag.reject_reason

    def test_rejects_when_forecast_inside_bracket(self) -> None:
        ct = _stub_contract(lower_f=60.0, upper_f=70.0)
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,  # inside [60, 70]
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert diag.reject_reason == "forecast_inside_bracket"

    def test_rejects_edge_too_close(self) -> None:
        # forecast 62.5 → gap_to_upper = 0.5 (below 1.0 floor)
        ct = _stub_contract(lower_f=60.0, upper_f=62.0)
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=62.5,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert "edge_too_close" in diag.reject_reason

    def test_rejects_edge_too_far(self) -> None:
        # forecast 70 → gap_to_upper = 8 (above 5°F cap)
        ct = _stub_contract(lower_f=60.0, upper_f=62.0)
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=70.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert "edge_too_far" in diag.reject_reason

    def test_rejects_below_edge_floor(self) -> None:
        # 3°F gap, but market NO ask = 0.90 (best_bid_yes=0.10) → empirical
        # hit rate ~0.85 < 0.90, edge = -0.05 → fails 3pp floor.
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0, best_bid_yes=0.10,
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert "edge_below_floor" in diag.reject_reason

    def test_rejects_wide_bracket_low_no_ask(self) -> None:
        """Apr 28 2026 PM: wide one-sided brackets at low NO ask reject.

        Pattern from lifetime data: 15 wide-NO trades net −$217.27 with
        the two outright settle losses (#83 seoul entry 0.19, #187 milan
        entry 0.34) at NO entry < 0.40. Width=121.2°F (one-sided "26°C or
        higher" sentinel) + best_bid_yes=0.65 (no_ask=0.35 < 0.50) → reject.

        Forecast=75°F is 3.8°F BELOW bracket lower=78.8°F (=26°C), so this
        is a valid ABOVE-direction tail-NO setup that would otherwise
        pass gates 1-3 before hitting the new wide-bracket gate at 4b.
        """
        ct = _stub_contract(
            lower_f=78.8, upper_f=200.0,  # one-sided "26°C or higher"
            best_bid_yes=0.65,             # no_ask = 0.35 (low)
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=75.0,  # 3.8°F below bracket
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert "wide_bracket_low_no_ask" in diag.reject_reason

    def test_passes_wide_bracket_high_no_ask(self) -> None:
        """Wide bracket with HIGH NO ask is fine — confidence supports the bet.

        Lifetime evidence: the one wide-bracket NO win (#200 buenos_aires)
        was at entry 0.82. Wide brackets aren't broken; the asymmetric
        downside is only a problem at low NO entry. With no_ask=0.85 the
        wide-bracket gate should NOT fire.
        """
        ct = _stub_contract(
            lower_f=64.4, upper_f=200.0,   # one-sided "18°C or higher"
            best_bid_yes=0.15,              # no_ask = 0.85 (high)
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=61.0,  # 3.4°F below bracket
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        # Should NOT reject for wide-bracket reason — may reject for
        # other reasons (e.g. edge too low) but not this one.
        assert "wide_bracket_low_no_ask" not in diag.reject_reason

    def test_rejects_low_liquidity(self) -> None:
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0, best_bid_yes=0.30, liquidity=50.0,
        )
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
        )
        assert plan is None
        assert "liquidity_too_thin" in diag.reject_reason

    def test_rejects_already_open_token(self) -> None:
        ct = _stub_contract(lower_f=60.0, upper_f=62.0, best_bid_yes=0.30)
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
            already_open_token_ids={ct.token_id_no},
        )
        assert plan is None
        assert diag.reject_reason == "already_open"

    def test_rejects_in_cooldown(self) -> None:
        ct = _stub_contract(lower_f=60.0, upper_f=62.0, best_bid_yes=0.30)
        engine = _make_engine_with(_realistic_errors)
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
            cooldown_token_ids={ct.token_id_no},
        )
        assert plan is None
        assert diag.reject_reason == "in_cooldown"

    def test_correlation_group_cap_blocks_overflow(self) -> None:
        ct = _stub_contract(
            city="london", lower_f=60.0, upper_f=62.0, best_bid_yes=0.30,
        )
        engine = _make_engine_with(_realistic_errors)
        # Pre-fill the western_europe group at $190 → next $30 trade overflows.
        used = {"western_europe": 190.0}
        plan, diag = tns.evaluate_tail_no_bracket(
            contract=ct, forecast_high_f=65.0,
            hit_rate_engine=engine,
            constraints=_stub_constraints(), portfolio_state=_stub_portfolio(),
            correlation_group_used=used,
        )
        assert plan is None
        assert "corr_group_full" in diag.reject_reason


# ---------------------------------------------------------------------------
# analyze_tail_no_brackets — integration over multiple contracts
# ---------------------------------------------------------------------------


class TestAnalyzeIntegration:
    def test_correlation_cap_enforced_across_contracts(self) -> None:
        # Three london (western_europe) contracts; cap = $200.
        # First two at $30 each fit; third should be rejected.
        contracts = []
        for i in range(3):
            contracts.append(_stub_contract(
                city="london",
                lower_f=60.0 + i * 0.1,  # tiny offset to make distinct
                upper_f=62.0 + i * 0.1,
                best_bid_yes=0.30,
                token_id_no=f"tok_{i}",
            ))
        # All have token_id_no = tok_0/1/2 so they're distinct.
        # Replace city in second to force same correlation group? They already
        # share london → western_europe. Only $200 cap allows ~6 × $30 trades.
        # Make many more to actually saturate.
        contracts = []
        for i in range(10):
            ct = _stub_contract(
                city="london",
                lower_f=60.0 + i * 0.001,
                upper_f=62.0 + i * 0.001,
                best_bid_yes=0.30,
                token_id_no=f"tok_{i}",
            )
            contracts.append(ct)
        forecasts = {(ct.city, ct.target_date): {"forecast_high_f": 65.0}
                     for ct in contracts}
        engine = _make_engine_with(_realistic_errors)
        result = tns.analyze_tail_no_brackets(
            contracts=contracts,
            forecasts_by_city_date=forecasts,
            constraints=_stub_constraints(),
            portfolio_state=_stub_portfolio(),
            hit_rate_engine=engine,
        )
        # All 10 brackets are otherwise valid. Cap = $200 / $30 base = 6 max
        # (technically 6.67 but we discretize). $30 × 6 = $180; 7th would
        # take us to $210 > $200 cap.
        assert len(result.trade_plan) == 6
        assert result.diagnostics["correlation_group_used"]["western_europe"] == 180.0
        assert result.diagnostics["reject_counters"].get("corr_group_full", 0) == 4

    def test_skips_contract_with_missing_forecast(self) -> None:
        ct = _stub_contract()
        engine = _make_engine_with(_realistic_errors)
        result = tns.analyze_tail_no_brackets(
            contracts=[ct],
            forecasts_by_city_date={},  # no forecasts at all
            constraints=_stub_constraints(),
            portfolio_state=_stub_portfolio(),
            hit_rate_engine=engine,
        )
        assert len(result.trade_plan) == 0
        assert result.diagnostics["reject_counters"]["no_forecast_for_target"] == 1

    def test_strategy_name_and_category_propagate(self) -> None:
        # Use best_bid_yes=0.31 → no_ask=0.69 (just below FLIP_NO_TO_YES_THRESHOLD
        # of 0.70) so this test exercises the normal NO path, not the flip experiment.
        ct = _stub_contract(
            city="london", lower_f=60.0, upper_f=62.0, best_bid_yes=0.31,
        )
        engine = _make_engine_with(_realistic_errors)
        result = tns.analyze_tail_no_brackets(
            contracts=[ct],
            forecasts_by_city_date={(ct.city, ct.target_date): {"forecast_high_f": 65.0}},
            constraints=_stub_constraints(),
            portfolio_state=_stub_portfolio(),
            hit_rate_engine=engine,
        )
        assert result.strategy_name == "weather_tail_no"
        assert len(result.trade_plan) == 1
        plan = result.trade_plan[0]
        assert plan.category == "weather_tail_no"
        assert plan.strategy_name == "weather_tail_no"
        assert plan.outcome == "NO"
        assert plan.side == "NO"
