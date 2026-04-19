"""Quick smoke test for the hourly-based 48h previous-runs fetch.

Hits ONE city × ONE model × 7 days — confirms Open-Meteo accepts the request
and our parser produces non-empty per-day results. Finishes in ~5 seconds.

Run BEFORE full calibration:

    python scripts/verify_48h_fetch.py
    python scripts/verify_48h_fetch.py --city nyc --model ecmwf

If this passes, the full `weather-calibrate` run is safe to start.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Make the package importable regardless of which conda/venv is active.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polymarket_strat.domain.weather.models import CITY_REGISTRY, WeatherModel  # noqa: E402
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="seoul")
    ap.add_argument("--model", default="gfs", choices=["gfs", "ecmwf"])
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    if args.city not in CITY_REGISTRY:
        print(f"FAIL: unknown city {args.city!r}", file=sys.stderr)
        return 2
    station = CITY_REGISTRY[args.city]
    model = WeatherModel.GFS if args.model == "gfs" else WeatherModel.ECMWF

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)

    client = GribDataClient()

    # 24h — known-good baseline (uses daily endpoint)
    r24 = client.fetch_archived_forecasts(station, model, start=start, end=end, lead_days=1)
    # 48h — the new hourly-based path
    r48 = client.fetch_archived_forecasts(station, model, start=start, end=end, lead_days=2)

    print(f"Station: {station.city}/{station.station_id} ({station.lat},{station.lon}) TZ={station.timezone}")
    print(f"Model: {args.model}")
    print(f"Date range: {start} .. {end}  ({args.days} days requested)")
    print()
    print(f"  24h fetch: {len(r24)} days returned")
    for d in sorted(r24):
        print(f"    {d}: {r24[d]:.1f}F")
    print()
    print(f"  48h fetch: {len(r48)} days returned")
    for d in sorted(r48):
        print(f"    {d}: {r48[d]:.1f}F")
    print()

    if len(r24) == 0:
        print("FAIL: 24h fetch returned 0 days — check network / Open-Meteo status", file=sys.stderr)
        return 1
    if len(r48) == 0:
        print("FAIL: 48h fetch returned 0 days — hourly endpoint parser broken", file=sys.stderr)
        return 1

    # Sanity: 48h values should be in reasonable ballpark vs 24h (same valid days)
    common = sorted(set(r24) & set(r48))
    if common:
        diffs = [abs(r24[d] - r48[d]) for d in common]
        mean_abs_diff = sum(diffs) / len(diffs)
        max_abs_diff = max(diffs)
        print(f"  Overlap: {len(common)} common days")
        print(f"  mean |24h - 48h|: {mean_abs_diff:.2f}F   max: {max_abs_diff:.2f}F")
        if max_abs_diff > 30.0:
            print("FAIL: 48h vs 24h differs by >30F — parser likely mis-aligning dates", file=sys.stderr)
            return 1

    print()
    print("PASS: 24h and 48h fetches both working. Ready for full weather-calibrate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
