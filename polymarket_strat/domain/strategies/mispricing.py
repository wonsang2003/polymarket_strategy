from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import BacktestResult, BacktestTrade, StrategyAnalysis, StrategySignal, TradePlan
from polymarket_strat.infrastructure.real_data import load_real_market_metadata, load_real_mispricing_backtest_rows
from polymarket_strat.risk import PortfolioRiskManager, clamp
from polymarket_strat.sample_data import build_sample_mispricing_calibration_history, build_sample_mispricing_markets


def _logit(probability: float) -> float:
    p = min(max(probability, 1e-6), 1 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


@dataclass(slots=True)
class MispricingConfig:
    edge_no_trade: float = 0.05
    edge_full_size: float = 0.10
    max_calibration_error: float = 0.18
    min_liquidity: float = 1500.0
    max_spread: float = 0.04
    small_position_ratio: float = 0.02
    full_position_ratio: float = 0.05
    profit_target_min: float = 0.05
    profit_target_max: float = 0.15


class MispricingProbabilityStrategy:
    name = "mispricing"

    def __init__(
        self,
        *,
        markets: list[dict[str, Any]] | None = None,
        calibration_history: list[dict[str, Any]] | None = None,
        config: MispricingConfig | None = None,
    ):
        self.markets = markets or build_sample_mispricing_markets()
        self.calibration_history = calibration_history or build_sample_mispricing_calibration_history()
        self.config = config or MispricingConfig()
        self._platt_a, self._platt_b = self._fit_platt_scaler(self.calibration_history)
        self._calibration_error = self._compute_calibration_error(self.calibration_history)

    def analyze(self, *, constraints: TradingConstraints, portfolio_state: PortfolioState) -> StrategyAnalysis:
        signals: list[StrategySignal] = []
        plans: list[TradePlan] = []
        risk_manager = PortfolioRiskManager(constraints, portfolio_state)
        calibrated_ok = self._calibration_error <= self.config.max_calibration_error

        for market in self.markets:
            true_probability = self.estimate_true_probability(market)
            market_probability = float(market["market_probability"])
            edge = true_probability - market_probability
            if abs(edge) < self.config.edge_no_trade:
                continue

            side = "BUY" if edge > 0 else "BUY"
            outcome = "YES" if edge > 0 else "NO"
            signal_score = abs(edge) * 20.0
            category = str(market.get("category", "other"))
            signal = StrategySignal(
                market=str(market["market_id"]),
                question=str(market["question"]),
                category=category,
                outcome=outcome,
                side=side,
                signal_score=signal_score,
                reference_price=market_probability if outcome == "YES" else (1.0 - market_probability),
                metadata={
                    "true_probability": true_probability,
                    "market_probability": market_probability,
                    "edge": edge,
                    "calibration_error": self._calibration_error,
                    "exit_target_probability": true_probability,
                },
            )
            signals.append(signal)

            expected_value = abs(edge)
            whale_metrics = {"win_rate": clamp(0.55 + expected_value, 0.0, 1.0), "avg_roi": clamp(expected_value, 0.0, 0.4)}
            decision = risk_manager.evaluate_trade(
                market=signal.market,
                question=signal.question,
                signal_score=signal.signal_score,
                whale_wallets=["mispricing-model", "calibrated-model"],
                whale_metrics=whale_metrics,
                best_ask=float(market["best_ask_yes"] if outcome == "YES" else market["best_ask_no"]),
                best_bid=float(market["best_bid_yes"] if outcome == "YES" else market["best_bid_no"]),
                top_ask_size=float(market["top_ask_liquidity"]),
            )

            rationale = list(decision.reasons)
            executable = decision.approved and calibrated_ok and float(market["liquidity_depth"]) >= self.config.min_liquidity
            if not calibrated_ok:
                rationale.append(f"calibration error {self._calibration_error:.3f} exceeds limit {self.config.max_calibration_error:.3f}")
            if float(market["liquidity_depth"]) < self.config.min_liquidity:
                executable = False
                rationale.append("liquidity depth below threshold")
            if float(market["spread"]) > self.config.max_spread:
                executable = False
                rationale.append("spread exceeds mispricing strategy threshold")

            target_ratio = self.config.small_position_ratio if abs(edge) < self.config.edge_full_size else self.config.full_position_ratio
            target_notional = min(decision.target_notional, constraints.bankroll * target_ratio)
            if target_notional < constraints.min_order_size:
                executable = False
                rationale.append("target size below minimum order size after mispricing sizing")

            if executable:
                risk_manager.register_fill(market=signal.market, category=decision.category, notional=target_notional)

            plans.append(
                TradePlan(
                    strategy_name=self.name,
                    market=signal.market,
                    question=signal.question,
                    category=decision.category,
                    outcome=signal.outcome,
                    token_id=str(market["token_yes"] if outcome == "YES" else market["token_no"]),
                    side="BUY",
                    signal_score=signal.signal_score,
                    target_notional=round(target_notional, 2),
                    reference_price=signal.reference_price,
                    best_ask=float(market["best_ask_yes"] if outcome == "YES" else market["best_ask_no"]),
                    best_bid=float(market["best_bid_yes"] if outcome == "YES" else market["best_bid_no"]),
                    spread=float(market["spread"]),
                    top_ask_size=float(market["top_ask_liquidity"]),
                    top_bid_size=float(market["top_bid_liquidity"]),
                    risk_score=decision.risk_score,
                    expected_value=expected_value,
                    executable=executable,
                    rationale=rationale,
                    metadata={
                        "true_probability": true_probability,
                        "market_probability": market_probability,
                        "edge": edge,
                        "exit_target_probability": true_probability,
                        "profit_target": clamp(abs(edge), self.config.profit_target_min, self.config.profit_target_max),
                    },
                    risk_metrics=decision.metrics,
                )
            )

        plans.sort(key=lambda item: (item.executable, item.expected_value), reverse=True)
        return StrategyAnalysis(
            strategy_name=self.name,
            signals=signals,
            trade_plan=plans,
            diagnostics={"calibration_error": self._calibration_error, "calibrated": calibrated_ok},
        )

    def backtest(self) -> BacktestResult:
        real_rows = load_real_mispricing_backtest_rows()
        if real_rows:
            return self._backtest_real_rows(real_rows)

        trades: list[BacktestTrade] = []
        returns: list[float] = []
        if self._calibration_error > self.config.max_calibration_error:
            return BacktestResult(
                strategy_name=self.name,
                trade_count=0,
                mean_return=0.0,
                median_return=0.0,
                win_rate=0.0,
                max_drawdown=0.0,
                expected_value=0.0,
                calibration_error=self._calibration_error,
                passed=False,
                diagnostics={"reason": "calibration_failed"},
                trades=[],
            )

        for market in self.markets:
            true_probability = self.estimate_true_probability(market)
            market_probability = float(market["historical_market_probability"])
            edge = true_probability - market_probability
            if abs(edge) < self.config.edge_no_trade:
                continue
            buy_yes = edge > 0
            entry_price = market_probability if buy_yes else (1.0 - market_probability)
            exit_price = float(market["historical_exit_probability"]) if buy_yes else (1.0 - float(market["historical_exit_probability"]))
            pnl = exit_price - entry_price
            returns.append(pnl / max(entry_price, 1e-6))
            trades.append(
                BacktestTrade(
                    market=str(market["market_id"]),
                    side="BUY YES" if buy_yes else "BUY NO",
                    entry_price=entry_price,
                    exit_price=exit_price,
                    expected_value=abs(edge),
                    pnl=pnl,
                    return_pct=returns[-1],
                    metadata={"true_probability": true_probability, "market_probability": market_probability},
                )
            )

        max_drawdown = _max_drawdown(returns)
        return BacktestResult(
            strategy_name=self.name,
            trade_count=len(trades),
            mean_return=statistics.fmean(returns) if returns else 0.0,
            median_return=statistics.median(returns) if returns else 0.0,
            win_rate=sum(1 for value in returns if value > 0) / len(returns) if returns else 0.0,
            max_drawdown=max_drawdown,
            expected_value=statistics.fmean([trade.expected_value for trade in trades]) if trades else 0.0,
            calibration_error=self._calibration_error,
            passed=(statistics.fmean(returns) if returns else 0.0) > 0 and max_drawdown < 0.20 and self._calibration_error <= self.config.max_calibration_error,
            diagnostics={"markets_evaluated": len(self.markets)},
            trades=trades,
        )

    def _backtest_real_rows(self, rows) -> BacktestResult:
        trades: list[BacktestTrade] = []
        returns: list[float] = []
        market_metadata = load_real_market_metadata() or {}
        if self._calibration_error > self.config.max_calibration_error:
            return BacktestResult(
                strategy_name=self.name,
                trade_count=0,
                mean_return=0.0,
                median_return=0.0,
                win_rate=0.0,
                max_drawdown=0.0,
                expected_value=0.0,
                calibration_error=self._calibration_error,
                passed=False,
                diagnostics={"reason": "calibration_failed", "data_source": "real_korean_2022_polling"},
                trades=[],
            )

        for row in rows:
            proxy_price = clamp(row.market_probability_proxy, 0.05, 0.95)
            true_probability = clamp(
                self._apply_platt(row.poll_probability + 0.08 * row.momentum_7d + 0.05 * row.momentum_14d + 0.05 * row.turnout_signal),
                0.01,
                0.99,
            )
            edge = true_probability - proxy_price
            if abs(edge) < self.config.edge_no_trade:
                continue
            buy_yes = edge > 0
            entry_price = proxy_price if buy_yes else (1.0 - proxy_price)
            payout = 1.0 if (buy_yes and row.actual_outcome == 1) or ((not buy_yes) and row.actual_outcome == 0) else 0.0
            pnl = payout - entry_price
            return_pct = pnl / max(entry_price, 1e-6)
            returns.append(return_pct)
            trades.append(
                BacktestTrade(
                    market=str(market_metadata.get("id", "241502")),
                    side="BUY YES" if buy_yes else "BUY NO",
                    entry_price=entry_price,
                    exit_price=float(payout),
                    expected_value=abs(edge),
                    pnl=pnl,
                    return_pct=return_pct,
                    metadata={
                        "date": row.date,
                        "true_probability": true_probability,
                        "market_probability_proxy": proxy_price,
                        "data_source": "real_korean_2022_polling_plus_market_proxy",
                    },
                )
            )

        max_drawdown = _max_drawdown(returns)
        return BacktestResult(
            strategy_name=self.name,
            trade_count=len(trades),
            mean_return=statistics.fmean(returns) if returns else 0.0,
            median_return=statistics.median(returns) if returns else 0.0,
            win_rate=sum(1 for value in returns if value > 0) / len(returns) if returns else 0.0,
            max_drawdown=max_drawdown,
            expected_value=statistics.fmean([trade.expected_value for trade in trades]) if trades else 0.0,
            calibration_error=self._calibration_error,
            passed=(statistics.fmean(returns) if returns else 0.0) > 0 and max_drawdown < 0.35,
            diagnostics={
                "data_source": "real_korean_2022_polling_with_polymarket_market_metadata",
                "market_question": market_metadata.get("question"),
                "market_slug": market_metadata.get("slug"),
                "row_count": len(rows),
                "market_probability_note": "Official historical Polymarket time-series was not publicly accessible in the available endpoints, so the backtest uses a lagged public-probability proxy from real polling observations.",
            },
            trades=trades,
        )

    def estimate_true_probability(self, market: dict[str, Any]) -> float:
        polling_baseline = clamp(float(market["poll_probability"]) - 0.5 * float(market["pollster_bias_adjustment"]), 0.01, 0.99)
        momentum_adjustment = 0.08 * float(market["poll_momentum_7d"]) + 0.05 * float(market["poll_momentum_14d"])
        turnout_adjustment = 0.04 * float(market["regional_pattern_signal"]) + 0.05 * float(market["turnout_signal"])
        behavior_strength = (
            0.30 * float(market["naver_trend_delta"])
            + 0.25 * float(market["youtube_velocity"])
            + 0.20 * float(market["community_sentiment"])
            + 0.25 * float(market["twitter_volume_spike"])
        )
        behavioral_adjustment = 0.0 if abs(behavior_strength) < 0.15 else 0.06 * behavior_strength
        raw_probability = clamp(polling_baseline + momentum_adjustment + turnout_adjustment + behavioral_adjustment, 0.01, 0.99)
        return clamp(self._apply_platt(raw_probability), 0.01, 0.99)

    def _fit_platt_scaler(self, history: list[dict[str, Any]]) -> tuple[float, float]:
        a = 1.0
        b = 0.0
        learning_rate = 0.03
        for _ in range(400):
            grad_a = 0.0
            grad_b = 0.0
            for row in history:
                x = _logit(float(row["raw_probability"]))
                y = float(row["outcome"])
                pred = _sigmoid(a * x + b)
                grad_a += (pred - y) * x
                grad_b += pred - y
            a -= learning_rate * grad_a / len(history)
            b -= learning_rate * grad_b / len(history)
        return a, b

    def _apply_platt(self, raw_probability: float) -> float:
        return _sigmoid(self._platt_a * _logit(raw_probability) + self._platt_b)

    def _compute_calibration_error(self, history: list[dict[str, Any]]) -> float:
        predictions = [self._apply_platt(float(row["raw_probability"])) for row in history]
        outcomes = [float(row["outcome"]) for row in history]
        return statistics.fmean((pred - outcome) ** 2 for pred, outcome in zip(predictions, outcomes, strict=True)) if history else 1.0


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    return max_dd
