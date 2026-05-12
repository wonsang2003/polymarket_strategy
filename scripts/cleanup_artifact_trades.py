"""Clean up artifact trades in trade_history.

Apr 25 2026 (LATE) — fix the London "lowest temperature" bug. Categorizes
all trade_history rows for cleanup:

  CATEGORY A: question contains "lowest" or "low temp" → category-error
              (model is HIGH-temp only; these should never have been
              entered). Marked artifact and pnl set to -notional (treat
              as full loss for accounting integrity, since the bot
              shouldn't be there).

  CATEGORY B: entry_price < 0.02 (below min_entry_price floor) →
              bracket-parser artifact. If pnl > 50× notional, recompute
              pnl=0 (artifact: not a real win).

  CATEGORY C: edge > 0.50 → absurd edge that the new gate would now
              reject. Flag for review but don't auto-modify pnl unless
              also in A or B.

By default: dry-run, prints what WOULD change. Pass --apply to write.

Run on EC2:
    venv/bin/python scripts/cleanup_artifact_trades.py            # dry-run
    venv/bin/python scripts/cleanup_artifact_trades.py --apply    # commit
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "weather" / "weather.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true",
                         help="Actually write changes (default dry-run)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[cleanup] DB missing: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, city, question, entry_price, notional, edge, "
        "pnl, outcome, side, token_side, settled_at "
        "FROM trade_history "
        "ORDER BY id"
    ).fetchall()

    cat_a: list[dict] = []  # low-temp markets (category error)
    cat_b: list[dict] = []  # entry_price < 0.02 (artifact)
    cat_c: list[dict] = []  # edge > 0.50 (absurd)

    for r in rows:
        d = dict(r)
        q = (d["question"] or "").lower()
        if "lowest" in q or "low temp" in q:
            cat_a.append(d)
            continue
        ep = float(d["entry_price"] or 0)
        if 0 < ep < 0.02:
            cat_b.append(d)
            continue
        edge_val = float(d["edge"] or 0)
        if abs(edge_val) > 0.50:
            cat_c.append(d)

    print(f"[cleanup] Total trade_history rows: {len(rows)}")
    print(f"  CAT A (low-temp markets):           {len(cat_a)}")
    print(f"  CAT B (entry_price < 0.02 artifact): {len(cat_b)}")
    print(f"  CAT C (edge > 0.50, suspicious):     {len(cat_c)}")
    print()

    if cat_a:
        print(f"--- CAT A: low-temp question (category error) ---")
        for d in cat_a:
            print(f"  id={d['id']:>3}  {d['city']:<14}  ep={d['entry_price']:.4f}  "
                  f"pnl={d['pnl']}  q={d['question'][:80]}")
    if cat_b:
        print(f"\n--- CAT B: entry_price < 0.02 ---")
        for d in cat_b:
            print(f"  id={d['id']:>3}  {d['city']:<14}  ep={d['entry_price']:.4f}  "
                  f"pnl={d['pnl']}  edge={d['edge']}")
    if cat_c:
        print(f"\n--- CAT C: edge > 0.50 (review) ---")
        for d in cat_c[:10]:
            print(f"  id={d['id']:>3}  {d['city']:<14}  edge={d['edge']:.2f}  "
                  f"pnl={d['pnl']}")
        if len(cat_c) > 10:
            print(f"  ... and {len(cat_c)-10} more")

    if not args.apply:
        print(f"\n[cleanup] DRY-RUN. Re-run with --apply to commit corrections.")
        return 0

    # Apply corrections — A and B both get exit_reason='artifact_cleanup',
    # plus pnl correction.
    n_corrected = 0
    cur = conn.cursor()
    for d in cat_a:
        # Low-temp markets: bot shouldn't have been there. If outcome resolved,
        # the "win" was on a market type the bot can't price. Treat all as
        # full notional loss for accounting clarity (zero out fake wins).
        if d["pnl"] is not None and float(d["pnl"]) > 0:
            cur.execute(
                "UPDATE trade_history SET pnl = -notional, "
                "exit_reason = COALESCE(exit_reason, '') || 'cleanup_low_temp;' "
                "WHERE id = ?",
                (d["id"],),
            )
            n_corrected += 1
            print(f"  CORRECTED id={d['id']}: pnl ${d['pnl']} -> -${d['notional']}")

    for d in cat_b:
        # Below min_entry_price floor. If pnl > 50× notional, zero it.
        if d["pnl"] is not None and d["notional"]:
            try:
                if float(d["pnl"]) > 50.0 * float(d["notional"]):
                    cur.execute(
                        "UPDATE trade_history SET pnl = 0, "
                        "exit_reason = COALESCE(exit_reason, '') || 'cleanup_artifact_floor;' "
                        "WHERE id = ?",
                        (d["id"],),
                    )
                    n_corrected += 1
                    print(f"  CORRECTED id={d['id']}: pnl ${d['pnl']} -> $0 (50× cap)")
            except (TypeError, ValueError):
                continue

    conn.commit()
    conn.close()
    print(f"\n[cleanup] Applied corrections to {n_corrected} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
