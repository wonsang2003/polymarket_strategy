from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from typing import Any

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import BacktestResult, BacktestTrade, StrategyAnalysis, StrategySignal, TradePlan
from polymarket_strat.risk import PortfolioRiskManager
from polymarket_strat.sample_data import build_sample_whales


def _float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None
    return None


def _side_is_buy(trade: dict[str, Any]) -> bool:
    side = str(trade.get("side", "")).upper()
    if side:
        return side == "BUY"
    trade_type = str(trade.get("type", "")).lower()
    return "buy" in trade_type


def _parse_stringified_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return []
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return parsed
    return []


def _market_outcome_token_map(market: dict[str, Any]) -> dict[str, str]:
    outcomes = [str(item) for item in _parse_stringified_list(market.get("outcomes"))]
    token_ids = [str(item) for item in _parse_stringified_list(market.get("clobTokenIds"))]
    return {
        outcome: token_id
        for outcome, token_id in zip(outcomes, token_ids, strict=False)
        if outcome and token_id
    }


@dataclass(slots=True)
class WhaleSelectionConfig:
    market_limit: int = 30
    holders_per_market: int = 20
    min_closed_positions: int = 6
    min_realized_pnl: float = 250.0
    min_win_rate: float = 0.53
    min_avg_roi: float = 0.03
    max_candidates: int = 250


@dataclass(slots=True)
class WhaleSignalConfig:
    lookback_hours: int = 48
    min_whale_count: int = 2
    min_signal_score: float = 0.2
    max_market_exposure: float = 0.15
    base_position_size: float = 0.03


