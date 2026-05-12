"""Late-entry tail-bracket NO strategy (Layer 3).

Apr 25 2026 — captures small-edge high-probability NO bets near settlement
when live IEM observations + model probability + market price all align.

Gate logic (refined from quant discussion):
  1. lead_hours <= 12 with adaptive nowcast margin
  2. P_model_yes in calibrated bin (<= 0.30 NO-side, >= 0.70 YES-side)
  3. Market price reasonable (NO-side: market_yes >= 0.10)
  4. Nowcast classification confirms direction
       NO-side: SETTLED_NO or SETTLED_NO_OVERSHOT
       YES-side: LIKELY_YES (post-peak, in-bracket)
  5. edge_after_fees >= 0.05
  6. Liquidity gate: top-3 NO depth >= 3x notional
  7. Position size = min(quarter-Kelly × ECE shrinkage, 5% bankroll)

This strategy is intentionally narrow — only a handful of trades per day
in normal weather, more on volatile days. It's designed to capture
"market price hasn't caught up to reality yet" inefficiencies.

It REUSES infrastructure:
  - market_scanner for active markets
  - quantile_pricer for model probability
  - station_client.fetch_today_running_max for IEM observation
  - nowcast.classify_bracket for live-obs gating
  - reliability.CityReliability for ECE shrinkage on size
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import StrategyAnalysis, StrategySignal, TradePlan
from polymarket_strat.domain.weather.models import (
    BracketContract,
    CITY_REGISTRY,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.domain.weather.nowcast import (
    BracketNowcast,
    adaptive_margin_f,
    classify_bracket,
)
from polymarket_strat.domain.weather.quantile_pricing import (
    QuantileBracketPricer,
    get_quantile_pricer,
)
from polymarket_strat.domain.weather.reliability import (
    BucketBlocklist,
    CityReliability,
    get_bucket_blocklist,
    get_city_reliability,
)
from polymarket_strat.infrastructure.weather.station_client import (
    StationObservationClient,
)


# Tail strategy constants
MAX_LEAD_HOURS = 12.0           # only trade when settlement <= 12h away
MIN_LEAD_HOURS = 0.5            # don't enter < 30 min from settle (no fill time)
NO_SIDE_MAX_P_MODEL = 0.30      # NO-side: model says bracket likely won't hit
YES_SIDE_MIN_P_MODEL = 0.70     # YES-side: model says bracket likely WILL hit
NO_SIDE_MIN_MARKET = 0.10       # NO-side: market_yes >= 10c (room to capture)
YES_SIDE_MAX_MARKET = 0.85      # YES-side: market_yes <= 85c
MIN_EDGE = 0.05                 # 5c minimum edge after fees
MIN_LIQUIDITY_MULTIPLE = 3.0    # top-3 depth must be 3x notional
MAX_POSITION_FRACTION = 0.05    # cap at 5% of bankroll
KELLY_FRACTION = 0.25           # quarter-Kelly base
FEE_RATE = 0.02                 # Polymarket: 2% fee on winnings only

# Lock-in time per CLAUDE.md §14 — 17:00 station local
SETTLEMENT_LOCK_IN_LOCAL_HOUR = 17


@dataclass(slots=True)
class TailGateResult:
    """Diagnostic record per evaluated bracket — for telemetry."""
    contract_id: str
    city: str
    bracket_lower_f: float
    bracket_upper_f: float
    side: str               # "NO" or "YES"
    passed: bool
    reject_reason: str = ""
    p_model_yes: Optional[float] = None
    market_yes: Optional[float] = None
    edge: Optional[float] = None
    nowcast_class: Optional[str] = None
    running_max_f: Optional[float] = None
    lead_hours: Optional[float] = None
    target_notional: Optional[float] = None
    kelly_fraction: Optional[float] = None
    reliability_mult: Optional[float] = None


def _kelly_fraction_no_side(p_no: float, no_price: float) -> float:
    """Kelly fraction for buying NO at price `no_price`.

    Math: Win prob = p_no, payout = (1 - no_price) per share net of $1
          (since NO pays $1). Loss = no_price.
          Effective odds b = (1 - no_price) / no_price.
          f* = (p_no × b - p_yes) / b
    """
    if no_price <= 0 or no_price >= 1:
        return 0.0
    b = (1.0 - no_price) / no_price
    if b <= 0:
        return 0.0
    p_yes = 1.0 - p_no
    f = (p_no * b - p_yes) / b
    return max(0.0, min(1.0, f))


def _kelly_fraction_yes_side(p_yes: float, yes_price: float) -> float:
    """Kelly fraction for buying YES at price `yes_price`.

    Math: Win prob = p_yes, payout = (1 - yes_price), loss = yes_price.
    """
    if yes_price <= 0 or yes_price >= 1:
        return 0.0
    b = (1.0 - yes_price) / yes_price
    if b <= 0:
        return 0.0
    p_no = 1.0 - p_yes
    f = (p_yes * b - p_no) / b
    return max(0.0, min(1.0, f))


def _edge_after_fees_no_side(p_no: float, no_price: float) -> float:
    """Edge per dollar invested when buying NO at no_price.

    EV per share = p_no × (1 - no_price) × (1 - fee) - p_yes × no_price
    Per dollar invested = EV / no_price
    """
    if no_price <= 0:
        return 0.0
    p_yes = 1.0 - p_no
    win_payoff = (1.0 - no_price) * (1.0 - FEE_RATE)
    loss_payoff = no_price
    ev_per_share = p_no * win_payoff - p_yes * loss_payoff
    return ev_per_share / no_price


def _edge_after_fees_yes_side(p_yes: float, yes_price: float) -> float:
    if yes_price <= 0:
        return 0.0
    p_no = 1.0 - p_yes
    win_payoff = (1.0 - yes_price) * (1.0 - FEE_RATE)
    loss_payoff = yes_price
    ev_per_share = p_yes * win_payoff - p_no * loss_payoff
    return ev_per_share / yes_price


def _settlement_lead_hours(
    target_date: date, station_timezone: str, now_utc: datetime
) -> Optional[float]:
    """Compute lead_hours from now to bracket settlement at station-local 17:00."""
    try:
        tz = ZoneInfo(station_timezone)
    except Exception:
        return None
    settlement_local = datetime.combine(
        target_date, time(SETTLEMENT_LOCK_IN_LOCAL_HOUR, 0), tzinfo=tz
    )
    delta_s = (settlement_local - now_utc).total_seconds()
    return delta_s / 3600.0


def evaluate_tail_bracket(
    *,
    contract: BracketContract,
    pricer: QuantileBracketPricer,
    station_client: StationObservationClient,
    reliability: CityReliability,
    blocklist: BucketBlocklist,
    constraints: TradingConstraints,
    portfolio_state: PortfolioState,
    now_utc: Optional[datetime] = None,
    forecast_high_f: Optional[float] = None,
    regime: SynopticRegime = SynopticRegime.STABLE_HIGH,
    ensemble_spread_f: float = 2.0,
    weather_model: WeatherModel = WeatherModel.GFS,
) -> tuple[Optional[TradePlan], TailGateResult]:
    """Evaluate one bracket through the tail strategy gates.

    Returns (TradePlan or None, diagnostic).

    Caller responsibilities:
      - Provide forecast_high_f (the model forecast value used for feature build)
      - Provide ensemble_spread_f when available
      - The pricer/station_client/reliability/blocklist singletons should be
        cached at the orchestrator level (autotrade scope), not per-call

    Why NO-side gets priority:
      Tail bin audit (Apr 25 2026) showed:
        [0.0, 0.05]: gap 2.4% — clean for NO-side trades
        [0.05, 0.10]: gap 3.3% — clean
        [0.95, 1.00]: gap 9.2% — risky for YES-side
      So NO-side trades on low-p-yes brackets are mathematically safe;
      YES-side trades on high-p-yes brackets need isotonic correction
      first. We support both gates here but caller can disable YES.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    diag = TailGateResult(
        contract_id=contract.market_id,
        city=contract.city,
        bracket_lower_f=contract.lower_f,
        bracket_upper_f=contract.upper_f,
        side="",
        passed=False,
    )

    # 0. Bucket blocklist
    blocked, reason = blocklist.is_blocked(
        city=contract.city, lead_hours=24, regime=regime.value,
    )
    if blocked:
        diag.reject_reason = f"blocklist:{reason}"
        return None, diag

    # 1. Lead time
    if contract.city not in CITY_REGISTRY:
        diag.reject_reason = "unknown_city"
        return None, diag
    station = CITY_REGISTRY[contract.city]
    lead_hours = _settlement_lead_hours(
        contract.target_date, station.timezone, now_utc
    )
    if lead_hours is None:
        diag.reject_reason = "tz_resolution_fail"
        return None, diag
    diag.lead_hours = lead_hours
    if lead_hours <= MIN_LEAD_HOURS:
        diag.reject_reason = f"lead_too_close ({lead_hours:.2f}h)"
        return None, diag
    if lead_hours > MAX_LEAD_HOURS:
        diag.reject_reason = f"lead_too_far ({lead_hours:.2f}h)"
        return None, diag

    # 2. Model probability (need forecast_high_f and a trained model)
    if forecast_high_f is None:
        diag.reject_reason = "no_forecast"
        return None, diag
    if not pricer.has_model(contract.city, 24):
        diag.reject_reason = "no_quantile_model"
        return None, diag

    p_model_yes = pricer.bracket_probability(
        city=contract.city,
        model=weather_model.value,
        forecast_high_f=forecast_high_f,
        obs_date=contract.target_date,
        lead_hours=24,
        regime=regime.value,
        lower_f=contract.lower_f,
        upper_f=contract.upper_f,
        ensemble_spread_f=ensemble_spread_f,
        apply_conformal=False,
    )
    if p_model_yes is None:
        diag.reject_reason = "pricer_returned_none"
        return None, diag
    diag.p_model_yes = p_model_yes

    # 3. Market price gate
    market_yes = float(contract.market_price_yes or 0.0)
    diag.market_yes = market_yes
    if market_yes <= 0 or market_yes >= 1:
        diag.reject_reason = f"bad_market_price ({market_yes})"
        return None, diag

    # 4. Side determination based on calibrated bin
    side: Optional[str] = None
    if p_model_yes <= NO_SIDE_MAX_P_MODEL and market_yes >= NO_SIDE_MIN_MARKET:
        side = "NO"
    elif p_model_yes >= YES_SIDE_MIN_P_MODEL and market_yes <= YES_SIDE_MAX_MARKET:
        side = "YES"
    else:
        diag.reject_reason = (
            f"not_in_calibrated_tail "
            f"(p_model={p_model_yes:.3f}, market={market_yes:.3f})"
        )
        return None, diag
    diag.side = side

    # 5. Nowcast — fetch live observation and classify
    margin = adaptive_margin_f(lead_hours)
    try:
        running_max_f, last_obs_utc, n_readings = (
            station_client.fetch_today_running_max(station)
        )
    except Exception as exc:
        diag.reject_reason = f"iem_fetch_error:{exc!r}"
        return None, diag
    diag.running_max_f = running_max_f

    nowcast_result = classify_bracket(
        station=station,
        bracket_lower_f=contract.lower_f,
        bracket_upper_f=contract.upper_f,
        running_max_f=running_max_f,
        last_obs_utc=last_obs_utc,
        n_readings=n_readings,
        no_margin_f=margin,
        now_utc=now_utc,
    )
    diag.nowcast_class = nowcast_result.classification.value

    if side == "NO":
        if nowcast_result.classification not in (
            BracketNowcast.SETTLED_NO, BracketNowcast.SETTLED_NO_OVERSHOT,
        ):
            diag.reject_reason = (
                f"nowcast_not_settled_no ({nowcast_result.classification.value})"
            )
            return None, diag
    else:  # YES side
        if nowcast_result.classification != BracketNowcast.LIKELY_YES:
            diag.reject_reason = (
                f"nowcast_not_likely_yes ({nowcast_result.classification.value})"
            )
            return None, diag

    # 6. Edge after fees
    if side == "NO":
        no_price = 1.0 - market_yes
        edge = _edge_after_fees_no_side(p_no=1.0 - p_model_yes, no_price=no_price)
        token_id = contract.token_id_no or ""
        ref_price = no_price
    else:
        yes_price = market_yes
        edge = _edge_after_fees_yes_side(p_yes=p_model_yes, yes_price=yes_price)
        token_id = contract.token_id_yes or ""
        ref_price = yes_price
    diag.edge = edge
    if edge < MIN_EDGE:
        diag.reject_reason = f"edge_too_small ({edge:.4f} < {MIN_EDGE})"
        return None, diag

    if not token_id:
        diag.reject_reason = f"no_{side.lower()}_token_id"
        return None, diag

    # 7. Position sizing — Kelly × reliability shrinkage, capped at 5% bankroll
    if side == "NO":
        kelly = _kelly_fraction_no_side(p_no=1.0 - p_model_yes, no_price=ref_price)
    else:
        kelly = _kelly_fraction_yes_side(p_yes=p_model_yes, yes_price=ref_price)
    diag.kelly_fraction = kelly

    rel_mult, _rel_diag = reliability.multiplier(
        city=contract.city, lead_hours=24,
    )
    diag.reliability_mult = rel_mult

    sized_fraction = min(
        KELLY_FRACTION * kelly * rel_mult,
        MAX_POSITION_FRACTION,
    )
    target_notional = sized_fraction * constraints.bankroll
    target_notional = max(
        target_notional, constraints.min_position_notional_usd
    )
    diag.target_notional = target_notional

    # 8. Liquidity gate
    if side == "NO":
        # Polymarket exposes top_ask for the YES token. NO depth is mirrored
        # at (1 - YES_bid) on the opposite side. For simplicity here we use
        # a conservative proxy: BracketContract.liquidity (overall market depth)
        # OR the contract's top_bid_size if exposed for NO.
        # In practice the Polymarket book API gives us actual NO levels;
        # the BracketContract dataclass needs a top_no_ask_size field for
        # precision. Until then, use overall liquidity as a coarse gate.
        depth_proxy = float(contract.liquidity or 0.0)
    else:
        depth_proxy = float(contract.liquidity or 0.0)
    required_depth = MIN_LIQUIDITY_MULTIPLE * target_notional
    if depth_proxy > 0 and depth_proxy < required_depth:
        diag.reject_reason = (
            f"liquidity_too_thin ({depth_proxy:.0f} < {required_depth:.0f})"
        )
        return None, diag

    # All gates passed — build TradePlan
    diag.passed = True
    diag.reject_reason = "PASSED"

    rationale = [
        f"tail_strategy: side={side}",
        f"lead={lead_hours:.2f}h, margin={margin:.2f}°F",
        f"p_model_yes={p_model_yes:.3f}, market_yes={market_yes:.3f}",
        f"edge_after_fees={edge:.4f}",
        f"nowcast={nowcast_result.classification.value} "
        f"(running_max={running_max_f}, gap={nowcast_result.reason})",
        f"kelly_full={kelly:.3f}, reliability_mult={rel_mult:.3f}, "
        f"sized_frac={sized_fraction:.4f}",
    ]

    plan = TradePlan(
        strategy_name="weather_tail",
        market=contract.market_id,
        question=contract.question,
        category="weather_tail",
        outcome=side,
        token_id=token_id,
        side=side,
        signal_score=edge / MIN_EDGE,  # normalized to 1.0 at threshold
        target_notional=target_notional,
        reference_price=ref_price,
        best_ask=ref_price,
        best_bid=ref_price - (contract.spread or 0.02),
        spread=float(contract.spread or 0.02),
        top_ask_size=float(contract.liquidity or 0.0),
        top_bid_size=float(contract.liquidity or 0.0),
        risk_score=1.0 - kelly,
        expected_value=edge * target_notional,
        executable=True,
        rationale=rationale,
        metadata={
            "city": contract.city,
            "target_date": contract.target_date.isoformat(),
            "bracket_lower_f": contract.lower_f,
            "bracket_upper_f": contract.upper_f,
            "p_model_yes": p_model_yes,
            "market_yes": market_yes,
            "edge_after_fees": edge,
            "lead_hours": lead_hours,
            "nowcast_class": nowcast_result.classification.value,
            "nowcast_running_max_f": running_max_f,
            "nowcast_margin_f": margin,
            "kelly_fraction": kelly,
            "reliability_mult": rel_mult,
            "sized_fraction": sized_fraction,
            "tail_audit_bin_gap": "see data/weather/tail_ece_audit.json",
        },
    )
    signal = StrategySignal(
        market=contract.market_id,
        question=contract.question,
        category="weather_tail",
        outcome=side,
        side=side,
        signal_score=edge / MIN_EDGE,
        reference_price=ref_price,
        metadata=plan.metadata,
    )
    return plan, diag


