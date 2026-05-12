"""Build a true 30-year per-(city, day_of_year) climatology from ERA5
reanalysis, replacing the leaky 1-year build_climatology() in
train_quantile_models.py.

Apr 25 2026 — fixes the climatology leak diagnosed by honest_ece.py.
The leaky version uses observations from the SAME period we train on,
so 7860/8030 (city, doy) cells have n=1 and `climo_mean` ≈ that day's
actual observed temp. The model's `forecast_anomaly` feature directly
encodes the label.

This script fetches ERA5 archive (Open-Meteo's archive-api endpoint)
for each city across 1991-2020 (30-year WMO normal period) and
aggregates per (city, day_of_year) → mean + std + n. Output schema
matches the existing climatology.json so it's a drop-in replacement.

Notes:
  - ERA5 is reanalysis. For climatology use this is the right input —
    we want long-term observed averages, not forecasts.
  - The 30-year window matches WMO standard normals (1991-2020).
  - Open-Meteo archive-api response uses `temperature_2m_max` (°C);
    we convert to °F to match the rest of the pipeline.

Run on EC2 (where Open-Meteo is reachable):
    venv/bin/python scripts/build_era5_climatology.py
    venv/bin/python scripts/build_era5_climatology.py --start 1991-01-01 --end 2020-12-31
    venv/bin/python scripts/build_era5_climatology.py --output /tmp/climatology_era5.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import requests


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.models import CITY_REGISTRY
from polymarket_strat.domain.weather.features import day_of_year


DEFAULT_OUTPUT = REPO_ROOT / "data" / "weather" / "climatology.json"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def fetch_era5_daily_max(
    lat: float, lon: float, start: str, end: str,
    timezone: str = "auto", retries: int = 3,
) -> tuple[list[str], list[float]]:
    """One HTTP call returns 30 years × 365 days of daily Tmax (≈10,950 values).
    Returns (date_strings, temps_in_F)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "daily": "temperature_2m_max",
        "timezone": timezone,
        "models": "era5",  # explicit ERA5 reanalysis
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(ARCHIVE_URL, params=params, timeout=120)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(2 ** attempt)
                continue
            j = r.json()
            daily = j.get("daily", {})
            dates = daily.get("time", [])
            temps_c = daily.get("temperature_2m_max", [])
            if len(dates) != len(temps_c):
                last_err = f"length mismatch dates={len(dates)} temps={len(temps_c)}"
                continue
            temps_f = [
                float(c_to_f(t)) if t is not None else None for t in temps_c
            ]
            return dates, temps_f
        except Exception as exc:
            last_err = repr(exc)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"ERA5 fetch failed after {retries} retries: {last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", default="1991-01-01")
    parser.add_argument("--end", default="2020-12-31")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--cities",
        type=lambda s: s.split(","),
        default=None,
        help="Comma-separated city keys; default = all in CITY_REGISTRY",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0,
        help="Sleep between cities to be polite to Open-Meteo (default 1.0s)",
    )
    args = parser.parse_args()

    cities = args.cities or sorted(CITY_REGISTRY.keys())
    print(f"[era5_climo] fetching ERA5 daily Tmax for {len(cities)} cities, "
          f"{args.start} → {args.end}", flush=True)

    by_city_doy: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    by_city_yr: dict[str, list[float]] = defaultdict(list)

    for i, city_key in enumerate(cities, 1):
        if city_key not in CITY_REGISTRY:
            print(f"[era5_climo]   {city_key}: UNKNOWN CITY, skipping", flush=True)
            continue
        station = CITY_REGISTRY[city_key]
        try:
            t0 = time.time()
            dates, temps = fetch_era5_daily_max(
                station.lat, station.lon, args.start, args.end,
                timezone=station.timezone,
            )
            elapsed = time.time() - t0
            n_total = len(temps)
            n_valid = sum(1 for t in temps if t is not None)
            for d_str, t in zip(dates, temps):
                if t is None:
                    continue
                try:
                    d = date.fromisoformat(d_str)
                except Exception:
                    continue
                doy = day_of_year(d)
                by_city_doy[city_key][doy].append(float(t))
                by_city_yr[city_key].append(float(t))
            print(f"[era5_climo]   [{i}/{len(cities)}] {city_key}: "
                  f"{n_valid}/{n_total} days ({elapsed:.1f}s)", flush=True)
        except Exception as exc:
            print(f"[era5_climo]   [{i}/{len(cities)}] {city_key}: "
                  f"FETCH ERR {exc!r}", flush=True)
            continue
        time.sleep(args.sleep)

    # Aggregate
    out_doy = {}
    for city, by_doy in by_city_doy.items():
        out_doy[city] = {}
        for doy, vals in by_doy.items():
            if len(vals) < 5:
                continue  # need enough samples for std to be meaningful
            out_doy[city][str(doy)] = {
                "mean": float(np.mean(vals)),
                "std": float(np.std(vals, ddof=1)),
                "n": len(vals),
            }

    out_yr = {}
    for city, vals in by_city_yr.items():
        if len(vals) < 30:
            continue
        out_yr[city] = {
            "mean": float(np.mean(vals)),
            "std": float(np.std(vals, ddof=1)),
            "n": len(vals),
        }

    payload = {"by_city_doy": out_doy, "by_city_yearmean": out_yr}

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    # Stats
    n_cells = sum(len(v) for v in out_doy.values())
    sample_counts = [
        e["n"] for v in out_doy.values() for e in v.values()
    ]
    print(f"\n[era5_climo] DONE")
    print(f"  cities          : {len(out_doy)}")
    print(f"  (city, doy) cells: {n_cells}")
    if sample_counts:
        a = np.array(sample_counts)
        print(f"  samples per cell: median={int(np.median(a))} "
              f"min={int(a.min())} max={int(a.max())}")
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
