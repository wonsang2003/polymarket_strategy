"""Reconcile: dashboard 3d P&L vs my 36h NO-only claim.

Both numbers are 'true' — they just measure different things. This script
decomposes the 72h (3d) window across every axis to show exactly where
the gap is."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(t):
    print()
    print("=" * 78)
    print(f"  {t}")
    print("=" * 78)


def fmt(v):
    if v is None:
        return "$0.00"
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def main():
    c = conn()
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
    print(f"NOW: {now_kst}\n")

    # =====================================================================
    # 1. 72h (= 3d) window — by side × outcome
    # =====================================================================
    hr("1. LAST 72h (3d) — by side × outcome (this is what dashboard shows)")
    rows = c.execute("""
        SELECT
          CASE WHEN token_side='NO' OR side LIKE '%NO%' THEN 'NO'
               WHEN side LIKE '%YES%' THEN 'YES'
               ELSE 'OTHER' END AS s,
          CASE WHEN outcome=2 THEN 'rebal'
               WHEN pnl>0 THEN 'WIN'
               ELSE 'LOSS' END AS r,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-72 hours')
        GROUP BY s, r
        ORDER BY s, r
    """).fetchall()
    print(f"  {'side':<6}{'result':<8}{'n':>5}  {'net':>12}  {'avg':>10}")
    grand = 0.0
    grand_n = 0
    for r in rows:
        print(f"  {r['s']:<6}{r['r']:<8}{r['n']:>5}  {fmt(r['net']):>12}  {fmt(r['avg']):>10}")
        grand += r['net'] or 0
        grand_n += r['n']
    print(f"\n  GRAND TOTAL (72h): n={grand_n}  net={fmt(grand)}")

    # =====================================================================
    # 2. Same window — collapse to net by SIDE only
    # =====================================================================
    hr("2. 72h by SIDE only (NO vs YES net)")
    rows = c.execute("""
        SELECT
          CASE WHEN token_side='NO' OR side LIKE '%NO%' THEN 'NO'
               WHEN side LIKE '%YES%' THEN 'YES'
               ELSE 'OTHER' END AS s,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-72 hours')
        GROUP BY s
    """).fetchall()
    for r in rows:
        print(f"  {r['s']:<6}: n={r['n']:>3}  net={fmt(r['net'])}")

    # =====================================================================
    # 3. NO directional only (the +$177 claim) — verify
    # =====================================================================
    hr("3. NO directional (settle, NOT rebal) by window")
    for hours in [24, 36, 48, 72]:
        r = c.execute("""
            SELECT COUNT(*) AS n,
                   ROUND(SUM(pnl),2) AS net,
                   SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins
            FROM trade_history
            WHERE outcome IS NOT NULL AND outcome != 2
              AND (token_side='NO' OR side LIKE '%NO%')
              AND datetime(settled_at) >= datetime('now', ?)
        """, (f'-{hours} hours',)).fetchone()
        wr = (r['wins'] / r['n'] * 100) if r['n'] else 0
        print(f"  Last {hours}h: n={r['n']:>3}  wins={r['wins']:>3}  win%={wr:>5.1f}  net={fmt(r['net'])}")

    # =====================================================================
    # 4. NO with rebal (the dashboard view)
    # =====================================================================
    hr("4. NO total (settle + rebal) by window")
    for hours in [24, 36, 48, 72]:
        r = c.execute("""
            SELECT COUNT(*) AS n,
                   ROUND(SUM(pnl),2) AS net
            FROM trade_history
            WHERE outcome IS NOT NULL
              AND (token_side='NO' OR side LIKE '%NO%')
              AND datetime(settled_at) >= datetime('now', ?)
        """, (f'-{hours} hours',)).fetchone()
        print(f"  Last {hours}h: n={r['n']:>3}  net={fmt(r['net'])}")

    # =====================================================================
    # 5. Where the rebal bleed is concentrated
    # =====================================================================
    hr("5. Rebal-exit concentration (last 72h)")
    r_total = c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl),2) AS net
        FROM trade_history
        WHERE outcome=2
          AND datetime(settled_at) >= datetime('now', '-72 hours')
    """).fetchone()
    print(f"  All rebal exits 72h: n={r_total['n']}, net={fmt(r_total['net'])}")
    rows = c.execute("""
        SELECT exit_reason, COUNT(*) AS n, ROUND(SUM(pnl),2) AS net
        FROM trade_history
        WHERE outcome=2
          AND datetime(settled_at) >= datetime('now', '-72 hours')
        GROUP BY exit_reason
        ORDER BY net
    """).fetchall()
    for r in rows:
        print(f"    {r['exit_reason'] or 'NULL':<26} n={r['n']:>3}  net={fmt(r['net'])}")

    # =====================================================================
    # 6. RECONCILIATION SUMMARY
    # =====================================================================
    hr("6. RECONCILIATION — where is the −$200?")
    # Total 72h
    r1 = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-72 hours')
    """).fetchone()
    # NO settle 72h
    r2 = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2
          AND (token_side='NO' OR side LIKE '%NO%')
          AND datetime(settled_at) >= datetime('now', '-72 hours')
    """).fetchone()
    # NO rebal 72h
    r3 = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome=2
          AND (token_side='NO' OR side LIKE '%NO%')
          AND datetime(settled_at) >= datetime('now', '-72 hours')
    """).fetchone()
    # YES total 72h
    r4 = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND side LIKE '%YES%'
          AND datetime(settled_at) >= datetime('now', '-72 hours')
    """).fetchone()
    print(f"  TOTAL 72h               : n={r1['n']:>3}  net={fmt(r1['net'])}")
    print(f"  ├─ NO settle (directional): n={r2['n']:>3}  net={fmt(r2['net'])}  ← +$177 claim")
    print(f"  ├─ NO rebal exits        : n={r3['n']:>3}  net={fmt(r3['net'])}  ← bleed")
    print(f"  └─ YES events           : n={r4['n']:>3}  net={fmt(r4['net'])}")
    leftover = (r1['net'] or 0) - (r2['net'] or 0) - (r3['net'] or 0) - (r4['net'] or 0)
    print(f"     (other/unaccounted) : {fmt(leftover)}")

    print(f"\n  CONCLUSION:")
    print(f"  Dashboard 3d P&L  ≈ {fmt(r1['net'])}  (everything)")
    print(f"  My 'NO is winning' = {fmt(r2['net'])}  (only NO settle, excluding rebal)")
    print(f"  The gap is {fmt((r2['net'] or 0) - (r1['net'] or 0))} of which:")
    print(f"     NO rebal: {fmt(r3['net'])}")
    print(f"     YES:     {fmt(r4['net'])}")

    c.close()


if __name__ == "__main__":
    main()
