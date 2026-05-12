"""Train per-(city, lead) quantile regression models from forecast_errors.

Apr 25 2026 — Layer 1 of the calibration overhaul. Outputs:
  data/weather/quantile_models/{city}_{lead}h.pkl per (city, lead) bucket
  data/weather/climatology.json (per-city day-of-year normals)
  data/weather/model_skill.json (per-(city, model) skill brier)
  data/weather/quantile_training_metrics.json (ECE, pinball loss summary)

Usage:
    python scripts/train_quantile_models.py
    python scripts/train_quantile_models.py --cities nyc,seoul,tokyo
    python scripts/train_quantile_models.py --leads 24
    python scripts/train_quantile_models.py --skip-climo --skip-skill
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.features import (
    FEATURE_NAMES,
    N_FEATURES,
    build_feature_vector,
    day_of_year,
)
from polymarket_strat.domain.weather.quantile_pricing import (
    TAU_GRID,
    train_quantile_models_for_bucket,
)


DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
MODELS_DIR = REPO_ROOT / "data" / "weather" / "quantile_models"
CLIMO_PATH = REPO_ROOT / "data" / "weather" / "climatology.json"
SKILL_PATH = REPO_ROOT / "data" / "weather" / "model_skill.json"
METRICS_PATH = REPO_ROOT / "data" / "weather" / "quantile_training_metrics.json"


def build_climatology(conn: sqlite3.Connection) -> dict:
    """Compute per-(city, day_of_year) historical mean+std of observed
    highs from the observations table. Smoothed with ±3 day windows
    via the runtime lookup; here we just store raw aggregations."""
    print("[train] building climatology...")
    rows = conn.execute(
        "SELECT city, obs_date, observed_high_f FROM observations "
        "WHERE observed_high_f IS NOT NULL"
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


def build_model_skill(conn: sqlite3.Connection) -> dict:
    """Per-(city, model) Brier proxy from forecast_errors.

    We use σ² of error_f as a soft skill proxy — lower variance =
    more accurate model for that city. Normalize so the value is in
    a brier-comparable range."""
    print("[train] building model skill index...")
    rows = conn.execute(
        "SELECT city, model, error_f FROM forecast_errors "
        "WHERE lead_hours = 24"
    ).fetchall()

    by_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        by_pair[(row["city"], row["model"])].append(float(row["error_f"]))

    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (city, model), errs in by_pair.items():
        if len(errs) < 5:
            continue
        # Skill = std of error normalized to [0, 1] band where lower
        # is better. We use σ/(σ+5) so σ=2 → 0.286, σ=4 → 0.444,
        # σ=10 → 0.667. Bounded, monotonic.
        sigma = float(np.std(errs, ddof=1))
        skill = sigma / (sigma + 5.0)
        out[city][model] = round(skill, 4)

    return dict(out)


def fetch_training_rows(
    conn: sqlite3.Connection,
    city: str,
    lead_hours: int,
) -> list[dict]:
    """Pull all forecast_errors rows for a (city, lead) plus the
    matching forecast_high_f and ensemble_spread_f from the forecasts
    table.

    The join is approximate — we match on (city, model, obs_date) and
    pick the forecast row with the closest valid_time. For training
    purposes this is sufficient.
    """
    rows = conn.execute(
        """
        SELECT
            fe.city,
            fe.model,
            fe.regime,
            fe.lead_hours,
            fe.error_f,
            fe.obs_date,
            fe.season,
            -- subquery to pull the matching forecast value
            (SELECT f.forecast_high_f FROM forecasts f
             WHERE f.city = fe.city AND f.model = fe.model
               AND date(f.valid_time) = fe.obs_date
               AND f.lead_hours = fe.lead_hours
             LIMIT 1) AS forecast_high_f,
            (SELECT f.ensemble_spread_f FROM forecasts f
             WHERE f.city = fe.city AND f.model = fe.model
               AND date(f.valid_time) = fe.obs_date
               AND f.lead_hours = fe.lead_hours
             LIMIT 1) AS ensemble_spread_f
        FROM forecast_errors fe
        WHERE fe.city = ? AND fe.lead_hours = ?
        """,
        (city, lead_hours),
    ).fetchall()

    out = []
    for row in rows:
        # The forecast_high_f might be None if the forecast row was
        # never saved — we can derive it from observed + error since
        # observations table has them. For simplicity skip rows
        # without forecast_high_f.
        f_high = row["forecast_high_f"]
        if f_high is None:
            # Try to derive: forecast = observed + error
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
            "season": int(row["season"]) if row["season"] is not None else -1,
            "forecast_high_f": float(f_high),
            "ensemble_spread_f": float(row["ensemble_spread_f"] or 0.0),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--cities",
        type=lambda s: s.split(","),
        default=None,
        help="Comma-separated cities; default = all",
    )
    parser.add_argument(
        "--leads",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[24, 48],
        help="Lead hours to train (default 24,48)",
    )
    parser.add_argument("--skip-climo", action="store_true")
    parser.add_argument("--skip-skill", action="store_true")
    parser.add_argument("--min-samples", type=int, default=80)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[train] no DB at {db_path}", file=sys.stderr)
        return 1

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # ---- Step 1: climatology ----
        if not args.skip_climo:
            climo = build_climatology(conn)
            with open(CLIMO_PATH, "w") as f:
                json.dump(climo, f, indent=2)
            print(f"[train] wrote {CLIMO_PATH}: {len(climo['by_city_doy'])} cities")

        # ---- Step 2: model skill ----
        if not args.skip_skill:
            skill = build_model_skill(conn)
            with open(SKILL_PATH, "w") as f:
                json.dump(skill, f, indent=2)
            print(f"[train] wrote {SKILL_PATH}: {len(skill)} cities")

        # Reload caches so feature builder sees fresh data
        from polymarket_strat.domain.weather.features import (
            reset_climatology_for_tests,
            reset_model_skill_for_tests,
            get_climatology,
        )
        reset_climatology_for_tests()
        reset_model_skill_for_tests()
        climo_lookup = get_climatology(str(CLIMO_PATH))

        # ---- Step 3: discover (city, lead) pairs ----
        cities_in_db = [
            r["city"] for r in conn.execute(
                "SELECT DISTINCT city FROM forecast_errors ORDER BY city"
            ).fetchall()
        ]
        cities = args.cities or cities_in_db

        # ---- Step 4: train each bucket ----
        metrics = {"buckets": [], "summary": {}}
        n_trained = 0
        n_skipped = 0
        for city in cities:
            for lead in args.leads:
                rows = fetch_training_rows(conn, city, lead)
                if len(rows) < args.min_samples:
                    print(f"[train]   {city}/{lead}h: SKIP ({len(rows)} samples < {args.min_samples})")
                    n_skipped += 1
                    continue

                # Build feature matrix + target vector
                X_rows = []
                y_rows = []
                for r in rows:
                    try:
                        feat = build_feature_vector(
                            city=r["city"], model=r["model"],
                            forecast_high_f=r["forecast_high_f"],
                            obs_date=date.fromisoformat(r["obs_date"]),
                            lead_hours=r["lead_hours"],
                            regime=r["regime"],
                            ensemble_spread_f=r["ensemble_spread_f"],
                            climo=climo_lookup,
                        )
                        if any(np.isnan(feat)) or any(np.isinf(feat)):
                            continue
                        X_rows.append(feat)
                        y_rows.append(r["error_f"])
                    except Exception as exc:
                        print(f"[train]   {city}/{lead}h: row error: {exc!r}", file=sys.stderr)
                        continue

                if len(X_rows) < args.min_samples:
                    print(f"[train]   {city}/{lead}h: SKIP after feature build ({len(X_rows)})")
                    n_skipped += 1
                    continue

                X = np.array(X_rows, dtype=np.float64)
                y = np.array(y_rows, dtype=np.float64)

                try:
                    artifact = train_quantile_models_for_bucket(
                        city=city, lead_hours=lead,
                        feature_matrix=X, error_targets=y,
                    )
                    out_path = MODELS_DIR / f"{city}_{lead}h.pkl"
                    artifact.save(str(out_path))
                    n_trained += 1
                    metrics["buckets"].append({
                        "city": city,
                        "lead_hours": lead,
                        "n_samples": int(len(X)),
                        "pinball_loss": round(artifact.holdout_pinball_loss, 4),
                        "conformal_widening": round(artifact.conformal_widening, 4),
                    })
                    print(
                        f"[train]   {city}/{lead}h: TRAINED "
                        f"n={len(X):>4}  pinball={artifact.holdout_pinball_loss:.3f}  "
                        f"conformal={artifact.conformal_widening:.2f}°F"
                    )
                except Exception as exc:
                    print(f"[train]   {city}/{lead}h: TRAIN ERROR: {exc!r}", file=sys.stderr)
                    n_skipped += 1

        # ---- Step 5: summary ----
        metrics["summary"] = {
            "n_trained": n_trained,
            "n_skipped": n_skipped,
            "leads_trained": args.leads,
            "median_pinball": round(
                float(np.median([b["pinball_loss"] for b in metrics["buckets"]]))
                if metrics["buckets"] else 0.0,
                4,
            ),
            "median_conformal_widening": round(
                float(np.median([b["conformal_widening"] for b in metrics["buckets"]]))
                if metrics["buckets"] else 0.0,
                4,
            ),
        }
        with open(METRICS_PATH, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"[train] DONE. {n_trained} buckets trained, {n_skipped} skipped.")
        print(f"[train] median pinball loss: {metrics['summary']['median_pinball']:.4f}")
        print(f"[train] median conformal widening: {metrics['summary']['median_conformal_widening']:.2f}°F")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
