"""Walk-forward backtest of weather error-distribution calibration.

PURPOSE
-------
In-sample calibration stats lie: the distribution was fit on the test point.
This script measures what would have happened if we'd operated the system day
by day — at each evaluation date D:

    1. Refit error distribution using ONLY errors with obs_date < D
    2. Pull the forecast for D (from `forecasts` table)
    3. Pull the realized observation for D (from `observations` table)
    4. For a set of synthetic brackets around the forecast value, score the
       predicted bracket probability
    5. Compare to realized outcome (did the bracket hit?)

This is how you prove calibration is real vs. overfit. Aligns with CLAUDE.md
§14 Priority 4 (validate distributions) and §12 edge-source ranking (genuine
statistical skill, not data snooping).

METRICS
-------
* Brier score         = mean (P - outcome)²       lower is better; 0.25 = chance
* Log-loss            = mean -log(P_chosen)       lower is better; 0.693 = chance
* Reliability bins    = binned predicted prob vs actual hit rate (calibration curve)
* Sharpness           = mean |P - 0.5|            high = confident, must pair w/ calibration

USAGE
-----
    python tools/walk_forward/backtest.py \
        --city seoul --model gfs \
        --start 2025-10-01 --end 2026-04-15

    # Bayesian variant (slower, but gives posterior-widened σ)
    python tools/walk_forward/backtest.py \
        --city seoul --model gfs --bayesian \
        --start 2026-01-01 --end 2026-04-15

    # All cities + CSV output
    python tools/walk_forward/backtest.py \
        --all-cities --csv out.csv

NOTES
-----
* Requires at least `--min-train 30` errors in the training window before it
  will attempt to fit. Dates with insufficient training are skipped and
  counted in the report.
* Bracket geometry tested: ±1°F, ±2°F, ±3°F, ±5°F, ±10°F symmetric around
  forecast, plus one-sided "above forecast", "below forecast", "above forecast
  + 5°F", "below forecast - 5°F" — covering the shape range we see on
  Polymarket.
* This DOES NOT ingest any market prices. It's a pure calibration score:
  "given our fitted distribution, how sharp AND calibrated are the probs?"
  To measure live trading PnL, use backtest.py (the thin wrapper) or extend
  this with a bracket-scanner join.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Add project root
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_strat.domain.weather.calibration import ErrorDistributionFitter  # noqa: E402
from polymarket_strat.domain.weather.forecast import BracketProbabilityCalculator  # noqa: E402
from polymarket_strat.domain.weather.models import (  # noqa: E402
    CITY_REGISTRY,
    ErrorDistribution,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase  # noqa: E402


# ----------------------------------------------------------------------
# Synthetic bracket geometries
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class BracketShape:
    """A synthetic bracket defined relative to the forecast value."""
    name: str
    # (lower_offset, upper_offset) — bracket = [F + lower, F + upper)
    # Use -inf / +inf for one-sided.
    lower_offset: float
    upper_offset: float

    def bounds(self, forecast_f: float) -> tuple[float, float]:
        lo = forecast_f + self.lower_offset if math.isfinite(self.lower_offset) else -1e6
        hi = forecast_f + self.upper_offset if math.isfinite(self.upper_offset) else 1e6
        return lo, hi

    def contains(self, observed_f: float, forecast_f: float) -> bool:
        lo, hi = self.bounds(forecast_f)
        return lo <= observed_f < hi


BRACKET_SHAPES: list[BracketShape] = [
    # Symmetric exact-degree brackets (these are the narrowest, hardest to price)
    BracketShape("pm_1F",   -1.0,  1.0),
    BracketShape("pm_2F",   -2.0,  2.0),
    BracketShape("pm_3F",   -3.0,  3.0),
    BracketShape("pm_5F",   -5.0,  5.0),
    BracketShape("pm_10F", -10.0, 10.0),
    # One-sided wide brackets (Seoul 18C+ style)
    BracketShape("above_F",      0.0, math.inf),
    BracketShape("below_F",      -math.inf, 0.0),
    BracketShape("above_F+5",    5.0, math.inf),
    BracketShape("below_F-5",    -math.inf, -5.0),
]


# ----------------------------------------------------------------------
# Core walk-forward
# ----------------------------------------------------------------------

@dataclass
class WalkForwardResult:
    """Single (date, city, model, bracket) evaluation row."""
    eval_date: str
    city: str
    model: str
    bracket: str
    forecast_f: float
    observed_f: float
    lower_f: float
    upper_f: float
    predicted_prob: float
    outcome: int                # 1 if observed in bracket, else 0
    brier: float
    log_loss: float
    n_train: int
    dist_family: str
    dist_mu: float
    dist_sigma: float


def _load_errors_with_dates(
    db: WeatherDatabase,
    city: str,
    model: WeatherModel,
) -> list[tuple[float, date, str]]:
    """Return (error_f, obs_date, regime) triples, bypassing the filtered accessor.

    We need the date alongside the error value to filter train < D, and the
    regime label to fit per-regime distributions for regime-aware inference.
    """
    rows = db._conn.execute(
        "SELECT error_f, obs_date, regime FROM forecast_errors WHERE city = ? AND model = ? ORDER BY obs_date",
        (city, model.value),
    ).fetchall()
    out: list[tuple[float, date, str]] = []
    for r in rows:
        try:
            d = date.fromisoformat(r["obs_date"])
        except (TypeError, ValueError):
            continue
        regime = r["regime"] if r["regime"] else SynopticRegime.STABLE_HIGH.value
        out.append((float(r["error_f"]), d, regime))
    return out


def _regime_from_str(s: str) -> SynopticRegime:
    try:
        return SynopticRegime(s)
    except ValueError:
        return SynopticRegime.STABLE_HIGH


def _load_forecast_on_date(
    db: WeatherDatabase,
    city: str,
    model: WeatherModel,
    valid_date: date,
    lead_hours: int = 24,
) -> float | None:
    """Forecast for valid_date, preferring `forecasts` table, falling back to
    reconstruction via error_f + observed_f (since error = forecast - observed).

    The reconstruction is lossless for walk-forward purposes — `forecast_errors`
    is populated by `calibrate()` directly from the forecast API and the
    observation, so it IS the historical record. The `forecasts` table only
    gets populated for dates the live pipeline has polled.
    """
    rows = db._conn.execute(
        """SELECT forecast_high_f, init_time, lead_hours FROM forecasts
           WHERE city = ? AND model = ? AND valid_time LIKE ?
           ORDER BY ABS(lead_hours - ?) ASC, init_time DESC LIMIT 1""",
        (city, model.value, f"{valid_date.isoformat()}%", lead_hours),
    ).fetchall()
    if rows:
        return float(rows[0]["forecast_high_f"])

    # Fallback: reconstruct forecast = observed + error
    err_row = db._conn.execute(
        """SELECT error_f FROM forecast_errors
           WHERE city = ? AND model = ? AND lead_hours = ? AND obs_date = ?
           LIMIT 1""",
        (city, model.value, lead_hours, valid_date.isoformat()),
    ).fetchone()
    if err_row is None:
        # try any lead_hours if the specific one is missing
        err_row = db._conn.execute(
            """SELECT error_f FROM forecast_errors
               WHERE city = ? AND model = ? AND obs_date = ? LIMIT 1""",
            (city, model.value, valid_date.isoformat()),
        ).fetchone()
    if err_row is None:
        return None
    obs = db.get_observation(city, valid_date)
    if obs is None:
        return None
    return float(obs["observed_high_f"]) + float(err_row["error_f"])


def _compute_bracket_prob(
    calc: BracketProbabilityCalculator,
    dist: ErrorDistribution,
    forecast_f: float,
    shape: BracketShape,
) -> float:
    lo, hi = shape.bounds(forecast_f)
    return calc.bracket_probability(
        forecast_f=forecast_f,
        error_dist=dist,
        lower_f=lo,
        upper_f=hi,
    )


def walk_forward_city_model(
    db: WeatherDatabase,
    city: str,
    model: WeatherModel,
    *,
    start: date,
    end: date,
    min_train: int = 30,
    lead_hours: int = 24,
    use_bayesian: bool = False,
    regime_aware: bool = True,
) -> tuple[list[WalkForwardResult], dict[str, int]]:
    """Walk from `start` to `end`, day by day.

    When ``regime_aware=True``: at each eval date D we (a) fit one distribution
    per regime from training errors labeled with that regime, and (b) look up
    the eval date's regime label to pick the matching distribution. When a
    regime has fewer than ``min_train`` samples we fall back to STABLE_HIGH
    (and if that's also insufficient, to the pooled fit of all available train
    errors). This mirrors how live inference will work once regime
    classification is wired into ``WeatherBracketStrategy``.

    LEAKAGE NOTE: the eval-date regime label stored in ``forecast_errors`` was
    computed by ``scripts/backfill_regimes.py`` using ERA5 reanalysis for that
    date (CAPE max + pressure tendency) — i.e. post-event data. Using it at
    inference therefore measures a *calibration ceiling* ("if we had a perfect
    regime classifier at forecast time, what's the best Brier we could hit?").
    It is NOT a strict out-of-sample test. The honest version will substitute
    a forecast-time classifier (ensemble spread, forecast CAPE max, forecast
    pressure tendency) once the ensemble-api back-fetch lands.

    Setting ``regime_aware=False`` reproduces the prior pooled-fit behavior
    for A/B comparison.
    """
    errors = _load_errors_with_dates(db, city, model)
    if not errors:
        return [], {"total_dates": 0, "no_errors_at_all": 1, "skipped_insufficient_train": 0,
                    "skipped_no_forecast": 0, "skipped_no_obs": 0, "skipped_fit_error": 0,
                    "evaluated": 0}

    # Fast map (obs_date -> regime) for eval-date lookup
    regime_by_date: dict[date, str] = {dd: r for (_, dd, r) in errors}

    fitter = ErrorDistributionFitter()
    calc = BracketProbabilityCalculator()
    results: list[WalkForwardResult] = []
    skip: dict[str, int] = defaultdict(int)

    d = start
    while d <= end:
        skip["total_dates"] += 1
        # Training set: all errors with obs_date strictly before d
        train_rows = [(e, r) for (e, dd, r) in errors if dd < d]
        train_errors = [e for (e, _) in train_rows]
        if len(train_errors) < min_train:
            skip["skipped_insufficient_train"] += 1
            d += timedelta(days=1)
            continue

        # Forecast for d
        forecast_f = _load_forecast_on_date(db, city, model, d, lead_hours)
        if forecast_f is None:
            skip["skipped_no_forecast"] += 1
            d += timedelta(days=1)
            continue

        # Observation for d
        obs_row = db.get_observation(city, d)
        if obs_row is None:
            skip["skipped_no_obs"] += 1
            d += timedelta(days=1)
            continue
        observed_f = float(obs_row["observed_high_f"])

        # Fit distribution(s)
        dists_by_regime: dict[str, ErrorDistribution] = {}
        pooled_dist: ErrorDistribution | None = None
        try:
            if regime_aware:
                # Bucket train rows by regime
                by_regime: dict[str, list[float]] = defaultdict(list)
                for e, r in train_rows:
                    by_regime[r].append(e)
                for regime_str, regime_errs in by_regime.items():
                    if len(regime_errs) < min_train:
                        continue
                    regime_enum = _regime_from_str(regime_str)
                    try:
                        if use_bayesian:
                            posterior = fitter.fit_bayesian(
                                regime_errs, city=city, model=model,
                                regime=regime_enum, lead_hours=lead_hours,
                            )
                            dists_by_regime[regime_str] = fitter.summarize_posterior(posterior)  # type: ignore[attr-defined]
                        else:
                            dists_by_regime[regime_str] = fitter.fit(
                                regime_errs, city=city, model=model,
                                regime=regime_enum, lead_hours=lead_hours,
                            )
                    except Exception as exc:
                        print(f"  [{d}] regime {regime_str} fit error: {exc}", file=sys.stderr)

            # Always keep a pooled fallback in case a regime has <min_train samples
            if use_bayesian:
                posterior = fitter.fit_bayesian(
                    train_errors, city=city, model=model,
                    regime=SynopticRegime.STABLE_HIGH, lead_hours=lead_hours,
                )
                pooled_dist = fitter.summarize_posterior(posterior)  # type: ignore[attr-defined]
            else:
                pooled_dist = fitter.fit(
                    train_errors, city=city, model=model,
                    regime=SynopticRegime.STABLE_HIGH, lead_hours=lead_hours,
                )
        except Exception as exc:
            skip["skipped_fit_error"] += 1
            print(f"  [{d}] fit error: {exc}", file=sys.stderr)
            d += timedelta(days=1)
            continue

        # Pick which distribution to use at inference
        if regime_aware:
            eval_regime = regime_by_date.get(d, SynopticRegime.STABLE_HIGH.value)
            dist = dists_by_regime.get(eval_regime)
            if dist is None:
                # Fallback priority: STABLE_HIGH regime fit → pooled fit
                dist = dists_by_regime.get(SynopticRegime.STABLE_HIGH.value) or pooled_dist
                skip["fallback_to_pooled"] += 1
            else:
                skip[f"regime_{eval_regime}"] += 1
        else:
            dist = pooled_dist

        if dist is None:
            skip["skipped_fit_error"] += 1
            d += timedelta(days=1)
            continue

        # Score every bracket shape
        for shape in BRACKET_SHAPES:
            p = _compute_bracket_prob(calc, dist, forecast_f, shape)
            # Clamp for log-loss safety
            p_safe = min(max(p, 1e-6), 1 - 1e-6)
            outcome = 1 if shape.contains(observed_f, forecast_f) else 0
            brier = (p - outcome) ** 2
            log_loss = -math.log(p_safe) if outcome else -math.log(1 - p_safe)
            lo, hi = shape.bounds(forecast_f)
            results.append(WalkForwardResult(
                eval_date=d.isoformat(),
                city=city, model=model.value,
                bracket=shape.name,
                forecast_f=forecast_f,
                observed_f=observed_f,
                lower_f=lo if math.isfinite(shape.lower_offset) else float("-inf"),
                upper_f=hi if math.isfinite(shape.upper_offset) else float("inf"),
                predicted_prob=round(p, 6),
                outcome=outcome,
                brier=round(brier, 6),
                log_loss=round(log_loss, 6),
                n_train=len(train_errors),
                dist_family=dist.family.value,
                dist_mu=round(dist.mu, 3),
                dist_sigma=round(dist.sigma, 3),
            ))

        skip["evaluated"] += 1
        d += timedelta(days=1)

    return results, dict(skip)


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def reliability_bins(results: list[WalkForwardResult], n_bins: int = 10) -> list[dict[str, Any]]:
    """10-bin calibration curve: (mean predicted in bin, actual hit rate in bin, n)."""
    bins: list[list[WalkForwardResult]] = [[] for _ in range(n_bins)]
    for r in results:
        b = min(int(r.predicted_prob * n_bins), n_bins - 1)
        bins[b].append(r)
    out = []
    for i, bucket in enumerate(bins):
        if not bucket:
            out.append({"bin": f"[{i/n_bins:.1f},{(i+1)/n_bins:.1f})", "n": 0,
                        "mean_pred": None, "hit_rate": None, "gap": None})
            continue
        mp = statistics.mean(r.predicted_prob for r in bucket)
        hr = statistics.mean(r.outcome for r in bucket)
        out.append({
            "bin": f"[{i/n_bins:.1f},{(i+1)/n_bins:.1f})",
            "n": len(bucket),
            "mean_pred": round(mp, 4),
            "hit_rate": round(hr, 4),
            "gap": round(mp - hr, 4),
        })
    return out


def summarize(results: list[WalkForwardResult]) -> dict[str, Any]:
    if not results:
        return {"n": 0}
    briers = [r.brier for r in results]
    logs = [r.log_loss for r in results]
    sharpness = [abs(r.predicted_prob - 0.5) for r in results]
    return {
        "n": len(results),
        "brier_mean": round(statistics.mean(briers), 5),
        "log_loss_mean": round(statistics.mean(logs), 5),
        "sharpness_mean": round(statistics.mean(sharpness), 5),
        "base_rate": round(statistics.mean(r.outcome for r in results), 4),
    }


def print_bracket_breakdown(results: list[WalkForwardResult]) -> None:
    by_bracket: dict[str, list[WalkForwardResult]] = defaultdict(list)
    for r in results:
        by_bracket[r.bracket].append(r)
    print(f"\n--- Per-bracket breakdown ---")
    print(f"  {'bracket':<10} {'n':>5} {'brier':>8} {'log_loss':>10} {'base_rate':>10} {'mean_pred':>10}")
    # Use BRACKET_SHAPES order
    order = {s.name: i for i, s in enumerate(BRACKET_SHAPES)}
    for name in sorted(by_bracket.keys(), key=lambda n: order.get(n, 999)):
        rs = by_bracket[name]
        br = statistics.mean(r.brier for r in rs)
        ll = statistics.mean(r.log_loss for r in rs)
        base = statistics.mean(r.outcome for r in rs)
        mp = statistics.mean(r.predicted_prob for r in rs)
        print(f"  {name:<10} {len(rs):>5} {br:>8.4f} {ll:>10.4f} {base:>10.4f} {mp:>10.4f}")


def print_reliability(results: list[WalkForwardResult]) -> None:
    print(f"\n--- Reliability diagram (10 bins) ---")
    print(f"  {'bin':<12} {'n':>6} {'mean_pred':>10} {'hit_rate':>10} {'gap':>8}")
    for row in reliability_bins(results):
        if row["n"] == 0:
            print(f"  {row['bin']:<12} {row['n']:>6} {'-':>10} {'-':>10} {'-':>8}")
        else:
            print(f"  {row['bin']:<12} {row['n']:>6} {row['mean_pred']:>10.4f} "
                  f"{row['hit_rate']:>10.4f} {row['gap']:>+8.4f}")


def print_interpretation(results: list[WalkForwardResult]) -> None:
    if not results:
        return
    agg = summarize(results)
    brier = agg["brier_mean"]
    ll = agg["log_loss_mean"]
    base = agg["base_rate"]
    # Baseline: always predict base rate
    baseline_brier = base * (1 - base)
    skill = 1.0 - brier / baseline_brier if baseline_brier > 0 else 0.0
    print(f"\n--- Interpretation ---")
    print(f"  Brier: {brier:.4f} (baseline {baseline_brier:.4f}, skill score {skill:+.3f})")
    print(f"  Log-loss: {ll:.4f} (naive chance = 0.6931)")
    if skill > 0.05:
        print(f"  POSITIVE skill. Calibration is doing real work — continue paper trading.")
    elif skill > -0.02:
        print(f"  NEAR ZERO skill. Distribution is close to base-rate predictor.")
        print(f"  Likely σ floor is dominating — need more real-forecast data.")
    else:
        print(f"  NEGATIVE skill. Model is WORSE than just quoting the base rate.")
        print(f"  Investigate: σ corruption, missing regime classification, bad station mapping.")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "weather" / "weather.db")
    parser.add_argument("--city", type=str, help="City key (e.g. seoul)")
    parser.add_argument("--model", type=str, default="gfs", help="Model (gfs/ecmwf)")
    parser.add_argument("--all-cities", action="store_true", help="Run for every calibrated (city, model) pair")
    parser.add_argument("--all-models", action="store_true", help="Iterate gfs + ecmwf instead of just --model")
    parser.add_argument("--start", type=str, help="Eval window start YYYY-MM-DD (default: 60 days ago)")
    parser.add_argument("--end", type=str, help="Eval window end YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--min-train", type=int, default=30,
                        help="Minimum training errors before evaluating a date (default 30)")
    parser.add_argument("--lead-hours", type=int, default=24, help="Forecast lead to use (default 24)")
    parser.add_argument("--bayesian", action="store_true",
                        help="Use fit_bayesian() + summarize_posterior() (PyMC, slow)")
    parser.add_argument("--pooled", action="store_true",
                        help="Disable regime-aware fit; reproduce the pre-backfill pooled-fit behavior "
                             "for A/B comparison")
    parser.add_argument("--csv", type=Path, help="Optional CSV of per-date per-bracket rows")
    args = parser.parse_args()

    # Defaults for date window
    today = date.today()
    start = date.fromisoformat(args.start) if args.start else today - timedelta(days=60)
    end = date.fromisoformat(args.end) if args.end else today - timedelta(days=1)

    if end < start:
        print(f"[walk-forward] end ({end}) before start ({start})", file=sys.stderr)
        return 2

    if not args.db.exists():
        print(f"[walk-forward] db not found: {args.db}", file=sys.stderr)
        return 2

    if args.all_cities:
        cities = list(CITY_REGISTRY.keys())
    elif args.city:
        cities = [args.city]
    else:
        print("[walk-forward] must specify --city or --all-cities", file=sys.stderr)
        return 2

    if args.all_models:
        models = [WeatherModel.GFS, WeatherModel.ECMWF]
    else:
        try:
            models = [WeatherModel(args.model.lower())]
        except ValueError:
            print(f"[walk-forward] unknown model: {args.model}", file=sys.stderr)
            return 2

    db = WeatherDatabase(args.db)
    all_results: list[WalkForwardResult] = []
    per_cm_summary: dict[tuple[str, str], dict[str, Any]] = {}
    per_cm_skips: dict[tuple[str, str], dict[str, int]] = {}

    try:
        for city in cities:
            for model in models:
                print(f"\n[walk-forward] {city:<14} {model.value:<6}  {start} → {end}  "
                      f"bayes={args.bayesian}  regime_aware={not args.pooled}")
                results, skips = walk_forward_city_model(
                    db, city, model,
                    start=start, end=end,
                    min_train=args.min_train,
                    lead_hours=args.lead_hours,
                    use_bayesian=args.bayesian,
                    regime_aware=not args.pooled,
                )
                all_results.extend(results)
                per_cm_summary[(city, model.value)] = summarize(results)
                per_cm_skips[(city, model.value)] = skips
    finally:
        db.close()

    # ---- report ----
    print(f"\n=== WALK-FORWARD CALIBRATION REPORT ===")
    print(f"DB:            {args.db}")
    print(f"Window:        {start} → {end}")
    print(f"Min-train:     {args.min_train}")
    print(f"Bayesian:      {args.bayesian}")
    print(f"Regime-aware:  {not args.pooled}")
    print(f"Bracket shapes: {[s.name for s in BRACKET_SHAPES]}")

    print(f"\n--- Per (city, model) summary ---")
    print(f"  {'city':<14} {'model':<6} {'n':>6} {'evaluated':>9} {'ins_train':>9} "
          f"{'no_fc':>6} {'no_obs':>7} {'brier':>7} {'logloss':>8}")
    for (city, model), agg in sorted(per_cm_summary.items()):
        sk = per_cm_skips[(city, model)]
        print(f"  {city:<14} {model:<6} {agg.get('n', 0):>6} "
              f"{sk.get('evaluated', 0):>9} {sk.get('skipped_insufficient_train', 0):>9} "
              f"{sk.get('skipped_no_forecast', 0):>6} {sk.get('skipped_no_obs', 0):>7} "
              f"{agg.get('brier_mean', float('nan')):>7.4f} "
              f"{agg.get('log_loss_mean', float('nan')):>8.4f}")

    if not all_results:
        print("\n[walk-forward] no evaluable dates. Likely causes:")
        print("  * insufficient forecast_errors history (need --min-train prior)")
        print("  * forecasts table empty for this window")
        print("  * observations table missing obs for this window")
        return 0

    print(f"\n--- OVERALL ---")
    agg = summarize(all_results)
    for k, v in agg.items():
        print(f"  {k:<20}: {v}")

    print_bracket_breakdown(all_results)
    print_reliability(all_results)
    print_interpretation(all_results)

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "eval_date", "city", "model", "bracket",
                "forecast_f", "observed_f", "lower_f", "upper_f",
                "predicted_prob", "outcome", "brier", "log_loss",
                "n_train", "dist_family", "dist_mu", "dist_sigma",
            ])
            w.writeheader()
            for r in all_results:
                w.writerow(r.__dict__)
        print(f"\n  wrote {args.csv} ({len(all_results)} rows)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