@dataclass(slots=True)
class WhaleScore:
    wallet: str
    realized_pnl: float
    total_cost: float
    win_rate: float
    avg_roi: float
    closed_positions: int
    unique_markets: int
    recency_score: float
    concentration_score: float
    score: float
    # True P&L fields — includes unrealized losses from open positions
    unrealized_pnl: float = 0.0
    true_net_pnl: float = 0.0
    true_win_rate: float = 0.0
    open_position_count: int = 0
    open_cost_basis: float = 0.0
    open_market_value: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class WhaleFollowingStrategy:
    name = "whale_following"

    def __init__(
        self,
        client: PolymarketPublicClient,
        *,
        selection: WhaleSelectionConfig | None = None,
        signal_config: WhaleSignalConfig | None = None,
    ):
        self.client = client
        self.selection = selection or WhaleSelectionConfig()
        self.signal_config = signal_config or WhaleSignalConfig()

    def discover_candidate_wallets(self) -> list[str]:
        markets = self.client.get_markets(limit=self.selection.market_limit, active=True)
        counter: Counter[str] = Counter()
        for market in markets:
            market_id = str(market.get("conditionId") or market.get("id") or "")
            if not market_id:
                continue
            raw_holders = self.client.get_market_holders(market_id, limit=self.selection.holders_per_market)
            # The data-api /holders endpoint returns [{token, holders: [...]}, ...]
            # Flatten the nested structure into individual holder dicts.
            holders: list[dict[str, Any]] = []
            for item in raw_holders:
                if isinstance(item, dict) and "holders" in item:
                    holders.extend(item["holders"])
                else:
                    holders.append(item)
            for holder in holders:
                wallet = str(
                    holder.get("proxyWallet")
                    or holder.get("user")
                    or holder.get("owner")
                    or ""
                )
                if wallet:
                    counter[wallet.lower()] += 1
        return [wallet for wallet, _ in counter.most_common(self.selection.max_candidates)]

    def score_wallet(self, wallet: str) -> WhaleScore | None:
        closed_positions = self.client.get_closed_positions(wallet)
        if len(closed_positions) < self.selection.min_closed_positions:
            return None

        # --- Realized metrics (from closed positions) ---
        realized_values = [_float(position.get("realizedPnl") or position.get("pnl")) for position in closed_positions]
        cost_values = [
            max(
                _float(position.get("totalBought")),
                _float(position.get("costBasis")),
                _float(position.get("averagePrice")) * _float(position.get("size"), 1.0),
                1.0,
            )
            for position in closed_positions
        ]
        rois = [realized / cost for realized, cost in zip(realized_values, cost_values, strict=True)]
        realized_pnl = sum(realized_values)
        closed_wins = sum(1 for value in realized_values if value > 0)
        closed_losses = len(realized_values) - closed_wins

        # --- Unrealized metrics (from open positions) ---
        open_positions = self.client.get_positions(wallet, limit=500)
        open_cost = 0.0
        open_value = 0.0
        open_wins = 0
        open_losses = 0
        for pos in open_positions:
            cost = _float(pos.get("totalBought") or pos.get("costBasis"), 0)
            size = _float(pos.get("size") or pos.get("amount"), 0)
            cur_price = _float(pos.get("curPrice") or pos.get("currentPrice"), 0)
            value = size * cur_price
            open_cost += cost
            open_value += value
            if value >= cost:
                open_wins += 1
            else:
                open_losses += 1

        unrealized_pnl = open_value - open_cost
        true_net_pnl = realized_pnl + unrealized_pnl

        # --- True win rate: count ALL positions (closed + open) ---
        total_positions = len(closed_positions) + len(open_positions)
        total_wins = closed_wins + open_wins
        true_win_rate = total_wins / total_positions if total_positions > 0 else 0.0
        # Closed-only win rate (for reference, but NOT used for scoring)
        closed_win_rate = closed_wins / len(closed_positions) if closed_positions else 0.0

        avg_roi = sum(rois) / len(rois)
        unique_markets = len(
            {
                str(position.get("conditionId") or position.get("market") or position.get("marketSlug") or "")
                for position in closed_positions
            }
            - {""}
        )
        latest_close = max((_parse_timestamp(position.get("endDate") or position.get("closedAt")) for position in closed_positions), default=None)
        recency_score = 0.3 if latest_close is None else math.exp(-max((datetime.now(tz=UTC) - latest_close).days, 0) / 30)
        concentration_score = min(unique_markets / max(len(closed_positions), 1), 1.0)

        # --- Score uses TRUE net PnL and TRUE win rate ---
        score = (
            0.35 * min(max(true_net_pnl, 0) / 5000.0, 1.0)
            + 0.20 * true_win_rate
            + 0.20 * min(max(avg_roi, 0) / 0.15, 1.0)
            + 0.15 * recency_score
            + 0.10 * concentration_score
        )

        # Gate on TRUE profitability — reject wallets that are net negative
        if true_net_pnl < self.selection.min_realized_pnl:
            return None
        if true_win_rate < self.selection.min_win_rate:
            return None
        if avg_roi < self.selection.min_avg_roi:
            return None

        return WhaleScore(
            wallet=wallet,
            realized_pnl=realized_pnl,
            total_cost=sum(cost_values),
            win_rate=true_win_rate,
            avg_roi=avg_roi,
            closed_positions=len(closed_positions),
            unique_markets=unique_markets,
            recency_score=recency_score,
            concentration_score=concentration_score,
            score=score,
            unrealized_pnl=unrealized_pnl,
            true_net_pnl=true_net_pnl,
            true_win_rate=true_win_rate,
            open_position_count=len(open_positions),
            open_cost_basis=open_cost,
            open_market_value=open_value,
            metadata={
                "sample_size": len(closed_positions),
                "open_positions": len(open_positions),
                "closed_win_rate": round(closed_win_rate, 3),
                "true_win_rate": round(true_win_rate, 3),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "true_net_pnl": round(true_net_pnl, 2),
            },
        )

    def rank_whales(self, candidate_wallets: list[str] | None = None) -> list[WhaleScore]:
        wallets = candidate_wallets or self.discover_candidate_wallets()
        whales = [score for score in (self.score_wallet(wallet) for wallet in wallets) if score is not None]
        whales.sort(key=lambda item: item.score, reverse=True)
        return whales

    def generate_copy_signals(self, whales: list[WhaleScore]) -> list[StrategySignal]:
        since = datetime.now(tz=UTC) - timedelta(hours=self.signal_config.lookback_hours)
        trade_buckets: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "weighted_price": 0.0,
                "weight": 0.0,
                "wallets": set(),
                "latest_time": None,
                "score": 0.0,
                "title": "",
                "token_id": "",
            }
        )

        for whale in whales:
            trades = self.client.get_trades(user=whale.wallet, limit=200)
            for trade in trades:
                trade_time = _parse_timestamp(trade.get("timestamp") or trade.get("createdAt"))
                if trade_time is None or trade_time < since or not _side_is_buy(trade):
                    continue
                market = str(trade.get("conditionId") or trade.get("market") or trade.get("marketSlug") or "")
                outcome = str(trade.get("outcome") or "")
                if not market or not outcome:
                    continue
                price = _float(trade.get("price"), 0.5)
                size = max(_float(trade.get("size") or trade.get("amount"), 0.0), 0.0)
                key = (market, outcome, "BUY")
                bucket = trade_buckets[key]
                whale_weight = whale.score * max(size, 1.0)
                bucket["weighted_price"] += price * whale_weight
                bucket["weight"] += whale_weight
                bucket["wallets"].add(whale.wallet)
                bucket["score"] += whale.score * max(size, 1.0) / 100.0
                if bucket["latest_time"] is None or trade_time > bucket["latest_time"]:
                    bucket["latest_time"] = trade_time
                # Capture title and token_id directly from the trade data
                if not bucket["title"]:
                    bucket["title"] = str(trade.get("title") or trade.get("question") or "")
                if not bucket["token_id"]:
                    bucket["token_id"] = str(trade.get("asset") or trade.get("tokenId") or "")

        signals: list[StrategySignal] = []
        for (market, outcome, side), bucket in trade_buckets.items():
            whale_wallets = sorted(bucket["wallets"])
            if len(whale_wallets) < self.signal_config.min_whale_count or bucket["weight"] <= 0:
                continue
            score = bucket["score"]
            if score < self.signal_config.min_signal_score:
                continue
            question = bucket["title"]
            signals.append(
                StrategySignal(
                    market=market,
                    question=question,
                    category="other",
                    outcome=outcome,
                    side=side,
                    signal_score=score,
                    reference_price=bucket["weighted_price"] / bucket["weight"],
                    metadata={
                        "whale_wallets": whale_wallets,
                        "latest_trade_time": bucket["latest_time"],
                        "token_id": bucket["token_id"],
                        "suggested_allocation": min(self.signal_config.base_position_size * score, self.signal_config.max_market_exposure),
                    },
                )
            )
        signals.sort(key=lambda item: item.signal_score, reverse=True)
        return signals

    def validate_whale_longevity(
        self,
        whales: list[WhaleScore],
        *,
        min_tier: str = "ELITE",
    ) -> tuple[list[WhaleScore], list[dict[str, Any]]]:
        """Filter whales by longevity tier. Returns (passing_whales, all_profiles).

        Pulls the full closed-position history for every whale, builds a
        performance profile, and keeps only those whose ``longevity_tier``
        meets or exceeds *min_tier*.

        Tier ranking (high → low): ELITE > STRONG > MODERATE > UNPROVEN
        """
        tier_rank = {"ELITE": 4, "STRONG": 3, "MODERATE": 2, "UNPROVEN": 1}
        threshold = tier_rank.get(min_tier, 4)
        passing: list[WhaleScore] = []
        profiles: list[dict[str, Any]] = []

        for idx, whale in enumerate(whales, 1):
            print(
                f"[longevity]   validating {idx}/{len(whales)}: {whale.wallet[:10]}...",
                file=sys.stderr,
            )
            raw_positions = self.client.get_closed_positions(whale.wallet, limit=500)
            dated: list[tuple[datetime | None, dict[str, Any]]] = []
            for pos in raw_positions:
                ts = _parse_timestamp(
                    pos.get("closedAt") or pos.get("endDate") or pos.get("timestamp")
                )
                dated.append((ts, pos))
            dated.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=UTC))

            dated_returns: list[tuple[datetime | None, float]] = []
            for ts, pos in dated:
                cost = max(
                    _float(pos.get("totalBought")),
                    _float(pos.get("costBasis")),
                    _float(pos.get("averagePrice")) * max(_float(pos.get("size"), 1.0), 1.0),
                    1.0,
                )
                realized = _float(pos.get("realizedPnl") or pos.get("pnl"))
                dated_returns.append((ts, realized / cost))

            profile = _build_whale_profile(whale, dated_returns)
            profiles.append(profile)
            tier_ok = tier_rank.get(profile["longevity_tier"], 0) >= threshold
            recently_active = profile["is_recently_active"]
            truly_profitable = whale.true_net_pnl > 0

            if tier_ok and recently_active and truly_profitable:
                passing.append(whale)
            elif tier_ok and not truly_profitable:
                print(
                    f"[longevity]     SKIPPED {whale.wallet[:10]}... — "
                    f"tier={profile['longevity_tier']} but TRUE NET negative "
                    f"(realized={whale.realized_pnl:+,.0f}, unrealized={whale.unrealized_pnl:+,.0f}, "
                    f"true_net={whale.true_net_pnl:+,.0f})",
                    file=sys.stderr,
                )
            elif tier_ok and not recently_active:
                print(
                    f"[longevity]     SKIPPED {whale.wallet[:10]}... — "
                    f"tier={profile['longevity_tier']} but dormant "
                    f"({profile['days_since_last_position']}d since last position, "
                    f"cutoff=90d)",
                    file=sys.stderr,
                )

        profitable_count = sum(1 for w in whales if w.true_net_pnl > 0)
        active_count = sum(1 for p in profiles if p["is_recently_active"])
        print(
            f"[longevity] {len(passing)}/{len(whales)} whales passed "
            f"{min_tier} tier + recency + true profitability filter "
            f"({profitable_count} truly profitable, {active_count} recently active, "
            f"{len(whales) - active_count} dormant).",
            file=sys.stderr,
        )
        return passing, profiles

    def analyze(self, *, constraints: TradingConstraints, portfolio_state: PortfolioState) -> StrategyAnalysis:
        whales = self.rank_whales()

        # --- ELITE-only gate: validate longevity before generating signals ---
        elite_whales, longevity_profiles = self.validate_whale_longevity(whales, min_tier="ELITE")
        if not elite_whales:
            print("[analyze] WARNING: No ELITE whales. Falling back to STRONG tier.", file=sys.stderr)
            elite_whales = [
                w for w, p in zip(whales, longevity_profiles)
                if p["longevity_tier"] in ("ELITE", "STRONG")
            ]
        if not elite_whales:
            print("[analyze] WARNING: No STRONG whales either. Using all scored whales.", file=sys.stderr)
            elite_whales = whales

        signals = self.generate_copy_signals(elite_whales)
        risk_manager = PortfolioRiskManager(constraints, portfolio_state)
        whale_lookup = {whale.wallet: whale for whale in elite_whales}
        plans: list[TradePlan] = []

        for signal in signals:
            whale_wallets = list(signal.metadata.get("whale_wallets", []))
            # Token ID is captured directly from the trade data during signal generation
            token_id = str(signal.metadata.get("token_id", ""))
            if not token_id:
                continue
            try:
                book = self.client.get_orderbook(token_id)
            except Exception:
                print(f"[analyze] Skipping signal — orderbook unavailable for {signal.question[:40]}", file=sys.stderr)
                continue
            bids = list(book.get("bids") or [])
            asks = list(book.get("asks") or [])
            best_bid = _float(bids[0].get("price")) if bids else 0.0
            best_ask = _float(asks[0].get("price")) if asks else 1.0
            top_bid_size = _float(bids[0].get("size")) if bids else 0.0
            top_ask_size = _float(asks[0].get("size")) if asks else 0.0
            spread = max(best_ask - best_bid, 0.0)
            whale_metrics = self._consensus_metrics(whale_wallets, whale_lookup)
            decision = risk_manager.evaluate_trade(
                market=signal.market,
                question=signal.question,
                signal_score=signal.signal_score,
                whale_wallets=whale_wallets,
                whale_metrics=whale_metrics,
                best_ask=best_ask,
                best_bid=best_bid,
                top_ask_size=top_ask_size,
            )
            executable = decision.approved and best_ask <= constraints.max_entry_price and spread <= constraints.max_spread
            if executable:
                risk_manager.register_fill(market=signal.market, category=decision.category, notional=decision.target_notional)
            plans.append(
                TradePlan(
                    strategy_name=self.name,
                    market=signal.market,
                    question=signal.question,
                    category=decision.category,
                    outcome=signal.outcome,
                    token_id=token_id,
                    side=signal.side,
                    signal_score=signal.signal_score,
                    target_notional=decision.target_notional,
                    reference_price=signal.reference_price,
                    best_ask=best_ask,
                    best_bid=best_bid,
                    spread=spread,
                    top_ask_size=top_ask_size,
                    top_bid_size=top_bid_size,
                    risk_score=decision.risk_score,
                    expected_value=max(signal.signal_score / 10.0, 0.0),
                    executable=executable,
                    rationale=decision.reasons,
                    metadata={"whale_wallets": whale_wallets, "whale_metrics": whale_metrics},
                    risk_metrics=decision.metrics,
                )
            )
        plans.sort(key=lambda item: (item.executable, -item.risk_score, item.signal_score), reverse=True)
        return StrategyAnalysis(
            strategy_name=self.name,
            signals=signals,
            trade_plan=plans,
            diagnostics={
                "elite_whales": [asdict(w) for w in elite_whales[:10]],
                "longevity_profiles": longevity_profiles,
                "total_candidates_before_filter": len(whales),
                "elite_count": len(elite_whales),
            },
        )

    def backtest(self) -> BacktestResult:
        print("[backtest] Discovering candidate wallets from live API...", file=sys.stderr)
        backtest_selection = WhaleSelectionConfig(
            market_limit=self.selection.market_limit,
            holders_per_market=self.selection.holders_per_market,
            min_closed_positions=self.selection.min_closed_positions,
            min_realized_pnl=self.selection.min_realized_pnl,
            min_win_rate=self.selection.min_win_rate,
            min_avg_roi=self.selection.min_avg_roi,
            max_candidates=60,
        )
        original_selection = self.selection
        self.selection = backtest_selection
        candidates = self.discover_candidate_wallets()
        print(f"[backtest] Scoring {len(candidates)} candidate wallets...", file=sys.stderr)
        whales = self.rank_whales(candidates)
        self.selection = original_selection
        print(f"[backtest] {len(whales)} whales passed quality filters. Running longevity validation...", file=sys.stderr)

        # --- ELITE-only gate (same as analyze) ---
        elite_whales, all_profiles = self.validate_whale_longevity(whales, min_tier="ELITE")
        elite_wallets = {w.wallet for w in elite_whales}

        # Graceful fallback: ELITE → STRONG → all scored whales
        if not elite_whales:
            print("[backtest] WARNING: No ELITE whales. Falling back to STRONG tier.", file=sys.stderr)
            elite_whales = [
                w for w, p in zip(whales, all_profiles)
                if p["longevity_tier"] in ("ELITE", "STRONG")
            ]
            elite_wallets = {w.wallet for w in elite_whales}
        if not elite_whales:
            print("[backtest] WARNING: No STRONG whales either. Using all scored whales for comparison.", file=sys.stderr)
            elite_whales = whales
            elite_wallets = {w.wallet for w in elite_whales}

        # Reconstruct trades only for elite whales (positions already fetched during validation)
        all_trades: list[BacktestTrade] = []
        elite_profiles = [p for p in all_profiles if p["wallet"] in elite_wallets]
        for whale in elite_whales:
            raw_positions = self.client.get_closed_positions(whale.wallet, limit=500)
            dated: list[tuple[datetime | None, dict[str, Any]]] = []
            for pos in raw_positions:
                ts = _parse_timestamp(pos.get("closedAt") or pos.get("endDate") or pos.get("timestamp"))
                dated.append((ts, pos))
            dated.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=UTC))

            for ts, pos in dated:
                cost = max(
                    _float(pos.get("totalBought")),
                    _float(pos.get("costBasis")),
                    _float(pos.get("averagePrice")) * max(_float(pos.get("size"), 1.0), 1.0),
                    1.0,
                )
                realized = _float(pos.get("realizedPnl") or pos.get("pnl"))
                ret = realized / cost
                avg_price = _float(pos.get("averagePrice"), 0.5)
                all_trades.append(
                    BacktestTrade(
                        market=str(pos.get("conditionId") or pos.get("market") or pos.get("marketSlug") or ""),
                        side="BUY",
                        entry_price=avg_price,
                        exit_price=avg_price * (1.0 + ret),
                        expected_value=whale.score,
                        pnl=realized,
                        return_pct=ret,
                        metadata={
                            "wallet": whale.wallet,
                            "closed_at": str(ts),
                            "cost_basis": round(cost, 2),
                        },
                    )
                )

        all_trades.sort(key=lambda t: str(t.metadata.get("closed_at", "")))

        overall_returns = [t.return_pct for t in all_trades]
        max_drawdown = _max_drawdown(overall_returns)
        overall_sharpe = _sharpe(overall_returns)
        mean_ret = statistics.fmean(overall_returns) if overall_returns else 0.0
        win_rate = sum(1 for r in overall_returns if r > 0) / len(overall_returns) if overall_returns else 0.0

        all_tier_counts = Counter(p["longevity_tier"] for p in all_profiles)
        elite_consistency = (
            statistics.fmean([p["consistency"] for p in elite_profiles])
            if elite_profiles
            else 0.0
        )
        elite_avg_months = (
            statistics.fmean([p["months_active"] for p in elite_profiles])
            if elite_profiles
            else 0.0
        )

        print(
            f"[backtest] Done. {len(elite_whales)} elite whales, {len(all_trades)} trades, "
            f"mean_return={mean_ret:.3f}, win_rate={win_rate:.2%}, "
            f"sharpe={overall_sharpe:.2f}, max_drawdown={max_drawdown:.2%}",
            file=sys.stderr,
        )

        return BacktestResult(
            strategy_name=self.name,
            trade_count=len(all_trades),
            mean_return=mean_ret,
            median_return=statistics.median(overall_returns) if overall_returns else 0.0,
            win_rate=win_rate,
            max_drawdown=max_drawdown,
            expected_value=mean_ret,
            calibration_error=None,
            passed=mean_ret > 0 and max_drawdown < 0.25 and overall_sharpe > 0.5,
            diagnostics={
                "sharpe_ratio": round(overall_sharpe, 3),
                "elite_whales_used": len(elite_whales),
                "total_candidates_scored": len(whales),
                "longevity_tier_distribution": dict(all_tier_counts),
                "elite_avg_consistency": round(elite_consistency, 3),
                "elite_avg_months_active": round(elite_avg_months, 1),
                # Full per-whale longevity reports
                "elite_whale_profiles": elite_profiles,
                "all_whale_profiles": all_profiles,
                "top_whales_by_score": [asdict(w) for w in elite_whales[:5]],
            },
            trades=all_trades,
        )

    def _consensus_metrics(self, whale_wallets: list[str], whale_lookup: dict[str, WhaleScore]) -> dict[str, float]:
        whales = [whale_lookup[wallet] for wallet in whale_wallets if wallet in whale_lookup]
        if not whales:
            return {"win_rate": 0.0, "avg_roi": 0.0}
        return {
            "win_rate": sum(whale.win_rate for whale in whales) / len(whales),
            "avg_roi": sum(whale.avg_roi for whale in whales) / len(whales),
        }


