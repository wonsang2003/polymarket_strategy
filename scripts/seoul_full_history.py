"""Dump every seoul trade in the DB — all columns, all eras."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


def main():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row

    total = c.execute(
        "SELECT COUNT(*) AS n FROM trade_history WHERE city='seoul'"
    ).fetchone()
    print(f"\nTOTAL SEOUL TRADES: {total['n']}\n")

    # Headline by status
    rows = c.execute("""
        SELECT
          CASE
            WHEN outcome IS NULL THEN 'OPEN'
            WHEN outcome = 2 THEN 'rebal_exit'
            WHEN pnl > 0 THEN 'WIN'
            ELSE 'LOSS'
          END AS status,
          COUNT(*) AS n,
          ROUND(SUM(pnl), 2) AS net,
          ROUND(AVG(pnl), 2) AS avg
        FROM trade_history
        WHERE city='seoul'
        GROUP BY status
        ORDER BY status
    """).fetchall()
    print("BY STATUS:")
    for r in rows:
        avg = f"${r['avg']:+.2f}" if r['avg'] is not None else '—'
        net = f"${r['net']:+.2f}" if r['net'] is not None else '—'
        print(f"  {r['status']:<12} n={r['n']:>3}  net={net:>10}  avg={avg}")

    # By category
    rows = c.execute("""
        SELECT
          COALESCE(category, '<null>') AS cat,
          COUNT(*) AS n,
          ROUND(SUM(pnl), 2) AS net,
          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history
        WHERE city='seoul'
        GROUP BY cat
        ORDER BY cat
    """).fetchall()
    print("\nBY CATEGORY:")
    for r in rows:
        net = f"${r['net']:+.2f}" if r['net'] is not None else '—'
        print(f"  {r['cat']:<28} n={r['n']:>3}  open={r['open']:>2}  net={net}")

    # ===== FULL DUMP — every column, every row =====
    print("\n" + "="*120)
    print("EVERY SEOUL TRADE — ITEMIZED")
    print("="*120)

    rows = c.execute("""
        SELECT id, target_date, COALESCE(category,'<null>') AS cat,
               side, COALESCE(token_side,'-') AS ts,
               ROUND(bracket_lower_f,1) AS lo,
               ROUND(bracket_upper_f,1) AS up,
               ROUND(entry_price,3) AS px,
               ROUND(model_prob,3) AS mp,
               ROUND(market_prob,3) AS mkt,
               ROUND(edge,3) AS edge,
               ROUND(notional,2) AS notl,
               outcome,
               ROUND(pnl,2) AS pnl,
               ROUND(entry_edge,3) AS ent_edge,
               COALESCE(regime,'-') AS rgm,
               created_at,
               settled_at,
               COALESCE(exit_reason,'-') AS rsn,
               SUBSTR(question, 1, 60) AS q
        FROM trade_history
        WHERE city='seoul'
        ORDER BY id
    """).fetchall()

    print(f"{'id':>4} {'target':<11} {'cat':<24} {'side':<8} {'ts':<3} "
          f"{'lo':>5} {'up':>6} {'px':>5} {'mp':>5} {'edge':>6} "
          f"{'notl':>5} {'out':>3} {'pnl':>9} {'rsn':<22}")
    for r in rows:
        out_s = str(r['outcome']) if r['outcome'] is not None else 'open'
        pnl_s = f"{r['pnl']:+.2f}" if r['pnl'] is not None else '   —'
        print(f"#{r['id']:>3} {r['target_date']:<11} {r['cat']:<24} "
              f"{(r['side'] or '-'):<8} {r['ts']:<3} "
              f"{r['lo']:>5.1f} {r['up']:>6.1f} {r['px']:>5.3f} "
              f"{(r['mp'] or 0):>5.3f} {(r['edge'] or 0):>+6.3f} "
              f"{r['notl']:>5.0f} {out_s:>3} {pnl_s:>9} {r['rsn']:<22}")

    # Detailed timeline view
    print("\n" + "="*120)
    print("TIMELINE — created/settled per trade")
    print("="*120)
    print(f"{'id':>4} {'created':<25} {'settled':<25} {'target':<11} {'pnl':>9} {'cat':<24}")
    for r in rows:
        pnl_s = f"{r['pnl']:+.2f}" if r['pnl'] is not None else '   —'
        cr = str(r['created_at'] or '')[:19]
        st = str(r['settled_at'] or '')[:19] if r['settled_at'] else '(open)'
        print(f"#{r['id']:>3} {cr:<25} {st:<25} {r['target_date']:<11} "
              f"{pnl_s:>9} {r['cat']:<24}")

    # Question text
    print("\n" + "="*120)
    print("QUESTION TEXTS")
    print("="*120)
    print(f"{'id':>4} {'target':<11} {'pnl':>8}  question")
    for r in rows:
        pnl_s = f"{r['pnl']:+.2f}" if r['pnl'] is not None else '  open'
        print(f"#{r['id']:>3} {r['target_date']:<11} {pnl_s:>8}  {r['q']}")

    c.close()


if __name__ == "__main__":
    main()
