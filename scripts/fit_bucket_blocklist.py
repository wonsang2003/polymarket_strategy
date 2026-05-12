"""Compute a data-driven (city, lead, regime) blocklist from trade_history.

Apr 24 2026 — Tier 2c of Apr 24 dev plan. Replaces manual post-hoc
triage with a nightly cron: every bucket that has accumulated n >= N_MIN
real trades and shows negative realized EV gets auto-blocked.

This is the quantitative version of the §11 Phase 2 spec: "after n ≥ 30
real trades on a specific (city, lead) pair, if realized EV is negative,
block that pair." We apply it at (city, lead, regime) granularity for
finer control, with a coarser (city, lead) fallback so that a city with
only a few regime samples doesn't get let off the hook.

Logic:
  1. Pull settled trades (outcome in {0, 1}) from trade_history.
  2. Group by (city, lead_bucket, regime). lead_bucket = 24 or 48 via
     the same bucketing as inference.
  3. Compute realized EV per bucket: mean(pnl) / mean(notional). We
     normalize by notional so a bucket of 30 $10 trades isn't masked
     by a bucket of 5 $50 trades.
  4. Write to data/weather/blocked_buckets.json with:
       - buckets having n >= N_MIN_PER_BUCKET and EV < 0 → blocked
       - coarser (city, lead) buckets with n >= N_MIN_PER_BUCKET and
         EV < 0 → blocked (covers cities with mixed-regime bleeding)

Usage:
  python scripts/fit_bucket_blocklist.py
  python scripts/fit_bucket_blocklist.py --dry-run
  python scripts/fit_bucket_blocklist.py --n-min 20     # fewer samples
  python scripts/fit_bucket_blocklist.py --ev-threshold -0.10
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"
DEFAULT_OUT = REPO_ROOT / "data" / "weather" / "blocked_buckets.json"


def lead_bucket_from_target_date_and_settle(target_date: str, settled_at: str) -> int | None:
    """Infer whether a trade was 24h or 48h lead at entry. Imperfect —
    uses settlement date as proxy for entry date. If settle_date == target_date
    → 24h lead (today's contract). If settle_date + 1 == target_date → 48h.
    Returns None when parse fails."""
    try:
        from datetime import date
        target = date.fromisoformat(target_date[:10])
        settle = date.fromisoformat(settled_at[:10])
        days = (target - settle).days
        # Settle on target_date means we held overnight → 24h lead
        # Settle 1 day before target means we placed a D+1 contract = 48h lead
        if days == 0:
            return 24
        if days == 1:
            return 48
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--n-min",
        type=int,
        default=20,
        help="Minimum settled trades in a bucket before it's eligible for auto-block (default 20)",
    )
    parser.add_argument(
        "--ev-threshold",
        type=float,
        default=0.0,
        help="Realized EV threshold; buckets below this (normalized by notional) get blocked",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[fit_bucket_blocklist] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT city, target_date, regime, notional, pnl, outcome, settled_at "
            "FROM trade_history WHERE outcome IN (0, 1) AND pnl IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    # Group by (city, lead_bucket, regime) and (city, lead_bucket)
    fine_buckets: dict[tuple[str, int, str], list[tuple[float, float]]] = {}
    coarse_buckets: dict[tuple[str, int], list[tuple[float, float]]] = {}

    for city, target_date, regime, notional, pnl, outcome, settled_at in rows:
        if notional is None or notional <= 0:
            continue
        lead = lead_bucket_from_target_date_and_settle(target_date or "", settled_at or "")
        if lead is None:
            continue
        regime_key = regime or "unknown"
        fine_buckets.setdefault((city, lead, regime_key), []).append((float(pnl), float(notional)))
        coarse_buckets.setdefault((city, lead), []).append((float(pnl), float(notional)))

    def bucket_stats(pnls_notionals: list[tuple[float, float]]) -> tuple[int, float, float, float]:
        n = len(pnls_notionals)
        total_pnl = sum(p for p, _ in pnls_notionals)
        total_notional = sum(nn for _, nn in pnls_notionals)
        ev_normalized = total_pnl / total_notional if total_notional > 0 else 0.0
        mean_pnl = total_pnl / n if n > 0 else 0.0
        return n, total_pnl, total_notional, ev_normalized

    blocked_fine: list[dict] = []
    blocked_coarse: list[dict] = []

    for (city, lead, regime_key), trades in sorted(fine_buckets.items()):
        n, total_pnl, total_notional, ev = bucket_stats(trades)
        if n >= args.n_min and ev < args.ev_threshold:
            blocked_fine.append({
                "city": city,
                "lead_hours": lead,
                "regime": regime_key,
                "n_trades": n,
                "total_pnl": round(total_pnl, 2),
                "total_notional": round(total_notional, 2),
                "ev_normalized": round(ev, 4),
            })

    for (city, lead), trades in sorted(coarse_buckets.items()):
        n, total_pnl, total_notional, ev = bucket_stats(trades)
        if n >= args.n_min and ev < args.ev_threshold:
            blocked_coarse.append({
                "city": city,
                "lead_hours": lead,
                "n_trades": n,
                "total_pnl": round(total_pnl, 2),
                "total_notional": round(total_notional, 2),
                "ev_normalized": round(ev, 4),
            })

    result = {
        "fit_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_db": str(db_path),
        "n_min": args.n_min,
        "ev_threshold": args.ev_threshold,
        "summary": {
            "n_fine_buckets_considered": len(fine_buckets),
            "n_coarse_buckets_considered": len(coarse_buckets),
            "n_fine_blocked": len(blocked_fine),
            "n_coarse_blocked": len(blocked_coarse),
        },
        "blocked_fine": blocked_fine,  # (city, lead, regime) triples
        "blocked_coarse": blocked_coarse,  # (city, lead) pairs
    }

    print(f"[fit_bucket_blocklist] {len(fine_buckets)} fine / {len(coarse_buckets)} coarse buckets considered")
    print(f"[fit_bucket_blocklist] blocked fine: {len(blocked_fine)}  coarse: {len(blocked_coarse)}")
    for b in blocked_coarse:
        print(
            f"  COARSE BLOCK  {b['city']:<16} lead={b['lead_hours']}h  n={b['n_trades']:>3}  "
            f"EV={b['ev_normalized']:+.4f}  pnl=${b['total_pnl']:+.2f}"
        )
    for b in blocked_fine:
        print(
            f"  FINE BLOCK    {b['city']:<16} lead={b['lead_hours']}h  "
            f"regime={b['regime']:<16} n={b['n_trades']:>3}  EV={b['ev_normalized']:+.4f}"
        )

    if args.dry_run:
        print("[fit_bucket_blocklist] --dry-run: NOT writing output")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)
    print(f"[fit_bucket_blocklist] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
