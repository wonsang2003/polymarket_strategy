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
    # Apr 28 2026 — both bumped after overnight audit showed marginal-edge
    # entries (4.5-9.3pp) and low-NO-ask entries (0.34) accounted for 4 of
    # 5 catastrophic full-notional losses in a single 8h window. See
    # comments in tail_no_strategy.py for the historical thresholds.
    assert tns.NO_ASK_MIN == 0.40
    assert tns.NO_ASK_MAX == 0.95
    assert tns.LIQUIDITY_MIN_USD == 200.0
    assert tns.EDGE_FLOOR_PP == 0.10
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
        # Market NO ask = 1 - 0.10 = 0.90. Edge_pp = 0.846 - 0.90 = -0.054 (NEGATIVE)
        # So this should REJECT on edge_below_floor — set up a better scenario.
        # Use no_ask = 0.70 (best_bid_yes = 0.30) instead.
        ct = _stub_contract(
            lower_f=60.0, upper_f=62.0, best_bid_yes=0.30,
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
        ct = _stub_contract(
            city="london", lower_f=60.0, upper_f=62.0, best_bid_yes=0.30,
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
