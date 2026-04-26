from __future__ import annotations

import argparse
import importlib.util
import json
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_strat.application.service import StrategyApplicationService
from polymarket_strat.config import AccountConfig, PortfolioState, TradingConstraints, load_env_file
from polymarket_strat.execution import LiveExecutor, PaperExecutor, order_to_dict
from polymarket_strat.presentation.reporting import write_strategy_report


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _load_portfolio_state(state_path: str, constraints: TradingConstraints) -> PortfolioState:
    return PortfolioState.load(state_path, constraints)


def run_analyze(strategy_name: str, *, use_sample: bool, state_path: str) -> None:
    constraints = TradingConstraints()
    portfolio_state = _load_portfolio_state(state_path, constraints)
    projected_state = portfolio_state.clone()
    service = StrategyApplicationService(use_sample=use_sample)
    analysis = service.analyze(strategy_name, constraints=constraints, portfolio_state=projected_state)
    _print_json(
        {
            "strategy": strategy_name,
            "risk_config": asdict(constraints),
            "portfolio_state": asdict(portfolio_state),
            "projected_portfolio_state": asdict(projected_state),
            **service.describe_analysis(analysis),
        }
    )


def run_execute(strategy_name: str, *, use_sample: bool, mode: str, state_path: str, confirm_live: bool) -> None:
    constraints = TradingConstraints()
    portfolio_state = _load_portfolio_state(state_path, constraints)
    projected_state = portfolio_state.clone()
    service = StrategyApplicationService(use_sample=use_sample)
    analysis = service.analyze(strategy_name, constraints=constraints, portfolio_state=projected_state)
    executable_orders = [item for item in analysis.trade_plan if item.executable]

    if mode == "paper":
        executor = PaperExecutor()
    else:
        if not confirm_live:
            raise ValueError("Live execution requires --confirm-live so you do not place real orders accidentally.")
        account = AccountConfig.from_env()
        executor = LiveExecutor(account)

    # Lazy-import DB only for weather strategy (avoids sqlite dep for other strategies)
    weather_db = None
    already_open_token_ids: set[str] = set()
    if strategy_name == "weather_bracket":
        from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
        weather_db = WeatherDatabase()
        # Prevent re-entering the same token that is already an open position
        for pos in weather_db.get_open_positions():
            if pos.get("token_id"):
                already_open_token_ids.add(pos["token_id"])
        # Also respect rebalance-cooldown tokens — a token we just exited
        # on edge collapse should not be re-entered for 6h (aligned with GFS
        # 00/06/12/18Z refresh cadence).
        already_open_token_ids |= weather_db.get_cooldown_tokens()

    # Apr 25 2026 — STRATEGY 7: Claude API qualitative gate.
    # After all quant gates approve a trade, send to Claude for a final
    # "real edge or artifact?" review. Gate is opt-in via ANTHROPIC_API_KEY
    # env var — when missing, every trade passes through (no-op).
    # Coherence-arb signals (strategy_subtype="coherence_arb") bypass
    # Claude review since they're pure-arithmetic edges that need no
    # qualitative validation.
    from polymarket_strat.notifications.claude_gate import get_claude_reviewer
    claude_gate = get_claude_reviewer() if strategy_name == "weather_bracket" else None
    claude_vetoes: list[dict[str, Any]] = []

    results = []
    for item in executable_orders:
        if item.token_id in already_open_token_ids:
            continue

        # Apr 25 2026 — Claude gate. Skip for arbitrage strategies.
        if (
            claude_gate is not None
            and claude_gate.enabled
            and item.metadata.get("strategy_subtype") != "coherence_arb"
        ):
            review_ctx = {
                "city": item.metadata.get("city"),
                "target_date": item.metadata.get("target_date"),
                "question": item.question,
                "bracket_lower_f": item.metadata.get("bracket_lower_f"),
                "bracket_upper_f": item.metadata.get("bracket_upper_f"),
                "model_prob": item.metadata.get("model_prob"),
                "market_prob": item.reference_price,
                "edge_after_fees": item.metadata.get("edge_after_fees"),
                "ensemble_spread_f": item.metadata.get("ensemble_spread_f"),
                "forecast_high_f_per_model": item.metadata.get("forecast_high_f_per_model"),
                "regime": item.metadata.get("regime"),
                "lead_hours": item.metadata.get("raw_lead_hours"),
                "season": item.metadata.get("season"),
                "strategy_subtype": item.metadata.get("strategy_subtype"),
            }
            review = claude_gate.review_trade(review_ctx)
            # Stamp Claude's verdict on the metadata so trade_history
            # and dashboards can show it.
            item.metadata["claude_approved"] = review.approved
            item.metadata["claude_confidence"] = review.confidence
            item.metadata["claude_reasoning"] = review.reasoning
            item.metadata["claude_red_flags"] = review.red_flags
            if not review.approved:
                claude_vetoes.append({
                    "city": item.metadata.get("city"),
                    "question": item.question[:60],
                    "confidence": review.confidence,
                    "reasoning": review.reasoning,
                    "red_flags": review.red_flags,
                })
                continue  # vetoed — skip execution

        order = executor.execute_market_buy(
            market=item.market,
            outcome=item.outcome,
            token_id=item.token_id,
            amount=item.target_notional,
            reference_price=item.best_ask,
        )
        order_dict = order_to_dict(order)
        results.append(order_dict)
        portfolio_state.cash = max(portfolio_state.cash - item.target_notional, 0.0)
        portfolio_state.open_positions[item.market] = portfolio_state.open_positions.get(item.market, 0.0) + item.target_notional
        portfolio_state.category_exposure[item.category] = portfolio_state.category_exposure.get(item.category, 0.0) + item.target_notional
        portfolio_state.category_position_counts[item.category] = portfolio_state.category_position_counts.get(item.category, 0) + 1

        # Persist weather bracket trades for paper-trading P&L tracking
        if weather_db is not None:
            meta = item.metadata
            raw_date = meta.get("target_date")
            try:
                tdate = date.fromisoformat(raw_date) if raw_date else date.today()
            except (ValueError, TypeError):
                tdate = date.today()
            expected_ev = _expected_pnl(
                model_prob=float(meta.get("model_prob", 0.0)),
                entry_price=float(item.best_ask),
                notional=float(item.target_notional),
            )
            trade_id = weather_db.save_trade(
                city=meta.get("city", ""),
                target_date=tdate,
                bracket_lower_f=float(meta.get("bracket_lower_f", 0.0)),
                bracket_upper_f=float(meta.get("bracket_upper_f", 0.0)),
                model_prob=float(meta.get("model_prob", 0.0)),
                market_prob=float(item.reference_price),
                edge=float(item.expected_value),
                kelly_fraction=float(meta.get("kelly_fraction", 0.0)),
                notional=float(item.target_notional),
                entry_price=float(item.best_ask),
                side=item.side,
                mode=mode,
                market_id=item.market,
                token_id=item.token_id,
                question=item.question,
                regime=meta.get("regime"),
                expected_pnl=expected_ev,
                # Persist rebalance baseline. entry_edge = fee-adjusted edge
                # at fill (computed by forecast.py::edge), content hash = the
                # forecast fingerprint the plan was built on. Both feed into
                # run_rebalance's dual-threshold decision at the next cycle.
                entry_edge=float(meta.get("edge_after_fees", item.expected_value)),
                forecast_content_hash=str(meta.get("forecast_content_hash") or ""),
                # Apr 24 2026 (Citadel fix #5) — which side (YES or NO) was
                # picked by strategy. Defaults to "YES" on legacy rows.
                token_side=str(meta.get("token_side") or "YES"),
                # Apr 24 2026 (data expansion) — per-model forecast diagnostics
                # extracted from signal.metadata. Enables full posthoc
                # analysis: "was GFS off, was ECMWF off, what was the
                # ensemble disagreement, which model run did we trade?"
                forecast_high_f_gfs=_get_nested_float(
                    meta.get("forecast_high_f_per_model"), "gfs"
                ),
                forecast_high_f_ecmwf=_get_nested_float(
                    meta.get("forecast_high_f_per_model"), "ecmwf"
                ),
                ensemble_spread_f=(
                    float(meta["ensemble_spread_f"])
                    if meta.get("ensemble_spread_f") is not None
                    else None
                ),
                # model_prob_raw is pre-isotonic; we don't currently split
                # it out from the ensemble mean but the hook is here for
                # when we do. For now, equal to model_prob (stamp post-
                # isotonic value; raw is NULL until we add explicit split).
                model_prob_raw=None,
                reliability_multiplier=(
                    float(meta["reliability_multiplier"])
                    if meta.get("reliability_multiplier") is not None
                    else None
                ),
                init_time_gfs=_get_nested_str(
                    meta.get("init_time_per_model"), "gfs"
                ),
                init_time_ecmwf=_get_nested_str(
                    meta.get("init_time_per_model"), "ecmwf"
                ),
                season=(
                    int(meta["season"])
                    if meta.get("season") is not None
                    else None
                ),
            )
            order_dict["trade_id"] = trade_id
            order_dict["expected_pnl"] = expected_ev

    if weather_db is not None:
        weather_db.close()

    portfolio_state.save(state_path)
    _print_json(
        {
            "strategy": strategy_name,
            "mode": mode,
            "orders": results,
            "count": len(results),
            "state_path": state_path,
            "portfolio_state": asdict(portfolio_state),
        }
    )


def run_positions() -> None:
    """Show all open (unsettled) paper/live weather trades."""
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
    db = WeatherDatabase()
    positions = db.get_open_positions()
    db.close()
    _print_json({"open_positions": positions, "count": len(positions)})


def _get_nested_float(d: Any, key: str) -> float | None:
    """Safely extract a float from a nested dict. Apr 24 data-expansion
    helper used in run_execute to unpack signal.metadata's per-model
    diagnostic dicts into trade_history columns. Returns None on any
    missing/malformed input so we never crash the fill path on a legacy
    signal that didn't carry the full diagnostic payload."""
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_nested_str(d: Any, key: str) -> str | None:
    """Mirror of _get_nested_float for string extraction (e.g. init_time
    timestamps, which come through as ISO8601 strings)."""
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return None


