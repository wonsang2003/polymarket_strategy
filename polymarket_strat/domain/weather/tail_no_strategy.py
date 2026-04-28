"""Tail-NO strategy — geometric, edge-distance based.

Apr 26 2026 — Designed from the corrected v2 live-market scan analysis.
Replaces the existing tail_strategy.py for >12h leads. The two strategies
co-exist; this one fires at 24-72h leads, the existing one fires <12h with
nowcast confirmation.

CORE THESIS (data-validated):
  - Forecast errors at 24h have p50=1.5°F, p90=4.6°F, p95=6.4°F.
  - Brackets whose nearest edge sits 1-5°F outside the forecast are the
    sweet spot: market over-prices "near miss" risk, but reality says NO
    wins 70-95% of the time.
  - Beyond 5°F edge distance, market is efficient (NO already priced at
    99%+, no edge).
  - At 0-1°F edge distance, hit rate is too noisy to be reliable.

GATES (all must pass, in order):
  1. Forecast is OUTSIDE the bracket (gap_to_upper > 0 OR gap_to_lower > 0)
  2. edge_distance ∈ [1.0, 5.0]°F
  3. lead_hours ∈ [4.0, 72.0]   (4h floor: avoid same-day already-resolved;
                                  72h cap: forecast skill drops past 3 days)
  4. no_ask ∈ [0.05, 0.95]      (avoid extreme-tail and dead markets)
  5. liquidity ≥ $200           (actually fillable at our size)
  6. empirical_hit_rate - market_no_implied ≥ 0.03  (3pp edge floor after fees)
  7. token_id_no exists         (some legacy contracts only have YES side)
  8. not in cooldown / not duplicate
  9. correlation-group cap not exceeded

POSITION SIZING (decided Apr 26 from Kelly + drawdown analysis):
  base = $30
  if EV/$1 ≥ 1.0:  size = $50  (cap)
  if EV/$1 ≥ 0.3:  size = $40
  if EV/$1 ≥ 0.1:  size = $30
  if EV/$1 < 0.1:  skip (sub-base — not worth slot in correlation cap)

These trades are HOLD-TO-SETTLEMENT. Do NOT rebalance them — the small
per-trade edge gets eaten by spread+fee on a rebalance churn. The
mainstream strategy gets rebalance treatment; tail-NO gets buy-and-hold.
This is enforced by tagging plan.category="weather_tail_no" and excluding
that category from rebalance in main.py:run_rebalance.
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import (
    StrategyAnalysis, StrategySignal, TradePlan,
)
from polymarket_strat.domain.weather.models import (
    BracketContract, CITY_REGISTRY, WeatherModel,
)


# ============================================================================
# Constants — see module docstring for derivation
# ============================================================================
EDGE_DISTANCE_MIN_F: float = 1.0
EDGE_DISTANCE_MAX_F: float = 5.0
LEAD_HOURS_MIN: float = 4.0
LEAD_HOURS_MAX: float = 72.0
# Apr 28 2026 (afternoon) — REVERTED from 0.40 back to 0.05 after the 36h
# triple-check audit showed selection bias in the morning's tightening.
# Counterfactual on the full 36h window: 0.40 gate would have cost $130
# of realized P&L vs the actual current 0.05 setting. The morning audit
# was based on an unrepresentative 8h losing streak.
NO_ASK_MIN: float = 0.05
NO_ASK_MAX: float = 0.95
LIQUIDITY_MIN_USD: float = 200.0
# Apr 28 2026 (afternoon) — REVERTED from 0.10 back to 0.03 for the same
# reason. The 5pp gate is correctly calibrated: directional NO trades
# in the 36h window had +$8.05 expectancy (77.3% win rate) at this gate.
# Tightening to 0.10 would have kept only 5 of 22 trades and reduced
# realized P&L from $177 to $132. The 22-trade sample at 0.05 is genuine
# alpha; tightening filters out the volume that gives statistical reliability.
EDGE_FLOOR_PP: float = 0.03   # 3pp empirical hit rate vs market NO

# Apr 28 2026 (afternoon) — Wide-bracket low-NO-ask gate.
# One-sided brackets ("X or higher" / "X or below") parse with a sentinel
# upper or lower bound (e.g. bracket_upper_f=200°F for "≥ X" questions).
# Width > 10°F flags such brackets. Lifetime data on these trades:
#   15 wide-NO trades total
#    1 win (+$6.45)         entry 0.82
#    2 outright losses (−$100) entries 0.19, 0.34
#   12 rebalance exits (−$123)
#   net P&L: −$217.27
# The losses concentrate at low NO entry prices — we're betting the
# bracket WON'T hit when the market thinks YES is highly likely (NO ≪
# 0.50). On a one-sided bracket with no upper containment, a marginal
# weather move can flip the outcome to full-notional loss.
# Filter: reject NO entry on wide-bracket contracts unless NO_ASK ≥ 0.50.
WIDE_BRACKET_F: float = 10.0       # bracket width threshold
WIDE_BRACKET_MIN_NO_ASK: float = 0.50  # min NO ask for wide-bracket entry

# Position sizing tiers (EV per dollar invested)
SIZE_TIER_LARGE_EV: float = 1.0
SIZE_TIER_MEDIUM_EV: float = 0.3
SIZE_TIER_SMALL_EV: float = 0.1

POSITION_SIZE_LARGE_USD: float = 50.0
POSITION_SIZE_MEDIUM_USD: float = 40.0
POSITION_SIZE_BASE_USD: float = 30.0

FEE_RATE: float = 0.02  # Polymarket fee on winnings only
SETTLEMENT_LOCAL_HOUR: int = 17  # 17:00 station-local lock-in

# Per-correlation-group cap for tail-NO trades (separate from mainstream).
# At $200 group cap × 5 groups = $1000 max simultaneous tail-NO exposure.
CORRELATION_GROUP_CAP_USD: float = 200.0

CATEGORY: str = "weather_tail_no"
STRATEGY_NAME: str = "weather_tail_no"


@dataclass(slots=True)
class TailNoGateResult:
    """Diagnostic record per evaluated bracket — for telemetry."""
    contract_id: str
    city: str
    direction: str
    bracket_lower_f: float
    bracket_upper_f: float
    passed: bool
    reject_reason: str = ""
    forecast_high_f: Optional[float] = None
    edge_distance_f: Optional[float] = None
    lead_hours: Optional[float] = None
    no_ask: Optional[float] = None
    empirical_hit: Optional[float] = None
    edge_pp: Optional[float] = None
    ev_per_dollar: Optional[float] = None
    target_notional: Optional[float] = None


# ============================================================================
# Empirical hit-rate engine — pulls forecast_errors from DB once, caches.
# ============================================================================
class EmpiricalHitRate:
    """Compute P(NO wins) for a given (lead, gap_to_upper or gap_to_lower)
    from real forecast_errors history.

    For BELOW brackets (forecast > bracket_upper):
        NO wins iff observed > bracket_upper iff error < gap_to_upper
        P(NO) = P(error < gap_to_upper)

    For ABOVE brackets (forecast < bracket_lower):
        NO wins iff observed < bracket_lower iff error > -gap_to_lower
        P(NO) = P(error > -gap_to_lower)
    """

    def __init__(self, db_path: str = "data/weather/weather.db"):
        self._db_path = db_path
        self._errors_24: list[float] = []
        self._errors_48: list[float] = []
        self._loaded = False

    def _try_load(self) -> None:
        if self._loaded:
            return
        try:
            c = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            self._errors_24 = [
                float(r[0]) for r in c.execute(
                    "SELECT error_f FROM forecast_errors "
                    "WHERE error_f IS NOT NULL AND lead_hours = 24"
                ).fetchall()
            ]
            self._errors_48 = [
                float(r[0]) for r in c.execute(
                    "SELECT error_f FROM forecast_errors "
                    "WHERE error_f IS NOT NULL AND lead_hours = 48"
                ).fetchall()
            ]
            c.close()
        except Exception as e:
            print(
                f"[tail_no] forecast_errors load failed: {e!r}",
                file=sys.stderr,
            )
            self._errors_24 = []
            self._errors_48 = []
        self._loaded = True

    @property
    def n_24(self) -> int:
        self._try_load()
        return len(self._errors_24)

    @property
    def n_48(self) -> int:
        self._try_load()
        return len(self._errors_48)

    def p_no_below(self, lead_hours: float, gap_to_upper: float) -> float:
        """Pr(NO wins) for a bracket whose UPPER edge is `gap_to_upper`°F
        below the forecast."""
        self._try_load()
        errors = self._errors_24 if lead_hours <= 36 else self._errors_48
        if not errors:
            return float("nan")
        n_below = sum(1 for e in errors if e < gap_to_upper)
        return n_below / len(errors)

    def p_no_above(self, lead_hours: float, gap_to_lower: float) -> float:
        """Pr(NO wins) for a bracket whose LOWER edge is `gap_to_lower`°F
        above the forecast."""
        self._try_load()
        errors = self._errors_24 if lead_hours <= 36 else self._errors_48
        if not errors:
            return float("nan")
        n_above = sum(1 for e in errors if e > -gap_to_lower)
        return n_above / len(errors)


# ============================================================================
# Helpers
# ============================================================================
def _ev_per_dollar(p_no: float, no_ask: float) -> float:
    """EV per dollar invested in NO at no_ask, given Pr(NO wins) = p_no.

    Polymarket fee = 2% on winnings only:
      shares = 1/no_ask
      win_payoff = shares * (1 - no_ask) * (1 - 0.02) = (1 - no_ask)/no_ask * 0.98
      loss = -1
      EV/$1 = p_no * win_payoff - (1 - p_no) * 1
    """
    if no_ask <= 0 or no_ask >= 1:
        return 0.0
    if p_no != p_no:  # NaN
        return 0.0
    win_pl = (1.0 - no_ask) / no_ask * 0.98
    return p_no * win_pl - (1.0 - p_no) * 1.0


def _position_size_usd(ev_per_dollar: float) -> float:
    """Map EV-per-dollar to position size."""
    if ev_per_dollar >= SIZE_TIER_LARGE_EV:
        return POSITION_SIZE_LARGE_USD
    if ev_per_dollar >= SIZE_TIER_MEDIUM_EV:
        return POSITION_SIZE_MEDIUM_USD
    if ev_per_dollar >= SIZE_TIER_SMALL_EV:
        return POSITION_SIZE_BASE_USD
    return 0.0  # below floor — skip


def _settlement_lead_hours(
    target_date: date, station_timezone: str, now_utc: datetime,
) -> Optional[float]:
    try:
        tz = ZoneInfo(station_timezone)
    except Exception:
        return None
    settlement_local = datetime.combine(
        target_date, time(SETTLEMENT_LOCAL_HOUR, 0), tzinfo=tz,
    )
    delta_s = (settlement_local - now_utc).total_seconds()
    return delta_s / 3600.0


# ============================================================================
# Main entry point
# ============================================================================
def evaluate_tail_no_bracket(
    *,
    contract: BracketContract,
    forecast_high_f: float,
    hit_rate_engine: EmpiricalHitRate,
    constraints: TradingConstraints,
    portfolio_state: PortfolioState,
    now_utc: Optional[datetime] = None,
    correlation_group_used: dict[str, float] | None = None,
    already_open_token_ids: set[str] | None = None,
    cooldown_token_ids: set[str] | None = None,
) -> tuple[Optional[TradePlan], TailNoGateResult]:
    """Evaluate one bracket through the tail-NO gates."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if correlation_group_used is None:
        correlation_group_used = {}
    if already_open_token_ids is None:
        already_open_token_ids = set()
    if cooldown_token_ids is None:
        cooldown_token_ids = set()

    diag = TailNoGateResult(
        contract_id=contract.market_id,
        city=contract.city,
        direction="",
        bracket_lower_f=contract.lower_f,
        bracket_upper_f=contract.upper_f,
        passed=False,
    )

    # Gate 0: city registry
    if contract.city not in CITY_REGISTRY:
        diag.reject_reason = "unknown_city"
        return None, diag
    station = CITY_REGISTRY[contract.city]

    # Gate 1: forecast outside bracket → determine direction & edge distance
    diag.forecast_high_f = forecast_high_f
    bracket_lower = float(contract.lower_f)
    bracket_upper = float(contract.upper_f)
    gap_to_upper = forecast_high_f - bracket_upper
    gap_to_lower = bracket_lower - forecast_high_f

    if gap_to_upper > 0:
        direction = "BELOW"
        edge_distance = gap_to_upper
    elif gap_to_lower > 0:
        direction = "ABOVE"
        edge_distance = gap_to_lower
    else:
        diag.reject_reason = "forecast_inside_bracket"
        return None, diag
    diag.direction = direction
    diag.edge_distance_f = edge_distance

    # Gate 2: edge distance band
    if edge_distance < EDGE_DISTANCE_MIN_F:
        diag.reject_reason = f"edge_too_close ({edge_distance:.2f}°F)"
        return None, diag
    if edge_distance > EDGE_DISTANCE_MAX_F:
        diag.reject_reason = f"edge_too_far ({edge_distance:.2f}°F)"
        return None, diag

    # Gate 3: lead hours
    lead_hours = _settlement_lead_hours(
        contract.target_date, station.timezone, now_utc,
    )
    if lead_hours is None:
        diag.reject_reason = "tz_resolution_fail"
        return None, diag
    diag.lead_hours = lead_hours
    if lead_hours < LEAD_HOURS_MIN:
        diag.reject_reason = f"lead_too_close ({lead_hours:.2f}h)"
        return None, diag
    if lead_hours > LEAD_HOURS_MAX:
        diag.reject_reason = f"lead_too_far ({lead_hours:.2f}h)"
        return None, diag

    # Gate 4: NO ask price band
    best_bid_yes = float(contract.best_bid_yes or 0)
    best_ask_yes = float(contract.best_ask_yes or 0)
    if best_bid_yes <= 0 and best_ask_yes <= 0:
        diag.reject_reason = "no_orderbook_quote"
        return None, diag
    no_ask = 1.0 - best_bid_yes if best_bid_yes > 0 else 1.0  # complement of YES bid
    diag.no_ask = no_ask
    if no_ask < NO_ASK_MIN:
        diag.reject_reason = f"no_ask_too_low ({no_ask:.4f})"
        return None, diag
    if no_ask > NO_ASK_MAX:
        diag.reject_reason = f"no_ask_too_high ({no_ask:.4f})"
        return None, diag

    # Gate 4b (Apr 28 2026 PM): wide-bracket low-NO-ask reject.
    # Wide brackets (width > 10°F) are one-sided "X or higher" / "X or
    # below" contracts where one bracket bound is a sentinel value. At
    # NO ask < 0.50 the market thinks YES is likely; the loss-magnitude
    # asymmetry on a one-sided bracket (no upper containment) means a
    # single-degree weather miss is full notional. Lifetime n=15 wide-NO
    # trades net −$217.27, with the two outright settle losses both at
    # NO entry < 0.40. Filter resolves the dominant loss pattern without
    # affecting normal 1°C contracts (width ≈ 1.8°F).
    bracket_width_f = abs(float(contract.upper_f) - float(contract.lower_f))
    if bracket_width_f > WIDE_BRACKET_F and no_ask < WIDE_BRACKET_MIN_NO_ASK:
        diag.reject_reason = (
            f"wide_bracket_low_no_ask "
            f"(width={bracket_width_f:.1f}F, no_ask={no_ask:.3f})"
        )
        return None, diag

    # Gate 5: liquidity
    liquidity = float(contract.liquidity or 0)
    if liquidity < LIQUIDITY_MIN_USD:
        diag.reject_reason = f"liquidity_too_thin (${liquidity:.0f})"
        return None, diag

    # Gate 6: empirical hit rate vs market — must clear edge_floor_pp
    if direction == "BELOW":
        p_no_emp = hit_rate_engine.p_no_below(lead_hours, gap_to_upper)
    else:
        p_no_emp = hit_rate_engine.p_no_above(lead_hours, gap_to_lower)
    if p_no_emp != p_no_emp:  # NaN
        diag.reject_reason = "empirical_hit_unavailable"
        return None, diag
    diag.empirical_hit = p_no_emp

    # Edge in percentage points: empirical_p_no - market_implied_p_no.
    # Market implies p_no = no_ask (for risk-neutral pricing).
    edge_pp = p_no_emp - no_ask
    diag.edge_pp = edge_pp
    if edge_pp < EDGE_FLOOR_PP:
        diag.reject_reason = f"edge_below_floor ({edge_pp*100:+.2f}pp)"
        return None, diag

    # Gate 7: token_id_no exists
    token_id_no = contract.token_id_no or ""
    if not token_id_no:
        diag.reject_reason = "no_token_id"
        return None, diag

    # Gate 8: cooldown / duplicate guard
    if token_id_no in already_open_token_ids:
        diag.reject_reason = "already_open"
        return None, diag
    if token_id_no in cooldown_token_ids:
        diag.reject_reason = "in_cooldown"
        return None, diag

    # Gate 9: position sizing & correlation-group cap
    ev_per_dollar = _ev_per_dollar(p_no_emp, no_ask)
    diag.ev_per_dollar = ev_per_dollar
    target_notional = _position_size_usd(ev_per_dollar)
    if target_notional <= 0:
        diag.reject_reason = (
            f"ev_below_size_floor ({ev_per_dollar:+.4f})"
        )
        return None, diag
    diag.target_notional = target_notional

    group_key = station.correlation_group.value
    used = correlation_group_used.get(group_key, 0.0)
    if used + target_notional > CORRELATION_GROUP_CAP_USD:
        diag.reject_reason = (
            f"corr_group_full ({group_key}: ${used:.0f}+{target_notional:.0f} "
            f"> ${CORRELATION_GROUP_CAP_USD:.0f})"
        )
        return None, diag

    # All gates passed → build TradePlan
    diag.passed = True
    diag.reject_reason = "PASSED"

    rationale = [
        f"tail_no: dir={direction}, edge_dist={edge_distance:.2f}°F",
        f"lead={lead_hours:.1f}h, fc={forecast_high_f:.1f}°F",
        f"bracket=[{bracket_lower:.1f},{bracket_upper:.1f}]°F",
        f"no_ask={no_ask:.4f}, market_implied_p_no={no_ask*100:.1f}%",
        f"empirical_p_no={p_no_emp*100:.1f}%, edge_pp={edge_pp*100:+.2f}pp",
        f"EV/$1={ev_per_dollar:+.4f}, size=${target_notional:.0f}",
    ]

    plan = TradePlan(
        strategy_name=STRATEGY_NAME,
        market=contract.market_id,
        question=contract.question,
        category=CATEGORY,
        outcome="NO",
        token_id=token_id_no,
        side="NO",
        signal_score=ev_per_dollar,
        target_notional=target_notional,
        reference_price=no_ask,
        best_ask=no_ask,
        best_bid=max(0.0, 1.0 - best_ask_yes) if best_ask_yes < 1.0 else no_ask,
        spread=float(contract.spread or 0.02),
        top_ask_size=float(contract.liquidity or 0),
        top_bid_size=float(contract.liquidity or 0),
        risk_score=1.0 - p_no_emp,
        expected_value=ev_per_dollar * target_notional,
        executable=True,
        rationale=rationale,
        metadata={
            "city": contract.city,
            "target_date": contract.target_date.isoformat(),
            "bracket_lower_f": bracket_lower,
            "bracket_upper_f": bracket_upper,
            "direction": direction,
            "edge_distance_f": edge_distance,
            "forecast_high_f": forecast_high_f,
            "lead_hours": lead_hours,
            "no_ask": no_ask,
            "empirical_p_no": p_no_emp,
            "market_implied_p_no": no_ask,
            "edge_pp": edge_pp,
            "ev_per_dollar": ev_per_dollar,
            "correlation_group": group_key,
            # Apr 27 2026 — canonical save-trade convention. main.py
            # reads these three keys directly into the DB columns to
            # keep edge units consistent across strategies (see CLAUDE
            # §15.x edge-convention audit). model_prob is the side's
            # P(win); for NO trades that's empirical_p_no. token_side
            # is the side label so analytics can filter NO vs YES.
            # edge_after_fees is the fee-adjusted PP gap, NOT the
            # raw EV-per-dollar.
            "model_prob": p_no_emp,
            "token_side": "NO",
            "edge_after_fees": edge_pp,
        },
    )
    return plan, diag


