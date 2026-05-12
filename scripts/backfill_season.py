"""Back-fill `season` column on existing forecast_errors rows.

Apr 24 2026 — Q5-A support script. The `season` column was added to
forecast_errors with DEFAULT -1, meaning every pre-migration row has
the pooled sentinel. At inference we want to try season-specific
distributions FIRST and fall back to pooled. But we can't fit
season-specific distributions if the forecast_errors rows don't have
their season set. This script walks every row, derives season from
obs_date.month, and UPDATEs in place.

Idempotent — safe to re-run. Only touches rows with season=-1 (so a
second run is a no-op).

Usage:
    python scripts/backfill_season.py              # real run
    python scripts/backfill_season.py --dry-run    # report only

After running, call `polymarket-strat weather-calibrate` to refit
per-season distributions from the now-season-tagged forecast_errors.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"


def season_from_month(m: int) -> int:
    """Inline copy of domain/weather/season.py logic — standalone."""
    if m in (12, 1, 2):
        return 0  # winter
    if m in (3, 4, 5):
        return 1  # spring
    if m in (6, 7, 8):
        return 2  # summer
    return 3  # fall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[backfill_season] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Check if column exists
        cols = [r[1] for r in conn.execute("PRAGMA table_info(forecast_errors)")]
        if "season" not in cols:
            print(
                "[backfill_season] forecast_errors.season column missing — "
                "run a Python invocation that triggers WeatherDatabase.__init__ "
                "first to apply the migration.",
                file=sys.stderr,
            )
            return 2

        total = conn.execute("SELECT COUNT(*) FROM forecast_errors").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM forecast_errors WHERE season = -1"
        ).fetchone()[0]
        print(f"[backfill_season] total rows: {total}, pending: {pending}")

        if pending == 0:
            print("[backfill_season] nothing to do")
            return 0

        # Batch UPDATE by month. Fast: 12 statements total.
        # Using CAST(strftime('%m', obs_date) AS INTEGER) to parse the
        # ISO date stored in TEXT form (obs_date is TEXT in schema).
        season_month_ranges = {
            0: (12, 1, 2),
            1: (3, 4, 5),
            2: (6, 7, 8),
            3: (9, 10, 11),
        }
        total_updated = 0
        for season, months in season_month_ranges.items():
            placeholders = ",".join("?" for _ in months)
            sql = (
                f"UPDATE forecast_errors SET season = ? "
                f"WHERE season = -1 "
                f"AND CAST(strftime('%m', obs_date) AS INTEGER) IN ({placeholders})"
            )
            if args.dry_run:
                # Count what we WOULD update
                count_sql = (
                    f"SELECT COUNT(*) FROM forecast_errors "
                    f"WHERE season = -1 "
                    f"AND CAST(strftime('%m', obs_date) AS INTEGER) IN ({placeholders})"
                )
                n = conn.execute(count_sql, months).fetchone()[0]
                total_updated += n
                print(f"  season={season} (months {months}): would update {n} rows")
            else:
                cur = conn.execute(sql, (season,) + months)
                total_updated += cur.rowcount
                print(f"  season={season} (months {months}): updated {cur.rowcount} rows")

        if args.dry_run:
            print(f"[backfill_season] --dry-run: NOT committing. Total candidates: {total_updated}")
            return 0

        conn.commit()
        print(f"[backfill_season] committed {total_updated} updates")
        # Sanity — season distribution after
        rows = conn.execute(
            "SELECT season, COUNT(*) FROM forecast_errors GROUP BY season ORDER BY season"
        ).fetchall()
        print("[backfill_season] season distribution post-update:")
        for season, n in rows:
            print(f"  season={season}: {n}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
