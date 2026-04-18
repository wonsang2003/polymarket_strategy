"""Nightly SQLite backup with rotation.

Usage:
    python scripts/backup_db.py                    # default paths, keeps 14 days
    python scripts/backup_db.py --keep 30          # keep 30 days
    python scripts/backup_db.py --dest /mnt/nas/polymarket_backups
    python scripts/backup_db.py --dry-run          # show what would happen

Cron example (daily 04:15 local):
    15 4 * * * /usr/bin/python3 /abs/path/polymarket_strat/scripts/backup_db.py >> /var/log/polymarket_backup.log 2>&1
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path so we can import the package
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "weather" / "weather.db",
        help="Path to live weather.db (default: data/weather/weather.db)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=ROOT / "data" / "weather" / "backups",
        help="Backup directory (default: data/weather/backups)",
    )
    parser.add_argument("--keep", type=int, default=14, help="Number of backups to retain (default 14)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"[backup] source DB missing: {args.db}", file=sys.stderr)
        return 2

    args.dest.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = args.dest / f"weather_{timestamp}.db"

    if args.dry_run:
        print(f"[backup] DRY RUN: would back up {args.db} -> {target}")
    else:
        # Use online backup API via WeatherDatabase.backup() — safe during writes
        db = WeatherDatabase(args.db)
        try:
            db.backup(target)
        finally:
            db.close()
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"[backup] wrote {target.name} ({size_mb:.2f} MB)")

    # Rotation: keep the newest N files, delete older
    backups = sorted(args.dest.glob("weather_*.db"), reverse=True)
    stale = backups[args.keep :]
    for old in stale:
        if args.dry_run:
            print(f"[backup] DRY RUN: would delete {old.name}")
        else:
            old.unlink()
            print(f"[backup] deleted {old.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
