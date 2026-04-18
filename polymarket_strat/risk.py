from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from polymarket_strat.config import PortfolioState, TradingConstraints


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    target_notional: float
    risk_score: float
    category: str
    reasons: list[str]
    metrics: dict[str, float]


class PortfolioRiskManager:
    def __init__(self, constraints: TradingConstraints, portfolio_state: PortfolioState | None = None):
        self.constraints = constraints
        self.portfolio_state = portfolio_state or PortfolioState.default(constraints)

    def infer_category(self, question: str) -> str:
        lowered = question.lower()
        if any(token in lowered for token in ("election", "candidate", "debate", "president", "senate")):
            return "politics"
        if any(token in lowered for token in ("btc", "bitcoin", "eth", "crypto", "sol")):
            return "crypto"
        if any(token in lowered for token in ("fed", "rate", "inflation", "recession", "economy")):
            return "macro"
        if any(token in lowered for token in ("nba", "nfl", "mlb", "cup", "match", "game")):
            return "sports"
        return "other"

    def evaluate_trade(
        self,
        *,
        market: str,
        question: str,
        signal_score: float,
        whale_wallets: list[str],
        whale_metrics: dict[str, float],
        best_ask: float,
        best_bid: float,
        top_ask_size: float,
    ) -> RiskDecision:
        reasons: list[str] = []
        category = self.infer_category(question)
        drawdown = self.portfolio_state.drawdown
        spread = max(best_ask - best_bid, 0.0)

        if drawdown >= self.constraints.drawdown_hard_limit:
            return RiskDecision(
                approved=False,
                target_notional=0.0,
                risk_score=1.0,
                category=category,
                reasons=[f"portfolio drawdown {drawdown:.2%} exceeds hard limit {self.constraints.drawdown_hard_limit:.2%}"],
                metrics={"drawdown": drawdown, "spread": spread},
            )

        risk_budget = min(
            self.constraints.max_total_notional,
            self.constraints.max_portfolio_at_risk * self.portfolio_state.current_equity,
            max(self.portfolio_state.cash - self.constraints.reserve_cash_ratio * self.portfolio_state.current_equity, 0.0),
        )

        if risk_budget < self.constraints.min_order_size:
            return RiskDecision(
                approved=False,
                target_notional=0.0,
                risk_score=1.0,
                category=category,
                reasons=["available risk budget is below the minimum order size"],
                metrics={"risk_budget": risk_budget, "drawdown": drawdown},
            )

        if len(self.portfolio_state.open_positions) >= self.constraints.max_open_positions:
            return RiskDecision(
                approved=False,
                target_notional=0.0,
                risk_score=1.0,
                category=category,
                reasons=[f"open positions already at limit {self.constraints.max_open_positions}"],
                metrics={"open_positions": float(len(self.portfolio_state.open_positions))},
            )

        if signal_score < self.constraints.min_signal_score:
            reasons.append(
                f"signal score {signal_score:.2f} below threshold {self.constraints.min_signal_score:.2f}"
            )

        if whale_metrics.get("win_rate", 0.0) < self.constraints.min_whale_win_rate:
            reasons.append(
                f"consensus whale win rate {whale_metrics.get('win_rate', 0.0):.2f} below threshold {self.constraints.min_whale_win_rate:.2f}"
            )

        if whale_metrics.get("avg_roi", 0.0) < self.constraints.min_whale_avg_roi:
            reasons.append(
                f"consensus whale avg ROI {whale_metrics.get('avg_roi', 0.0):.2f} below threshold {self.constraints.min_whale_avg_roi:.2f}"
            )

        category_used = self.portfolio_state.category_exposure.get(category, 0.0)
        if category_used >= self.constraints.max_category_notional:
            reasons.append(f"category exposure already at limit for {category}")

        if self.portfolio_state.category_position_counts.get(category, 0) >= self.constraints.max_correlated_positions:
            reasons.append(f"correlated position count already at limit for {category}")

        liquidity_cap = top_ask_size * best_ask * self.constraints.liquidity_haircut
        base_size = self.constraints.bankroll * 0.02
        score_multiplier = clamp(signal_score / max(self.constraints.min_signal_score, 0.01), 0.5, 2.5)
        whale_multiplier = clamp(len(whale_wallets) / max(self.constraints.min_whale_agreement, 1), 1.0, 2.0)
        drawdown_multiplier = 1.0
        if drawdown >= self.constraints.drawdown_soft_limit:
            drawdown_multiplier = clamp(
                1.0 - ((drawdown - self.constraints.drawdown_soft_limit) / max(self.constraints.drawdown_hard_limit - self.constraints.drawdown_soft_limit, 0.01)),
                0.25,
                1.0,
            )
            reasons.append(
                f"drawdown {drawdown:.2%} above soft limit {self.constraints.drawdown_soft_limit:.2%}, size reduced"
            )

        slippage_multiplier = clamp(1.0 - spread * self.constraints.slippage_haircut_per_spread, 0.2, 1.0)
        if spread > self.constraints.max_spread:
            reasons.append(f"spread {spread:.3f} exceeds max {self.constraints.max_spread:.3f}")

        target_notional = min(
            base_size
            * score_multiplier
            * whale_multiplier
            * self.constraints.confidence_boost
            * drawdown_multiplier
            * slippage_multiplier,
            liquidity_cap,
            risk_budget,
            self.constraints.max_single_trade_notional,
            self.constraints.max_market_notional - self.portfolio_state.open_positions.get(market, 0.0),
            self.constraints.max_category_notional - category_used,
        )
        target_notional = max(target_notional, 0.0)

        if target_notional < self.constraints.min_order_size:
            reasons.append(
                f"risk-adjusted size {target_notional:.2f} below min order size {self.constraints.min_order_size:.2f}"
            )

        risk_score = 1.0 - clamp(
            0.35 * min(signal_score / 5.0, 1.0)
            + 0.20 * whale_metrics.get("win_rate", 0.0)
            + 0.15 * min(whale_metrics.get("avg_roi", 0.0) / 0.20, 1.0)
            + 0.15 * clamp(top_ask_size / max(self.constraints.min_top_book_liquidity, 1.0), 0.0, 1.0)
            + 0.15 * (1.0 - clamp(spread / max(self.constraints.max_spread, 0.001), 0.0, 1.0)),
            0.0,
            1.0,
        )

        approved = not reasons and target_notional >= self.constraints.min_order_size
        if approved:
            reasons.append("risk checks passed")

        return RiskDecision(
            approved=approved,
            target_notional=round(target_notional, 2),
            risk_score=risk_score,
            category=category,
            reasons=reasons,
            metrics={
                "drawdown": drawdown,
                "spread": spread,
                "risk_budget": risk_budget,
                "liquidity_cap": liquidity_cap,
            },
        )

    def register_fill(self, *, market: str, category: str, notional: float) -> None:
        self.portfolio_state.cash = max(self.portfolio_state.cash - notional, 0.0)
        self.portfolio_state.open_positions[market] = self.portfolio_state.open_positions.get(market, 0.0) + notional
        self.portfolio_state.category_exposure[category] = self.portfolio_state.category_exposure.get(category, 0.0) + notional
        self.portfolio_state.category_position_counts[category] = self.portfolio_state.category_position_counts.get(category, 0) + 1


def decision_to_dict(decision: RiskDecision) -> dict[str, Any]:
    return asdict(decision)
