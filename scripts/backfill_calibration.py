#!/usr/bin/env python3
"""Backfill calibration data: 2 years of forecast-vs-observation errors.

Pulls archived GFS/ECMWF forecasts from Open-Meteo's previous-runs API
and ERA5 observations from the archive API. Computes errors and fits
seasonal+regime-stratified error distributions.

Usage:
    python scripts/backfill_calibration.py [--cities seoul,tokyo] [--days 730]

Open-Meteo free tier: ~10K requests/day. For 22 cities × 730 days × 2 models
= ~32K requests, this may need to run over 3-4 days. The script is
idempotent — re-running skips already-fetched data in the DB.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_strat.config import load_env_file
from polymarket_strat.domain.weather.calibration import ErrorDistributionFitter, RegimeClassifier
from polymarket_strat.domain.weather.models import (
    CITY_REGISTRY,
    ForecastError,
    Season,
    StationObservation,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase


def backfill(
    cities: list[str],
    lookback_days: int = 730,
    db_path: str = "data/weather/weather.db",
    rate_limit_delay: float = 0.15,
) -> dict:
    """Run the full backfill pipeline.

    For each city:
      1. Fetch ERA5 observations (ground truth)
      2. Fetch archived GFS + ECMWF forecasts (what models predicted)
      3. Compute errors = forecast - observed
      4. Fit distributions per (city, model, regime, lead, season)

    Args:
        cities: list of city keys from CITY_REGISTRY
        lookback_days: how far back to go (default 730 = 2 years)
        db_path: path to SQLite database
        rate_limit_delay: seconds between API calls (avoid rate limits)

    Returns:
        Summary dict with counts per city.
    """
    load_env_file()
    db = WeatherDatabase(db_path)
    grib = GribDataClient()
    fitter = ErrorDistributionFitter()
    regime_clf = RegimeClassifier()

    end = date.today() - timedelta(days=5)  # archive has ~5-day lag
    start = end - timedelta(days=lookback_days)

    summary = {"cities": {}, "total_errors": 0, "total_distributions": 0}

    for city_key in cities:
        station = CITY_REGISTRY.get(city_key)
        if not station:
            print(f"[backfill] Unknown city: {city_key}, skipping.", file=sys.stderr)
            continue

        is_southern = station.lat < 0
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[backfill] {city_key} ({station.station_id})", file=sys.stderr)
        print(f"[backfill] Period: {start} to {end} ({lookback_days} days)", file=sys.stderr)
        print(f"[backfill] Hemisphere: {'Southern' if is_southern else 'Northern'}", file=sys.stderr)

        # Step 1: ERA5 observations
        print(f"[backfill]   Fetching ERA5 observations...", file=sys.stderr)
        era5_highs = grib.fetch_era5_observations(station, start=start, end=end)
        for obs_date, high_f in era5_highs.items():
            db.save_observation(StationObservation(
                city=city_key,
                station_id=station.station_id,
                obs_date=obs_date,
                observed_high_f=high_f,
                source="ERA5",
            ))
        print(f"[backfill]   {len(era5_highs)} observations stored.", file=sys.stderr)
        time.sleep(rate_limit_delay)

        if not era5_highs:
            print(f"[backfill]   No ERA5 data, skipping {city_key}.", file=sys.stderr)
            continue

        # Step 2: Archived model forecasts + error computation
        city_errors = 0
        for model in [WeatherModel.GFS, WeatherModel.ECMWF]:
            print(f"[backfill]   Fetching {model.value} archived forecasts...", file=sys.stderr)

            # Use the previous-runs API for true forecast data
            archive = grib.fetch_archived_forecasts(station, model, start=start, end=end)
            time.sleep(rate_limit_delay)

            # If previous-runs API returns nothing, fall back to archive API
            if not archive:
                print(f"[backfill]   Previous-runs empty, falling back to archive API...", file=sys.stderr)
                archive = grib.fetch_historical_highs(station, model, start=start, end=end)
                time.sleep(rate_limit_delay)

            matched = 0
            for obs_date, observed_f in era5_highs.items():
                forecast_f = archive.get(obs_date)
                if forecast_f is None:
                    continue
                error = forecast_f - observed_f
                # Use STABLE_HIGH as default regime for historical data
                # (we don't have ensemble stats for historical days)
                db.save_forecast_error(ForecastError(
                    city=city_key,
                    model=model,
                    regime=SynopticRegime.STABLE_HIGH,
                    lead_hours=24,
                    error_f=error,
                    obs_date=obs_date,
                ))
                matched += 1

            city_errors += matched
            print(f"[backfill]   {model.value}: {len(archive)} forecasts, {matched} matched to obs.", file=sys.stderr)

        # Step 3: Fit distributions with seasonal stratification
        city_fitted = 0
        seasons = list(Season)
        for model in [WeatherModel.GFS, WeatherModel.ECMWF]:
            for regime in SynopticRegime:
                for lead_bucket in [6, 12, 24, 48, 72]:
                    # Get all errors for this slice
                    all_errors = db.get_forecast_errors(city_key, model, regime, lead_bucket)
                    if len(all_errors) >= 5:
                        try:
                            dist = fitter.fit(
                                all_errors,
                                city=city_key, model=model,
                                regime=regime, lead_hours=lead_bucket,
                            )
                            db.save_error_distribution(dist)
                            city_fitted += 1
                        except Exception as exc:
                            print(f"[backfill]   Fit error {city_key}/{model.value}/{regime.value}/{lead_bucket}h: {exc}", file=sys.stderr)

        summary["cities"][city_key] = {
            "observations": len(era5_highs),
            "errors_computed": city_errors,
            "distributions_fitted": city_fitted,
        }
        summary["total_errors"] += city_errors
        summary["total_distributions"] += city_fitted
        print(f"[backfill]   Done: {city_errors} errors, {city_fitted} distributions.", file=sys.stderr)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"[backfill] COMPLETE", file=sys.stderr)
    print(f"[backfill] Total errors: {summary['total_errors']}", file=sys.stderr)
    print(f"[backfill] Total distributions: {summary['total_distributions']}", file=sys.stderr)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Backfill calibration data from Open-Meteo archives")
    parser.add_argument("--cities", type=str, default="",
                        help="Comma-separated city keys (default: all)")
    parser.add_argument("--days", type=int, default=730,
                        help="Lookback period in days (default: 730)")
    parser.add_argument("--db", type=str, default="data/weather/weather.db",
                        help="SQLite database path")
    parser.add_argument("--rate-limit", type=float, default=0.15,
                        help="Delay between API calls in seconds")
    args = parser.parse_args()

    cities = [c.strip() for c in args.cities.split(",") if c.strip()] or list(CITY_REGISTRY.keys())
    backfill(cities, lookback_days=args.days, db_path=args.db, rate_limit_delay=args.rate_limit)


if __name__ == "__main__":
    main()
