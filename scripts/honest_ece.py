"""HONEST out-of-sample ECE measurement.

Apr 25 2026 — diagnosing whether the naive measure_ece.py 5.44% ECE
is real or an artifact of in-sample leakage. This script enforces:

  1. Temporal train/test split (NOT random).
       - Train rows: obs_date < CUTOFF (default = 2026-03-15, ~5 weeks before today)
       - Test rows : obs_date >= CUTOFF
  2. Climatology built from TRAINING data only.
       - Naive script builds climatology from ALL observations including
         test-period rows. With only ~1 obs per (city, doy), the
         `climo_mean` feature ≈ test-day's actual observed temp →
         features encode the label.
  3. Model skill index built from TRAINING data only.
  4. Synthetic ±2°F brackets centered on the FORECAST (same geometry
     as the live signal pipeline + naive measure_ece, so the comparison
     is apples-to-apples).

Reports per-bucket and aggregate ECE alongside the naive number so the
gap quantifies how much was leakage.

Run on EC2:
    venv/bin/python scripts/honest_ece.py [--cutoff 2026-03-15]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.features import (
    ClimatologyLookup,
    build_feature_vector,
    day_of_year,
    reset_climatology_for_tests,
    reset_model_skill_for_tests,
)
from polymarket_strat.domain.weather.quantile_pricing import (
    QuantileBracketPricer,
    train_quantile_models_for_bucket,
)


DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
DEFAULT_CUTOFF = "2026-03-15"


def build_train_only_climatology(
    conn: sqlite3.Connection, cutoff: date
) -> dict:
    """Same as scripts/train_quantile_models.py:build_climatology but
    only uses observations from before cutoff."""
    rows = conn.execute(
        "SELECT city, obs_date, observed_high_f FROM observations "
        "WHERE observed_high_f IS NOT NULL AND obs_date < ?",
        (cutoff.isoformat(),),
    ).fetchall()

    by_city_doy: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    by_city_yr: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        try:
            d = date.fromisoformat(str(row["obs_date"])[:10])
        except Exception:
            continue
        doy = day_of_year(d)
        by_city_doy[row["city"]][doy].append(float(row["observed_high_f"]))
        by_city_yr[row["city"]].append(float(row["observed_high_f"]))

    out_doy = {}
    for city, by_doy in by_city_doy.items():
        out_doy[city] = {
            str(doy): {
                "mean": float(np.mean(vals)),
                "std": float(np.std(vals, ddof=1)) if len(vals) > 1 else 5.0,
                "n": len(vals),
            }
            for doy, vals in by_doy.items()
        }

    out_yr = {}
    for city, vals in by_city_yr.items():
        out_yr[city] = {
            "mean": float(np.mean(vals)),
            "std": float(np.std(vals, ddof=1)) if len(vals) > 1 else 15.0,
            "n": len(vals),
        }
    return {"by_city_doy": out_doy, "by_city_yearmean": out_yr}


def fetch_rows_with_split(
    conn: sqlite3.Connection, city: str, lead: int, cutoff: date
) -> tuple[list[dict], list[dict]]:
    """Same join as train_quantile_models.fetch_training_rows but
    splits by obs_date < cutoff (train) vs >= cutoff (test)."""
    rows = conn.execute(
        """
        SELECT
            fe.city, fe.model, fe.regime, fe.lead_hours, fe.error_f,
            fe.obs_date, fe.season,
            (SELECT f.forecast_high_f FROM forecasts f
             WHERE f.city = fe.city AND f.model = fe.model
               AND date(f.valid_time) = fe.obs_date
               AND f.lead_hours = fe.lead_hours LIMIT 1) AS forecast_high_f,
            (SELECT f.ensemble_spread_f FROM forecasts f
             WHERE f.city = fe.city AND f.model = fe.model
               AND date(f.valid_time) = fe.obs_date
               AND f.lead_hours = fe.lead_hours LIMIT 1) AS ensemble_spread_f
        FROM forecast_errors fe
        WHERE fe.city = ? AND fe.lead_hours = ?
        ORDER BY fe.obs_date
        """,
        (city, lead),
    ).fetchall()

    train, test = [], []
    for row in rows:
        f_high = row["forecast_high_f"]
        if f_high is None:
            obs_row = conn.execute(
                "SELECT observed_high_f FROM observations WHERE city = ? AND obs_date = ?",
                (row["city"], row["obs_date"]),
            ).fetchone()
            if obs_row is None or obs_row["observed_high_f"] is None:
                continue
            f_high = float(obs_row["observed_high_f"]) + float(row["error_f"])
        item = {
            "city": row["city"],
            "model": row["model"],
            "regime": row["regime"] or "stable_high",
            "lead_hours": int(row["lead_hours"]),
            "error_f": float(row["error_f"]),
            "obs_date": str(row["obs_date"])[:10],
            "forecast_high_f": float(f_high),
            "ensemble_spread_f": float(row["ensemble_spread_f"] or 0.0),
            "observed_high_f": float(f_high) - float(row["error_f"]),
        }
        try:
            d = date.fromisoformat(item["obs_date"])
        except Exception:
            continue
        if d < cutoff:
            train.append(item)
        else:
            test.append(item)
    return train, test


def compute_ece(probs: list[float], outcomes: list[int], n_bins: int = 10):
    if not probs:
        return float("nan"), []
    p = np.asarray(probs)
    o = np.asarray(outcomes)
    edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    bins = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p <= hi if i == n_bins - 1 else p < hi)
        if not m.any():
            continue
        bp = float(p[m].mean())
        ba = float(o[m].mean())
        gap = abs(bp - ba)
        ece += gap * m.sum() / len(p)
        bins.append({
            "lo": float(lo), "hi": float(hi),
            "n": int(m.sum()),
            "predicted_mean": round(bp, 4),
            "actual_freq": round(ba, 4),
            "gap": round(gap, 4),
        })
    return ece, bins


def build_features_with_climo(
    rows: list[dict], climo: ClimatologyLookup, mask_climatology: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for r in rows:
        try:
            feat = build_feature_vector(
                city=r["city"], model=r["model"],
                forecast_high_f=r["forecast_high_f"],
                obs_date=date.fromisoformat(r["obs_date"]),
                lead_hours=r["lead_hours"],
                regime=r["regime"],
                ensemble_spread_f=r["ensemble_spread_f"],
                climo=climo,
                mask_climatology=mask_climatology,
            )
            if not all(np.isfinite(feat)):
                continue
            X.append(feat); y.append(r["error_f"])
        except Exception:
            continue
    return np.array(X, dtype=np.float64), np.array(y, dtype=np.float64)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--cutoff", default=DEFAULT_CUTOFF,
        help="Train < this date, test >= this date (ISO format)",
    )
    parser.add_argument(
        "--leads", type=lambda s: [int(x) for x in s.split(",")],
        default=[24],
        help="Lead hours to evaluate (default: 24 only)",
    )
    parser.add_argument("--min-train", type=int, default=80)
    parser.add_argument("--min-test", type=int, default=15)
    parser.add_argument(
        "--mask-climatology", action="store_true",
        help="Zero out climatology features in train and test "
             "(isolates how much the climo leak contributed to ECE)",
    )
    parser.add_argument(
        "--external-climatology", default=None,
        help="Path to a pre-built climatology JSON (e.g., from ERA5 1991-2020). "
             "When set, skip the internal train-only build and use this file. "
             "By construction this is leak-free if the date range doesn't "
             "overlap the training data window.",
    )
    args = parser.parse_args()

    cutoff = date.fromisoformat(args.cutoff)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[honest_ece] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    tmpdir = Path(tempfile.mkdtemp(prefix="honest_ece_"))
    if args.external_climatology:
        train_climo_path = Path(args.external_climatology)
        if not train_climo_path.exists():
            print(f"[honest_ece] external climatology not found: "
                  f"{train_climo_path}", file=sys.stderr)
            return 1
        print(f"[honest_ece] using external climatology: {train_climo_path}")
        with open(train_climo_path) as f:
            train_climo = json.load(f)
        n_climo_cells = sum(len(v) for v in train_climo["by_city_doy"].values())
    else:
        # Build train-only climatology in a tempdir so we don't overwrite prod
        train_climo_path = tmpdir / "climatology_train.json"
        print(f"[honest_ece] building train-only climatology (cutoff={cutoff}, "
              f"path={train_climo_path}) ...")
        train_climo = build_train_only_climatology(conn, cutoff)
        with open(train_climo_path, "w") as f:
            json.dump(train_climo, f)
        n_climo_cells = sum(len(v) for v in train_climo["by_city_doy"].values())
    print(f"[honest_ece] climatology cells: {n_climo_cells}")

    # Reset module singleton so feature builder reads our scoped climo
    reset_climatology_for_tests()
    reset_model_skill_for_tests()
    climo = ClimatologyLookup.from_json(str(train_climo_path))

    cities = sorted({r["city"] for r in conn.execute(
        "SELECT DISTINCT city FROM forecast_errors").fetchall()})

    per_bucket = []
    all_probs, all_outs = [], []

    for city in cities:
        for lead in args.leads:
            train_rows, test_rows = fetch_rows_with_split(conn, city, lead, cutoff)
            if len(train_rows) < args.min_train or len(test_rows) < args.min_test:
                print(f"[honest_ece]   {city}/{lead}h: SKIP "
                      f"(train={len(train_rows)}, test={len(test_rows)})")
                continue

            X_train, y_train = build_features_with_climo(
                train_rows, climo, mask_climatology=args.mask_climatology
            )
            X_test, y_test = build_features_with_climo(
                test_rows, climo, mask_climatology=args.mask_climatology
            )
            if len(X_train) < args.min_train or len(X_test) < args.min_test:
                continue

            try:
                # Use ALL training rows (no inner holdout — we have a real
                # test set now). Set holdout_frac small so we still get
                # conformal widening computed.
                artifact = train_quantile_models_for_bucket(
                    city=city, lead_hours=lead,
                    feature_matrix=X_train, error_targets=y_train,
                    holdout_frac=0.10,
                    max_iter=200,
                )
            except Exception as exc:
                print(f"[honest_ece]   {city}/{lead}h: TRAIN ERR {exc!r}")
                continue

            # Save to a tempdir so we can use the pricer interface
            ckpt = tmpdir / f"{city}_{lead}h.pkl"
            artifact.save(str(ckpt))
            pricer = QuantileBracketPricer(models_dir=str(tmpdir))

            # Score test rows: synthetic ±2°F bracket centered on forecast
            probs, outs = [], []
            for r in test_rows:
                lower_f = r["forecast_high_f"] - 2.0
                upper_f = r["forecast_high_f"] + 2.0
                try:
                    obs_d = date.fromisoformat(r["obs_date"])
                except Exception:
                    continue
                p = pricer.bracket_probability(
                    city=r["city"], model=r["model"],
                    forecast_high_f=r["forecast_high_f"],
                    obs_date=obs_d,
                    lead_hours=r["lead_hours"],
                    regime=r["regime"],
                    lower_f=lower_f, upper_f=upper_f,
                    ensemble_spread_f=r["ensemble_spread_f"],
                    apply_conformal=False,
                    mask_climatology=args.mask_climatology,
                )
                if p is None:
                    continue
                outcome = 1 if (lower_f <= r["observed_high_f"] < upper_f) else 0
                probs.append(p); outs.append(outcome)

            if not probs:
                continue

            ece, bins = compute_ece(probs, outs)
            brier = float(np.mean((np.array(probs) - np.array(outs))**2))
            per_bucket.append({
                "city": city, "lead": lead,
                "n_train": len(X_train), "n_test": len(probs),
                "ece": round(ece, 4),
                "brier": round(brier, 4),
                "hit_rate": round(float(np.mean(outs)), 4),
                "mean_pred": round(float(np.mean(probs)), 4),
            })
            all_probs.extend(probs); all_outs.extend(outs)
            print(f"[honest_ece]   {city}/{lead}h: "
                  f"n_train={len(X_train):>4}  n_test={len(probs):>4}  "
                  f"ece={ece*100:.2f}%  brier={brier:.4f}")

            # Cleanup model file
            ckpt.unlink(missing_ok=True)

    agg_ece, agg_bins = compute_ece(all_probs, all_outs)
    agg_brier = (float(np.mean((np.array(all_probs) - np.array(all_outs))**2))
                 if all_probs else float("nan"))

    print()
    print("=" * 64)
    print("HONEST OOS ECE REPORT")
    print("=" * 64)
    print(f"  Cutoff (train < / test >=): {cutoff}")
    print(f"  Climatology built from train rows only (cells={n_climo_cells})")
    print(f"  Total holdout predictions:  {len(all_probs)}")
    print(f"  Aggregate ECE:              {agg_ece*100:.2f}%")
    print(f"  Aggregate Brier:            {agg_brier:.4f}")
    print()
    print(f"  Reliability bins:")
    print(f"    {'lo':>5}{'hi':>6}{'n':>7}{'pred':>8}{'actual':>8}{'gap':>7}")
    for b in agg_bins:
        print(f"    {b['lo']:>5.2f}{b['hi']:>6.2f}{b['n']:>7}"
              f"{b['predicted_mean']:>8.3f}{b['actual_freq']:>8.3f}"
              f"{b['gap']:>7.3f}")
    print()
    print(f"  Compare with naive measure_ece.py (random split + leaked climo):")
    print(f"    Naive aggregate ECE: 5.44%  (Apr 25 2026)")
    print()
    if agg_ece > 0.10:
        print(f"  → CONCLUSION: naive ECE was severely optimistic. True OOS")
        print(f"    {agg_ece*100:.1f}% means quantile pricer offers no real")
        print(f"    calibration advantage over parametric (~10% gap).")
    elif agg_ece > 0.07:
        print(f"  → CONCLUSION: naive ECE was modestly optimistic. True OOS")
        print(f"    {agg_ece*100:.1f}% is a real improvement but doesn't clear")
        print(f"    the 7% target. Consider wiring on with caution.")
    else:
        print(f"  → CONCLUSION: even with honest splits, OOS ECE is")
        print(f"    {agg_ece*100:.1f}% ≤ 7%. Pricer earns its keep.")

    # Persist
    out = {
        "cutoff": str(cutoff),
        "n_total": len(all_probs),
        "aggregate_ece": round(agg_ece, 4),
        "aggregate_brier": round(agg_brier, 4),
        "per_bucket": per_bucket,
        "reliability_bins": agg_bins,
    }
    out_path = REPO_ROOT / "data" / "weather" / "honest_ece_report.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Wrote {out_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
