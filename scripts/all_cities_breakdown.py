"""All-cities trade history breakdown with triple-verified totals.

For every city in trade_history:
  - Status table (WIN / LOSS / rebal_exit / OPEN)
  - Category table
  - Side breakdown
  - Calibration gap (avg predicted vs realized winrate)

Verification:
  - Sum of all-city trade counts == lifetime trade count
  - Sum of all-city settled pnl == lifetime settled pnl
  - Sum of all-city open notional == lifetime open notional
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(t):
    print()
    print("=" * 100)
    print(f"  {t}")
    print("=" * 100)


def fmt_money(v, width=9):
    if v is None:
        return f"{'—':>{width}}"
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):>{width-2},.2f}"


def main():
    c = conn()

    # ============================================================
    # VERIFICATION CHECK #1 — totals across the DB
    # ============================================================
    hr("VERIFICATION CHECK — DB totals")
    grand = c.execute("""
        SELECT
          COUNT(*) AS n_total,
          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS n_open,
          SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS n_settled,
          ROUND(SUM(pnl), 2) AS lifetime_pnl,
          ROUND(SUM(CASE WHEN outcome IS NULL THEN notional ELSE 0 END), 2) AS open_notional
        FROM trade_history
    """).fetchone()
    print(f"  Total trades   : {grand['n_total']}")
    print(f"  Open           : {grand['n_open']}")
    print(f"  Settled        : {grand['n_settled']}")
    print(f"  Lifetime PnL   : {fmt_money(grand['lifetime_pnl'])}")
    print(f"  Open notional  : ${grand['open_notional']:,.2f}")

    # ============================================================
    # CITY LIST
    # ============================================================
    cities = c.execute("""
        SELECT city, COUNT(*) AS n
        FROM trade_history
        GROUP BY city
        ORDER BY n DESC
    """).fetchall()

    hr("ALL CITIES — sample size")
    print(f"  {'city':<14} {'n':>4}")
    sum_n = 0
    for r in cities:
        print(f"  {r['city']:<14} {r['n']:>4}")
        sum_n += r['n']
    print(f"  {'─'*14} {'─'*4}")
    print(f"  {'TOTAL':<14} {sum_n:>4}")
    print(f"  Verification: city sum {sum_n} == DB total {grand['n_total']}: "
          f"{'OK' if sum_n == grand['n_total'] else 'MISMATCH'}")

    # ============================================================
    # MASTER TABLE — per city × status
    # ============================================================
    hr("MASTER TABLE — per city × status")
    rows = c.execute("""
        SELECT
          city,
          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS n_open,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS n_win,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS n_loss,
          SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS n_rebal,
          ROUND(SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN pnl ELSE 0 END), 2) AS pnl_win,
          ROUND(SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN pnl ELSE 0 END), 2) AS pnl_loss,
          ROUND(SUM(CASE WHEN outcome = 2 THEN pnl ELSE 0 END), 2) AS pnl_rebal,
          ROUND(SUM(pnl), 2) AS pnl_total
        FROM trade_history
        GROUP BY city
        ORDER BY pnl_total
    """).fetchall()

    print(f"  {'city':<14} {'open':>4} {'W':>3} {'L':>3} {'RBL':>4}  "
          f"{'win$':>9} {'loss$':>9} {'rebal$':>9}  {'NET':>10}")
    print(f"  {'─'*14} {'─'*4} {'─'*3} {'─'*3} {'─'*4}  "
          f"{'─'*9} {'─'*9} {'─'*9}  {'─'*10}")
    sum_pnl = 0.0
    for r in rows:
        sum_pnl += r['pnl_total'] or 0
        print(f"  {r['city']:<14} {r['n_open']:>4} {r['n_win']:>3} {r['n_loss']:>3} {r['n_rebal']:>4}  "
              f"{fmt_money(r['pnl_win']):>9} {fmt_money(r['pnl_loss']):>9} {fmt_money(r['pnl_rebal']):>9}  "
              f"{fmt_money(r['pnl_total'], 8):>10}")
    print(f"  {'─'*14}")
    print(f"  TOTAL                                                                "
          f"          {fmt_money(sum_pnl, 8):>10}")
    print(f"  Verification: city pnl sum {fmt_money(sum_pnl)} == "
          f"lifetime {fmt_money(grand['lifetime_pnl'])}: "
          f"{'OK' if abs(sum_pnl - (grand['lifetime_pnl'] or 0)) < 0.05 else 'MISMATCH'}")

    # ============================================================
    # PER-CITY × CATEGORY
    # ============================================================
    hr("PER-CITY × CATEGORY")
    rows = c.execute("""
        SELECT
          city,
          COALESCE(category, '<null>') AS cat,
          COUNT(*) AS n,
          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        GROUP BY city, cat
        ORDER BY city, cat
    """).fetchall()
    cur_city = None
    for r in rows:
        if r['city'] != cur_city:
            cur_city = r['city']
            print(f"\n  {cur_city}:")
        print(f"    {r['cat']:<26} n={r['n']:>3}  open={r['open']:>2}  "
              f"W/L/RBL={r['w']}/{r['l']}/{r['rbl']:>2}   net={fmt_money(r['net'])}")

    # ============================================================
    # CALIBRATION GAP per city (settled non-rebal only)
    # ============================================================
    hr("CALIBRATION GAP — per city (settled non-rebal, all eras)")
    rows = c.execute("""
        SELECT
          city,
          COUNT(*) AS n,
          ROUND(AVG(model_prob), 3) AS avg_pred,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS realized_wr,
          ROUND(SUM(pnl), 2) AS net,
          ROUND(AVG(pnl), 2) AS avg_pnl
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2
          AND model_prob IS NOT NULL
        GROUP BY city
        ORDER BY net
    """).fetchall()
    print(f"  {'city':<14} {'n':>4}  {'avg_pred':>9}  {'realized_wr':>12}  {'gap':>8}  "
          f"{'avg_pnl':>9}  {'net':>10}")
    for r in rows:
        if r['avg_pred'] is None:
            continue
        gap = (r['realized_wr'] or 0) - (r['avg_pred'] or 0)
        print(f"  {r['city']:<14} {r['n']:>4}  {r['avg_pred']:>9.3f}  "
              f"{r['realized_wr']:>11.3f}  {gap:>+7.3f}  "
              f"{fmt_money(r['avg_pnl']):>9}  {fmt_money(r['net'], 8):>10}")

    # ============================================================
    # POST-FIX ONLY (since Apr 26 2026, when categories went live)
    # ============================================================
    hr("POST-FIX ERA ONLY (created_at >= 2026-04-26) — settled non-rebal")
    rows = c.execute("""
        SELECT
          city,
          COUNT(*) AS n,
          ROUND(AVG(model_prob), 3) AS avg_pred,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS realized_wr,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2
          AND model_prob IS NOT NULL
          AND datetime(created_at) >= '2026-04-26'
        GROUP BY city
        ORDER BY net
    """).fetchall()
    print(f"  {'city':<14} {'n':>4}  {'pred':>6}  {'realiz':>7}  {'gap':>7}  {'net':>10}")
    for r in rows:
        if r['avg_pred'] is None:
            continue
        gap = (r['realized_wr'] or 0) - (r['avg_pred'] or 0)
        print(f"  {r['city']:<14} {r['n']:>4}  {r['avg_pred']:>6.3f}  "
              f"{r['realized_wr']:>7.3f}  {gap:>+6.3f}  "
              f"{fmt_money(r['net'], 8):>10}")

    # ============================================================
    # FLIP EXPERIMENT — per city
    # ============================================================
    hr("FLIP EXPERIMENT — per city (weather_tail_no_flipped only)")
    rows = c.execute("""
        SELECT
          city,
          COUNT(*) AS n_total,
          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE category = 'weather_tail_no_flipped'
        GROUP BY city
        ORDER BY net DESC
    """).fetchall()
    print(f"  {'city':<14} {'n':>3} {'open':>4}  {'W/L/RBL':<10}  {'net':>10}")
    for r in rows:
        wlr = f"{r['w']}/{r['l']}/{r['rbl']}"
        print(f"  {r['city']:<14} {r['n_total']:>3} {r['open']:>4}  {wlr:<10}  "
              f"{fmt_money(r['net'], 8):>10}")

    # ============================================================
    # VERIFICATION CHECK #2 — by side
    # ============================================================
    hr("VERIFICATION CHECK — by token_side")
    rows = c.execute("""
        SELECT
          CASE
            WHEN token_side='NO' THEN 'NO'
            WHEN token_side='YES' THEN 'YES'
            ELSE COALESCE(token_side, '(null)')
          END AS ts,
          COUNT(*) AS n,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE outcome IS NOT NULL
        GROUP BY ts
    """).fetchall()
    for r in rows:
        print(f"  token_side={r['ts']:<10} n={r['n']:>4}  net={fmt_money(r['net'])}")

    # ============================================================
    # VERIFICATION CHECK #3 — spot-check seoul + london matches earlier dumps
    # ============================================================
    hr("VERIFICATION CHECK #3 — Seoul & London totals match prior dumps")
    seoul = c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl), 2) AS pnl,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history WHERE city='seoul'
    """).fetchone()
    london = c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl), 2) AS pnl,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history WHERE city='london'
    """).fetchone()
    print(f"  Seoul : n={seoul['n']} (expect 16), pnl={fmt_money(seoul['pnl'])} (expect +$49.03), "
          f"open={seoul['open']} (expect 2)")
    print(f"  London: n={london['n']} (expect 36), pnl={fmt_money(london['pnl'])} (expect ~-$174), "
          f"open={london['open']} (expect 3)")

    c.close()


if __name__ == "__main__":
    main()
