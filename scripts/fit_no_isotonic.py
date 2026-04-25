"""Fit NO-side isotonic regression from settled real NO trades.

Why this exists (Apr 26 2026 — fix #2 from NO-side bleed analysis):
  The existing `scripts/fit_isotonic.py` learns a calibration curve from
  the walk-forward CSV. That CSV scores synthetic ±2°F brackets, not the
  1°C-wide (≈1.8°F) brackets Polymarket actually offers in EU/Asia. The
  NO-side bleed (sao_paulo p=0.86 → -$2.21, munich p=0.77 → -$1.87,
  hong_kong p=0.92 → -$1.35) concentrates on those narrow brackets, and
  the synthetic curve never saw them.

  Fix #1 (Plan B tightening + narrow-bracket NO cap, deployed Apr 26)
  blocks the worst-case entries. This is fix #2 — a data-driven NO-side
  calibration curve fit from `trade_history` itself. The curve learns
  the real (predicted_p_NO, actual_NO_outcome) relationship from settled
  trades, which directly captures the failure mode.

Architecture:
  - Source: SQLite `trade_history` table.
  - Filter: token_side='NO' AND pnl IS NOT NULL (settled, with realized
    outcome). Excludes outcome=2 (rebalance closes — those are NOT
    settlements, so the actual NO/YES outcome is unknown at exit).
  - Pair: (predicted_p, actual_NO=1 if pnl>0 else 0). For NO trades,
    model_prob already stores P(NO) per side-agnostic refactor in
    strategy.py.
  - Output: data/weather/isotonic_no_calibration.json with the same
    knot-array format as fit_isotonic.py, but ONLY a global curve
    (per-city would require ≥150 NO trades per city, we currently have
    <50 total).
  - Activation gate: only write the curve if n_total ≥ 30 (sample-size
    floor); below that, write an identity-fallback record so the
    inference path detects "not enough data, no correction".

Inference is wired in `polymarket_strat/domain/weather/forecast.py`:
  IsotonicCalibrator.calibrate_no(p_no, ...).

Run nightly via cron alongside fit_isotonic.py:
  5 5 * * * fit_isotonic.py
  6 5 * * * fit_no_isotonic.py        # add this line

Usage:
  python scripts/fit_no_isotonic.py
  python scripts/fit_no_isotonic.py --dry-run
  python scripts/fit_no_isotonic.py --min-samples 50    # raise the floor
  python scripts/fit_no_isotonic.py --db /path/to/db    # alt DB
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
DEFAULT_OUT = REPO_ROOT / "data" / "weather" / "isotonic_no_calibration.json"


def load_no_trades(db_path: Path) -> list[tuple[float, int, float]]:
    """Return list of (predicted_p_NO, actual_NO_outcome, bracket_width_f).

    actual_NO_outcome is 1 if pnl > 0 (NO bet won → YES did not happen),
    0 otherwise. We exclude outcome=2 rows (rebalance exits — those don't
    represent a true NO/YES resolution; the bet was closed prematurely).
    """
    if not db_path.exists():
        print(f"[fit_no_isotonic] DB not found: {db_path}", file=sys.stderr)
        return []

    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = c.execute(
        """
        SELECT model_prob, pnl, bracket_lower_f, bracket_upper_f
        FROM trade_history
        WHERE token_side = 'NO'
          AND pnl IS NOT NULL
          AND (outcome IS NULL OR outcome IN (0, 1))
          AND model_prob IS NOT NULL
        """
    ).fetchall()
    c.close()

    out: list[tuple[float, int, float]] = []
    for model_prob, pnl, lo, up in rows:
        try:
            p = float(model_prob)
            if not (0.0 <= p <= 1.0):
                continue
            actual = 1 if float(pnl) > 0 else 0
            width = float(up or 0) - float(lo or 0)
            out.append((p, actual, width))
        except (TypeError, ValueError):
            continue
    return out


def fit_isotonic_no(
    pred: np.ndarray, obs: np.ndarray, *, label: str = ""
) -> dict:
    """Fit a NO-side isotonic curve and return serializable knots.

    Returns a record with:
      x, y           — knot arrays (np.interp at inference)
      n              — sample count
      brier_before   — uncalibrated Brier score
      brier_after    — calibrated Brier score
      identity_fallback — True if n < 20, in which case x/y are identity
    """
    pred = np.asarray(pred, dtype=np.float64)
    obs = np.asarray(obs, dtype=np.float64)
    mask = np.isfinite(pred) & np.isfinite(obs)
    pred = pred[mask]
    obs = obs[mask]
    n = int(len(pred))

    if n < 20:
        return {
            "x": [0.0, 1.0],
            "y": [0.0, 1.0],
            "n": n,
            "brier_before": float("nan"),
            "brier_after": float("nan"),
            "identity_fallback": True,
            "note": f"insufficient samples (n={n}) — identity fallback",
        }

    ir = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    ir.fit(pred, obs)

    brier_before = float(np.mean((pred - obs) ** 2))
    brier_after = float(np.mean((ir.predict(pred) - obs) ** 2))

    return {
        "x": ir.X_thresholds_.tolist(),
        "y": ir.y_thresholds_.tolist(),
        "n": n,
        "brier_before": brier_before,
        "brier_after": brier_after,
        "identity_fallback": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--min-samples",
        type=int,
        default=30,
        help="Minimum settled NO trades required to publish a non-identity "
             "curve (default: 30 — low floor since NO trades are scarce).",
    )
    parser.add_argument(
        "--narrow-width-f",
        type=float,
        default=2.0,
        help="Bracket width threshold (°F) below which a bracket is narrow. "
             "Narrow brackets get their own curve when sample count permits.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = Path(args.db)
    rows = load_no_trades(db)
    print(f"[fit_no_isotonic] loaded {len(rows)} settled NO trades from {db}")

    if len(rows) == 0:
        print("[fit_no_isotonic] no NO trades — writing empty/identity record")
        rows = []

    pred_all = np.array([r[0] for r in rows]) if rows else np.array([])
    obs_all = np.array([r[1] for r in rows]) if rows else np.array([])
    widths = np.array([r[2] for r in rows]) if rows else np.array([])

    # ---- global fit (all NO trades pooled)
    global_fit = fit_isotonic_no(pred_all, obs_all, label="global")
    print(
        f"[fit_no_isotonic] global  n={global_fit['n']}  "
        f"brier {global_fit['brier_before']:.4f} → {global_fit['brier_after']:.4f}  "
        f"(Δ {global_fit['brier_before'] - global_fit['brier_after']:+.4f})"
    )

    # ---- narrow-bracket fit (width < threshold) — only if enough samples
    narrow_mask = widths < args.narrow_width_f
    narrow_n = int(narrow_mask.sum()) if rows else 0
    narrow_fit: dict | None = None
    if narrow_n >= args.min_samples:
        narrow_fit = fit_isotonic_no(
            pred_all[narrow_mask], obs_all[narrow_mask], label="narrow"
        )
        print(
            f"[fit_no_isotonic] narrow  n={narrow_fit['n']}  "
            f"brier {narrow_fit['brier_before']:.4f} → {narrow_fit['brier_after']:.4f}  "
            f"(Δ {narrow_fit['brier_before'] - narrow_fit['brier_after']:+.4f})"
        )
    else:
        print(
            f"[fit_no_isotonic] narrow  n={narrow_n} < min_samples={args.min_samples} "
            f"— skipping narrow-only curve, global will apply"
        )

    result = {
        "fit_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "trade_history WHERE token_side='NO' AND pnl IS NOT NULL",
        "min_samples": args.min_samples,
        "narrow_width_threshold_f": args.narrow_width_f,
        "global": global_fit,
    }
    if narrow_fit is not None:
        result["narrow"] = narrow_fit

    if args.dry_run:
        print("[fit_no_isotonic] --dry-run: NOT writing output")
        print(json.dumps(result, indent=2))
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)
    print(f"[fit_no_isotonic] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
