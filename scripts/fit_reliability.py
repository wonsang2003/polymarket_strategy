"""Compute per-(city, lead) reliability index for position sizing.

Apr 24 2026 — Tier 2b of Apr 24 dev plan. See CLAUDE.md §15.3.1 and
reliability.py for motivation.

For each (city, lead_hours) pair compute:
    brier    = mean Brier score over walk-forward (predicted, outcome) pairs
    n_samples = number of real-forecast errors in forecast_errors DB table
                for this (city, any model, this lead)

Output JSON used at runtime by reliability.CityReliability to shrink
position sizing for noisy / small-sample cities.

Usage:
    python scripts/fit_reliability.py
    python scripts/fit_reliability.py --dry-run
    python scripts/fit_reliability.py --db data/weather/weather.db \
                                      --csv-24 tools/walk_forward/last_run_24h.csv \
                                      --csv-48 tools/walk_forward/last_run_48h.csv

Output shape:
    {
      "fit_at_utc": "...",
      "source_csvs": {"24": "...", "48": "..."},
      "source_db": "data/weather/weather.db",
      "per_city": {
          "nyc": {
              "24": {"brier": 0.149, "n_samples": 91},
              "48": {"brier": 0.196, "n_samples": 91}
          },
          ...
      },
      "summary": {
          "best_24h": ["wellington", 0.098],
          "worst_24h": ["toronto", 0.132],
          ...
      }
    }
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_24 = REPO_ROOT / "tools" / "walk_forward" / "last_run_24h.csv"
DEFAULT_CSV_48 = REPO_ROOT / "tools" / "walk_forward" / "last_run_48h.csv"
DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
DEFAULT_OUT = REPO_ROOT / "data" / "weather" / "reliability.json"


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"[fit_reliability] WARN missing {path}", file=sys.stderr)
        return pd.DataFrame(columns=["city", "predicted_prob", "outcome"])
    df = pd.read_csv(path)
    return df.dropna(subset=["predicted_prob", "outcome", "city"]).copy()


def sample_counts_from_db(db_path: Path, lead_hours: int) -> dict[str, int]:
    """n_samples per city at the given lead from forecast_errors table.

    We sum across models because the reliability multiplier is per-city,
    not per-(city, model). The GFS and ECMWF samples both contribute to
    our confidence in the city's calibration."""
    if not db_path.exists():
        print(f"[fit_reliability] WARN no DB at {db_path}", file=sys.stderr)
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT city, COUNT(*) AS n FROM forecast_errors "
            "WHERE lead_hours = ? GROUP BY city",
            (lead_hours,),
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--csv-24", default=str(DEFAULT_CSV_24))
    parser.add_argument("--csv-48", default=str(DEFAULT_CSV_48))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df24 = load_csv(Path(args.csv_24))
    df48 = load_csv(Path(args.csv_48))
    n24 = sample_counts_from_db(Path(args.db), 24)
    n48 = sample_counts_from_db(Path(args.db), 48)

    result: dict = {
        "fit_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_csvs": {"24": str(args.csv_24), "48": str(args.csv_48)},
        "source_db": str(args.db),
        "per_city": {},
        "summary": {},
    }

    all_cities = sorted(
        set(df24["city"].unique().tolist()) |
        set(df48["city"].unique().tolist()) |
        set(n24.keys()) | set(n48.keys())
    )

    for city in all_cities:
        per_lead: dict = {}
        for lead_str, df, n_map in [("24", df24, n24), ("48", df48, n48)]:
            city_df = df[df["city"] == city]
            if len(city_df) == 0:
                continue
            # Brier = mean((pred - outcome)^2)
            brier = float(((city_df["predicted_prob"] - city_df["outcome"]) ** 2).mean())
            n_samples = int(n_map.get(city, 0))
            per_lead[lead_str] = {
                "brier": round(brier, 6),
                "n_samples": n_samples,
                "n_walkforward_pairs": int(len(city_df)),
            }
        if per_lead:
            result["per_city"][city] = per_lead

    # Summary: best / worst per lead
    for lead_str in ("24", "48"):
        ranked = sorted(
            (
                (city, per_lead[lead_str]["brier"])
                for city, per_lead in result["per_city"].items()
                if lead_str in per_lead
            ),
            key=lambda x: x[1],
        )
        if ranked:
            result["summary"][f"best_{lead_str}h"] = list(ranked[0])
            result["summary"][f"worst_{lead_str}h"] = list(ranked[-1])

    # Print summary to stderr for ops visibility
    print(f"[fit_reliability] fit {len(result['per_city'])} cities")
    for lead_str in ("24", "48"):
        ranked = sorted(
            (
                (city, per_lead[lead_str]["brier"], per_lead[lead_str]["n_samples"])
                for city, per_lead in result["per_city"].items()
                if lead_str in per_lead
            ),
            key=lambda x: x[1],
        )
        if ranked:
            print(
                f"[fit_reliability] {lead_str}h: best={ranked[0][0]} "
                f"brier={ranked[0][1]:.4f} n={ranked[0][2]}  "
                f"worst={ranked[-1][0]} brier={ranked[-1][1]:.4f} n={ranked[-1][2]}"
            )

    if args.dry_run:
        print("[fit_reliability] --dry-run: NOT writing output")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)
    print(f"[fit_reliability] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