def _monthly_returns(dated_returns: list[tuple[datetime | None, float]]) -> list[tuple[str, float]]:
    """Group returns by calendar month. Returns [(YYYY-MM, mean_return)]."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for ts, ret in dated_returns:
        if ts is None:
            continue
        buckets[ts.strftime("%Y-%m")].append(ret)
    return sorted((month, statistics.fmean(rets)) for month, rets in buckets.items())


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    std = statistics.stdev(returns)
    return (mean / std) if std > 0 else 0.0


def _consistency_score(monthly_rets: list[float]) -> float:
    """Returns 0–1. Higher = monthly returns are more stable (low variance relative to mean)."""
    if len(monthly_rets) < 2:
        return 0.5
    mean = statistics.fmean(monthly_rets)
    std = statistics.stdev(monthly_rets)
    if mean <= 0:
        return 0.0
    cv = std / abs(mean)
    return round(1.0 / (1.0 + cv), 4)


def _longevity_tier(months_active: int, win_rate: float, consistency: float) -> str:
    if months_active >= 6 and win_rate >= 0.60 and consistency >= 0.55:
        return "ELITE"
    if months_active >= 3 and win_rate >= 0.55 and consistency >= 0.40:
        return "STRONG"
    if months_active >= 2 and win_rate >= 0.50:
        return "MODERATE"
    return "UNPROVEN"


def _build_whale_profile(
    whale: "WhaleScore",
    dated_returns: list[tuple[datetime | None, float]],
    *,
    recency_cutoff_days: int = 90,
) -> dict[str, Any]:
    monthly = _monthly_returns(dated_returns)
    monthly_rets = [r for _, r in monthly]
    pos_rets = [r for _, r in dated_returns]
    sharpe = _sharpe(pos_rets)
    consistency = _consistency_score(monthly_rets)

    half = len(dated_returns) // 2
    early_wr = sum(1 for _, r in dated_returns[:half] if r > 0) / max(half, 1)
    recent_wr = sum(1 for _, r in dated_returns[half:] if r > 0) / max(len(dated_returns) - half, 1)

    max_loss_streak = streak = 0
    for _, r in dated_returns:
        if r < 0:
            streak += 1
            max_loss_streak = max(max_loss_streak, streak)
        else:
            streak = 0

    if recent_wr > early_wr + 0.03:
        trend = "improving"
    elif recent_wr < early_wr - 0.03:
        trend = "declining"
    else:
        trend = "stable"

    # Recency: when was their last closed position?
    timestamps = [ts for ts, _ in dated_returns if ts is not None]
    last_active = max(timestamps) if timestamps else None
    now = datetime.now(tz=UTC)
    days_since_last = (now - last_active).days if last_active else 9999
    is_recently_active = days_since_last <= recency_cutoff_days

    # Count positions closed in last 90 days
    cutoff = now - timedelta(days=recency_cutoff_days)
    recent_positions = sum(1 for ts, _ in dated_returns if ts is not None and ts >= cutoff)

    months_active = len(monthly)
    return {
        "wallet": whale.wallet,
        "score": round(whale.score, 4),
        "total_pnl": round(whale.realized_pnl, 2),
        "unrealized_pnl": round(whale.unrealized_pnl, 2),
        "true_net_pnl": round(whale.true_net_pnl, 2),
        "open_positions": whale.open_position_count,
        "open_cost_basis": round(whale.open_cost_basis, 2),
        "open_market_value": round(whale.open_market_value, 2),
        "win_rate": round(whale.win_rate, 3),
        "true_win_rate": round(whale.true_win_rate, 3),
        "avg_roi": round(whale.avg_roi, 3),
        "closed_positions": whale.closed_positions,
        "months_active": months_active,
        "monthly_returns": [round(r, 4) for r in monthly_rets],
        "monthly_labels": [label for label, _ in monthly],
        "sharpe": round(sharpe, 3),
        "consistency": consistency,
        "early_win_rate": round(early_wr, 3),
        "recent_win_rate": round(recent_wr, 3),
        "trend": trend,
        "max_consecutive_losses": max_loss_streak,
        "last_active_date": str(last_active.date()) if last_active else None,
        "days_since_last_position": days_since_last,
        "recent_positions_90d": recent_positions,
        "is_recently_active": is_recently_active,
        "longevity_tier": _longevity_tier(months_active, whale.win_rate, consistency),
    }


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    return max_dd
