"""WeatherBracketStrategy — the main orchestrator.

Implements the Strategy Protocol (analyze + backtest) by wiring together:
  - WeatherMarketScanner (discover contracts)
  - GribDataClient (fetch forecasts)
  - RegimeClassifier (classify synoptic regime)
  - ErrorDistributionFitter (calibrate error distributions)
  - BracketProbabilityCalculator (compute model probs, edges, Kelly)
  - WeatherDatabase (persist everything)
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path
from typing import Any

from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import (
    BacktestResult,
    BacktestTrade,
    StrategyAnalysis,
    StrategySignal,
    TradePlan,
)
from polymarket_strat.domain.weather.calibration import ErrorDistributionFitter, RegimeClassifier
from polymarket_strat.domain.weather.forecast import BracketProbabilityCalculator
from polymarket_strat.domain.weather.models import (
    CITY_REGISTRY,
    BracketContract,
    CorrelationGroup,
    ErrorDistribution,
    ForecastError,
    StationObservation,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
from polymarket_strat.infrastructure.weather.station_client import StationObservationClient
from polymarket_strat.risk import PortfolioRiskManager


class WeatherBracketStrategy:
    name = "weather_bracket"

    def __init__(
        self,
        *,
        grib_client: GribDataClient,
        station_client: StationObservationClient,
        market_scanner: WeatherMarketScanner,
        db: WeatherDatabase,
        min_edge: float = 0.05,
        fee_rate: float = 0.02,
        max_positions_per_group: int = 2,
        model_weights: dict[WeatherModel, float] | None = None,
    ):
        self.grib = grib_client
        self.stations = station_client
        self.scanner = market_scanner
        self.db = db
        self.min_edge = min_edge
        self.fee_rate = fee_rate
        self.max_positions_per_group = max_positions_per_group
        self.calc = BracketProbabilityCalculator(model_weights)
        self.regime_clf = RegimeClassifier()
        self.fitter = ErrorDistributionFitter()

    # ------------------------------------------------------------------
    # Strategy Protocol
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        constraints: TradingConstraints,
        portfolio_state: PortfolioState,
    ) -> StrategyAnalysis:
        """Live signal generation: discover contracts, fetch forecasts, price brackets."""
        contracts = self.scanner.find_weather_bracket_markets()
        if not contracts:
            return StrategyAnalysis(strategy_name=self.name, signals=[], trade_plan=[],
                                   diagnostics={"reason": "no_weather_contracts_found"})

        # Group contracts by (city, target_date) — normalization must be per-date,
        # not across all dates for a city. Mixing April 16 and April 17 brackets
        # in a single normalization is meaningless.
        by_city_date: dict[tuple[str, date], list[BracketContract]] = defaultdict(list)
        for c in contracts:
            by_city_date[(c.city, c.target_date)].append(c)

        signals: list[StrategySignal] = []
        plans: list[TradePlan] = []
        risk_mgr = PortfolioRiskManager(constraints, portfolio_state)
        group_counts: dict[str, int] = defaultdict(int)
        # Cache forecasts per city so we don't re-fetch for each date
        forecast_cache: dict[str, Any] = {}

        for (city, target_date), date_contracts in by_city_date.items():
            station = CITY_REGISTRY.get(city)
            if not station:
                continue

            # Fetch latest model forecasts (cached per city within one analyze run)
            if city not in forecast_cache:
                fetched = self.grib.fetch_all_models(station)
                if not fetched:
                    print(f"[weather] No forecasts available for {city}, skipping.", file=sys.stderr)
                    forecast_cache[city] = []
                else:
                    forecast_cache[city] = fetched
            forecasts = forecast_cache[city]
            if not forecasts:
                continue

            # Classify regime — prefer ensemble-based (82 members) over heuristic
            try:
                ens_stats = self.grib.fetch_ensemble_spread_stats(
                    station, target_date=target_date
                )
                if ens_stats.get("n_members", 0) >= 3:
                    regime = self.regime_clf.classify_from_ensemble(
                        spread_f=ens_stats["spread"],
                        std_f=ens_stats["std"],
                        skewness=ens_stats["skewness"],
                        cape_max=ens_stats["cape_max"],
                        n_members=ens_stats["n_members"],
                    )
                else:
                    raise ValueError("too few members")
            except Exception:
                # Fallback to heuristic spread-based classification
                spreads = [fc.ensemble_spread_f for fc in forecasts if fc.ensemble_spread_f > 0]
                max_spread = max(spreads) if spreads else 0.0
                regime = self.regime_clf.classify_from_spread(model_spread_f=max_spread)

            # Get calibrated error distributions, with fallback to STABLE_HIGH
            # if the current regime hasn't been calibrated yet.
            def _load_dists(r: SynopticRegime) -> tuple[list[ErrorDistribution], list[Any]]:
                dists, fcs = [], []
                for fc in forecasts:
                    dist = self.db.get_error_distribution(city, fc.model, r, _bucket_lead(fc.lead_hours))
                    # HRRR/NAM share gfs_seamless in the archive so we calibrate only GFS.
                    # At inference we use live HRRR/NAM forecasts (real independent signal)
                    # but borrow the GFS error distribution as the uncertainty estimate.
                    if dist is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                        dist = self.db.get_error_distribution(
                            city, WeatherModel.GFS, r, _bucket_lead(fc.lead_hours)
                        )
                    if dist is None:
                        continue
                    # Skip outlier distributions: large bias (|μ|>5°F) or extreme spread
                    # (σ>5°F) indicate poor calibration data (marine layer artifacts,
                    # IEM station gaps, urban microclimate effects not resolved by 25km models).
                    if abs(dist.mu) > 5.0 or dist.sigma > 5.0:
                        print(
                            f"[weather] Skipping outlier dist {city}/{fc.model.value}: "
                            f"μ={dist.mu:.2f}°F σ={dist.sigma:.2f}°F",
                            file=sys.stderr,
                        )
                        continue
                    dists.append(dist)
                    fcs.append(fc)
                return dists, fcs

            error_dists, matching_forecasts = _load_dists(regime)
            if not error_dists and regime != SynopticRegime.STABLE_HIGH:
                # Regime-specific data not yet calibrated; fall back to stable_high
                error_dists, matching_forecasts = _load_dists(SynopticRegime.STABLE_HIGH)

            if not error_dists:
                print(
                    f"[weather] No calibrated distributions for {city} (tried {regime.value} + stable_high). "
                    f"Run: polymarket-strat weather-calibrate --cities {city}",
                    file=sys.stderr,
                )
                continue

            # Price all brackets for this (city, date) independently
            brackets = [(c.lower_f, c.upper_f) for c in date_contracts]
            market_probs = [c.market_price_yes for c in date_contracts]

            priced = self.calc.price_all_brackets(
                forecasts=matching_forecasts,
                error_dists=error_dists,
                brackets=brackets,
                market_probs=market_probs,
                fee_rate=self.fee_rate,
                regime=regime,
            )

            # Emit signals and plans for tradeable edges
            group_key = station.correlation_group.value
            for bp, contract in zip(priced, date_contracts):
                if bp.edge_after_fees < self.min_edge:
                    continue
                if group_counts[group_key] >= self.max_positions_per_group:
                    continue
                # Skip near-zero prices: unrealistic fills and 50x+ implied leverage
                if contract.market_price_yes < constraints.min_entry_price:
                    continue

                signal = StrategySignal(
                    market=contract.market_id,
                    question=contract.question,
                    category="weather",
                    outcome=f"{bp.lower_f:.0f}-{bp.upper_f:.0f}F",
                    side="BUY",
                    signal_score=bp.edge_after_fees * 20,
                    reference_price=bp.market_prob,
                    metadata={
                        "city": city,
                        "regime": regime.value,
                        "model_prob": round(bp.model_prob, 4),
                        "edge_after_fees": round(bp.edge_after_fees, 4),
                        "kelly_fraction": round(bp.kelly_fraction, 4),
                        "shrinkage": round(bp.uncertainty_shrinkage, 4),
                        "contributing_models": [m.value for m in bp.contributing_models],
                        "token_id": contract.token_id_yes,
                        "bracket_lower_f": bp.lower_f,
                        "bracket_upper_f": bp.upper_f,
                        "target_date": contract.target_date.isoformat(),
                    },
                )
                signals.append(signal)

                target_notional = min(
                    bp.kelly_fraction * constraints.bankroll,
                    constraints.max_single_trade_notional,
                )
                if target_notional < constraints.min_order_size:
                    continue

                plans.append(TradePlan(
                    strategy_name=self.name,
                    market=contract.market_id,
                    question=contract.question,
                    category="weather",
                    outcome=signal.outcome,
                    token_id=contract.token_id_yes,
                    side="BUY",
                    signal_score=signal.signal_score,
                    target_notional=round(target_notional, 2),
                    reference_price=bp.market_prob,
                    best_ask=contract.best_ask_yes,
                    best_bid=contract.best_bid_yes,
                    spread=contract.spread,
                    top_ask_size=0.0,
                    top_bid_size=0.0,
                    risk_score=1.0 - bp.uncertainty_shrinkage,
                    expected_value=bp.edge_after_fees,
                    executable=contract.spread <= constraints.max_spread,
                    rationale=[
                        f"Model prob {bp.model_prob:.1%} vs market {bp.market_prob:.1%}",
                        f"Edge after fees: {bp.edge_after_fees:.1%}",
                        f"Regime: {regime.value}, shrinkage: {bp.uncertainty_shrinkage:.2f}",
                    ],
                    metadata=signal.metadata,
                ))
                group_counts[group_key] += 1

        plans.sort(key=lambda p: (-p.expected_value, p.risk_score))
        return StrategyAnalysis(
            strategy_name=self.name,
            signals=signals,
            trade_plan=plans,
            diagnostics={
                "cities_scanned": len({city for city, _ in by_city_date}),
                "contracts_found": len(contracts),
                "signals_generated": len(signals),
                "plans_generated": len(plans),
                "correlation_group_counts": dict(group_counts),
            },
        )

    def backtest(self) -> BacktestResult:
        """Backtest using historical forecasts, observations, and error distributions.

        Loads data from the SQLite DB.  You must run calibrate() first to
        populate forecast_errors and error_distributions.
        """
        trades: list[BacktestTrade] = []
        all_errors = self.db.get_trades(limit=5000)

        if not all_errors:
            return BacktestResult(
                strategy_name=self.name,
                trade_count=0, mean_return=0, median_return=0, win_rate=0,
                max_drawdown=0, expected_value=0, calibration_error=None,
                passed=False,
                diagnostics={"reason": "no_historical_trades_in_db"},
            )

        returns: list[float] = []
        for t in all_errors:
            pnl = t.get("pnl")
            notional = t.get("notional", 1)
            if pnl is not None and notional > 0:
                ret = pnl / notional
                returns.append(ret)
                trades.append(BacktestTrade(
                    market=t.get("city", ""),
                    side=t.get("side", "BUY"),
                    entry_price=t.get("market_prob", 0.5),
                    exit_price=t.get("market_prob", 0.5) + (pnl / max(notional, 1)),
                    expected_value=t.get("edge", 0),
                    pnl=pnl,
                    return_pct=ret,
                    metadata={"city": t.get("city"), "regime": t.get("regime")},
                ))

        if not returns:
            return BacktestResult(
                strategy_name=self.name,
                trade_count=0, mean_return=0, median_return=0, win_rate=0,
                max_drawdown=0, expected_value=0, calibration_error=None,
                passed=False,
                diagnostics={"reason": "no_settled_trades"},
            )

        max_dd = _max_drawdown(returns)
        mean_ret = statistics.fmean(returns)
        sharpe = (mean_ret / statistics.stdev(returns) * len(returns) ** 0.5) if len(returns) > 1 else 0

        return BacktestResult(
            strategy_name=self.name,
            trade_count=len(trades),
            mean_return=mean_ret,
            median_return=statistics.median(returns),
            win_rate=sum(1 for r in returns if r > 0) / len(returns),
            max_drawdown=max_dd,
            expected_value=mean_ret,
            calibration_error=None,
            passed=mean_ret > 0 and max_dd < 0.15 and sharpe > 1.0,
            diagnostics={"sharpe": round(sharpe, 3)},
            trades=trades,
        )

    # ------------------------------------------------------------------
    # Calibration pipeline
    # ------------------------------------------------------------------

    def calibrate(
        self,
        *,
        cities: list[str] | None = None,
        lookback_days: int = 365,
    ) -> dict[str, Any]:
        """Offline calibration: fetch observations, compute errors, fit distributions.

        1. For each city, fetch station observations for the lookback window.
        2. Load matching historical forecasts from DB.
        3. Compute forecast - observed errors.
        4. Group by (city, model, regime, lead_bucket).
        5. Fit error distributions via scipy MLE.
        6. Store fitted distributions in DB.

        Returns a summary of what was calibrated.
        """
        target_cities = cities or list(CITY_REGISTRY.keys())
        end = date.today() - timedelta(days=5)   # archive API has ~5-day lag
        start = end - timedelta(days=lookback_days)
        summary: dict[str, Any] = {"cities": {}, "total_distributions_fitted": 0}

        for city_key in target_cities:
            station = CITY_REGISTRY.get(city_key)
            if not station:
                continue

            print(f"[calibrate] {city_key}: fetching observations {start} to {end}...", file=sys.stderr)
            obs_list = self.stations.fetch_daily_highs(station, start=start, end=end)
            for obs in obs_list:
                self.db.save_observation(obs)
            obs_lookup = {obs.obs_date: obs.observed_high_f for obs in obs_list}

            if not obs_lookup:
                # IEM ASOS has limited coverage outside North America.
                # Fall back to ERA5 reanalysis (~0.3-0.5°C RMSE vs station).
                print(
                    f"[calibrate] {city_key}: IEM returned no data, "
                    f"falling back to ERA5 reanalysis.",
                    file=sys.stderr,
                )
                era5_highs = self.grib.fetch_era5_observations(station, start=start, end=end)
                if not era5_highs:
                    print(f"[calibrate] {city_key}: ERA5 also empty, skipping.", file=sys.stderr)
                    continue
                for obs_date, high_f in era5_highs.items():
                    era5_obs = StationObservation(
                        city=city_key,
                        station_id=station.station_id,
                        obs_date=obs_date,
                        observed_high_f=high_f,
                        source="ERA5",
                    )
                    self.db.save_observation(era5_obs)
                obs_lookup = era5_highs
                print(f"[calibrate] {city_key}: Using {len(era5_highs)} ERA5 observations.", file=sys.stderr)

            # Backfill forecast errors from Open-Meteo archive for GFS and ECMWF only.
            # HRRR and NAM both map to gfs_seamless in the archive, producing identical
            # data as GFS — calibrating them separately creates 3x GFS weighting in the
            # ensemble and artificially inflates confidence.  At inference time, HRRR/NAM
            # will fall back to the GFS distribution via the regime fallback.
            _ARCHIVE_CALIBRATION_MODELS = {WeatherModel.GFS, WeatherModel.ECMWF}
            # Purge any stale HRRR/NAM distributions that may exist from earlier runs.
            self.db.delete_distributions_for_models(
                city_key, [WeatherModel.HRRR, WeatherModel.NAM]
            )

            city_fitted = 0
            for model in WeatherModel:
                if model not in _ARCHIVE_CALIBRATION_MODELS:
                    continue

                # Prefer the previous-runs API (true operational forecasts,
                # ~90-day rolling archive) over the reanalysis archive.
                # The archive API (gfs_seamless / ecmwf_ifs025) has already
                # assimilated observations → σ is 2-4x too tight (0.9°F vs
                # the real 2.5-4°F from NWS verification).  The previous-runs
                # API stores what the model actually predicted before the event.
                prev_runs_start = max(start, end - timedelta(days=90))
                print(
                    f"[calibrate]   {city_key}/{model.value}: "
                    f"fetching real forecasts {prev_runs_start}→{end} (previous-runs API)...",
                    file=sys.stderr,
                )
                archive_prev = self.grib.fetch_archived_forecasts(
                    station, model, start=prev_runs_start, end=end
                )

                # Fill older dates from reanalysis archive (only source available
                # beyond ~90 days).  σ floor in BracketProbabilityCalculator
                # (2.5°F) prevents overconfidence from these samples.
                archive_hist: dict[date, float] = {}
                if start < prev_runs_start:
                    print(
                        f"[calibrate]   {city_key}/{model.value}: "
                        f"filling {start}→{prev_runs_start - timedelta(days=1)} from reanalysis archive...",
                        file=sys.stderr,
                    )
                    archive_hist = self.grib.fetch_historical_highs(
                        station, model, start=start, end=prev_runs_start - timedelta(days=1)
                    )

                # Merge: real forecasts override reanalysis where both exist
                archive = {**archive_hist, **archive_prev}
                print(
                    f"[calibrate]   {city_key}/{model.value}: "
                    f"{len(archive_prev)} real-forecast days + {len(archive_hist)} reanalysis days "
                    f"= {len(archive)} total",
                    file=sys.stderr,
                )

                for obs_date, observed_f in obs_lookup.items():
                    forecast_f = archive.get(obs_date)
                    if forecast_f is None:
                        continue
                    error = forecast_f - observed_f
                    # Use STABLE_HIGH as the default regime (upper-air classification
                    # requires GRIB fields not yet fetched during historical runs).
                    self.db.save_forecast_error(
                        ForecastError(
                            city=city_key,
                            model=model,
                            regime=SynopticRegime.STABLE_HIGH,
                            lead_hours=24,
                            error_f=error,
                            obs_date=obs_date,
                        )
                    )

                for regime in SynopticRegime:
                    for lead_bucket in [6, 12, 24, 48, 72]:
                        errors = self.db.get_forecast_errors(city_key, model, regime, lead_bucket)
                        if len(errors) < 5:
                            continue
                        try:
                            dist = self.fitter.fit(
                                errors,
                                city=city_key,
                                model=model,
                                regime=regime,
                                lead_hours=lead_bucket,
                            )
                            self.db.save_error_distribution(dist)
                            city_fitted += 1
                        except (ValueError, Exception) as exc:
                            print(f"[calibrate]   {city_key}/{model.value}/{regime.value}/{lead_bucket}h: {exc}", file=sys.stderr)

            summary["cities"][city_key] = {
                "observations": len(obs_list),
                "distributions_fitted": city_fitted,
            }
            summary["total_distributions_fitted"] += city_fitted

        print(f"[calibrate] Done. {summary['total_distributions_fitted']} distributions fitted.", file=sys.stderr)
        return summary


def _bucket_lead(lead_hours: int) -> int:
    """Round lead time to nearest calibration bucket."""
    for bucket in [6, 12, 24, 48, 72]:
        if lead_hours <= bucket:
            return bucket
    return 72


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= 1.0 + r
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd
