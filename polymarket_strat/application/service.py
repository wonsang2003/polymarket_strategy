from __future__ import annotations

from dataclasses import asdict
from typing import Any

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import BacktestResult, StrategyAnalysis
from polymarket_strat.domain.strategies.mispricing import MispricingProbabilityStrategy
from polymarket_strat.domain.strategies.whale_following import WhaleFollowingStrategy
from polymarket_strat.infrastructure.real_data import load_real_data_status
from polymarket_strat.sample_data import SamplePolymarketClient


class StrategyApplicationService:
    def __init__(self, *, use_sample: bool):
        self.use_sample = use_sample
        self.client = SamplePolymarketClient() if use_sample else PolymarketPublicClient()

    def available_strategies(self) -> list[str]:
        return ["whale_following", "mispricing", "weather_bracket"]

    def create_strategy(self, strategy_name: str):
        if strategy_name == "whale_following":
            return WhaleFollowingStrategy(self.client)
        if strategy_name == "mispricing":
            return MispricingProbabilityStrategy()
        if strategy_name == "weather_bracket":
            from pathlib import Path

            from polymarket_strat.config import WeatherConfig
            from polymarket_strat.domain.weather.strategy import WeatherBracketStrategy
            from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
            from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner
            from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
            from polymarket_strat.infrastructure.weather.station_client import StationObservationClient

            from polymarket_strat.domain.weather.models import WeatherModel

            cfg = WeatherConfig()
            return WeatherBracketStrategy(
                grib_client=GribDataClient(cache_dir=Path(cfg.grib_cache_dir)),
                station_client=StationObservationClient(),
                market_scanner=WeatherMarketScanner(self.client),
                db=WeatherDatabase(cfg.db_path),
                min_edge=cfg.min_edge,
                fee_rate=cfg.fee_rate,
                max_positions_per_group=cfg.max_positions_per_group,
                model_weights={
                    WeatherModel.GFS: cfg.gfs_weight,
                    WeatherModel.ECMWF: cfg.ecmwf_weight,
                    WeatherModel.HRRR: cfg.hrrr_weight,
                    WeatherModel.NAM: cfg.nam_weight,
                },
            )
        raise ValueError(f"Unknown strategy: {strategy_name}")

    def analyze(self, strategy_name: str, *, constraints: TradingConstraints, portfolio_state: PortfolioState) -> StrategyAnalysis:
        strategy = self.create_strategy(strategy_name)
        return strategy.analyze(constraints=constraints, portfolio_state=portfolio_state)

    def backtest(self, strategy_name: str) -> list[BacktestResult]:
        if strategy_name == "all":
            return [self.create_strategy(name).backtest() for name in self.available_strategies()]
        return [self.create_strategy(strategy_name).backtest()]

    def describe_analysis(self, analysis: StrategyAnalysis) -> dict[str, Any]:
        return {
            "strategy_name": analysis.strategy_name,
            "signals": [asdict(signal) for signal in analysis.signals],
            "trade_plan": [asdict(plan) for plan in analysis.trade_plan],
            "diagnostics": analysis.diagnostics,
        }

    def real_data_status(self) -> dict[str, Any]:
        return load_real_data_status()
