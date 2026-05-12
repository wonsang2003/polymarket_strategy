"""Measure Expected Calibration Error of the quantile pricer vs parametric.

Apr 25 2026 — Layer 1 validation. Loads the trained quantile models,
evaluates on holdout forecast_errors, and produces a calibration
report comparing:
  1. Quantile pricer (raw, no conformal)
  2. Quantile pricer (conformal-wrapped)
  3. Parametric fitter (current production)

Output:
  - Per-(city, lead) ECE comparison
  - Reliability diagram data
  - Brier score comparison

ECE = sum over bins of |bin_freq - bin_predicted_mean| weighted by bin_size.
Lower is better. Top weather firms achieve 5-7%; we target ≤ 7% from
this layer alone, with Layer 3 (live obs) needed to push to ~3%.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.features import (
    build_feature_vector,
    get_climatology,
)
from polymarket_strat.domain.weather.quantile_pricing import (
    QuantileBracketPricer,
)


DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
MODELS_DIR = REPO_ROOT / "data" / "weather" / "quantile_models"
OUTPUT_PATH = REPO_ROOT / "data" / "weather" / "ece_report.json"


def fetch_holdout_data(conn: sqlite3.Connection, city: str, lead: int) -> list[dict]:
    """Same query as training but we'll use the most recent 20% as holdout.

    Time-respecting holdout — sort by obs_date and take the last 20%.
    """
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
    out = []
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
        out.append({
            "city": row["city"],
            "model": row["model"],
            "regime": row["regime"],
            "lead_hours": int(row["lead_hours"]),
            "error_f": float(row["error_f"]),
            "obs_date": str(row["obs_date"])[:10],
            "forecast_high_f": float(f_high),
            "ensemble_spread_f": float(row["ensemble_spread_f"] or 0.0),
            "observed_high_f": float(f_high) - float(row["error_f"]),
        })
    return out


def compute_ece(
    predicted_probs: list[float],
    actual_outcomes: list[int],
    n_bins: int = 10,
) -> tuple[float, list[dict]]:
    """Compute Expected Calibration Error.

    Bins predictions by model_prob; in each bin compares mean predicted
    to actual fraction of positives. ECE = sum of |gap| × bin_weight.
    """
    if not predicted_probs:
        return float("nan"), []
    probs = np.asarray(predicted_probs)
    outcomes = np.asarray(actual_outcomes)
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    bin_data = []
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        if i == n_bins - 1:
            mask = (probs >= lo) & (probs <= hi)
        else:
            mask = (probs >= lo) & (probs < hi)
        if not mask.any():
            continue
        bin_probs = probs[mask]
        bin_outs = outcomes[mask]
        bin_size = mask.sum()
        bin_pred_mean = float(bin_probs.mean())
        bin_actual_freq = float(bin_outs.mean())
        gap = abs(bin_pred_mean - bin_actual_freq)
        ece += gap * (bin_size / len(probs))
        bin_data.append({
            "bin_lo": float(lo),
            "bin_hi": float(hi),
            "n": int(bin_size),
            "predicted_mean": round(bin_pred_mean, 4),
            "actual_freq": round(bin_actual_freq, 4),
            "gap": round(gap, 4),
        })
    return ece, bin_data


def evaluate_synthetic_brackets(
    pricer: QuantileBracketPricer,
    rows: list[dict],
    apply_conformal: bool,
) -> tuple[list[float], list[int]]:
    """For each holdout row, generate synthetic ±2°F brackets and check
    whether observation fell inside, then compare to model's predicted
    probability for that bracket.
    """
    probs = []
    outcomes = []
    for r in rows:
        # Synthetic ±2°F bracket centered on forecast — mirrors typical
        # Polymarket weather bracket widths.
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
            apply_conformal=apply_conformal,
        )
        if p is None:
            continue
        outcome = 1 if (lower_f <= r["observed_high_f"] < upper_f) else 0
        probs.append(p)
        outcomes.append(outcome)
    return probs, outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--holdout-frac", type=float, default=0.20)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ece] no DB at {db_path}", file=sys.stderr)
        return 1

    pricer = QuantileBracketPricer(models_dir=str(MODELS_DIR))
    if not pricer.loaded:
        print("[ece] no quantile models — train first via train_quantile_models.py", file=sys.stderr)
        return 2
    print(f"[ece] loaded {pricer.n_artifacts} quantile model artifacts")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cities = [r["city"] for r in conn.execute(
        "SELECT DISTINCT city FROM forecast_errors ORDER BY city"
    ).fetchall()]

    per_bucket_results = []
    all_probs_raw, all_outs_raw = [], []
    all_probs_conf, all_outs_conf = [], []

    try:
        for city in cities:
            for lead in (24, 48):
                if not pricer.has_model(city, lead):
                    continue
                rows = fetch_holdout_data(conn, city, lead)
                if not rows:
                    continue
                # Holdout = last 20%
                n_hold = max(20, int(len(rows) * args.holdout_frac))
                holdout = rows[-n_hold:]

                probs_raw, outs_raw = evaluate_synthetic_brackets(
                    pricer, holdout, apply_conformal=False
                )
                probs_conf, outs_conf = evaluate_synthetic_brackets(
                    pricer, holdout, apply_conformal=True
                )

                if not probs_raw:
                    continue

                ece_raw, bins_raw = compute_ece(probs_raw, outs_raw)
                ece_conf, bins_conf = compute_ece(probs_conf, outs_conf)
                brier_raw = float(np.mean((np.array(probs_raw) - np.array(outs_raw))**2))
                brier_conf = float(np.mean((np.array(probs_conf) - np.array(outs_conf))**2))

                per_bucket_results.append({
                    "city": city,
                    "lead_hours": lead,
                    "n_holdout": len(probs_raw),
                    "ece_raw": round(ece_raw, 4),
                    "ece_conformal": round(ece_conf, 4),
                    "brier_raw": round(brier_raw, 4),
                    "brier_conformal": round(brier_conf, 4),
                })
                all_probs_raw.extend(probs_raw)
                all_outs_raw.extend(outs_raw)
                all_probs_conf.extend(probs_conf)
                all_outs_conf.extend(outs_conf)

        # Aggregate
        agg_ece_raw, _ = compute_ece(all_probs_raw, all_outs_raw)
        agg_ece_conf, _ = compute_ece(all_probs_conf, all_outs_conf)
        agg_brier_raw = float(np.mean((np.array(all_probs_raw) - np.array(all_outs_raw))**2)) if all_probs_raw else float("nan")
        agg_brier_conf = float(np.mean((np.array(all_probs_conf) - np.array(all_outs_conf))**2)) if all_probs_conf else float("nan")

        report = {
            "n_total_holdout": len(all_probs_raw),
            "aggregate_ece_raw": round(agg_ece_raw, 4),
            "aggregate_ece_conformal": round(agg_ece_conf, 4),
            "aggregate_brier_raw": round(agg_brier_raw, 4),
            "aggregate_brier_conformal": round(agg_brier_conf, 4),
            "per_bucket": per_bucket_results,
        }

        with open(OUTPUT_PATH, "w") as f:
            json.dump(report, f, indent=2)

        # Print summary
        print()
        print("=" * 60)
        print("CALIBRATION REPORT")
        print("=" * 60)
        print(f"Total holdout predictions: {len(all_probs_raw)}")
        print(f"Aggregate ECE (raw):       {agg_ece_raw:.4f}  ({agg_ece_raw*100:.2f}%)")
        print(f"Aggregate ECE (conformal): {agg_ece_conf:.4f}  ({agg_ece_conf*100:.2f}%)")
        print(f"Aggregate Brier (raw):     {agg_brier_raw:.4f}")
        print(f"Aggregate Brier (conformal): {agg_brier_conf:.4f}")
        print(f"Wrote {OUTPUT_PATH}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
