"""Backfill historical GFS 24h-lead forecast errors via Open-Meteo's
historical-forecast-api endpoint.

Apr 25 2026 — adds ~5 years (2021-03-01 onward, post-FV3 GFS v16) of
real archived operational forecasts to the forecast_errors table,
expanding the calibration window from ~365 days to ~1860 days.

Why this improves the model:
  - Quantile regression bucket sample size jumps from n≈1500 → n≈11000
    per (city, 24h) bucket. Tail quantiles (τ=0.05, 0.95) become
    statistically meaningful instead of edge-noise dominated.
  - Per-city ECE estimate reliability triples — n_holdout per city goes
    from ~120 to ~400.
  - σ floor (currently 2.5°F) can be relaxed for buckets where n_real
    crosses 200; we get genuine forecast errors instead of reanalysis-
    tight ones.

Method:
  1. For each city, query historical-forecast-api with
     `hourly=temperature_2m_previous_day1` to get GFS's 24h-lead hourly
     temperature time series. Resample to daily max in station-local TZ.
  2. Query archive-api with `daily=temperature_2m_max&models=era5` for
     ground-truth observations on the same dates.
  3. Compute error_f = forecast_max - observed_max.
  4. INSERT OR IGNORE into forecast_errors (regime='stable_high' default)
     and observations (source='ERA5').

Idempotent. Re-running skips already-present rows.

Run on EC2:
    venv/bin/python scripts/backfill_gfs_historical.py
    venv/bin/python scripts/backfill_gfs_historical.py --start 2021-03-01
    venv/bin/python scripts/backfill_gfs_historical.py --cities nyc,seoul
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.models import CITY_REGISTRY


HFORE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARC_URL = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"

# Post-FV3 GFS v16 starts 2021-03-22. Round up to 2021-04-01 for clean
# month boundary.
DEFAULT_START = "2021-04-01"


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def fetch_with_retry(url, params, max_retries=4):
    """Open-Meteo rate-limits per-minute. Backoff exponentially."""
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=120)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = 65  # full minute + buffer
                print(f"    [429] sleeping {wait}s...", flush=True)
                time.sleep(wait)
                last_err = "429"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(2 ** attempt)
        except Exception as exc:
            last_err = repr(exc)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"failed after {max_retries} retries: {last_err}")


def fetch_gfs_24h_lead_hourly(city_key, lat, lon, tz, start, end):
    """Returns dict {date_str: forecast_max_f} for the date range."""
    j = fetch_with_retry(HFORE_URL, {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "hourly": "temperature_2m_previous_day1",
        "models": "gfs_seamless",
        "timezone": tz,
        "temperature_unit": "fahrenheit",
    })
    hourly = j.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m_previous_day1", [])
    if not times or not temps:
        return {}

    by_date: dict[str, list[float]] = defaultdict(list)
    for ts, t in zip(times, temps):
        if t is None:
            continue
        # ts format: "YYYY-MM-DDTHH:MM" — split on T
        date_str = ts[:10]
        by_date[date_str].append(float(t))

    return {d: max(vals) for d, vals in by_date.items() if vals}


def fetch_era5_obs(city_key, lat, lon, tz, start, end):
    """Returns dict {date_str: observed_max_f}."""
    j = fetch_with_retry(ARC_URL, {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "daily": "temperature_2m_max",
        "models": "era5",
        "timezone": tz,
        "temperature_unit": "fahrenheit",
    })
    daily = j.get("daily", {})
    times = daily.get("time", [])
    temps = daily.get("temperature_2m_max", [])
    out = {}
    for d, t in zip(times, temps):
        if t is None:
            continue
        out[str(d)] = float(t)
    return out


def date_chunks(start_iso, end_iso, max_days=400):
    """Yield (chunk_start, chunk_end) pairs to keep response sizes manageable."""
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    cur = start
    while cur <= end:
        chunk_end = min(cur.replace(year=cur.year + 1) if cur.month == 1 and cur.day == 1
                        else cur.replace(month=12, day=31) if cur.year < end.year
                        else end, end)
        yield cur.isoformat(), chunk_end.isoformat()
        cur = chunk_end
        # advance one day
        from datetime import timedelta
        cur = cur + timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument(
        "--cities", type=lambda s: s.split(","), default=None,
        help="Comma-separated city keys; default = all in CITY_REGISTRY",
    )
    parser.add_argument(
        "--sleep", type=float, default=10.0,
        help="Sleep between cities to be polite to Open-Meteo (default 10s)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cities = args.cities or sorted(CITY_REGISTRY.keys())
    print(f"[backfill_gfs] {len(cities)} cities, {args.start} → {args.end}",
          flush=True)
    print(f"[backfill_gfs] DB: {args.db}", flush=True)
    print(f"[backfill_gfs] dry-run: {args.dry_run}", flush=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total_inserted_errors = 0
    total_inserted_obs = 0
    total_skipped = 0

    for i, city_key in enumerate(cities, 1):
        if city_key not in CITY_REGISTRY:
            print(f"[backfill_gfs] [{i}/{len(cities)}] {city_key}: UNKNOWN, skip",
                  flush=True)
            continue
        station = CITY_REGISTRY[city_key]
        ts0 = time.time()

        # Iterate year chunks (one HTTP call per year per data source)
        all_forecasts: dict[str, float] = {}
        all_obs: dict[str, float] = {}
        for chunk_start, chunk_end in date_chunks(args.start, args.end):
            try:
                fc = fetch_gfs_24h_lead_hourly(
                    city_key, station.lat, station.lon, station.timezone,
                    chunk_start, chunk_end,
                )
                all_forecasts.update(fc)
                # Polite spacing within a city's chunks
                time.sleep(2.0)
                ob = fetch_era5_obs(
                    city_key, station.lat, station.lon, station.timezone,
                    chunk_start, chunk_end,
                )
                all_obs.update(ob)
                time.sleep(2.0)
            except Exception as exc:
                print(f"  {city_key} {chunk_start}..{chunk_end}: ERR {exc!r}",
                      flush=True)
                continue

        # Join + write
        new_errs = 0
        new_obs = 0
        skipped = 0
        for date_str, fmax in all_forecasts.items():
            obs_max = all_obs.get(date_str)
            if obs_max is None:
                skipped += 1
                continue
            error_f = fmax - obs_max

            if not args.dry_run:
                # Write observation row (idempotent via PK)
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO observations "
                        "(city, station_id, obs_date, observed_high_f, source) "
                        "VALUES (?, ?, ?, ?, 'ERA5')",
                        (city_key, station.station_id, date_str, obs_max),
                    )
                    if conn.total_changes > new_obs:
                        new_obs = conn.total_changes
                except Exception as exc:
                    print(f"  obs insert err {city_key} {date_str}: {exc!r}",
                          flush=True)

                # Write forecast_error row. season=-1 (unknown — backfill_regimes
                # can update later). regime='stable_high' default.
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO forecast_errors "
                        "(city, model, regime, lead_hours, error_f, obs_date, season) "
                        "VALUES (?, 'gfs', 'stable_high', 24, ?, ?, -1)",
                        (city_key, error_f, date_str),
                    )
                    new_errs += 1
                except Exception as exc:
                    print(f"  err insert err {city_key} {date_str}: {exc!r}",
                          flush=True)

        if not args.dry_run:
            conn.commit()

        total_inserted_errors += new_errs
        total_skipped += skipped
        elapsed = time.time() - ts0
        print(f"[backfill_gfs] [{i}/{len(cities)}] {city_key}: "
              f"forecasts={len(all_forecasts)} obs={len(all_obs)} "
              f"new_errs={new_errs} skipped={skipped} ({elapsed:.1f}s)",
              flush=True)

        # Polite spacing between cities
        if i < len(cities):
            time.sleep(args.sleep)

    conn.close()
    print(f"\n[backfill_gfs] DONE")
    print(f"  total new forecast_errors: {total_inserted_errors}")
    print(f"  total skipped (no obs):    {total_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