def _pnl(
    *,
    outcome: int,
    notional: float,
    entry_price: float,
    fee: float = 0.02,
    token_side: str = "YES",
) -> float:
    """Compute P&L for a binary bracket trade.

    Polymarket charges the 2% fee on WINNINGS ONLY (profit above notional),
    not on the full payout. See `.claude/claude.md` §4.1 for the derivation.
        WIN:  pnl = n_shares * (1 - entry_price) * (1 - fee)
        LOSS: pnl = -notional

    Apr 24 2026 (Citadel fix #5) — `token_side` is "YES" (legacy default)
    or "NO". The `outcome` column from trade_history stores market-side
    truth: 1=YES resolved, 0=NO resolved. For NO token holders the
    win/loss semantics flip:
        YES token: WIN when outcome==1
        NO  token: WIN when outcome==0
    The `entry_price` is the price we ACTUALLY PAID (in NO-token units
    for NO-side trades), so the per-share payout math is identical once
    the win/loss decision flips.
    """
    # Apr 25 2026 (LATE) — entry_price sanity floor. The bot is supposed
    # to refuse trades with entry_price < min_entry_price (default 0.02 =
    # 50× implied leverage). Anything below 0.02 is either:
    #   (a) bracket-parser artifact (degenerate bounds → P=0 or P=1)
    #   (b) low-temp market mismatched with high-temp model (today's bug:
    #       London lowest-temp bracket priced at 0.0065)
    #   (c) market resolution happened pre-settlement (don't enter)
    # In all cases the resulting "win" is fake — refuse to settle a win
    # at obscene multiples. Cap pnl at 10× notional (already extreme;
    # legitimate weather wins are 30¢ to ~$1 per share).
    n_shares = notional / entry_price if entry_price > 0 else 0
    if token_side == "NO":
        won = outcome == 0
    else:
        won = outcome == 1

    if won:
        raw_pnl = n_shares * (1 - entry_price) * (1 - fee)
        # Sanity cap: if entry_price slipped below the min_entry_price
        # constraint floor (0.02), the trade was an artifact and the
        # "win" payout is fake. Legitimate trades cap at ~50× notional
        # (entry_price=0.02 → 1/0.02 = 50 shares per $1, win pays
        # ~50 × 0.98 × notional ≈ 49× notional).
        # Threshold = 50× notional. Above that → entry_price was below
        # 0.02 floor → artifact → return 0.
        max_legitimate_pnl = 50.0 * notional
        if raw_pnl > max_legitimate_pnl:
            import sys
            print(
                f"[settle] PNL CAP triggered: raw=${raw_pnl:.2f} > 50× notional "
                f"${max_legitimate_pnl:.2f}. entry_price={entry_price:.4f} below "
                f"min_entry_price=0.02 floor. Likely artifact (e.g. low-temp "
                f"market or degenerate bracket parse). Capping pnl at 0.",
                file=sys.stderr,
            )
            return 0.0  # artifact, not real win
        return round(raw_pnl, 4)
    return round(-notional, 4)


def _expected_pnl(
    *, model_prob: float, entry_price: float, notional: float, fee: float = 0.02
) -> float:
    """Model-predicted EV for a YES-token trade at entry time.

    Immutable scoreboard value — computed once at `save_trade` time and
    never mutates. At settlement, compare against realised `pnl` to tell
    "bad luck" from "bad model" (see CLAUDE.md §11, Day-30 gate).

    Derivation (per $1 of notional, entry_price p, P = model_prob, f = fee):
        WIN  (prob P):   pnl/$ = (1-f)(1-p)/p
        LOSS (prob 1-P): pnl/$ = -1
        EV/$ = P * (1-f)(1-p)/p  -  (1-P)

    Sanity: fee-free, this collapses to (P - p)/p = raw_edge / p.
    """
    if entry_price <= 0 or notional <= 0:
        # Degenerate input — min_entry_price=0.02 is enforced upstream, so
        # this only fires on malformed calls. Return 0 rather than NaN.
        return 0.0
    win_per_dollar = (1.0 - fee) * (1.0 - entry_price) / entry_price
    ev_per_dollar = model_prob * win_per_dollar - (1.0 - model_prob)
    return round(ev_per_dollar * notional, 4)


# ---------------------------------------------------------------------------
# Rebalance — dual-threshold exit rule (Apr 24 2026)
# ---------------------------------------------------------------------------
#
# Runs at the top of each autotrade cycle, BEFORE settlement and BEFORE the
# market scan. For each open position we:
#   1. Re-fetch the current Open-Meteo forecast at the correct horizon.
#   2. Recompute p_model (ensemble-weighted bracket CDF) at the current
#      forecast + calibrated error distribution for the live regime.
#   3. Fetch best_ask from the CLOB for a CONSERVATIVE view of current edge
#      ("if I had to re-enter right now, would edge still exist?").
#   4. Compare `current_edge = p_model - best_ask - fee_drag` to the
#      `entry_edge` stored at trade time.
#   5. Apply the DUAL THRESHOLD:
#        - forecast_content_hash UNCHANGED (stale p_model, market-only move):
#          exit if drop ≥ 0.15
#        - forecast_content_hash CHANGED (fresh p_model, real new info):
#          exit if drop ≥ 0.10
#      Logic: market-only moves are dirtier signal (noise, low liquidity
#      mispriced ticks), so we tolerate more. Fresh-forecast moves are a
#      cleaner decision boundary — cut harder.
#   6. On exit, compute paper-mode PnL using best_bid (the price we could
#      actually hit), insert a 6-hour token cooldown, mark the trade row
#      with outcome=2 (sentinel for "exited via rebalance") and pnl.
#
# Rebalance is deliberately one-sided: it only fires on edge *collapse*
# (current_edge < entry_edge by threshold amount). This makes it immune to
# the "model stale → market moves toward us → spurious increase in apparent
# edge" failure mode, because we never ADD to a position here — only close.
# Add-to-position under fresh-forecast gating is a separate backlog item.
_REBALANCE_DROP_THRESHOLD_STALE = 0.15   # hash unchanged
_REBALANCE_DROP_THRESHOLD_FRESH = 0.10   # hash changed

# Apr 24 2026 (Citadel fix #2) — profit-take threshold. Exit when the
# market has moved >= this many cents in our favor from entry, regardless
# of whether model_prob still supports the edge. Rationale:
#   - A trade that enters at 0.35 and is now bid 0.55 has 20¢ of unrealized
#     gain. If we hold to settlement (24-48h away), roughly 35% of "winners"
#     mean-revert before settlement (prob mass on outcome=0 is still ~35%
#     when our model says 65% — and real hit rate is lower than that).
#   - Expected value of booking +20¢ now vs holding: booking = +20¢ × 1.0.
#     Holding: +25¢ × 0.65 + (-35¢) × 0.35 = 16.25 - 12.25 = +4¢. We keep
#     16¢ more by booking.
#   - Math only holds when price move > 15¢. At +15¢ book/hold EV is close
#     to even (+15 × 1 vs +25 × 0.65 - 35 × 0.35 = 16.25 - 12.25 = +4, so
#     hold by 11¢). At +20¢ book dominates.
# Threshold conservatively at 20¢ (price-based, not edge-based) so we book
# regardless of whether the model still agrees — the market has already
# agreed with us, and conversion back to settlement is uncertain.
_REBALANCE_PROFIT_TAKE_PRICE_GAIN = 0.20

_REBALANCE_COOLDOWN_HOURS = 3            # Apr 24 2026 (fix #9): was 6h,
                                         # halved since we now have explicit
                                         # forecast-hash detection — fresh
                                         # re-entries after cooldown are no
                                         # longer restricted by forecast-run
                                         # cadence.


def _breakeven_current_model_prob(
    *,
    entry_price: float,
    best_bid: float,
    fee_rate: float = 0.02,
) -> float:
    """Analytical breakeven: the current model_prob at which EV_hold = EV_exit.

    Apr 24 2026 (Citadel Q3). Rigorous derivation — math in the strategy
    doc. For a binary bracket token bought at `entry_price` (P_e) now
    quoted at best_bid `P_now`, the exit P&L per $1 notional (deterministic,
    fee on gains only) is:

        EV_exit = 0.98 × (P_now - P_e) / P_e

    The hold-to-settlement EV, conditional on true outcome probability P*:

        EV_hold(P*) = 0.98 × P* × (1 - P_e)/P_e - (1 - P*)

    Solving EV_exit = EV_hold(P*) for the indifference P*:

        P*_breakeven = (0.98 × P_now + 0.02 × P_e) / (0.98 + 0.02 × P_e)

    Interpretation: if our current model_prob > P*_breakeven, hold
    (expected gain-to-settlement exceeds certain cash). If current_model
    < P*_breakeven, exit (model now disagrees enough with the run-up
    that cashing in is preferred).

    This replaces the 20¢-price-gain heuristic with a model-aware rule.
    The heuristic fires too early on well-calibrated hot-streak trades
    (over-exits, burns ~30¢/$1 expected gain) and too late on model-
    collapse trades (under-exits, extends losses). The breakeven rule
    handles both correctly.

    For well-calibrated model, exiting at best_bid = 0.55 after entry at
    0.35 gives P*_breakeven ≈ 0.553 — so hold if current model still
    says >55%, exit if <55%.

    NOTE: this rule ASSUMES `current_model_prob` is side-native. For YES
    positions, caller passes YES-side model_prob (0.55 if our model says
    YES will settle with 55% probability). For NO positions, caller
    passes NO-side model_prob (= 1 - p_yes). The formula is identical
    under the flip because entry_price and best_bid are also side-native.
    """
    if entry_price <= 0 or entry_price >= 1:
        # Degenerate entry — no meaningful breakeven. Return 0.5 (coin flip)
        # so the caller's comparison `current_model < breakeven` is benign.
        return 0.5
    numerator = (1.0 - fee_rate) * best_bid + fee_rate * entry_price
    denominator = (1.0 - fee_rate) + fee_rate * entry_price
    return numerator / denominator


def _compute_current_edge(
    *,
    model_prob: float,
    best_ask: float,
    fee_rate: float = 0.02,
    token_side: str = "YES",
) -> float:
    """Fee-adjusted current edge at best_ask (buy-side mark).

    Mirrors BracketProbabilityCalculator.edge() math — keep in sync if
    the fee model changes there.

    For YES positions:
        edge = (p_model - best_ask_yes) - fee * p_model * (1 - best_ask_yes)

    For NO positions (Apr 24 2026 Citadel fix #5):
        We hold the NO token. Our "p_model" for the YES outcome is the
        input; NO-side probability is (1 - p_model_yes). The market "ask"
        on NO is what we'd pay to exit or re-enter, synthetically
        (1 - best_bid_yes). Caller is expected to pass:
            model_prob   = 1 - p_model_yes   (NO-side model prob)
            best_ask     = 1 - best_bid_yes  (NO-side synthetic ask)
        and the same formula applies.
    """
    raw = model_prob - best_ask
    fee_drag = fee_rate * model_prob * (1.0 - best_ask)
    return raw - fee_drag


