from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class StrategySignal:
    market: str
    question: str
    category: str
    outcome: str
    side: str
    signal_score: float
    reference_price: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradePlan:
    strategy_name: str
    market: str
    question: str
    category: str
    outcome: str
    token_id: str
    side: str
    signal_score: float
    target_notional: float
    reference_price: float
    best_ask: float
    best_bid: float
    spread: float
    top_ask_size: float
    top_bid_size: float
    risk_score: float
    expected_value: float
    executable: bool
    rationale: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    risk_metrics: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyAnalysis:
    strategy_name: str
    signals: list[StrategySignal]
    trade_plan: list[TradePlan]
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BacktestTrade:
    market: str
    side: str
    entry_price: float
    exit_price: float
    expected_value: float
    pnl: float
    return_pct: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BacktestResult:
    strategy_name: str
    trade_count: int
    mean_return: float
    median_return: float
    win_rate: float
    max_drawdown: float
    expected_value: float
    calibration_error: float | None
    passed: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)
    trades: list[BacktestTrade] = field(default_factory=list)


class Strategy(Protocol):
    name: str

    def analyze(self, *, constraints: Any, portfolio_state: Any) -> StrategyAnalysis:
        ...

    def backtest(self) -> BacktestResult:
        ...
