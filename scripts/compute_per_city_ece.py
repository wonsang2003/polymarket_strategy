"""Augment reliability.json with per-city ECE shrinkage.

Apr 25 2026 — extends the existing reliability infrastructure (#52,
fit_reliability.py + reliability.py) with per-city Expected Calibration
Error from the quantile pricer's honest OOS evaluation.

Why both ECE and Brier?
  - Brier = (predicted - actual)² mean. Penalizes both miscalibration
    AND lack of sharpness. A "shrug" model that predicts 0.5 forever
    has high Brier but ZERO calibration error.
  - ECE = |predicted_mean - actual_freq| weighted by bin size.
    Pure calibration metric. Doesn't penalize sharpness.
  Trading needs both to be good — sharpness without calibration is
  random, calibration without sharpness is shrug. So our reliability
  multiplier multiplies them independently.

Bayesian shrinkage (k=50):
    ECE_shrunk(city) = (n_test × ECE_city + k × ECE_aggregate) / (n_test + k)

  At n=50: 50/50 weight between city and aggregate.
  At n=125: 71/29 — leans city-specific.
  At n=20: 29/71 — pulls hard toward aggregate (insufficient data).
  This is mathematically equivalent to a Beta-Binomial conjugate prior
  with effective prior strength k.

Output: extends reliability.json with `ece_shrunk` and `n_test` fields
on each per_city entry. Existing `brier` and `n_samples` fields are
preserved untouched for backward compatibility.

Run on EC2 nightly via cron after honest_ece.py:
    venv/bin/python scripts/compute_per_city_ece.py
    venv/bin/python scripts/compute_per_city_ece.py --shrinkage-k 100
    venv/bin/python scripts/compute_per_city_ece.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HONEST_ECE = REPO_ROOT / "data" / "weather" / "honest_ece_report.json"
DEFAULT_RELIABILITY = REPO_ROOT / "data" / "weather" / "reliability.json"


def shrunk_ece(
    ece_city: float, n_test: int, ece_aggregate: float, k: int
) -> float:
    """Bayesian shrinkage toward aggregate prior."""
    if n_test <= 0:
        return float(ece_aggregate)
    return (n_test * ece_city + k * ece_aggregate) / (n_test + k)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--honest-ece", default=str(DEFAULT_HONEST_ECE),
        help="Path to honest_ece_report.json (output of scripts/honest_ece.py)",
    )
    parser.add_argument(
        "--reliability", default=str(DEFAULT_RELIABILITY),
        help="Path to reliability.json (will be extended in place)",
    )
    parser.add_argument(
        "--shrinkage-k", type=int, default=50,
        help="Prior strength for Bayesian shrinkage (default 50)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    honest_path = Path(args.honest_ece)
    rel_path = Path(args.reliability)

    if not honest_path.exists():
        print(f"[per_city_ece] honest ECE report missing: {honest_path}",
              file=sys.stderr)
        print(f"[per_city_ece] run: venv/bin/python scripts/honest_ece.py "
              f"--leads 24 --external-climatology data/weather/climatology.json",
              file=sys.stderr)
        return 1

    with open(honest_path) as f:
        honest = json.load(f)

    aggregate_ece = float(honest.get("aggregate_ece", 0.10))
    per_bucket = honest.get("per_bucket", []) or []
    if not per_bucket:
        print(f"[per_city_ece] no per_bucket data in {honest_path}",
              file=sys.stderr)
        return 2

    # Index per_bucket by (city, lead) for lookup
    bucket_index: dict[tuple[str, int], dict] = {}
    for b in per_bucket:
        try:
            city = str(b["city"])
            lead = int(b["lead"])
            bucket_index[(city, lead)] = b
        except (KeyError, TypeError, ValueError):
            continue

    # Load existing reliability.json (must exist; we extend in place)
    if rel_path.exists():
        with open(rel_path) as f:
            reliability = json.load(f)
    else:
        # Fresh file — minimal scaffold
        reliability = {
            "fit_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_csvs": {},
            "per_city": {},
            "summary": {},
        }

    per_city = reliability.setdefault("per_city", {})
    n_updated = 0
    n_added = 0

    # Annotate each (city, lead) entry with ece_shrunk + n_test
    for (city, lead), b in bucket_index.items():
        try:
            ece_raw = float(b["ece"])
            n_test = int(b["n_test"])
        except (KeyError, TypeError, ValueError):
            continue
        ece_shrunk_value = shrunk_ece(ece_raw, n_test, aggregate_ece, args.shrinkage_k)
        lead_str = str(lead)

        if city not in per_city:
            per_city[city] = {}
            n_added += 1
        if lead_str not in per_city[city]:
            per_city[city][lead_str] = {}
        per_city[city][lead_str].update({
            "ece_raw": round(ece_raw, 6),
            "ece_shrunk": round(ece_shrunk_value, 6),
            "n_test": n_test,
        })
        n_updated += 1

    # Top-level metadata for the ECE pass
    reliability["per_city_ece_meta"] = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_honest_ece": str(honest_path),
        "aggregate_ece": round(aggregate_ece, 6),
        "shrinkage_k": args.shrinkage_k,
        "n_buckets_updated": n_updated,
    }

    # Print summary
    print(f"[per_city_ece] aggregate ECE prior: {aggregate_ece:.4f}")
    print(f"[per_city_ece] shrinkage k: {args.shrinkage_k}")
    print(f"[per_city_ece] cities annotated: {n_updated}")
    print()
    print(f"  {'city':16s}{'lead':>5s}{'n_test':>8s}{'ece_raw':>10s}{'ece_shrunk':>12s}")
    for city in sorted(per_city.keys()):
        for lead_str in sorted(per_city[city].keys()):
            entry = per_city[city][lead_str]
            if "ece_shrunk" not in entry:
                continue
            print(f"  {city:16s}{lead_str:>5s}{entry.get('n_test',0):>8}"
                  f"{entry.get('ece_raw',0):>10.4f}{entry.get('ece_shrunk',0):>12.4f}")

    if args.dry_run:
        print("\n[per_city_ece] --dry-run: NOT writing")
        return 0

    rel_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rel_path, "w") as f:
        json.dump(reliability, f, indent=2)
    print(f"\n[per_city_ece] wrote {rel_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