def analyze_tail_brackets(
    *,
    contracts: list[BracketContract],
    forecasts_by_city: dict[str, dict[str, float]],
    constraints: TradingConstraints,
    portfolio_state: PortfolioState,
    pricer: Optional[QuantileBracketPricer] = None,
    station_client: Optional[StationObservationClient] = None,
    reliability: Optional[CityReliability] = None,
    blocklist: Optional[BucketBlocklist] = None,
    now_utc: Optional[datetime] = None,
) -> StrategyAnalysis:
    """Top-level analysis: iterate contracts and return tail-strategy plans.

    Args:
        contracts: from market_scanner.find_weather_bracket_markets()
        forecasts_by_city: {city: {"forecast_high_f": float, "ensemble_spread_f": float, "regime": str}}
                            Caller fetches forecasts (existing infra has this).
        constraints, portfolio_state: standard
        pricer/station_client/reliability/blocklist: injected for testability
        now_utc: defaults to datetime.now(UTC)

    Returns StrategyAnalysis with:
        signals: one per passed contract
        trade_plan: same items as TradePlan (executable)
        diagnostics: counters by reject reason
    """
    pricer = pricer or get_quantile_pricer()
    station_client = station_client or StationObservationClient()
    reliability = reliability or get_city_reliability()
    blocklist = blocklist or get_bucket_blocklist()

    signals: list[StrategySignal] = []
    plans: list[TradePlan] = []
    diags: list[TailGateResult] = []
    counters: dict[str, int] = {}

    for contract in contracts:
        forecast_data = forecasts_by_city.get(contract.city) or {}
        forecast_high_f = forecast_data.get("forecast_high_f")
        ensemble_spread_f = forecast_data.get("ensemble_spread_f", 2.0)
        regime_str = forecast_data.get("regime", "stable_high")
        try:
            regime = SynopticRegime(regime_str)
        except ValueError:
            regime = SynopticRegime.STABLE_HIGH

        plan, diag = evaluate_tail_bracket(
            contract=contract,
            pricer=pricer,
            station_client=station_client,
            reliability=reliability,
            blocklist=blocklist,
            constraints=constraints,
            portfolio_state=portfolio_state,
            now_utc=now_utc,
            forecast_high_f=forecast_high_f,
            regime=regime,
            ensemble_spread_f=ensemble_spread_f,
        )
        diags.append(diag)
        bucket = diag.reject_reason.split(" ")[0].split(":")[0] or "unknown"
        counters[bucket] = counters.get(bucket, 0) + 1
        if plan is not None:
            plans.append(plan)
            sig_meta = dict(plan.metadata)
            signals.append(StrategySignal(
                market=plan.market, question=plan.question,
                category=plan.category, outcome=plan.outcome,
                side=plan.side, signal_score=plan.signal_score,
                reference_price=plan.reference_price,
                metadata=sig_meta,
            ))

    return StrategyAnalysis(
        strategy_name="weather_tail",
        signals=signals,
        trade_plan=plans,
        diagnostics={
            "n_evaluated": len(diags),
            "n_passed": len(plans),
            "reject_counters": counters,
            "passed_diags": [d for d in diags if d.passed],
        },
    )
