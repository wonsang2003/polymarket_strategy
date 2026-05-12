"""Per-city climate-aware season backfill for forecast_errors.

Apr 24 2026 — replaces the original scripts/backfill_season.py which
applied uniform NH quartiles to all cities. This version uses the
CITY_SEASON_SCHEDULE from domain/weather/season.py, which gives:
  - NH 4-season cities: winter=DJF, spring=MAM, summer=JJA, fall=SON
    (unchanged for Group A — those rows won't move)
  - SH 4-season cities (Wellington/Sydney/BA/SP): flipped by 6 months,
    so every row's season will change.
  - Tropical 2-season cities (Miami/HK/MexCity): only 0 (dry) or 1 (wet).
    Rows previously labeled 2 or 3 will REMAP to the correct 0 or 1.
  - Arid (Dubai): same 2-season pattern.

Idempotent — safe to re-run. Writes are done in a single transaction
per city. Legacy rows at season=-1 are also updated.

IMPORTANT: after running this script, the error_distributions table
should be cleared of per-season rows (season >= 0) and refitted via
scripts/refit_seasonal_distributions.py — the OLD fits were keyed to
WRONG seasons for SH/tropical cities and must be discarded.

Usage:
    python scripts/backfill_season_per_city.py              # real
    python scripts/backfill_season_per_city.py --dry-run    # preview
    python scripts/backfill_season_per_city.py --cities nyc,wellington
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.season import (
    CITY_SEASON_SCHEDULE,
    climate_type,
    season_from_date,
)


DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--cities",
        type=lambda s: s.split(","),
        default=None,
        help="Comma-separated cities, default: all with CITY_SEASON_SCHEDULE entries",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[backfill_per_city] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cities = args.cities or sorted(CITY_SEASON_SCHEDULE.keys())

    try:
        total_scanned = 0
        total_updated = 0
        per_city_stats = {}

        for city in cities:
            if city not in CITY_SEASON_SCHEDULE:
                print(f"[backfill_per_city] unknown city: {city} (skip)", file=sys.stderr)
                continue

            ct = climate_type(city)
            # Pull every row for this city. For performance we could
            # filter by season mismatches but the volume is small (<30k
            # rows across all cities) so a full scan per city is fine.
            rows = conn.execute(
                "SELECT id, obs_date, season FROM forecast_errors WHERE city = ?",
                (city,),
            ).fetchall()

            scanned = len(rows)
            changed = 0
            updates: list[tuple[int, int]] = []

            for row in rows:
                try:
                    obs = date.fromisoformat(row["obs_date"][:10])
                except Exception:
                    continue
                new_season = season_from_date(obs, city)
                old_season = row["season"]
                if old_season != new_season:
                    updates.append((new_season, row["id"]))
                    changed += 1

            per_city_stats[city] = {
                "climate_type": ct,
                "scanned": scanned,
                "will_change": changed,
            }
            total_scanned += scanned
            total_updated += changed

            if not args.dry_run and updates:
                conn.executemany(
                    "UPDATE forecast_errors SET season = ? WHERE id = ?",
                    updates,
                )
                conn.commit()

        # Report
        print("=" * 72)
        print(f"{'city':<14} {'climate_type':<22} {'scanned':>8} {'changed':>8}")
        print("-" * 72)
        for city, s in per_city_stats.items():
            print(
                f"{city:<14} {s['climate_type']:<22} "
                f"{s['scanned']:>8} {s['will_change']:>8}"
            )
        print("-" * 72)
        print(f"{'TOTAL':<14} {'':<22} {total_scanned:>8} {total_updated:>8}")

        if args.dry_run:
            print("\n[backfill_per_city] --dry-run: NOT committed")
        else:
            print(f"\n[backfill_per_city] committed {total_updated} updates")
            # Post-update distribution per city (sanity)
            print("\n[backfill_per_city] final season distribution per city:")
            for city in cities:
                if city not in CITY_SEASON_SCHEDULE:
                    continue
                rows = conn.execute(
                    "SELECT season, COUNT(*) FROM forecast_errors "
                    "WHERE city = ? GROUP BY season ORDER BY season",
                    (city,),
                ).fetchall()
                dist = ", ".join(f"s{s}={n}" for s, n in rows)
                print(f"  {city:<14}: {dist}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
