from __future__ import annotations

from dataclasses import asdict
from typing import Any

from polymarket_strat.application.service import StrategyApplicationService


def run_backtests(strategy_name: str = "all", *, use_sample: bool = True) -> list[dict[str, Any]]:
    service = StrategyApplicationService(use_sample=use_sample)
    return [asdict(result) for result in service.backtest(strategy_name)]
