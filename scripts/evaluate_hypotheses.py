"""Auto-evaluate shipped hypotheses 7 days after they land.

Runs nightly via cron (05:50 KST — after calibration / accuracy refits).
Closes the second half of the J-curve loop: every shipped change gets
a measured outcome that goes back into the DB so the next review's
prompt sees real verdicts.

Methodology — INTENTIONALLY BLUNT, with caveats:
  baseline = sum(pnl)  for trades settled in [shipped_at − 7d, shipped_at)
  observed = sum(pnl)  for trades settled in [shipped_at, shipped_at + 7d]
  delta    = observed − baseline

This conflates lots of confounds (regime shift, volume change, other ships
in the same window). It is a SIGNAL, not proof. The evaluator records the
arithmetic + a methodology caveat in actual_effect; the verdict that drops
out of this number gets weighted by the next review (Claude can override
with a richer narrative).

Verdict classification:
  - delta vs expected within ±30% of expected magnitude → win
  - delta same sign as expected, |delta| < |expected|   → partial
  - delta opposite sign of expected                      → lose
  - |expected| not parseable or |delta| < $5             → inconclusive

Calibration write-back:
  For each evaluated hypothesis, update claude_calibration.was_correct
  (1 for win/partial, 0 for lose, NULL for inconclusive).

Idempotent: only processes rows where evaluation_at IS NULL.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")
EVAL_WINDOW_DAYS = 7


def conn():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


# Pull a dollar value out of expected_effect strings like
# "+$570/wk", "$612/7d", "+ $80 / wk", "−$40/wk net" etc.
EXPECTED_DOLLAR = re.compile(r"([+\-−]?)\s*\$\s*([\d.,]+)")


def parse_expected_dollars(text: str | None) -> float | None:
    """Return signed expected P&L delta in dollars, or None if unparseable."""
    if not text:
        return None
    m = EXPECTED_DOLLAR.search(text)
    if not m:
        return None
    sign = -1.0 if m.group(1) in ("-", "−") else 1.0
    try:
        magnitude = float(m.group(2).replace(",", ""))
    except ValueError:
        return None
    return sign * magnitude


def measure_delta(c: sqlite3.Connection, shipped_at: str) -> tuple[float, float, float]:
    """Return (baseline, observed, delta) over ±7d windows around shipped_at."""
    # SQLite datetime arithmetic. shipped_at is ISO-8601 UTC.
    row = c.execute(
        """
        SELECT
          COALESCE(SUM(CASE
            WHEN datetime(settled_at) >= datetime(?, '-7 days')
             AND datetime(settled_at) <  datetime(?)
            THEN pnl ELSE 0 END), 0) AS baseline,
          COALESCE(SUM(CASE
            WHEN datetime(settled_at) >= datetime(?)
             AND datetime(settled_at) <= datetime(?, '+7 days')
            THEN pnl ELSE 0 END), 0) AS observed
        FROM trade_history
        WHERE outcome IS NOT NULL
        """,
        (shipped_at, shipped_at, shipped_at, shipped_at),
    ).fetchone()
    baseline = float(row["baseline"] or 0)
    observed = float(row["observed"] or 0)
    return baseline, observed, observed - baseline


def classify_verdict(expected: float | None, delta: float) -> str:
    """Map (expected, observed delta) → win / partial / lose / inconclusive."""
    if abs(delta) < 5.0:
        return "inconclusive"
    if expected is None or abs(expected) < 5.0:
        # Can't parse expected → call it inconclusive even if delta is large.
        # We don't want to falsely credit a lucky regime to a vague hypothesis.
        return "inconclusive"

    expected_sign = 1.0 if expected > 0 else -1.0
    delta_sign = 1.0 if delta > 0 else -1.0

    if expected_sign != delta_sign:
        return "lose"

    # Same sign — graded by magnitude.
    ratio = abs(delta) / max(abs(expected), 1.0)
    if 0.7 <= ratio <= 1.3:
        return "win"
    if ratio > 1.3:
        return "win"  # outperformed; still a win
    return "partial"  # right direction, smaller magnitude


def evaluate(c: sqlite3.Connection, now: datetime) -> list[dict]:
    """Find unevaluated shipped hypotheses past the 7d window. Update each."""
    cutoff = (now - timedelta(days=EVAL_WINDOW_DAYS)).isoformat()

    rows = c.execute(
        """
        SELECT id, hypothesis, expected_effect, confidence_pct, shipped_at
        FROM strategy_hypotheses
        WHERE status = 'shipped'
          AND shipped_at IS NOT NULL
          AND evaluation_at IS NULL
          AND datetime(shipped_at) <= datetime(?)
        """,
        (cutoff,),
    ).fetchall()

    results = []
    for r in rows:
        shipped_at = r["shipped_at"]
        baseline, observed, delta = measure_delta(c, shipped_at)
        expected = parse_expected_dollars(r["expected_effect"])
        verdict = classify_verdict(expected, delta)

        actual_effect = (
            f"7d-pre baseline ${baseline:+.2f} → "
            f"7d-post observed ${observed:+.2f} → "
            f"delta ${delta:+.2f}. "
            f"expected_parsed=${(expected if expected is not None else float('nan')):+.2f}. "
            "Methodology: blunt total-pnl ±7d window; uncontrolled for "
            "concurrent regime shift / cross-rec interaction. "
            "Treat as signal, not proof."
        )

        c.execute(
            """
            UPDATE strategy_hypotheses
               SET evaluation_at     = ?,
                   actual_effect     = ?,
                   measured_pnl_delta= ?,
                   verdict           = ?
             WHERE id = ?
            """,
            (now.isoformat(), actual_effect, delta, verdict, r["id"]),
        )

        # Calibration update — was_correct tracks confidence accuracy.
        # Win/partial → 1; lose → 0; inconclusive → leave NULL (not informative).
        was_correct = (
            1 if verdict in ("win", "partial")
            else 0 if verdict == "lose"
            else None
        )
        c.execute(
            """
            UPDATE claude_calibration
               SET actual_outcome = ?,
                   was_correct    = ?
             WHERE hypothesis_id = ?
               AND actual_outcome IS NULL
            """,
            (verdict, was_correct, r["id"]),
        )

        results.append({
            "id": r["id"],
            "verdict": verdict,
            "delta": delta,
            "expected": expected,
            "confidence_pct": r["confidence_pct"],
        })

    c.commit()
    return results


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    c = conn()
    try:
        results = evaluate(c, now)
    finally:
        c.close()

    if not results:
        print(f"[evaluator] {now.isoformat()} — no hypotheses ready for evaluation.")
        return 0

    print(f"[evaluator] {now.isoformat()} — evaluated {len(results)} hypotheses:")
    for r in results:
        exp_str = f"${r['expected']:+.2f}" if r['expected'] is not None else "?"
        print(
            f"  {r['id']:<22} verdict={r['verdict']:<13} "
            f"delta=${r['delta']:+.2f}  expected={exp_str}  conf={r['confidence_pct']}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