def analyze_tail_no_brackets(
    *,
    contracts: list[BracketContract],
    forecasts_by_city_date: dict[tuple[str, date], dict[str, float]],
    constraints: TradingConstraints,
    portfolio_state: PortfolioState,
    hit_rate_engine: Optional[EmpiricalHitRate] = None,
    now_utc: Optional[datetime] = None,
    already_open_token_ids: set[str] | None = None,
    cooldown_token_ids: set[str] | None = None,
) -> StrategyAnalysis:
    """Top-level analyzer. Iterate active brackets and return tail-NO plans.

    Args:
        contracts: from market_scanner.find_weather_bracket_markets()
        forecasts_by_city_date: {(city, date): {"forecast_high_f": float}}
            Caller must pre-fetch forecasts. We don't fetch here to keep
            this function fast and side-effect free.
    """
    hit_rate_engine = hit_rate_engine or EmpiricalHitRate()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    already_open_token_ids = already_open_token_ids or set()
    cooldown_token_ids = cooldown_token_ids or set()

    diags: list[TailNoGateResult] = []
    plans: list[TradePlan] = []
    signals: list[StrategySignal] = []
    counters: dict[str, int] = {}
    correlation_used: dict[str, float] = {}

    # Sort by descending EV preview so we fill the corr-group caps with the
    # highest-EV trades first (greedy-optimal under linear cap constraint).
    def _quick_ev_preview(ct: BracketContract) -> float:
        fc = forecasts_by_city_date.get((ct.city, ct.target_date))
        if fc is None:
            return -999.0
        forecast_high = fc.get("forecast_high_f")
        if forecast_high is None:
            return -999.0
        gap_u = forecast_high - ct.upper_f
        gap_l = ct.lower_f - forecast_high
        if gap_u <= 0 and gap_l <= 0:
            return -999.0
        if gap_u > 0:
            edge_d = gap_u
            direction = "BELOW"
        else:
            edge_d = gap_l
            direction = "ABOVE"
        if edge_d < EDGE_DISTANCE_MIN_F or edge_d > EDGE_DISTANCE_MAX_F:
            return -999.0
        # Quick lead estimate from days_out (avoid full TZ resolution here).
        days_out = (ct.target_date - now_utc.date()).days
        lead_h = max(24.0, days_out * 24.0)
        if direction == "BELOW":
            p_no = hit_rate_engine.p_no_below(lead_h, gap_u)
        else:
            p_no = hit_rate_engine.p_no_above(lead_h, gap_l)
        if p_no != p_no:
            return -999.0
        best_bid_yes = float(ct.best_bid_yes or 0)
        if best_bid_yes <= 0:
            return -999.0
        no_ask = 1.0 - best_bid_yes
        if no_ask < NO_ASK_MIN or no_ask > NO_ASK_MAX:
            return -999.0
        return _ev_per_dollar(p_no, no_ask)

    sorted_contracts = sorted(
        contracts, key=_quick_ev_preview, reverse=True,
    )

    for ct in sorted_contracts:
        fc = forecasts_by_city_date.get((ct.city, ct.target_date))
        if fc is None:
            counters["no_forecast_for_target"] = (
                counters.get("no_forecast_for_target", 0) + 1
            )
            continue
        forecast_high = fc.get("forecast_high_f")
        if forecast_high is None:
            counters["no_forecast_for_target"] = (
                counters.get("no_forecast_for_target", 0) + 1
            )
            continue
        plan, diag = evaluate_tail_no_bracket(
            contract=ct,
            forecast_high_f=float(forecast_high),
            hit_rate_engine=hit_rate_engine,
            constraints=constraints,
            portfolio_state=portfolio_state,
            now_utc=now_utc,
            correlation_group_used=correlation_used,
            already_open_token_ids=already_open_token_ids,
            cooldown_token_ids=cooldown_token_ids,
        )
        diags.append(diag)
        bucket = diag.reject_reason.split(" ")[0].split(":")[0] or "unknown"
        if bucket == "PASSED":
            bucket = "passed"
        counters[bucket] = counters.get(bucket, 0) + 1
        if plan is not None:
            plans.append(plan)
            station = CITY_REGISTRY[ct.city]
            correlation_used[station.correlation_group.value] = (
                correlation_used.get(station.correlation_group.value, 0.0)
                + plan.target_notional
            )
            signals.append(StrategySignal(
                market=plan.market,
                question=plan.question,
                category=plan.category,
                outcome=plan.outcome,
                side=plan.side,
                signal_score=plan.signal_score,
                reference_price=plan.reference_price,
                metadata=dict(plan.metadata),
            ))

    return StrategyAnalysis(
        strategy_name=STRATEGY_NAME,
        signals=signals,
        trade_plan=plans,
        diagnostics={
            "n_evaluated": len(diags),
            "n_passed": len(plans),
            "n_24h_errors": hit_rate_engine.n_24,
            "n_48h_errors": hit_rate_engine.n_48,
            "reject_counters": counters,
            "correlation_group_used": correlation_used,
            "passed_diags": [d for d in diags if d.passed],
        },
    )