def _station_local_lead_hours(
    station: Any,
    target_date: date,
    *,
    now_utc: datetime | None = None,
) -> float:
    """Station-local wall-clock lead to 17:00 settlement lock-in.

    Factored out of strategy.py::analyze so run_rebalance can use the same
    definition of "how far are we from observation" without duplicating
    the timezone math. Returns raw hours (can be negative → past
    settlement → caller should skip).
    """
    from polymarket_strat.domain.weather.strategy import _LOCK_IN_LOCAL

    try:
        tz = ZoneInfo(station.timezone)
    except Exception:
        return -1.0
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    settlement_local = datetime.combine(target_date, _LOCK_IN_LOCAL, tzinfo=tz)
    return (settlement_local - now_local).total_seconds() / 3600.0


def _run_tail_strategy_pass(
    *,
    mainstream_analysis,
    constraints,
    portfolio_state,
):
    """Run the tail-bracket strategy and return (plans, diagnostics).

    Apr 25 2026 — Layer 3 of the late-entry tail strategy. Hooked into
    run_autotrade after the mainstream analyze() call. Re-uses the same
    market scanner output to avoid double-scanning Polymarket.

    Returns:
        (list[TradePlan], diagnostics dict)
        Plans use category="weather_tail" for downstream filtering.
    """
    from polymarket_strat.api import PolymarketPublicClient
    from polymarket_strat.domain.weather.tail_strategy import (
        analyze_tail_brackets,
    )
    from polymarket_strat.infrastructure.weather.market_scanner import (
        WeatherMarketScanner,
    )
    from polymarket_strat.infrastructure.weather.station_client import (
        StationObservationClient,
    )
    from polymarket_strat.infrastructure.weather.grib_client import GribDataClient as GribClient
    from polymarket_strat.domain.weather.models import (
        CITY_REGISTRY, SynopticRegime, WeatherModel,
    )

    # 1. Re-scan markets — same call mainstream uses; cheap, hits Polymarket cache
    scanner = WeatherMarketScanner(PolymarketPublicClient())
    contracts = scanner.find_weather_bracket_markets()
    if not contracts:
        return [], {"n_evaluated": 0, "skipped": "no_contracts"}

    # 2. Pre-build forecast snapshot per (city, lead_bucket) for the brackets
    # whose lead falls in tail-strategy range. Cache to avoid re-fetching.
    grib = GribClient()
    forecasts_by_city: dict[str, dict] = {}
    today = date.today()

    # We only need forecasts for cities with same-day or next-day brackets
    relevant_cities = {c.city for c in contracts
                        if c.target_date in (today, today.replace(day=today.day))
                        and c.city in CITY_REGISTRY}

    for city in relevant_cities:
        station = CITY_REGISTRY[city]
        try:
            fetched = grib.fetch_all_models(station, lead_hours=24)
        except Exception as exc:
            print(f"[tail]   {city}: forecast fetch error: {exc!r}",
                  file=sys.stderr)
            continue
        if not fetched:
            continue
        # Use GFS as the canonical forecast for tail strategy. Mainstream
        # ensembles use multi-model; tail uses GFS directly for simplicity
        # (the ECE audit was on GFS-trained quantile models).
        gfs_forecast = next(
            (f for f in fetched if f.model == WeatherModel.GFS), None
        )
        if gfs_forecast is None:
            continue
        forecasts_by_city[city] = {
            "forecast_high_f": float(gfs_forecast.forecast_high_f),
            "ensemble_spread_f": float(gfs_forecast.ensemble_spread_f or 2.0),
            "regime": "stable_high",  # default — proper regime is set by strategy.py
        }

    if not forecasts_by_city:
        return [], {"n_evaluated": 0, "skipped": "no_forecasts"}

    # 3. Run tail analyzer
    result = analyze_tail_brackets(
        contracts=contracts,
        forecasts_by_city=forecasts_by_city,
        constraints=constraints,
        portfolio_state=portfolio_state,
        station_client=StationObservationClient(),
    )
    return result.trade_plan, result.diagnostics


def _run_tail_no_strategy_pass(
    *,
    constraints,
    portfolio_state,
    db,
):
    """Run the production tail-NO strategy (Apr 26 2026).

    Edge-distance-based gating, 24-72h leads. Buy NO on brackets where
    the forecast sits 1-5°F outside the bracket and market mispriced the
    near-tail risk. See domain/weather/tail_no_strategy.py.

    Returns:
        (list[TradePlan], diagnostics dict)
    """
    import sys

    from polymarket_strat.api import PolymarketPublicClient
    from polymarket_strat.domain.weather.tail_no_strategy import (
        analyze_tail_no_brackets, EmpiricalHitRate,
    )
    from polymarket_strat.infrastructure.weather.market_scanner import (
        WeatherMarketScanner,
    )
    from polymarket_strat.infrastructure.weather.grib_client import (
        GribDataClient,
    )
    from polymarket_strat.domain.weather.models import (
        CITY_REGISTRY, WeatherModel,
    )
    from collections import defaultdict
    from datetime import date, datetime, timezone

    # 1. Pull live brackets.
    scanner = WeatherMarketScanner(PolymarketPublicClient())
    contracts = scanner.find_weather_bracket_markets()
    if not contracts:
        return [], {"n_evaluated": 0, "skipped": "no_contracts"}

    # 2. Fetch forecasts per (city, target_date) — distinct from mainstream
    # because tail-NO needs forecasts for every (city, date) the brackets
    # cover, including 48h+ targets the mainstream strategy may not have
    # cached. Reuse the same Open-Meteo client.
    grib = GribDataClient()
    today = datetime.now(timezone.utc).date()
    fc_cache: dict[tuple[str, date], dict[str, float]] = {}

    city_dates: dict[str, set[date]] = defaultdict(set)
    for c_ in contracts:
        city_dates[c_.city].add(c_.target_date)

    for city, dates in city_dates.items():
        if city not in CITY_REGISTRY:
            continue
        station = CITY_REGISTRY[city]
        for tgt in sorted(dates):
            days_out = (tgt - today).days
            if days_out < 0 or days_out > 4:
                continue
            lead_h = max(24, days_out * 24)
            try:
                fcs = grib.fetch_all_models(
                    station=station, lead_hours=lead_h,
                )
                fcs = [
                    f for f in fcs
                    if f.model in (WeatherModel.GFS, WeatherModel.ECMWF)
                ]
                highs = [
                    f.forecast_high_f for f in fcs if f.forecast_high_f
                ]
                if highs:
                    fc_cache[(city, tgt)] = {
                        "forecast_high_f": sum(highs) / len(highs),
                    }
            except Exception as exc:
                print(
                    f"[tail_no] {city} d+{days_out}: forecast error: {exc!r}",
                    file=sys.stderr,
                )

    if not fc_cache:
        return [], {"n_evaluated": 0, "skipped": "no_forecasts"}

    # 3. Pull already-open + cooldown tokens from DB.
    already_open: set[str] = set()
    cooldown: set[str] = set()
    try:
        for pos in db.get_open_positions():
            tid = pos.get("token_id")
            if tid:
                already_open.add(tid)
        cooldown = db.get_cooldown_tokens()
    except Exception as exc:
        print(f"[tail_no] cooldown/open lookup error: {exc!r}",
              file=sys.stderr)

    # 4. Run analyzer.
    engine = EmpiricalHitRate()
    result = analyze_tail_no_brackets(
        contracts=contracts,
        forecasts_by_city_date=fc_cache,
        constraints=constraints,
        portfolio_state=portfolio_state,
        hit_rate_engine=engine,
        already_open_token_ids=already_open,
        cooldown_token_ids=cooldown,
    )
    return result.trade_plan, result.diagnostics


