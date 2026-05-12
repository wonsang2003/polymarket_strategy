"""Tail-focused ECE audit with fine [0,0.05]/[0.05,0.10]/.../[0.95,1.0] bins.

Apr 25 2026 — Layer 1 of the late-entry tail-bracket NO strategy. Before
we can trade tail brackets at 5-15c market price, we need to know whether
the model is well-calibrated in those probability ranges. Standard
honest_ece with 10 equal bins puts only ~30-50 samples per tail bin,
giving ±10% CI per gap estimate. That's not enough to greenlight or
redlight a tail strategy.

Approach:
  1. Same temporal split + train-only climatology as honest_ece.
  2. Per test row, evaluate MULTIPLE synthetic brackets (8 offsets per
     row, intentionally including tail brackets):
        narrow center:    (-1, +1)
        wide center:      (-3, +3)
        cold near-miss:   (-5, -2)
        hot near-miss:    (+2, +5)
        far cold tail:    (-10, -5)
        far hot tail:     (+5, +10)
        one-sided cold:   (-100, -3)   # likely YES (forecast above this)
        one-sided hot:    (+3, +100)   # likely YES if very hot, else NO tail
     Each row produces 8 (predicted, outcome) pairs. With ~2200 test
     rows that's ~17K evaluations vs 2200 in regular honest_ece.
  3. Bin into finer edges (12 bins instead of 10):
        [0.0, 0.05], [0.05, 0.10], [0.10, 0.15], [0.15, 0.30],
        [0.30, 0.40], [0.40, 0.50], [0.50, 0.60], [0.60, 0.70],
        [0.70, 0.85], [0.85, 0.90], [0.90, 0.95], [0.95, 1.0]
     Tail bins get ~500+ samples → ±2-3% CI on gap.

Output: data/weather/tail_ece_audit.json with per-bin n, predicted_mean,
actual_freq, gap. Decision matrix at the end:
  - All tail bins gap < 5%   → tail strategy is safe to ship
  - Tail bins gap 5-10%      → needs tail-specific isotonic correction
  - Tail bins gap > 10%      → DO NOT ship tail strategy. Redo training.

Run on EC2:
    venv/bin/python scripts/tail_ece_audit.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.features import (
    ClimatologyLookup,
    build_feature_vector,
    reset_climatology_for_tests,
    reset_model_skill_for_tests,
)
from polymarket_strat.domain.weather.quantile_pricing import (
    QuantileBracketPricer,
    train_quantile_models_for_bucket,
)


DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
DEFAULT_CUTOFF = "2026-03-15"
DEFAULT_OUT = REPO_ROOT / "data" / "weather" / "tail_ece_audit.json"
DEFAULT_CLIMO = REPO_ROOT / "data" / "weather" / "climatology.json"

# Fine bins — extra resolution at the tails
FINE_BIN_EDGES = [
    0.00, 0.05, 0.10, 0.15, 0.30, 0.40, 0.50,
    0.60, 0.70, 0.85, 0.90, 0.95, 1.00,
]

# Bracket offsets (in °F) — generate 8 brackets per test row.
# (lower_offset, upper_offset) relative to forecast value.
# Including "wide tail" bounds (-100, +100) approximates one-sided brackets
# while still staying within the synthetic-evaluation framework.
BRACKET_OFFSETS = [
    (-1.0, 1.0),     # narrow center
    (-3.0, 3.0),     # wide center
    (-5.0, -2.0),    # cold near-miss
    (2.0, 5.0),      # hot near-miss
    (-10.0, -5.0),   # cold tail
    (5.0, 10.0),     # hot tail
    (-100.0, -3.0),  # one-sided cold (forecast above → low YES prob → NO tail)
    (3.0, 100.0),    # one-sided hot (forecast below → low YES prob → NO tail)
]


# Inline helpers from honest_ece (avoid sys.path gymnastics)

from collections import defaultdict


def build_train_only_climatology(conn, cutoff: date) -> dict:
    rows = conn.execute(
        "SELECT city, obs_date, observed_high_f FROM observations "
        "WHERE observed_high_f IS NOT NULL AND obs_date < ?",
        (cutoff.isoformat(),),
    ).fetchall()
    by_city_doy: dict = defaultdict(lambda: defaultdict(list))
    by_city_yr: dict = defaultdict(list)
    from polymarket_strat.domain.weather.features import day_of_year
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
        out_yr[city] = {"mean": float(np.mean(vals)),
                         "std": float(np.std(vals, ddof=1)) if len(vals) > 1 else 15.0,
                         "n": len(vals)}
    return {"by_city_doy": out_doy, "by_city_yearmean": out_yr}


def fetch_rows_with_split(conn, city: str, lead: int, cutoff: date):
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
            "city": row["city"], "model": row["model"],
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
        (train if d < cutoff else test).append(item)
    return train, test


def build_features_with_climo(rows, climo, mask_climatology=False):
    X, y = [], []
    for r in rows:
        try:
            feat = build_feature_vector(
                city=r["city"], model=r["model"],
                forecast_high_f=r["forecast_high_f"],
                obs_date=date.fromisoformat(r["obs_date"]),
                lead_hours=r["lead_hours"], regime=r["regime"],
                ensemble_spread_f=r["ensemble_spread_f"],
                climo=climo, mask_climatology=mask_climatology,
            )
            if not all(np.isfinite(feat)):
                continue
            X.append(feat); y.append(r["error_f"])
        except Exception:
            continue
    return np.array(X, dtype=np.float64), np.array(y, dtype=np.float64)


def compute_fine_ece(
    probs: list[float], outcomes: list[int],
    bin_edges: list[float] = FINE_BIN_EDGES,
):
    if not probs:
        return float("nan"), []
    p = np.asarray(probs)
    o = np.asarray(outcomes)
    ece = 0.0
    bins = []
    for i in range(len(bin_edges) - 1):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        # Last bin uses inclusive hi
        if i == len(bin_edges) - 2:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        n = int(mask.sum())
        if n == 0:
            bins.append({"lo": float(lo), "hi": float(hi), "n": 0,
                         "predicted_mean": None, "actual_freq": None,
                         "gap": None, "ci_half_width": None})
            continue
        bp = float(p[mask].mean())
        ba = float(o[mask].mean())
        gap = abs(bp - ba)
        # Wilson approx CI for the actual_freq estimate (95%)
        ci_half = 1.96 * np.sqrt(max(ba * (1 - ba), 1e-9) / n)
        ece += gap * n / len(p)
        bins.append({
            "lo": float(lo), "hi": float(hi), "n": n,
            "predicted_mean": round(bp, 4),
            "actual_freq": round(ba, 4),
            "gap": round(gap, 4),
            "ci_half_width": round(float(ci_half), 4),
        })
    return ece, bins


def evaluate_tail_brackets(
    pricer: QuantileBracketPricer, test_rows: list[dict],
) -> tuple[list[float], list[int], list[dict]]:
    """For each test row, generate brackets at all BRACKET_OFFSETS and
    collect (predicted, outcome) pairs."""
    probs, outs = [], []
    per_offset_stats: dict[tuple[float, float], dict] = {}
    for off in BRACKET_OFFSETS:
        per_offset_stats[off] = {"n": 0, "sum_p": 0.0, "sum_y": 0.0}

    for r in test_rows:
        forecast = r["forecast_high_f"]
        try:
            obs_d = date.fromisoformat(r["obs_date"])
        except Exception:
            continue
        for off in BRACKET_OFFSETS:
            lo_off, hi_off = off
            lower_f = forecast + lo_off
            upper_f = forecast + hi_off
            p = pricer.bracket_probability(
                city=r["city"], model=r["model"],
                forecast_high_f=forecast, obs_date=obs_d,
                lead_hours=r["lead_hours"], regime=r["regime"],
                lower_f=lower_f, upper_f=upper_f,
                ensemble_spread_f=r["ensemble_spread_f"],
                apply_conformal=False,
            )
            if p is None:
                continue
            outcome = 1 if (lower_f <= r["observed_high_f"] < upper_f) else 0
            probs.append(p); outs.append(outcome)
            s = per_offset_stats[off]
            s["n"] += 1
            s["sum_p"] += p
            s["sum_y"] += outcome

    offset_summary = []
    for off, s in per_offset_stats.items():
        if s["n"] == 0:
            continue
        offset_summary.append({
            "lower_offset_f": off[0],
            "upper_offset_f": off[1],
            "n": s["n"],
            "mean_pred": round(s["sum_p"] / s["n"], 4),
            "mean_actual": round(s["sum_y"] / s["n"], 4),
        })
    return probs, outs, offset_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--leads", type=lambda s: [int(x) for x in s.split(",")], default=[24])
    parser.add_argument("--min-train", type=int, default=80)
    parser.add_argument("--min-test", type=int, default=15)
    parser.add_argument("--external-climatology", default=str(DEFAULT_CLIMO))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    cutoff = date.fromisoformat(args.cutoff)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[tail_ece] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Climatology setup
    tmpdir = Path(tempfile.mkdtemp(prefix="tail_ece_"))
    if Path(args.external_climatology).exists():
        train_climo_path = Path(args.external_climatology)
        with open(train_climo_path) as f:
            train_climo = json.load(f)
    else:
        train_climo_path = tmpdir / "climatology_train.json"
        train_climo = build_train_only_climatology(conn, cutoff)
        with open(train_climo_path, "w") as f:
            json.dump(train_climo, f)

    print(f"[tail_ece] using climatology: {train_climo_path}")

    reset_climatology_for_tests()
    reset_model_skill_for_tests()
    climo = ClimatologyLookup.from_json(str(train_climo_path))

    cities = sorted({r["city"] for r in conn.execute(
        "SELECT DISTINCT city FROM forecast_errors").fetchall()})

    all_probs, all_outs = [], []
    per_bucket = []
    aggregate_offset_stats: dict[tuple[float, float], list] = {}

    for city in cities:
        for lead in args.leads:
            train_rows, test_rows = fetch_rows_with_split(conn, city, lead, cutoff)
            if len(train_rows) < args.min_train or len(test_rows) < args.min_test:
                continue
            X_train, y_train = build_features_with_climo(train_rows, climo)
            if len(X_train) < args.min_train:
                continue
            try:
                artifact = train_quantile_models_for_bucket(
                    city=city, lead_hours=lead,
                    feature_matrix=X_train, error_targets=y_train,
                    holdout_frac=0.10, max_iter=200,
                )
            except Exception as exc:
                print(f"[tail_ece]   {city}/{lead}h: TRAIN ERR {exc!r}")
                continue
            ckpt = tmpdir / f"{city}_{lead}h.pkl"
            artifact.save(str(ckpt))
            pricer = QuantileBracketPricer(models_dir=str(tmpdir))

            probs, outs, offset_sum = evaluate_tail_brackets(pricer, test_rows)
            if not probs:
                continue

            for entry in offset_sum:
                key = (entry["lower_offset_f"], entry["upper_offset_f"])
                aggregate_offset_stats.setdefault(key, []).append(entry)

            ece, bins = compute_fine_ece(probs, outs)
            per_bucket.append({
                "city": city, "lead": lead,
                "n_eval": len(probs),
                "ece_fine": round(ece, 4),
            })
            all_probs.extend(probs); all_outs.extend(outs)
            print(f"[tail_ece]   {city}/{lead}h: n_eval={len(probs):>4}  "
                  f"ece_fine={ece*100:.2f}%")
            ckpt.unlink(missing_ok=True)

    agg_ece, agg_bins = compute_fine_ece(all_probs, all_outs)

    print()
    print("=" * 70)
    print("TAIL-FOCUSED ECE AUDIT")
    print("=" * 70)
    print(f"  Total bracket evaluations:  {len(all_probs)}")
    print(f"  Aggregate fine-bin ECE:     {agg_ece*100:.2f}%")
    print()
    print(f"  Reliability bins (FINE):")
    print(f"    {'lo':>5}{'hi':>6}{'n':>7}{'pred':>8}{'actual':>8}{'gap':>7}{'CI±':>8}")
    for b in agg_bins:
        if b["n"] == 0:
            print(f"    {b['lo']:>5.2f}{b['hi']:>6.2f}{0:>7}    --     --    --     --")
            continue
        print(f"    {b['lo']:>5.2f}{b['hi']:>6.2f}{b['n']:>7}"
              f"{b['predicted_mean']:>8.3f}{b['actual_freq']:>8.3f}"
              f"{b['gap']:>7.3f}{b['ci_half_width']:>8.3f}")

    # Decision matrix
    print()
    print(f"  Tail bins decision (the 4 outermost bins):")
    tail_bins = [b for b in agg_bins if b["n"] > 0 and (b["hi"] <= 0.15 or b["lo"] >= 0.85)]
    if not tail_bins:
        print(f"    INSUFFICIENT TAIL DATA — try more BRACKET_OFFSETS")
    else:
        worst_gap = max(tail_bins, key=lambda b: b["gap"])
        print(f"    {'lo':>5}{'hi':>6}{'n':>7}{'pred':>8}{'actual':>8}{'gap':>8}{'CI±':>7}")
        for b in tail_bins:
            print(f"    {b['lo']:>5.2f}{b['hi']:>6.2f}{b['n']:>7}"
                  f"{b['predicted_mean']:>8.3f}{b['actual_freq']:>8.3f}"
                  f"{b['gap']:>8.3f}{b['ci_half_width']:>7.3f}")
        print()
        max_gap = worst_gap["gap"]
        if max_gap < 0.05:
            print(f"  ✓ All tail gaps < 5%. Tail strategy SAFE to ship as-is.")
        elif max_gap < 0.10:
            print(f"  ~ Worst tail gap = {max_gap*100:.1f}% (5-10% range).")
            print(f"    Recommend TAIL-SPECIFIC isotonic correction before shipping.")
        else:
            print(f"  ✗ Worst tail gap = {max_gap*100:.1f}% > 10%. Do NOT ship.")
            print(f"    Investigate model bias in tails before any tail trades.")

    # Per-offset summary (which offsets land in which prob ranges)
    print()
    print(f"  Per-offset summary (which bracket geometry generates which probs):")
    print(f"    {'low':>6}{'high':>6}{'n_total':>10}{'mean_pred':>12}{'mean_actual':>14}")
    for key in BRACKET_OFFSETS:
        if key not in aggregate_offset_stats:
            continue
        entries = aggregate_offset_stats[key]
        total_n = sum(e["n"] for e in entries)
        if total_n == 0:
            continue
        mp = sum(e["mean_pred"] * e["n"] for e in entries) / total_n
        ma = sum(e["mean_actual"] * e["n"] for e in entries) / total_n
        print(f"    {key[0]:>6.1f}{key[1]:>6.1f}{total_n:>10}{mp:>12.3f}{ma:>14.3f}")

    # Persist
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "n_total": len(all_probs),
            "aggregate_fine_ece": round(agg_ece, 4),
            "fine_bins": agg_bins,
            "per_bucket": per_bucket,
            "bracket_offsets": [list(o) for o in BRACKET_OFFSETS],
        }, f, indent=2)
    print(f"\n  wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
