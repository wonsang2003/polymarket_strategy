"""Pin tail-strategy gate logic.

Apr 25 2026 — Layer 3.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.weather.models import (
    BracketContract, CITY_REGISTRY, SynopticRegime, WeatherModel,
)
from polymarket_strat.domain.weather.nowcast import (
    BracketNowcast, adaptive_margin_f,
)
from polymarket_strat.domain.weather.reliability import (
    BucketBlocklist, CityReliability,
)
from polymarket_strat.domain.weather.tail_strategy import (
    KELLY_FRACTION,
    MAX_POSITION_FRACTION,
    MIN_EDGE,
    NO_SIDE_MAX_P_MODEL,
    NO_SIDE_MIN_MARKET,
    YES_SIDE_MIN_P_MODEL,
    _edge_after_fees_no_side,
    _edge_after_fees_yes_side,
    _kelly_fraction_no_side,
    _kelly_fraction_yes_side,
    _settlement_lead_hours,
    analyze_tail_brackets,
    evaluate_tail_bracket,
)


# --- Helpers --------------------------------------------------------------


def _make_contract(
    *, city="nyc", lower_f=80.0, upper_f=82.0, market_yes=0.10,
    target_date=date(2026, 8, 15), token_no="no123", token_yes="yes123",
    market_id="m1", liquidity=200.0, spread=0.02,
) -> BracketContract:
    return BracketContract(
        market_id=market_id,
        question=f"{city} max temp {lower_f}-{upper_f}F",
        city=city,
        target_date=target_date,
        lower_f=lower_f, upper_f=upper_f,
        token_id_yes=token_yes, token_id_no=token_no,
        market_price_yes=market_yes,
        best_ask_yes=market_yes,
        best_bid_yes=market_yes - 0.01,
        spread=spread,
        liquidity=liquidity,
    )


def _make_pricer_mock(prob_yes: float):
    pricer = MagicMock()
    pricer.has_model.return_value = True
    pricer.bracket_probability.return_value = prob_yes
    return pricer


def _make_station_client_mock(running_max_f, last_obs_utc=None, n_readings=12):
    if last_obs_utc is None:
        last_obs_utc = datetime.now(timezone.utc)
    sc = MagicMock()
    sc.fetch_today_running_max.return_value = (
        running_max_f, last_obs_utc, n_readings
    )
    return sc


def _make_reliability_mock(mult: float = 0.85) -> CityReliability:
    rel = MagicMock(spec=CityReliability)
    rel.multiplier.return_value = (mult, {"fallback": False})
    return rel


def _make_blocklist_mock(blocked: bool = False) -> BucketBlocklist:
    bl = MagicMock(spec=BucketBlocklist)
    bl.is_blocked.return_value = (blocked, "fine:test/24h/test" if blocked else "")
    return bl


# --- Math --------------------------------------------------------------


class TestKellyMath:
    def test_no_side_high_prob_high_kelly(self) -> None:
        # p_no=0.98, no_price=0.88 → Kelly should be ~0.83
        f = _kelly_fraction_no_side(p_no=0.98, no_price=0.88)
        assert f == pytest.approx(0.83, abs=0.02)

    def test_no_side_zero_edge_zero_kelly(self) -> None:
        # If model agrees with market, no kelly
        f = _kelly_fraction_no_side(p_no=0.88, no_price=0.88)
        assert f == pytest.approx(0.0, abs=1e-3)

    def test_yes_side_kelly_symmetric(self) -> None:
        # p_yes=0.98, yes_price=0.88 → same as NO-side mirror
        f = _kelly_fraction_yes_side(p_yes=0.98, yes_price=0.88)
        assert f == pytest.approx(0.83, abs=0.02)

    def test_kelly_clamped_to_zero_when_negative_edge(self) -> None:
        # p_no=0.50, no_price=0.90 (overpaying) → negative kelly clamped to 0
        f = _kelly_fraction_no_side(p_no=0.50, no_price=0.90)
        assert f == 0.0


class TestEdgeMath:
    def test_no_side_positive_edge_when_market_overprices_yes(self) -> None:
        # market_yes=0.12 (NO=0.88), p_yes=0.02 (model) → strong edge
        edge = _edge_after_fees_no_side(p_no=0.98, no_price=0.88)
        assert edge > 0.05  # at least 5c

    def test_no_side_zero_edge_when_no_disagreement(self) -> None:
        edge = _edge_after_fees_no_side(p_no=0.88, no_price=0.88)
        # Tiny negative because of fee
        assert -0.04 < edge < 0.0

    def test_yes_side_positive_edge_at_high_prob(self) -> None:
        # market_yes=0.85, model says 0.95 → 10c edge
        edge = _edge_after_fees_yes_side(p_yes=0.95, yes_price=0.85)
        assert edge > 0.05


class TestSettlementLeadHours:
    def test_lead_hours_at_5pm_local(self) -> None:
        # Aug 15, 2026 at 22:00 UTC = 18:00 local NYC (post-settlement)
        # Settlement is at 17:00 local same day → lead = -1 hr
        target = date(2026, 8, 15)
        now_utc = datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)
        lead = _settlement_lead_hours(target, "America/New_York", now_utc)
        assert lead == pytest.approx(-1.0, abs=0.05)

    def test_lead_hours_at_noon_local(self) -> None:
        # Aug 15, 2026 at 16:00 UTC = 12:00 local NYC
        target = date(2026, 8, 15)
        now_utc = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)
        lead = _settlement_lead_hours(target, "America/New_York", now_utc)
        assert lead == pytest.approx(5.0, abs=0.1)


class TestAdaptiveMargin:
    def test_adaptive_margin_scales_with_hours(self) -> None:
        assert adaptive_margin_f(1.0) == pytest.approx(1.5, abs=1e-6)
        assert adaptive_margin_f(3.0) == pytest.approx(4.5, abs=1e-6)
        assert adaptive_margin_f(5.0) == pytest.approx(7.5, abs=1e-6)

    def test_adaptive_margin_floor(self) -> None:
        assert adaptive_margin_f(0.1) == pytest.approx(1.0, abs=1e-6)

    def test_adaptive_margin_cap(self) -> None:
        # 20h × 1.5 = 30 → capped at 15
        assert adaptive_margin_f(20.0) == pytest.approx(15.0, abs=1e-6)


# --- Gate logic --------------------------------------------------------------


class TestEvaluateTailBracket:
    @pytest.fixture
    def base_state(self):
        constraints = TradingConstraints()  # default $1000 bankroll
        portfolio = PortfolioState.default(constraints)
        return constraints, portfolio

    @pytest.fixture
    def evening_now_utc(self):
        # Aug 15 22:00 UTC = 18:00 local NYC. Past peak (>16:00).
        # Settlement 17:00 local = 21:00 UTC. So lead = -1h (post settle).
        # We'll use a SETTLEMENT_DAY = Aug 16 so lead is positive.
        return datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)

    @pytest.fixture
    def post_peak_pre_settle_now(self):
        """Aug 15 17:30 local NYC = 21:30 UTC. After 16:00 peak threshold.
        Settlement Aug 15 17:00 local already passed by 30 min → use Aug 16."""
        return datetime(2026, 8, 15, 21, 30, tzinfo=timezone.utc)

    def test_passes_no_side_settled_no(self, base_state) -> None:
        """Classic NO-side trade: model says 2%, market 12%, observation
        SETTLED_NO with 5°F gap, post-peak."""
        constraints, portfolio = base_state
        contract = _make_contract(
            city="nyc", lower_f=80.0, upper_f=82.0, market_yes=0.12,
            target_date=date(2026, 8, 16),  # tomorrow at the time of "now"
        )
        pricer = _make_pricer_mock(prob_yes=0.02)
        # Aug 15 22:00 UTC = 18:00 local. Settlement Aug 16 17:00 local = 21:00 UTC.
        # Lead = (Aug 16 21:00 UTC - Aug 15 22:00 UTC) / 3600 = 23 hours
        # That's > MAX_LEAD_HOURS=12, would fail. Use Aug 15 same day.
        now_utc = datetime(2026, 8, 15, 17, 0, tzinfo=timezone.utc)  # 13:00 local NYC
        # Settlement Aug 15 17:00 local NYC = 21:00 UTC. lead = 4h.
        contract = _make_contract(
            city="nyc", lower_f=80.0, upper_f=82.0, market_yes=0.12,
            target_date=date(2026, 8, 15),
        )
        # Today's max so far at 13:00 local: 70°F. Bracket lower 80°F.
        # adaptive_margin at 4h = 6°F. Gap = 10°F > 6°F → SETTLED_NO.
        # BUT 13:00 local is pre-peak (before 16:00). So peak_likely_past = False.
        # Need to test post-peak case instead.
        now_utc = datetime(2026, 8, 15, 21, 30, tzinfo=timezone.utc)  # 17:30 local NYC
        # Settlement Aug 15 17:00 local already passed by 30 min — would be NEGATIVE lead
        # Use Aug 16 settle (lead = ~24h, too far)
        # OR use a later target date to get tomorrow's lead
        # Actually for a CLEAN POST-PEAK pre-SETTLE trade we need:
        #   now between PEAK_LIKELY_PAST_HOUR (16:00) and 17:00 settlement
        # That's 1-hour window. Pick 16:30 local = 20:30 UTC.
        now_utc = datetime(2026, 8, 15, 20, 30, tzinfo=timezone.utc)
        # Lead = 30 min. Below MIN_LEAD_HOURS=0.5? Exactly 0.5. Use 16:00 local instead.
        now_utc = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
        # Lead = 1.0h. Adaptive margin = max(1, 1*1.5) = 1.5°F.

        station_client = _make_station_client_mock(running_max_f=70.0)
        reliability = _make_reliability_mock(mult=0.85)
        blocklist = _make_blocklist_mock(blocked=False)

        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=pricer,
            station_client=station_client,
            reliability=reliability,
            blocklist=blocklist,
            constraints=constraints,
            portfolio_state=portfolio,
            now_utc=now_utc,
            forecast_high_f=72.0,
            ensemble_spread_f=2.0,
        )
        assert plan is not None, f"expected plan, rejected: {diag.reject_reason}"
        assert plan.side == "NO"
        assert plan.token_id == "no123"
        assert plan.signal_score >= 1.0  # edge >= MIN_EDGE means score >= 1.0
        assert plan.target_notional > 0
        assert plan.target_notional <= MAX_POSITION_FRACTION * constraints.bankroll
        assert diag.passed is True

    def test_rejects_when_lead_too_far(self, base_state) -> None:
        constraints, portfolio = base_state
        contract = _make_contract(target_date=date(2026, 8, 30))  # weeks ahead
        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(),
            constraints=constraints, portfolio_state=portfolio,
            now_utc=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
            forecast_high_f=72.0,
        )
        assert plan is None
        assert "lead_too_far" in diag.reject_reason

    def test_rejects_when_p_model_in_middle(self, base_state) -> None:
        """Model says 50% — not in calibrated tail. Reject."""
        constraints, portfolio = base_state
        contract = _make_contract(market_yes=0.40, target_date=date(2026, 8, 15))
        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=_make_pricer_mock(0.50),  # mid-bin
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(),
            constraints=constraints, portfolio_state=portfolio,
            now_utc=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
            forecast_high_f=72.0,
        )
        assert plan is None
        assert "not_in_calibrated_tail" in diag.reject_reason

    def test_rejects_when_market_too_low_no_edge(self, base_state) -> None:
        """Market says 5% (matches model) — no edge to capture."""
        constraints, portfolio = base_state
        contract = _make_contract(market_yes=0.05, target_date=date(2026, 8, 15))
        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(),
            constraints=constraints, portfolio_state=portfolio,
            now_utc=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
            forecast_high_f=72.0,
        )
        assert plan is None
        # Could be market threshold or edge gate
        assert any(s in diag.reject_reason for s in [
            "not_in_calibrated_tail", "edge_too_small",
        ])

    def test_rejects_when_nowcast_open(self, base_state) -> None:
        """Pre-peak (12:00 local) → nowcast OPEN → reject NO trade."""
        constraints, portfolio = base_state
        # 12:00 local NYC = 16:00 UTC
        now_utc = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)
        # Settlement is 17:00 local = 21:00 UTC. Lead = 5h.
        contract = _make_contract(
            market_yes=0.12, target_date=date(2026, 8, 15)
        )
        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),  # below bracket
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(),
            constraints=constraints, portfolio_state=portfolio,
            now_utc=now_utc, forecast_high_f=72.0,
        )
        assert plan is None
        assert "nowcast" in diag.reject_reason

    def test_rejects_when_blocklisted(self, base_state) -> None:
        constraints, portfolio = base_state
        plan, diag = evaluate_tail_bracket(
            contract=_make_contract(target_date=date(2026, 8, 15)),
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(blocked=True),
            constraints=constraints, portfolio_state=portfolio,
            now_utc=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
            forecast_high_f=72.0,
        )
        assert plan is None
        assert "blocklist" in diag.reject_reason

    def test_position_capped_at_5_percent_bankroll(self, base_state) -> None:
        """Even with extreme edge, position caps at 5%."""
        constraints, portfolio = base_state
        contract = _make_contract(
            market_yes=0.50, target_date=date(2026, 8, 15)
        )
        # market_yes=0.50 means NO=0.50. p_yes_model=0.02 → p_no=0.98.
        # Kelly massive. ECE shrinkage 0.85. Should cap at 5% × 1000 = $50.
        now_utc = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(mult=0.85),
            blocklist=_make_blocklist_mock(),
            constraints=constraints, portfolio_state=portfolio,
            now_utc=now_utc, forecast_high_f=72.0,
        )
        assert plan is not None, f"rejected: {diag.reject_reason}"
        assert plan.target_notional <= MAX_POSITION_FRACTION * constraints.bankroll + 0.001


class TestAnalyzeTailBrackets:
    def test_handles_empty_contract_list(self) -> None:
        constraints = TradingConstraints()
        portfolio = PortfolioState.default(constraints)
        result = analyze_tail_brackets(
            contracts=[],
            forecasts_by_city={},
            constraints=constraints,
            portfolio_state=portfolio,
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(),
        )
        assert result.strategy_name == "weather_tail"
        assert result.signals == []
        assert result.trade_plan == []
        assert result.diagnostics["n_evaluated"] == 0

    def test_aggregates_diagnostics(self) -> None:
        """Mix of pass + reject — diagnostics counter reflects both."""
        constraints = TradingConstraints()
        portfolio = PortfolioState.default(constraints)
        # Two contracts: one passes, one rejected by lead too far
        c1 = _make_contract(target_date=date(2026, 8, 15), market_id="m1")
        c2 = _make_contract(target_date=date(2026, 8, 30), market_id="m2")
        forecasts = {"nyc": {"forecast_high_f": 72.0,
                              "ensemble_spread_f": 2.0,
                              "regime": "stable_high"}}
        result = analyze_tail_brackets(
            contracts=[c1, c2],
            forecasts_by_city=forecasts,
            constraints=constraints, portfolio_state=portfolio,
            pricer=_make_pricer_mock(0.02),
            station_client=_make_station_client_mock(70.0),
            reliability=_make_reliability_mock(),
            blocklist=_make_blocklist_mock(),
            now_utc=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
        )
        assert result.diagnostics["n_evaluated"] == 2
        # At most 1 passed (c2 rejected by lead)
        assert result.diagnostics["n_passed"] <= 1
        assert "lead_too_far" in result.diagnostics["reject_counters"]