def run_rebalance(
    *,
    mode: str = "paper",
    confirm_live: bool = False,
    env_file: str = ".env",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Exit positions whose edge has collapsed since entry.

    Dual threshold keyed on forecast_content_hash vs the stored entry hash
    (see module docstring above). Paper-mode closes update trade_history
    directly; live-mode closes require the caller to pass `--confirm-live`
    and are gated behind Phase 3 (not implemented yet — raises if reached).

    `--dry-run` reports what WOULD close without committing any DB writes.
    Useful when tuning thresholds or validating a new calibration set.
    """
    import sys

    from polymarket_strat.api import PolymarketPublicClient
    from polymarket_strat.domain.weather.calibration import RegimeClassifier
    from polymarket_strat.domain.weather.forecast import (
        BracketProbabilityCalculator,
        forecast_content_hash,
    )
    from polymarket_strat.domain.weather.models import (
        CITY_REGISTRY,
        SynopticRegime,
        WeatherModel,
    )
    from polymarket_strat.domain.weather.strategy import _bucket_lead
    from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase

    load_env_file(env_file)
    print(f"[rebalance] Starting (mode={mode}, dry_run={dry_run})...", file=sys.stderr)

    if mode == "live" and not dry_run and not confirm_live:
        raise ValueError(
            "Live rebalance requires --confirm-live so you do not place "
            "real sell orders accidentally. Note: live-mode exits are "
            "Phase 3 scope — paper mode is the current path."
        )

    db = WeatherDatabase()
    client = PolymarketPublicClient()
    grib = GribDataClient()
    calc = BracketProbabilityCalculator()
    regime_clf = RegimeClassifier()

    open_positions = db.get_open_positions()
    now_utc = datetime.now(timezone.utc)
    cooldown_until = (now_utc + timedelta(hours=_REBALANCE_COOLDOWN_HOURS)).isoformat()
    now_iso = now_utc.isoformat()

    # Cache forecasts per (city, lead_bucket) so multiple brackets for the
    # same city on the same day (e.g. Tokyo 20°C, 21°C, 22°C for Apr 20)
    # share one Open-Meteo roundtrip.
    forecast_cache: dict[tuple[str, int], tuple[list[Any], str]] = {}

    exits: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for pos in open_positions:
        city = pos.get("city") or ""
        token_id = pos.get("token_id")
        entry_edge = pos.get("entry_edge")
        stored_hash = pos.get("forecast_content_hash") or ""
        notional = float(pos.get("notional") or 0)
        entry_price = float(pos.get("entry_price") or 0)
        bracket_lower_f = float(pos.get("bracket_lower_f") or 0)
        bracket_upper_f = float(pos.get("bracket_upper_f") or 0)
        category = (pos.get("category") or "").strip()

        # Apr 26 2026 — TAIL-NO trades are buy-and-hold by design. Their
        # per-trade EV (1-3pp) is too small to survive a rebalance churn
        # (spread + 2% fee on the close + 2% fee on the next entry would
        # consume more than the edge). Skip them; let them ride to
        # natural settlement. The empirical hit rate analysis was conditioned
        # on bracket geometry NOT changing, so partial-hold reduces the
        # statistical guarantees.
        if category == "weather_tail_no":
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "tail_no_hold_to_settlement",
            })
            continue

        # Legacy rows pre-dating the entry_edge column can't be rebalanced
        # (we have no baseline to drop from). Flag and skip — they still
        # settle normally at resolution.
        if entry_edge is None:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "legacy_no_entry_edge",
            })
            continue
        if not token_id:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "missing_token_id",
            })
            continue

        station = CITY_REGISTRY.get(city)
        if not station:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "unknown_city",
            })
            continue

        try:
            target_date = date.fromisoformat(pos.get("target_date") or "")
        except (ValueError, TypeError):
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "bad_target_date",
            })
            continue

        # Compute lead in station-local time to the 17:00 lock-in point.
        # Past-settlement positions fall through to the Polymarket/IEM
        # settle path — not our job here.
        raw_lead_h = _station_local_lead_hours(station, target_date, now_utc=now_utc)
        if raw_lead_h <= 0:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "past_settlement",
            })
            continue
        if raw_lead_h < 6.0:
            # Same rule as analyze() — very-short-horizon rebalance adds
            # noise with little decision value.
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "too_close_to_settlement",
            })
            continue

        lead_bucket = _bucket_lead(int(raw_lead_h))
        cache_key = (city, lead_bucket)

        if cache_key not in forecast_cache:
            try:
                fetched = grib.fetch_all_models(station, lead_hours=lead_bucket)
            except Exception as exc:
                print(
                    f"[rebalance] {city} @ {lead_bucket}h: forecast fetch failed: {exc}",
                    file=sys.stderr,
                )
                fetched = []
            # Mirror the strategy.py HRRR/NAM long-lead drop so the hash
            # we recompute here is consistent with the one computed at
            # entry time (analyze() writes the hash AFTER dropping).
            if lead_bucket > 36:
                fetched = [
                    fc for fc in fetched
                    if fc.model not in {WeatherModel.HRRR, WeatherModel.NAM}
                ]
            content_hash = forecast_content_hash(fetched) if fetched else ""
            forecast_cache[cache_key] = (fetched, content_hash)
        forecasts, current_hash = forecast_cache[cache_key]

        if not forecasts:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "no_current_forecast",
            })
            continue

        # Load regime + dists. Mirror strategy.py::_load_dists but simpler
        # (single bracket, no normalization loop). Fall back to STABLE_HIGH
        # if the live regime isn't calibrated, same as strategy.py.
        try:
            ens_stats = grib.fetch_ensemble_spread_stats(
                station, target_date=target_date
            )
            if ens_stats.get("n_members", 0) >= 3:
                regime = regime_clf.classify_from_ensemble(
                    spread_f=ens_stats["spread"],
                    std_f=ens_stats["std"],
                    skewness=ens_stats["skewness"],
                    cape_max=ens_stats["cape_max"],
                    n_members=ens_stats["n_members"],
                )
            else:
                raise ValueError("too few members")
        except Exception:
            spreads = [fc.ensemble_spread_f for fc in forecasts if fc.ensemble_spread_f > 0]
            max_spread = max(spreads) if spreads else 0.0
            regime = regime_clf.classify_from_spread(model_spread_f=max_spread)

        def _load_dists_for_rebalance(r: SynopticRegime):
            dists, fcs = [], []
            for fc in forecasts:
                fc_bucket = _bucket_lead(fc.lead_hours)
                dist = db.get_error_distribution(city, fc.model, r, fc_bucket)
                if dist is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                    dist = db.get_error_distribution(city, WeatherModel.GFS, r, fc_bucket)
                if dist is None and fc_bucket != 24:
                    base = db.get_error_distribution(city, fc.model, r, 24)
                    if base is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                        base = db.get_error_distribution(city, WeatherModel.GFS, r, 24)
                    if base is not None:
                        scale = (fc_bucket / 24.0) ** 0.5
                        from polymarket_strat.domain.weather.models import ErrorDistribution
                        dist = ErrorDistribution(
                            city=base.city, model=base.model, regime=base.regime,
                            lead_hours=fc_bucket, family=base.family,
                            mu=base.mu, sigma=base.sigma * scale,
                            shape=base.shape, nu=base.nu, n_samples=base.n_samples,
                        )
                if dist is None:
                    continue
                if abs(dist.mu) > 5.0 or dist.sigma > 5.0:
                    continue
                dists.append(dist)
                fcs.append(fc)
            return dists, fcs

        error_dists, matching_forecasts = _load_dists_for_rebalance(regime)
        if not error_dists and regime != SynopticRegime.STABLE_HIGH:
            error_dists, matching_forecasts = _load_dists_for_rebalance(SynopticRegime.STABLE_HIGH)
        if not error_dists:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "no_calibrated_dists",
            })
            continue

        # Recompute p_model. NOTE: we do NOT normalize across sibling
        # brackets here — normalization is a cycle-level operation on the
        # full event slate. For a single open position, the raw CDF
        # probability is the relevant quantity.
        model_prob, prob_std = calc.ensemble_bracket_probability(
            forecasts=matching_forecasts,
            error_dists=error_dists,
            lower_f=bracket_lower_f,
            upper_f=bracket_upper_f,
        )

        # Fetch the current best_bid / best_ask from the CLOB orderbook.
        try:
            book = client.get_orderbook(token_id)
        except Exception as exc:
            print(
                f"[rebalance] {city} token {token_id[:10]}...: orderbook fetch failed: {exc}",
                file=sys.stderr,
            )
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "orderbook_fetch_failed",
            })
            continue

        best_ask = _best_price_from_book(book, side="asks", take_min=True)
        best_bid = _best_price_from_book(book, side="bids", take_min=False)
        if best_ask is None or best_bid is None:
            skipped.append({
                "id": pos["id"],
                "city": city,
                "reason": "no_book_prices",
            })
            continue

        # Apr 24 2026 (Citadel fix #5) — for NO-side positions we hold the
        # NO token, so the `book` we just fetched is for the NO token:
        # best_ask/best_bid are already NO-side prices. Our `model_prob`
        # from ensemble_bracket_probability is always the YES-side
        # probability. For edge math on a NO position:
        #     model_prob_no = 1 - model_prob_yes
        #     best_ask_no   = already in `best_ask` (it's the NO-token ask)
        # Same formula applies with flipped model_prob.
        token_side = str(pos.get("token_side") or "YES")
        edge_model_prob = (1.0 - model_prob) if token_side == "NO" else model_prob

        # Mark the exit at best_ask (conservative: could we re-enter?).
        current_edge = _compute_current_edge(
            model_prob=edge_model_prob, best_ask=best_ask, token_side=token_side
        )
        edge_drop = float(entry_edge) - current_edge

        hash_changed = bool(stored_hash) and current_hash != stored_hash
        threshold = (
            _REBALANCE_DROP_THRESHOLD_FRESH
            if hash_changed
            else _REBALANCE_DROP_THRESHOLD_STALE
        )

        # Apr 24 2026 (Citadel fix #2 + Q3) — profit-take triggers.
        # TWO rules evaluated; either fires → exit.
        #
        # RULE A (rigorous, Citadel Q3): the model-aware breakeven.
        #   Computes P*_breakeven = EV-neutral threshold between holding
        #   to settlement and exiting now. If current edge_model_prob is
        #   BELOW breakeven, the expected gain-to-settlement is less than
        #   the certain cash we can realize at best_bid — exit.
        #   Requires a fresh, isotonic-calibrated current model_prob.
        #
        # RULE B (heuristic safety net, Citadel fix #2): 20¢ price gain.
        #   Kept as a backup for:
        #     - isotonic calibration still maturing (first 2 weeks of
        #       paper data); P_breakeven sensitive to P_now overconfidence
        #     - forecast refresh failures that leave model_prob stale
        #     - liquidity risk (20¢ in the hand > 25¢ in the ladder)
        #   Expected to be removed after isotonic convergence (track
        #   how often Rule A vs Rule B fires in the exits log).
        price_gain = best_bid - entry_price

        # Rule A: compute side-appropriate breakeven.
        # `edge_model_prob` was already flipped for NO positions above.
        # `best_bid` and `entry_price` are both side-native (NO-token
        # prices for NO positions, YES for YES).
        breakeven_mp = _breakeven_current_model_prob(
            entry_price=float(entry_price),
            best_bid=float(best_bid),
        )
        breakeven_triggered = edge_model_prob < breakeven_mp

        record = {
            "id": pos["id"],
            "city": city,
            "question": (pos.get("question") or "")[:80],
            "target_date": pos.get("target_date"),
            "token_id": token_id,
            "entry_edge": round(float(entry_edge), 4),
            "entry_price": round(entry_price, 4),
            "notional": notional,
            "model_prob": round(model_prob, 4),
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "current_edge": round(current_edge, 4),
            "edge_drop": round(edge_drop, 4),
            "price_gain": round(price_gain, 4),
            "hash_changed": hash_changed,
            "threshold": threshold,
            "profit_take_threshold": _REBALANCE_PROFIT_TAKE_PRICE_GAIN,
            "breakeven_model_prob": round(breakeven_mp, 4),
            "breakeven_triggered": breakeven_triggered,
            "regime": regime.value,
        }

        # Gate the exit. Evaluation order reflects structural correctness:
        #   1. breakeven_triggered (Rule A — model-aware, rigorous)
        #   2. profit_take (Rule B — heuristic safety net at +20¢)
        #   3. edge_drop (existing stale/fresh rule — catches collapses)
        # Fail-through to hold.
        trigger: str | None = None
        if breakeven_triggered:
            trigger = "breakeven_triggered"
        elif price_gain >= _REBALANCE_PROFIT_TAKE_PRICE_GAIN:
            trigger = "profit_take"
        elif edge_drop >= threshold:
            trigger = "edge_drop_fresh_model" if hash_changed else "edge_drop_stale_model"

        if trigger is None:
            holds.append(record)
            continue

        # Exit. Paper-mode P&L: shares * (best_bid - entry_price), with
        # the 2% fee applied only to gains (matches Polymarket's
        # fee-on-winnings-only semantics). Losses pay no fee on top.
        shares = notional / entry_price if entry_price > 0 else 0.0
        gross = shares * (best_bid - entry_price)
        fee = 0.02 * gross if gross > 0 else 0.0
        exit_pnl = round(gross - fee, 4)

        record["exit_pnl"] = exit_pnl
        record["shares"] = round(shares, 4)
        record["reason"] = trigger

        if not dry_run and mode == "paper":
            db.close_position_as_exit(
                pos["id"],
                pnl=exit_pnl,
                exit_price=best_bid,
                settled_at=now_iso,
                exit_reason=trigger,  # breakeven_triggered / profit_take / edge_drop_*
            )
            db.insert_cooldown(
                token_id=token_id,
                closed_at=now_iso,
                cooldown_until=cooldown_until,
            )
        elif not dry_run and mode == "live":
            # Phase 3 — SafeLiveExecutor integration not wired into
            # rebalance yet. Leaving the dry-run-safe branch in place so
            # --dry-run --mode live still reports the decision surface.
            raise NotImplementedError(
                "Live-mode rebalance exits are Phase 3 scope. "
                "Use --dry-run to preview or stay in paper mode."
            )

        exits.append(record)

    db.close()

    # Telegram is fire-and-forget — wrap in its own try so notification
    # failures can't undo the DB writes we just made.
    if exits:
        _rebalance_notify(exits, dry_run=dry_run, env_file=env_file)

    summary = {
        "mode": mode,
        "dry_run": dry_run,
        "exits": exits,
        "holds": holds,
        "skipped": skipped,
        "exit_count": len(exits),
        "hold_count": len(holds),
        "skip_count": len(skipped),
        "exit_pnl_total": round(sum(e.get("exit_pnl", 0.0) for e in exits), 4),
    }
    print(
        f"[rebalance] {summary['exit_count']} exits, "
        f"{summary['hold_count']} holds, "
        f"{summary['skip_count']} skipped, "
        f"total exit P&L ${summary['exit_pnl_total']:+,.2f}",
        file=sys.stderr,
    )
    return summary


def _best_price_from_book(book: dict[str, Any], *, side: str, take_min: bool) -> float | None:
    """Extract best ask (take_min=True on asks) or best bid (take_min=False on bids).

    Polymarket CLOB /book response: `{"bids": [{"price": "0.53", "size": "120"}...], "asks": [...]}`.
    Both sides are ordered best-first from the server in practice but we
    recompute here so we don't depend on server ordering.
    """
    levels = book.get(side) or []
    prices: list[float] = []
    for lvl in levels:
        try:
            prices.append(float(lvl.get("price") or 0))
        except (TypeError, ValueError):
            continue
    prices = [p for p in prices if p > 0]
    if not prices:
        return None
    return min(prices) if take_min else max(prices)


def _rebalance_notify(exits: list[dict[str, Any]], *, dry_run: bool, env_file: str) -> None:
    """Fire-and-forget Telegram exit alert. Never raises."""
    try:
        from polymarket_strat.config import TelegramConfig
        from polymarket_strat.notifications.telegram import TelegramNotifier

        load_env_file(env_file)
        config = TelegramConfig.from_env()
        notifier = TelegramNotifier(config)
        notifier.send_exit_alert(exits=exits, dry_run=dry_run)
    except Exception as exc:
        import sys
        print(f"[rebalance] Telegram notification failed: {exc}", file=sys.stderr)


def _snapshot_open_position_prices(db: Any, client: Any) -> int:
    """Write one market_prices row per currently-open position.

    Called at the top of run_autotrade (after settle + rebalance). Keeps
    the dashboard's MTM panel fresh without requiring it to hit the CLOB
    from the read-only Streamlit process. Fail-silent per-token: if one
    orderbook fetch errors we log and move on rather than poisoning the
    whole cycle.
    """
    import sys

    positions = db.get_open_positions()
    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0

    for pos in positions:
        token_id = pos.get("token_id")
        if not token_id:
            continue
        market_id = pos.get("market_id")

        best_bid = best_ask = mid = None
        bid_size = ask_size = None
        outcome_prices_json: str | None = None

        try:
            book = client.get_orderbook(token_id)
        except Exception as exc:
            print(
                f"[snapshot] orderbook fetch failed for token {str(token_id)[:10]}...: {exc}",
                file=sys.stderr,
            )
            book = {}

        if book:
            best_bid = _best_price_from_book(book, side="bids", take_min=False)
            best_ask = _best_price_from_book(book, side="asks", take_min=True)
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if bids:
                try:
                    bid_size = float(bids[0].get("size") or 0)
                except (TypeError, ValueError):
                    bid_size = None
            if asks:
                try:
                    ask_size = float(asks[0].get("size") or 0)
                except (TypeError, ValueError):
                    ask_size = None

        # outcomePrices captures the extreme 0.99/0.01 resolution signal
        # the settle path already keys on. Storing them alongside the book
        # tick lets the dashboard flag "market says YES won but we haven't
        # settled yet" cases without another Gamma roundtrip.
        if market_id:
            try:
                mkt = client.get_market(market_id)
                raw = mkt.get("outcomePrices") if mkt else None
                if raw is not None:
                    outcome_prices_json = raw if isinstance(raw, str) else json.dumps(raw)
            except Exception:
                outcome_prices_json = None

        db.insert_market_price(
            token_id=str(token_id),
            market_id=str(market_id) if market_id else None,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
            bid_size=bid_size,
            ask_size=ask_size,
            outcome_prices_json=outcome_prices_json,
            fetched_at_utc=now_iso,
        )
        written += 1

    return written


def _resolve_via_polymarket(pos: dict[str, Any], client: Any) -> int | None:
    """Ask Polymarket Gamma API whether this market has resolved.

    Returns 1 (YES won), 0 (YES lost), or None (still open / unknown).

    **Not gated on `target_date`** — can and should be called hourly for
    every open position regardless of wall-clock. Polymarket itself is the
    source of truth for whether the event has resolved. The station-local
    gate on the IEM fallback stays; IEM daily-high aggregation isn't
    finalised until the day has ended.

    Resolution rule (price-first, Apr 23 2026):
      - Extreme `outcomePrices[0]` alone (≥0.99 for YES won, ≤0.01 for
        YES lost) is sufficient to settle. The formal `closed=True` /
        `acceptingOrders=False` flags are NOT required — in practice
        Polymarket often leaves markets "open + accepting orders" for
        hours after the underlying event has resolved and liquidity has
        converged. Example observed on Apr 22 2026: nyc/toronto/sao_paulo
        brackets with outPx=["0.997", "0.003"] / ["0.011", "0.989"] but
        closed=False, acceptingOrders=True. Prior gate blocked these
        forever until Polymarket manually flipped the flag.
      - Threshold stays strict at 0.99 / 0.01 (NOT 0.95) to prevent
        mid-trading spike settlements. For weather brackets the price
        only converges this tight at or near the resolution window
        because Polymarket's 2% fee + MM spread keeps interior prices
        off the extremes until the outcome is realized.
      - Layer 2 — IEM fallback via `_station_day_ended` — still handles
        markets Polymarket removed or never priced to certainty.
    """
    mkt_id = pos.get("market_id") or pos.get("token_id")
    if not mkt_id:
        return None
    try:
        mkt = client.get_market(mkt_id)
    except Exception:
        return None
    if not mkt:
        return None

    raw_prices = mkt.get("outcomePrices") or []
    if isinstance(raw_prices, str):
        import json as _json
        try:
            raw_prices = _json.loads(raw_prices)
        except Exception:
            return None
    if not raw_prices:
        return None

    try:
        p_yes = float(raw_prices[0])
    except (TypeError, ValueError):
        return None

    # Price-first: extreme outcomePrices alone. The prior gate
    # `(closed OR not acceptingOrders)` was removed Apr 23 2026 after live
    # observation showed Polymarket leaves markets "open" with outPx at
    # 0.99+ for hours post-event.
    if p_yes >= 0.99:
        return 1
    if p_yes <= 0.01:
        return 0
    return None


# IEM/ERA5 grace window after station-local midnight. IEM posts the final
# daily-max within ~1-2h of local EOD (final METAR of the day plus
# aggregation latency). 2h is conservative and avoids partial-day highs.
_SETTLE_GRACE_HOURS = 2


def _station_day_ended(
    city: str,
    target_date: date,
    *,
    grace_hours: int = _SETTLE_GRACE_HOURS,
    now: datetime | None = None,
) -> bool:
    """True iff `target_date` at the city's station is fully past + grace.

    The IEM / ERA5 fallback in `_settle_from_iem` can only return a *final*
    daily-max once the station's local calendar day has ended and enough
    wall-clock has passed for the aggregator to post the last observation.
    Without this gate the fallback returns a *partial-day* max — whatever
    readings have been posted so far — and the bracket comparison resolves
    spuriously (see Apr 22→23 2026 Toronto/NYC/Amsterdam premature losses).

    Uses `CITY_REGISTRY[city].timezone` (IANA string, e.g. ``America/Toronto``,
    ``Pacific/Auckland``) — the same registry that drives settlement-time
    math elsewhere in the pipeline (see CLAUDE.md §6).

    Fail-closed: unknown city, missing TZ, or bad ZoneInfo → return False.
    That keeps the fallback off when we can't compute the gate correctly,
    and we wait for Polymarket's authoritative resolution instead.

    Parameters
    ----------
    city
        Registry key (e.g. ``"toronto"``).
    target_date
        The contract's resolution date.
    grace_hours
        Buffer after local midnight for the station aggregator to post
        the final reading. Default 2h.
    now
        Clock override — for tests. Default ``datetime.now(UTC)``.
    """
    from polymarket_strat.domain.weather.models import CITY_REGISTRY

    station = CITY_REGISTRY.get(city)
    if not station or not getattr(station, "timezone", None):
        return False
    try:
        tz = ZoneInfo(station.timezone)
    except Exception:
        return False

    eod_local = datetime.combine(
        target_date + timedelta(days=1), time(0, 0), tzinfo=tz
    )
    clock = now if now is not None else datetime.now(timezone.utc)
    # If caller passed a naive `now`, assume UTC (tests usually do this).
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock >= eod_local + timedelta(hours=grace_hours)


def _settle_from_iem(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Determine trade outcome from actual IEM weather observation.

    Falls back to this when Polymarket API doesn't have the market
    (resolved markets get removed from the API).

    Returns dict with outcome/pnl/observed_high_f, or None if no observation.
    """
    from polymarket_strat.domain.weather.models import CITY_REGISTRY
    from polymarket_strat.infrastructure.weather.station_client import StationObservationClient

    city = pos.get("city", "")
    station = CITY_REGISTRY.get(city)
    if not station:
        return None

    try:
        target_date = date.fromisoformat(pos["target_date"])
    except (ValueError, TypeError, KeyError):
        return None

    obs_list = StationObservationClient().fetch_daily_highs(
        station, start=target_date, end=target_date
    )
    if not obs_list:
        return None

    observed_high_f = obs_list[0].observed_high_f
    lower_f = float(pos.get("bracket_lower_f", -999))
    upper_f = float(pos.get("bracket_upper_f", 999))
    outcome = 1 if lower_f <= observed_high_f < upper_f else 0

    return {
        "outcome": outcome,
        "observed_high_f": round(observed_high_f, 2),
        "pnl": _pnl(
            outcome=outcome,
            notional=float(pos.get("notional", 0)),
            entry_price=float(pos.get("entry_price") or pos.get("market_prob") or 0),
            token_side=str(pos.get("token_side") or "YES"),
        ),
    }


def run_settle(trade_id: int | None, *, auto: bool) -> None:
    """Settle weather trades.

    Resolution priority:
      1. Polymarket API outcomePrices (fast, works for active/recent markets)
      2. IEM weather observation (reliable fallback for resolved/deleted markets)

    --auto settles all trades whose target_date has passed.
    --trade-id N settles one specific trade.
    """
    from polymarket_strat.api import PolymarketPublicClient
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase

    db = WeatherDatabase()
    client = PolymarketPublicClient()
    settled = []
    errors = []

    positions = db.get_open_positions()
    if trade_id is not None:
        positions = [p for p in positions if p["id"] == trade_id]
        if not positions:
            db.close()
            _print_json({"error": f"No open trade with id={trade_id}"})
            return

    for pos in positions:
        target_date_str = pos.get("target_date", "")
        try:
            target_date = date.fromisoformat(target_date_str)
        except (ValueError, TypeError):
            continue

        outcome: int | None = None
        observed_high_f: float | None = None
        source = "unknown"

        # --- Strategy 1: Polymarket API (hourly-safe, no target_date gate) ---
        # Polymarket is the source of truth for resolution; ask on every
        # settle call regardless of wall-clock. An event like "Highest temp
        # in Seoul Apr 22" resolves ~16:00-18:00 station-local on the day
        # itself — we want to settle within the next hourly cron tick, not
        # at 00:00 Seoul the following day (CLAUDE.md §14 priority).
        outcome = _resolve_via_polymarket(pos, client)
        if outcome is not None:
            source = "polymarket_api"

        # --- Strategy 2: IEM observation (fallback for stale/deleted markets) ---
        # IEM daily-max aggregation requires the day to have ended AT THE
        # STATION'S LOCAL CLOCK (+ grace for the final reading to post),
        # not at the host's. Using `date.today()` here reads EC2's local
        # clock (KST, UTC+9), so at 01:00 KST on Apr 23 a Toronto Apr 22
        # contract would fall through while it's still 12:00 EDT Apr 22 at
        # the station → partial-day max → spurious loss. Gate on the
        # station clock instead. (Apr 22→23 2026 fix.)
        day_done = _station_day_ended(pos.get("city", ""), target_date)
        if outcome is None and day_done:
            iem = _settle_from_iem(pos)
            if iem:
                outcome = iem["outcome"]
                observed_high_f = iem["observed_high_f"]
                source = "iem_observation"

        if outcome is None:
            # Preserve the prior --auto skip semantics: if the station day
            # isn't actually done AND Polymarket hasn't resolved, just
            # quietly wait for the next hourly check. Don't flood errors.
            if auto and not day_done:
                continue
            errors.append({
                "id": pos["id"],
                "city": pos.get("city"),
                "target_date": target_date_str,
                "error": "market unresolved and no IEM observation yet — try again tomorrow",
            })
            continue

        trade_pnl = _pnl(
            outcome=outcome,
            notional=float(pos.get("notional", 0)),
            entry_price=float(pos.get("entry_price") or pos.get("market_prob") or 0),
            token_side=str(pos.get("token_side") or "YES"),
        )
        # Apr 24 2026 (data expansion) — compute forecast error for this
        # trade. Need the ensemble forecast (pre-trade) and the observed
        # station max. Store in trade_history for posthoc regression:
        # does our observed-minus-forecast error correlate with PnL?
        # If we got lucky vs if we got the physics right?
        fcst_gfs = pos.get("forecast_high_f_gfs")
        fcst_ecmwf = pos.get("forecast_high_f_ecmwf")
        _forecast_error_f = None
        if observed_high_f is not None and (fcst_gfs is not None or fcst_ecmwf is not None):
            # Simple ensemble average (equal-weight of whichever are present)
            fvals = [v for v in (fcst_gfs, fcst_ecmwf) if v is not None]
            if fvals:
                ensemble_mean_f = sum(fvals) / len(fvals)
                _forecast_error_f = round(ensemble_mean_f - observed_high_f, 3)
        db.settle_trade(
            pos["id"],
            outcome=outcome,
            pnl=trade_pnl,
            observed_high_f=observed_high_f,
            forecast_error_f=_forecast_error_f,
        )
        expected_ev = pos.get("expected_pnl")
        record: dict[str, Any] = {
            "id": pos["id"],
            "city": pos.get("city"),
            "target_date": target_date_str,
            "question": pos.get("question", ""),
            "outcome": "YES" if outcome == 1 else "NO",
            "pnl": trade_pnl,
            "expected_pnl": expected_ev,
            "residual": (
                round(trade_pnl - float(expected_ev), 4)
                if expected_ev is not None else None
            ),
            "source": source,
        }
        if observed_high_f is not None:
            record["observed_high_f"] = observed_high_f
        settled.append(record)

    db.close()
    total_pnl = sum(s["pnl"] for s in settled)
    total_expected = sum(
        float(s["expected_pnl"]) for s in settled if s.get("expected_pnl") is not None
    )
    _print_json({
        "settled": settled,
        "settled_count": len(settled),
        "total_pnl": round(total_pnl, 4),
        "total_expected_pnl": round(total_expected, 4),
        "total_residual": round(total_pnl - total_expected, 4),
        "errors": errors,
    })


def run_autotrade(
    *,
    mode: str = "paper",
    confirm_live: bool = False,
    state_path: str = "runtime/portfolio_state.json",
    env_file: str = ".env",
    max_open: int = 30,
    drawdown_brake_pct: float | None = None,
) -> dict[str, Any]:
    """Full autotrade cycle: settle → safety check → analyze → execute → notify.

    Returns a JSON-serializable summary of the cycle.
    """
    import sys

    from polymarket_strat.config import TelegramConfig
    from polymarket_strat.execution import LiveExecutor, PaperExecutor, order_to_dict
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
    from polymarket_strat.notifications.telegram import TelegramNotifier

    load_env_file(env_file)
    print(f"[autotrade] Starting cycle (mode={mode})...", file=sys.stderr)

    db = WeatherDatabase()
    constraints = TradingConstraints()
    # Resolve daily-drawdown brake: CLI override → constraints default.
    # constraints.max_daily_drawdown is 14% (Apr 19 2026), sized to
    # accommodate ~2.8 full-loss positions under the 5% per-position cap.
    if drawdown_brake_pct is None:
        drawdown_brake_pct = constraints.max_daily_drawdown
    cycle: dict[str, Any] = {"mode": mode, "drawdown_brake_pct": drawdown_brake_pct}

    # ------------------------------------------------------------------
    # Step 1: Settle any resolved trades (hourly Polymarket + IEM fallback)
    #
    # Run on EVERY open position regardless of target_date. Polymarket is
    # the authoritative source and resolves ~16-18:00 station-local on the
    # day itself; waiting for date-gates would delay settlement by up to
    # 15h (the UTC↔Seoul offset). IEM fallback only fires once the STATION'S
    # local day has ended + grace (see `_station_day_ended`), because
    # daily-high aggregation isn't final before then and the host's clock
    # (KST on EC2) doesn't line up with station-local midnight.
    # ------------------------------------------------------------------
    print("[autotrade] Step 1: Checking for resolved trades...", file=sys.stderr)
    from polymarket_strat.api import PolymarketPublicClient
    poly_client = PolymarketPublicClient()

    open_before = db.get_open_positions()
    settled: list[dict[str, Any]] = []

    for pos in open_before:
        try:
            target_date = date.fromisoformat(pos.get("target_date", ""))
        except (ValueError, TypeError):
            continue

        outcome: int | None = None
        observed_high_f: float | None = None
        source = "unknown"

        # Polymarket API first — hourly-safe, no wall-clock gate.
        outcome = _resolve_via_polymarket(pos, poly_client)
        if outcome is not None:
            source = "polymarket_api"
            trade_pnl = _pnl(
                outcome=outcome,
                notional=float(pos.get("notional", 0)),
                entry_price=float(pos.get("entry_price") or pos.get("market_prob") or 0),
                token_side=str(pos.get("token_side") or "YES"),
            )
        elif _station_day_ended(pos.get("city", ""), target_date):
            # IEM fallback only after the station-local day has ended + grace.
            iem = _settle_from_iem(pos)
            if iem is None:
                continue
            outcome = iem["outcome"]
            trade_pnl = iem["pnl"]
            observed_high_f = iem.get("observed_high_f")
            source = "iem_observation"
        else:
            continue  # unresolved + station day not yet done, wait for next tick

        # Apr 24 2026 — stamp observed + forecast_error on the trade row
        # regardless of which resolution path fired. Same logic as above.
        fcst_gfs = pos.get("forecast_high_f_gfs")
        fcst_ecmwf = pos.get("forecast_high_f_ecmwf")
        _forecast_error_f = None
        if observed_high_f is not None and (fcst_gfs is not None or fcst_ecmwf is not None):
            fvals = [v for v in (fcst_gfs, fcst_ecmwf) if v is not None]
            if fvals:
                ensemble_mean_f = sum(fvals) / len(fvals)
                _forecast_error_f = round(ensemble_mean_f - observed_high_f, 3)
        db.settle_trade(
            pos["id"],
            outcome=outcome,
            pnl=trade_pnl,
            observed_high_f=observed_high_f,
            forecast_error_f=_forecast_error_f,
        )
        settled.append({
            "id": pos["id"],
            "city": pos.get("city"),
            "question": pos.get("question", "")[:50],
            "outcome": "YES" if outcome == 1 else "NO",
            "pnl": trade_pnl,
            "observed_high_f": observed_high_f,
            "source": source,
            "expected_pnl": pos.get("expected_pnl"),
        })

    settled_pnl = sum(s["pnl"] for s in settled)
    # Expected PnL sum lets us tell variance from miscalibration: if
    # realised << expected over N ≥ 30 trades, model is overclaiming edge.
    settled_expected = sum(
        float(s["expected_pnl"]) for s in settled if s.get("expected_pnl") is not None
    )
    cycle["settled_count"] = len(settled)
    cycle["settled_pnl"] = round(settled_pnl, 4)
    cycle["settled_expected_pnl"] = round(settled_expected, 4)
    cycle["settled_residual"] = round(settled_pnl - settled_expected, 4)
    cycle["settled"] = settled
    print(
        f"[autotrade]   Settled {len(settled)} trades, "
        f"realised ${settled_pnl:+,.2f} vs expected ${settled_expected:+,.2f} "
        f"(residual ${settled_pnl - settled_expected:+,.2f})",
        file=sys.stderr,
    )

    # ------------------------------------------------------------------
    # Step 1b: Rebalance — exit positions whose edge has collapsed (Apr 24)
    #
    # Runs BEFORE the scan so freed capital is reusable in the SAME cycle:
    #   settle (step 1) → rebalance exits → MTM snapshot → scan+execute
    # Paper-mode P&L is booked immediately via close_position_as_exit().
    # Live mode is Phase 3 scope — the function raises rather than placing
    # a real sell order, so this is safe for the EC2 paper cron.
    # ------------------------------------------------------------------
    print("[autotrade] Step 1b: Rebalancing open positions...", file=sys.stderr)
    try:
        rebalance_summary = run_rebalance(mode=mode, env_file=env_file)
    except NotImplementedError as exc:
        print(f"[autotrade]   rebalance skipped (live mode not wired): {exc}", file=sys.stderr)
        rebalance_summary = {"exits": [], "exit_count": 0, "exit_pnl_total": 0.0}
    except Exception as exc:
        # Never let rebalance failures block the main scan — a failed
        # forecast fetch on one city shouldn't prevent new trades.
        print(f"[autotrade]   rebalance errored: {exc}", file=sys.stderr)
        rebalance_summary = {"exits": [], "exit_count": 0, "exit_pnl_total": 0.0}
    cycle["rebalance"] = {
        "exit_count": rebalance_summary.get("exit_count", 0),
        "hold_count": rebalance_summary.get("hold_count", 0),
        "skip_count": rebalance_summary.get("skip_count", 0),
        "exit_pnl_total": rebalance_summary.get("exit_pnl_total", 0.0),
    }

    # ------------------------------------------------------------------
    # Step 1c: MTM snapshot — one market_prices row per surviving open
    # position. Populates the dashboard's unrealised-P&L panel without
    # requiring the read-only Streamlit process to hit the CLOB itself.
    # ------------------------------------------------------------------
    print("[autotrade] Step 1c: Snapshotting open-position prices...", file=sys.stderr)
    try:
        snapshot_count = _snapshot_open_position_prices(db, poly_client)
    except Exception as exc:
        print(f"[autotrade]   snapshot errored: {exc}", file=sys.stderr)
        snapshot_count = 0
    cycle["mtm_snapshot_count"] = snapshot_count

    # ------------------------------------------------------------------
    # Step 2: Safety gates
    # ------------------------------------------------------------------
    open_now = db.get_open_positions()
    cycle["open_positions"] = len(open_now)

    if len(open_now) >= max_open:
        cycle["skipped"] = f"max_open_positions ({max_open})"
        print(f"[autotrade] BRAKE: {len(open_now)} open positions >= {max_open}. Skipping execution.", file=sys.stderr)
        _autotrade_notify(cycle, env_file)
        db.close()
        _print_json(cycle)
        return cycle

    # Daily drawdown check: sum of all P&L settled today (host-local; this
    # gate is about operator-visible daily loss budget, not about when a
    # given market resolves, so reading the host clock is appropriate).
    all_trades = db.get_trades(limit=500)
    today_str = date.today().isoformat()
    today_pnl = sum(
        float(t.get("pnl") or 0)
        for t in all_trades
        if t.get("settled_at") and str(t["settled_at"])[:10] == today_str
    )
    brake_threshold = -drawdown_brake_pct * constraints.bankroll
    if today_pnl < brake_threshold:
        cycle["skipped"] = f"daily_drawdown (today P&L ${today_pnl:+,.2f} < ${brake_threshold:,.2f})"
        print(f"[autotrade] BRAKE: daily P&L ${today_pnl:+,.2f} < ${brake_threshold:,.2f}. Skipping execution.", file=sys.stderr)
        _autotrade_notify(cycle, env_file)
        db.close()
        _print_json(cycle)
        return cycle

    # ------------------------------------------------------------------
    # Step 3: Analyze weather brackets
    # ------------------------------------------------------------------
    print("[autotrade] Step 3: Analyzing weather brackets...", file=sys.stderr)
    portfolio_state = _load_portfolio_state(state_path, constraints)
    service = StrategyApplicationService(use_sample=False)
    analysis = service.analyze("weather_bracket", constraints=constraints, portfolio_state=portfolio_state)
    executable = [p for p in analysis.trade_plan if p.executable]
    print(f"[autotrade]   {len(analysis.signals)} signals, {len(executable)} executable trades.", file=sys.stderr)
    # Propagate strategy-layer telemetry so Telegram/CloudWatch can see WHY a
    # cycle produced zero executable trades (per-gate rejection histogram,
    # no-forecast / no-dists contract counts, HRRR long-lead drops). Without
    # this the Lambda cycle is a black box — only the end-state is visible.
    cycle["diagnostics"] = analysis.diagnostics or {}

    # ------------------------------------------------------------------
    # Step 3b: Tail-bracket NO strategy (Apr 25 2026 — Layer 3)
    # Adds late-entry low-prob NO bets when:
    #   - lead ≤ 12h
    #   - p_model_yes in calibrated tail bin (≤0.30 or ≥0.70)
    #   - market price has room (≥10c YES for NO trades)
    #   - live IEM observation confirms via nowcast
    #   - edge after fees ≥ 5c
    # Reuses contract scan + forecast cache from mainstream strategy.
    # See domain/weather/tail_strategy.py for gate logic.
    # ------------------------------------------------------------------
    try:
        tail_plans, tail_diag = _run_tail_strategy_pass(
            mainstream_analysis=analysis,
            constraints=constraints,
            portfolio_state=portfolio_state,
        )
        # Merge tail plans into executable list. Tag with category for
        # downstream telemetry / settlement / dashboard.
        if tail_plans:
            print(f"[autotrade]   tail strategy: +{len(tail_plans)} plans",
                  file=sys.stderr)
            executable.extend(tail_plans)
        cycle["tail_strategy"] = {
            "n_plans": len(tail_plans),
            "diagnostics": tail_diag,
        }
    except Exception as exc:
        # Never let tail-strategy errors break mainstream execution.
        print(f"[autotrade]   tail strategy ERROR: {exc!r}", file=sys.stderr)
        cycle["tail_strategy"] = {
            "n_plans": 0, "error": repr(exc),
        }

    # ------------------------------------------------------------------
    # Step 3c: Tail-NO strategy v2 (Apr 26 2026 — production)
    # Geometric edge-distance-based; fires at 24-72h leads on brackets
    # whose nearest edge sits 1-5°F outside the forecast. Empirical hit
    # rate from forecast_errors corpus (n=24,472). Position size $30-50
    # by EV tier. Plans tagged category="weather_tail_no" so rebalance
    # skips them (these are buy-and-hold to settlement).
    # See domain/weather/tail_no_strategy.py.
    # ------------------------------------------------------------------
    try:
        tail_no_plans, tail_no_diag = _run_tail_no_strategy_pass(
            constraints=constraints,
            portfolio_state=portfolio_state,
            db=db,
        )
        if tail_no_plans:
            print(f"[autotrade]   tail_no strategy: +{len(tail_no_plans)} plans",
                  file=sys.stderr)
            executable.extend(tail_no_plans)
        cycle["tail_no_strategy"] = {
            "n_plans": len(tail_no_plans),
            "diagnostics": tail_no_diag,
        }
    except Exception as exc:
        import traceback
        print(f"[autotrade]   tail_no strategy ERROR: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        cycle["tail_no_strategy"] = {
            "n_plans": 0, "error": repr(exc),
        }

    # ------------------------------------------------------------------
    # Step 4: Execute trades
    # ------------------------------------------------------------------
    if not executable:
        cycle["new_trade_count"] = 0
        cycle["skipped"] = cycle.get("skipped") or "no_executable_signals"
    else:
        print(f"[autotrade] Step 4: Executing {len(executable)} trades ({mode})...", file=sys.stderr)
        if mode == "live":
            if not confirm_live:
                cycle["skipped"] = "live_mode_requires_--confirm-live"
                _autotrade_notify(cycle, env_file)
                db.close()
                _print_json(cycle)
                return cycle
            executor = LiveExecutor(AccountConfig.from_env())
        else:
            executor = PaperExecutor()

        # Prevent re-booking the same token_id on every cron tick.
        # Mirrors the guard in run_execute (CLAUDE.md §10 bug fix #7). Also
        # union the 6h rebalance-cooldown set so a token we just exited via
        # edge collapse isn't re-entered on the same cycle — `_run_scan`
        # already picks up new entry opportunities on sibling brackets via
        # the updated p_model, which is the intended add-to-new-bracket path.
        already_open_token_ids: set[str] = {
            pos["token_id"] for pos in db.get_open_positions() if pos.get("token_id")
        }
        cooldown_tokens = db.get_cooldown_tokens()
        already_open_token_ids |= cooldown_tokens
        cycle["cooldown_token_count"] = len(cooldown_tokens)

        executed: list[dict[str, Any]] = []
        skipped_duplicates = 0
        for item in executable:
            if item.token_id and item.token_id in already_open_token_ids:
                skipped_duplicates += 1
                continue
            order = executor.execute_market_buy(
                market=item.market,
                outcome=item.outcome,
                token_id=item.token_id,
                amount=item.target_notional,
                reference_price=item.best_ask,
            )
            order_dict = order_to_dict(order)
            # Mark as open so later items in the same cycle aren't re-booked
            if item.token_id:
                already_open_token_ids.add(item.token_id)

            # Persist to weather DB
            meta = item.metadata
            raw_date = meta.get("target_date")
            today_host = date.today()  # fallback only — meta usually has a real target_date
            try:
                tdate = date.fromisoformat(raw_date) if raw_date else today_host
            except (ValueError, TypeError):
                tdate = today_host

            expected_ev = _expected_pnl(
                model_prob=float(meta.get("model_prob", 0)),
                entry_price=float(item.best_ask),
                notional=float(item.target_notional),
            )
            db.save_trade(
                city=meta.get("city", ""),
                target_date=tdate,
                bracket_lower_f=float(meta.get("bracket_lower_f", 0)),
                bracket_upper_f=float(meta.get("bracket_upper_f", 0)),
                model_prob=float(meta.get("model_prob", 0)),
                market_prob=float(item.reference_price),
                edge=float(item.expected_value),
                kelly_fraction=float(meta.get("kelly_fraction", 0)),
                notional=float(item.target_notional),
                entry_price=float(item.best_ask),
                side=item.side,
                mode=mode,
                market_id=item.market,
                token_id=item.token_id,
                question=item.question,
                regime=meta.get("regime"),
                expected_pnl=expected_ev,
                # See run_execute — rebalance baseline columns.
                entry_edge=float(meta.get("edge_after_fees", item.expected_value)),
                forecast_content_hash=str(meta.get("forecast_content_hash") or ""),
            )

            # Update portfolio state
            portfolio_state.cash = max(portfolio_state.cash - item.target_notional, 0)
            portfolio_state.open_positions[item.market] = (
                portfolio_state.open_positions.get(item.market, 0) + item.target_notional
            )
            order_dict["city"] = meta.get("city")
            order_dict["outcome"] = item.outcome
            order_dict["expected_pnl"] = expected_ev
            executed.append(order_dict)

        portfolio_state.save(state_path)
        cycle["new_trade_count"] = len(executed)
        cycle["new_trades"] = executed
        cycle["skipped_duplicates"] = skipped_duplicates

    # Cumulative P&L
    all_settled = [t for t in db.get_trades(limit=5000) if t.get("pnl") is not None]
    cycle["cumulative_pnl"] = round(sum(float(t["pnl"]) for t in all_settled), 4)
    cycle["open_positions"] = len(db.get_open_positions())

    db.close()

    # ------------------------------------------------------------------
    # Step 5: Telegram notification
    # ------------------------------------------------------------------
    _autotrade_notify(cycle, env_file)

    print(f"[autotrade] Cycle complete. {cycle.get('new_trade_count', 0)} new trades, cumulative P&L: ${cycle.get('cumulative_pnl', 0):+,.2f}", file=sys.stderr)
    _print_json(cycle)
    return cycle


def _autotrade_notify(cycle: dict[str, Any], env_file: str) -> None:
    """Send Telegram summary. Silently skip if no Telegram config."""
    try:
        from polymarket_strat.config import TelegramConfig
        from polymarket_strat.notifications.telegram import TelegramNotifier

        load_env_file(env_file)
        config = TelegramConfig.from_env()
        notifier = TelegramNotifier(config)

        # Settlement report
        if cycle.get("settled"):
            notifier.send_settlement_report(
                settled=cycle["settled"],
                total_pnl=cycle.get("settled_pnl", 0),
            )

        # Rebalance exits (if any). run_rebalance already sends its own
        # notification when invoked as a standalone CLI, but when called
        # inline from run_autotrade the summary is in cycle['rebalance']
        # and we handle the Telegram path here for symmetry. Rebalance
        # exits already have Telegram sent inline from run_rebalance, so
        # skip here to avoid duplication — the summary line still surfaces
        # in send_autotrade_summary.

        # New trade notification
        if cycle.get("new_trades"):
            notifier.send_trade_executed(trades=cycle["new_trades"])

        # Consolidated summary
        notifier.send_autotrade_summary(cycle=cycle)
    except Exception as exc:
        import sys
        print(f"[autotrade] Telegram notification failed: {exc}", file=sys.stderr)


def run_backtest(strategy_name: str, *, use_sample: bool) -> None:
    service = StrategyApplicationService(use_sample=use_sample)
    _print_json({"backtests": [asdict(result) for result in service.backtest(strategy_name)]})


def run_doctor(env_file: str, state_path: str) -> None:
    constraints = TradingConstraints()
    state = _load_portfolio_state(state_path, constraints)
    env_path = Path(env_file)
    py_clob_client_installed = importlib.util.find_spec("py_clob_client") is not None
    env_text = env_path.read_text() if env_path.exists() else ""
    env_values: dict[str, str] = {}
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env_values[key.strip()] = value.strip()
    private_key_value = env_values.get("POLYMARKET_PRIVATE_KEY", "")
    funder_value = env_values.get("POLYMARKET_FUNDER", "")
    payload = {
        "checks": {
            "env_file_exists": env_path.exists(),
            "state_file_exists": Path(state_path).exists(),
            "py_clob_client_installed": py_clob_client_installed,
            "has_private_key": bool(private_key_value) and "YOUR_PRIVATE_KEY" not in private_key_value,
            "has_funder": bool(funder_value) and "YOUR_FUNDER_ADDRESS" not in funder_value,
        },
        "available_strategies": StrategyApplicationService(use_sample=True).available_strategies(),
        "real_data_status": StrategyApplicationService(use_sample=True).real_data_status(),
        "risk_config": asdict(constraints),
        "portfolio_state": asdict(state),
    }
    _print_json(payload)


def run_report(*, use_sample: bool, state_path: str, output_path: str) -> None:
    path = write_strategy_report(output_path, use_sample=use_sample, state_path=state_path)
    _print_json({"report_path": str(path), "use_sample": use_sample, "state_path": state_path})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket multi-strategy trading toolkit.")
    parser.add_argument("--env-file", default=".env", help="Path to local environment file.")
    parser.add_argument("--state-file", default="runtime/portfolio_state.json", help="Path to persisted portfolio state.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze a strategy and build its trade plan.")
    analyze.add_argument("--strategy", choices=["whale_following", "mispricing", "weather_bracket"], default="whale_following")
    analyze.add_argument("--sample", action="store_true", help="Use bundled sample data.")

    execute = subparsers.add_parser("execute", help="Execute the currently executable trade plan.")
    execute.add_argument("--strategy", choices=["whale_following", "mispricing", "weather_bracket"], default="whale_following")
    execute.add_argument("--sample", action="store_true", help="Use bundled sample data.")
    execute.add_argument("--mode", choices=["paper", "live"], default="paper")
    execute.add_argument("--confirm-live", action="store_true")

    backtest = subparsers.add_parser("backtest", help="Backtest one strategy or all strategies.")
    backtest.add_argument("--strategy", choices=["whale_following", "mispricing", "weather_bracket", "all"], default="all")
    backtest.add_argument("--sample", action="store_true", help="Use bundled sample data.")

    doctor = subparsers.add_parser("doctor", help="Check local readiness for strategy execution.")
    doctor.add_argument("--sample", action="store_true", help="Use bundled sample configuration.")

    report = subparsers.add_parser("report", help="Generate a visual HTML report for analyses and backtests.")
    report.add_argument("--sample", action="store_true", help="Use bundled sample data.")
    report.add_argument("--output", default="reports/strategy_report.html", help="Where to write the HTML report.")

    monitor = subparsers.add_parser("monitor", help="Poll ELITE whales and send Telegram alerts for large trades.")
    monitor.add_argument("--min-size", type=float, default=1000.0, help="Minimum trade notional ($) to trigger alert.")
    monitor.add_argument("--monitor-state", default="runtime/whale_monitor_state.json", help="Path to monitor state file.")

    insider = subparsers.add_parser(
        "insider",
        help="Scan political/economic markets for insider-anomaly signals (volume spikes, new wallets, coordinated buys, price impact).",
    )
    insider.add_argument(
        "--min-score",
        type=float,
        default=0.15,
        help="Minimum combined suspicion score (0–1+) to trigger a Telegram alert.",
    )
    insider.add_argument(
        "--insider-state",
        default="runtime/insider_monitor_state.json",
        help="Path to insider monitor state file.",
    )

    wcal = subparsers.add_parser("weather-calibrate", help="Run offline calibration for weather error distributions.")
    wcal.add_argument(
        "--cities",
        default="all",
        help="Comma-separated city keys (no spaces), e.g. london,hong_kong,chicago — or 'all'.",
    )
    wcal.add_argument("--lookback-days", type=int, default=365, help="Days of history to calibrate from.")

    subparsers.add_parser("positions", help="Show all open (unsettled) paper/live weather trades.")

    settle = subparsers.add_parser("settle", help="Settle resolved weather trades and compute P&L.")
    settle.add_argument("--trade-id", type=int, default=None, help="Settle a specific trade by ID.")
    settle.add_argument("--auto", action="store_true", help="Auto-settle all open positions whose target date has passed.")

    auto = subparsers.add_parser("autotrade", help="Full automated cycle: settle → analyze → execute → notify.")
    auto.add_argument("--mode", choices=["paper", "live"], default="paper", help="Trading mode.")
    auto.add_argument("--confirm-live", action="store_true", help="Required safety flag for live execution.")
    auto.add_argument("--max-open", type=int, default=30, help="Max open positions before skipping execution.")

    rebal = subparsers.add_parser(
        "rebalance",
        help=(
            "Exit positions whose edge has collapsed since entry. Dual "
            "threshold: -0.15 if forecast unchanged, -0.10 if forecast "
            "refreshed. Inserts a 6h token cooldown on each exit."
        ),
    )
    rebal.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode. Live-mode exits are Phase 3 scope and raise.",
    )
    rebal.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required safety flag for live execution (Phase 3 only).",
    )
    rebal.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which positions WOULD exit without writing to the DB.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    load_env_file(Path(args.env_file))

    if args.command == "analyze":
        run_analyze(args.strategy, use_sample=args.sample, state_path=args.state_file)
        return
    if args.command == "execute":
        run_execute(
            args.strategy,
            use_sample=args.sample,
            mode=args.mode,
            state_path=args.state_file,
            confirm_live=args.confirm_live,
        )
        return
    if args.command == "backtest":
        run_backtest(args.strategy, use_sample=args.sample)
        return
    if args.command == "doctor":
        run_doctor(args.env_file, args.state_file)
        return
    if args.command == "report":
        run_report(use_sample=args.sample, state_path=args.state_file, output_path=args.output)
        return
    if args.command == "monitor":
        from polymarket_strat.monitor import run_monitor

        result = run_monitor(
            min_size=args.min_size,
            state_path=args.monitor_state,
            env_file=args.env_file,
        )
        _print_json(result)
        return
    if args.command == "weather-calibrate":
        from polymarket_strat.application.service import StrategyApplicationService

        svc = StrategyApplicationService(use_sample=False)
        strategy = svc.create_strategy("weather_bracket")
        cities = None if args.cities == "all" else args.cities.split(",")
        result = strategy.calibrate(cities=cities, lookback_days=args.lookback_days)
        _print_json(result)
        return
    if args.command == "insider":
        from polymarket_strat.monitor import run_insider_monitor

        result = run_insider_monitor(
            min_score=args.min_score,
            state_path=args.insider_state,
            env_file=args.env_file,
        )
        _print_json(result)
        return
    if args.command == "positions":
        run_positions()
        return
    if args.command == "settle":
        run_settle(args.trade_id, auto=args.auto)
        return
    if args.command == "autotrade":
        run_autotrade(
            mode=args.mode,
            confirm_live=args.confirm_live,
            state_path=args.state_file,
            env_file=args.env_file,
            max_open=args.max_open,
        )
        return
    if args.command == "rebalance":
        result = run_rebalance(
            mode=args.mode,
            confirm_live=args.confirm_live,
            env_file=args.env_file,
            dry_run=args.dry_run,
        )
        _print_json(result)
        return
    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
