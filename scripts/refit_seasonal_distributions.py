"""Refit error_distributions from existing forecast_errors, with per-season splits.

Apr 24 2026 — Q5-A support script. The full `polymarket-strat weather-calibrate`
command fetches 365 days of historical forecasts from Open-Meteo (slow, 30-60 min).
This script SKIPS the fetch and only refits distributions from forecast_errors
already in the DB. Use after:
  1. `scripts/backfill_season.py` populated season on legacy rows
  2. Schema migration added season column to error_distributions

For each (city, model, regime, lead_bucket):
  - Fit POOLED distribution (season=-1) from ALL rows
  - Fit PER-SEASON distribution from rows matching each season 0..3
Uses ErrorDistributionFitter (same code path as weather-calibrate) so the
fitted parameters are identical — just split by season.

Usage:
    python scripts/refit_seasonal_distributions.py          # all cities
    python scripts/refit_seasonal_distributions.py --cities nyc,tokyo
    python scripts/refit_seasonal_distributions.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.calibration import ErrorDistributionFitter
from polymarket_strat.domain.weather.models import (
    CITY_REGISTRY,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.domain.weather.season import climate_type, n_seasons
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase


DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"

# Mirror the lead buckets used by the main calibrate() loop.
_LEAD_BUCKETS = [6, 12, 24, 48, 72]
# Only calibrate models that have their own independent archive — HRRR/NAM
# borrow GFS at inference, so don't waste fits on duplicated data.
_CALIBRATION_MODELS = {WeatherModel.GFS, WeatherModel.ECMWF}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--cities",
        type=lambda s: s.split(","),
        default=None,
        help="Comma-separated city list, default: all cities in registry",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[refit_seasonal] no DB at {db_path}", file=sys.stderr)
        return 1

    db = WeatherDatabase(str(db_path))
    fitter = ErrorDistributionFitter()

    cities = args.cities or list(CITY_REGISTRY.keys())

    pooled_fitted = 0
    seasonal_fitted = 0
    failed_pooled = 0
    failed_seasonal = 0

    print(f"[refit_seasonal] {len(cities)} cities × {len(_CALIBRATION_MODELS)} models × "
          f"{len(SynopticRegime)} regimes × {len(_LEAD_BUCKETS)} leads × (1 pooled + 4 seasons)")

    for city in cities:
        if city not in CITY_REGISTRY:
            print(f"[refit_seasonal] unknown city: {city} (skip)", file=sys.stderr)
            continue
        for model in _CALIBRATION_MODELS:
            for regime in SynopticRegime:
                for lead_bucket in _LEAD_BUCKETS:
                    # --- Pooled fit (season=-1)
                    errors_pooled = db.get_forecast_errors(
                        city, model, regime, lead_bucket, season=None
                    )
                    if len(errors_pooled) >= 5:
                        try:
                            dist = fitter.fit(
                                errors_pooled,
                                city=city, model=model, regime=regime,
                                lead_hours=lead_bucket,
                            )
                            # default season=-1 via dataclass default
                            if not args.dry_run:
                                db.save_error_distribution(dist)
                            pooled_fitted += 1
                        except Exception as exc:
                            failed_pooled += 1
                            print(
                                f"[refit_seasonal]   POOLED {city}/{model.value}/{regime.value}/{lead_bucket}h: {exc}",
                                file=sys.stderr,
                            )

                    # --- Per-season fits. Range depends on city climate:
                    # 4-season cities iterate 0..3, tropical/arid 0..1.
                    # Iterating beyond n_seasons yields zero-sample
                    # queries that fail the >=5 filter anyway, but scoping
                    # the loop is cleaner and logs fewer meaningless lines.
                    for season in range(n_seasons(city)):
                        errors_season = db.get_forecast_errors(
                            city, model, regime, lead_bucket, season=season
                        )
                        if len(errors_season) < 5:
                            continue
                        try:
                            dist = fitter.fit(
                                errors_season,
                                city=city, model=model, regime=regime,
                                lead_hours=lead_bucket,
                            )
                            dist.season = season
                            if not args.dry_run:
                                db.save_error_distribution(dist)
                            seasonal_fitted += 1
                        except Exception as exc:
                            failed_seasonal += 1
                            print(
                                f"[refit_seasonal]   S{season} {city}/{model.value}/{regime.value}/{lead_bucket}h: {exc}",
                                file=sys.stderr,
                            )

    db.close()
    print(f"[refit_seasonal] pooled fits:    {pooled_fitted} ({failed_pooled} failed)")
    print(f"[refit_seasonal] seasonal fits:  {seasonal_fitted} ({failed_seasonal} failed)")
    if args.dry_run:
        print("[refit_seasonal] --dry-run: NOT committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
