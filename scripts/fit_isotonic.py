"""Fit isotonic regression calibration curves from walk-forward output.

Motivation (Apr 24 2026 — see CLAUDE.md §14 priority 7c and Apr 24 dev plan):
  Live paper results show the model is 3-4x overconfident on low-p trades
  (predicted 26% / actual 6% hit rate at p<30%; predicted 33% / actual 10%
  at 30-50%). Walk-forward reliability diagrams show the same pattern in
  mid-confidence bins.

  The root cause is a mix of (a) under-dispersed σ from reanalysis-biased
  calibration, (b) garbage fits from broken skew_normal optimizers, and
  (c) selection bias from the gate itself. Fixes to each of these are in
  flight, but they're structural changes that take weeks to validate.

  Isotonic regression is the post-hoc calibration safety net that works
  TODAY without depending on any of the structural fixes landing. It
  takes (predicted_probability, actual_outcome) pairs from our walk-
  forward backtest and learns a monotonic remapping:
      calibrated_p = isotonic(predicted_p)
  Applied as the LAST step in bracket_probability() it corrects for
  systematic bias without changing any upstream math.

Usage:
  python scripts/fit_isotonic.py                 # fit from both leads, write JSON
  python scripts/fit_isotonic.py --dry-run       # report what would be written
  python scripts/fit_isotonic.py --min-samples 200
                                                 # raise threshold for per-city fits

Output:
  data/weather/isotonic_calibration.json — structure:
    {
      "fit_at_utc": "2026-04-24T17:30:00+00:00",
      "source": "tools/walk_forward/last_run_{24,48}h.csv",
      "global": {
          "24": {"x": [...], "y": [...], "n": 12345},
          "48": {"x": [...], "y": [...], "n": 12345}
      },
      "per_city": {
          "nyc": {"24": {...}, "48": {...}},
          ...
      }
    }

The per-city calibrations are used when n_samples >= min_samples.
Otherwise the inference path falls back to the global calibration for
that lead, then to identity (no correction) if even the global fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_24 = REPO_ROOT / "tools" / "walk_forward" / "last_run_24h.csv"
DEFAULT_CSV_48 = REPO_ROOT / "tools" / "walk_forward" / "last_run_48h.csv"
DEFAULT_OUT = REPO_ROOT / "data" / "weather" / "isotonic_calibration.json"


def fit_isotonic(pred: np.ndarray, obs: np.ndarray) -> dict:
    """Fit IsotonicRegression and return serializable (x, y) knots.

    Inference-side uses np.interp(raw_p, x, y) to reproduce exactly what
    sklearn does internally — this means we don't need sklearn at inference
    time and the JSON is portable.

    Returns:
        {"x": [...], "y": [...], "n": int, "brier_before": float, "brier_after": float}
    """
    pred = np.asarray(pred, dtype=np.float64)
    obs = np.asarray(obs, dtype=np.float64)
    # Drop any NaNs that might have slipped through from partial rows
    mask = np.isfinite(pred) & np.isfinite(obs)
    pred = pred[mask]
    obs = obs[mask]
    n = len(pred)
    if n < 20:
        return {
            "x": [0.0, 1.0],
            "y": [0.0, 1.0],
            "n": n,
            "brier_before": float("nan"),
            "brier_after": float("nan"),
            "note": "insufficient samples — identity fallback",
        }

    ir = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    ir.fit(pred, obs)
    # Extract the piecewise-linear knots. These are the minimum data needed
    # to reproduce ir.predict() at inference time via np.interp.
    x_knots = ir.X_thresholds_.tolist()
    y_knots = ir.y_thresholds_.tolist()

    brier_before = float(np.mean((pred - obs) ** 2))
    calibrated = ir.predict(pred)
    brier_after = float(np.mean((calibrated - obs) ** 2))

    return {
        "x": x_knots,
        "y": y_knots,
        "n": int(n),
        "brier_before": brier_before,
        "brier_after": brier_after,
    }


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"[fit_isotonic] WARN missing: {path}", file=sys.stderr)
        return pd.DataFrame(columns=["city", "predicted_prob", "outcome"])
    df = pd.read_csv(path)
    # Some legacy rows may be NaN; drop
    df = df.dropna(subset=["predicted_prob", "outcome"]).copy()
    df["predicted_prob"] = df["predicted_prob"].astype(float)
    df["outcome"] = df["outcome"].astype(float)
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--csv-24", default=str(DEFAULT_CSV_24))
    parser.add_argument("--csv-48", default=str(DEFAULT_CSV_48))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--min-samples",
        type=int,
        default=150,
        help="Minimum samples to fit a per-city isotonic curve (default: 150)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df24 = load_csv(Path(args.csv_24))
    df48 = load_csv(Path(args.csv_48))
    print(f"[fit_isotonic] 24h rows: {len(df24)}  48h rows: {len(df48)}")

    result: dict = {
        "fit_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "24": str(args.csv_24),
            "48": str(args.csv_48),
        },
        "min_samples_per_city": args.min_samples,
        "global": {},
        "per_city": {},
    }

    # ---- global fits (all cities pooled, one per lead)
    for lead_key, df in [("24", df24), ("48", df48)]:
        if len(df) == 0:
            continue
        fit = fit_isotonic(df["predicted_prob"].values, df["outcome"].values)
        result["global"][lead_key] = fit
        print(
            f"[fit_isotonic] global lead={lead_key}h  n={fit['n']}  "
            f"brier {fit['brier_before']:.4f} → {fit['brier_after']:.4f}"
        )

    # ---- per-city fits
    all_cities = sorted(set(df24["city"].unique().tolist() + df48["city"].unique().tolist()))
    for city in all_cities:
        per_city_fits: dict = {}
        for lead_key, df in [("24", df24), ("48", df48)]:
            city_df = df[df["city"] == city]
            if len(city_df) < args.min_samples:
                continue
            fit = fit_isotonic(
                city_df["predicted_prob"].values,
                city_df["outcome"].values,
            )
            per_city_fits[lead_key] = fit
        if per_city_fits:
            result["per_city"][city] = per_city_fits
            for lead_key, fit in per_city_fits.items():
                print(
                    f"[fit_isotonic] {city:<16} lead={lead_key}h  n={fit['n']:>5}  "
                    f"brier {fit['brier_before']:.4f} → {fit['brier_after']:.4f}  "
                    f"(Δ {fit['brier_before'] - fit['brier_after']:+.4f})"
                )

    n_per_city = sum(len(v) for v in result["per_city"].values())
    n_global = len(result["global"])
    print(
        f"[fit_isotonic] summary: {n_global} global fits, "
        f"{len(result['per_city'])} cities × {n_per_city} per-(city,lead) fits"
    )

    if args.dry_run:
        print("[fit_isotonic] --dry-run: NOT writing output")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)
    print(f"[fit_isotonic] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
