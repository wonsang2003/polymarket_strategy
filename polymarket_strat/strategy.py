from __future__ import annotations

from polymarket_strat.domain.models import BacktestResult, StrategyAnalysis, StrategySignal, TradePlan
from polymarket_strat.domain.strategies.mispricing import MispricingConfig, MispricingProbabilityStrategy
from polymarket_strat.domain.strategies.whale_following import (
    WhaleFollowingStrategy as WhaleTrackerStrategy,
    WhaleScore,
    WhaleSelectionConfig,
    WhaleSignalConfig as SignalConfig,
)

STRATEGY_NAMES = ("whale_following", "mispricing")

__all__ = [
    "BacktestResult",
    "MispricingConfig",
    "MispricingProbabilityStrategy",
    "SignalConfig",
    "StrategyAnalysis",
    "StrategySignal",
    "STRATEGY_NAMES",
    "TradePlan",
    "WhaleScore",
    "WhaleSelectionConfig",
    "WhaleTrackerStrategy",
]
