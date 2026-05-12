"""Quantify catastrophic_flip value: rebal exit breakdown by reason × category.

Tests user hypothesis: catastrophic_flip is helping; the bigger losses on
weather_tail_no come from somewhere else (not catastrophic_flip itself).

Compares:
  1. Per-(category, exit_reason) avg P&L and count
  2. Counterfactual hold-to-settlement P&L vs realized catastrophic exit
  3. NO-side vs YES-side (flipped) loss magnitudes — entry price asymmetry
"""
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

    # ========== 1. Per-category × exit_reason breakdown (last 48h) ==========
    hr("1. REBAL EXITS by category × exit_reason (last 48h)")
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               COALESCE(exit_reason, 'unknown') AS reason,
               COUNT(*) AS n,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(pnl), 2) AS avg,
               ROUND(MIN(pnl), 2) AS worst,
               ROUND(MAX(pnl), 2) AS best
        FROM trade_history
        WHERE outcome = 2
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        GROUP BY cat, reason
        ORDER BY cat, reason
    """).fetchall()
    print(f"  {'category':<26}{'reason':<26}{'n':>3}  {'net':>11}  {'avg':>9}  "
          f"{'worst':>8}")
    for r in rows:
        print(f"  {r['cat']:<26}{r['reason']:<26}{r['n']:>3}  "
              f"{fmt(r['net']):>11}  {fmt(r['avg']):>9}  {fmt(r['worst']):>8}")

    # ========== 2. Catastrophic-flip-only details ==========
    hr("2. CATASTROPHIC_FLIP only — by category (last 48h)")
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               COUNT(*) AS n,
               ROUND(AVG(entry_price), 3) AS avg_entry,
               ROUND(AVG(pnl), 2) AS avg_pnl,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(MIN(pnl), 2) AS worst
        FROM trade_history
        WHERE outcome = 2 AND exit_reason = 'catastrophic_flip'
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        GROUP BY cat
    """).fetchall()
    print(f"  {'category':<26} {'n':>3} {'avg_entry':>9}  {'avg_pnl':>9}  "
          f"{'net':>11}  {'worst':>8}")
    for r in rows:
        print(f"  {r['cat']:<26} {r['n']:>3} {r['avg_entry']:>9.3f}  "
              f"{fmt(r['avg_pnl']):>9}  {fmt(r['net']):>11}  {fmt(r['worst']):>8}")

    # ========== 3. Compare exit_reason on weather_tail_no specifically ==========
    hr("3. weather_tail_no rebal — broken down by reason (last 48h)")
    rows = c.execute("""
        SELECT COALESCE(exit_reason, 'unknown') AS reason,
               COUNT(*) AS n,
               ROUND(AVG(pnl), 2) AS avg,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(entry_price), 3) AS avg_entry
        FROM trade_history
        WHERE outcome = 2 AND category = 'weather_tail_no'
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        GROUP BY reason
        ORDER BY net
    """).fetchall()
    print(f"  {'reason':<26} {'n':>3} {'avg_entry':>9}  {'avg':>9}  {'net':>11}")
    for r in rows:
        print(f"  {r['reason']:<26} {r['n']:>3} {r['avg_entry']:>9.3f}  "
              f"{fmt(r['avg']):>9}  {fmt(r['net']):>11}")

    # ========== 4. Counterfactual: would hold-to-settlement be better? ==========
    hr("4. COUNTERFACTUAL — catastrophic_flip vs hold-to-settlement")
    print("  For each catastrophic_flip exit, compute expected hold P&L")
    print("  assuming market price at exit = market's true probability.")
    print()

    rows = c.execute("""
        SELECT id, city, COALESCE(category, '<null>') AS cat,
               token_side, side,
               ROUND(entry_price, 3) AS entry,
               notional,
               ROUND(pnl, 2) AS realized_exit_pnl
        FROM trade_history
        WHERE outcome = 2 AND exit_reason = 'catastrophic_flip'
          AND datetime(settled_at) >= datetime('now', '-48 hours')
    """).fetchall()

    total_realized = 0.0
    total_hold_expected = 0.0
    total_hold_lower = 0.0  # if market was right
    total_hold_upper = 0.0  # if mean-reversion partial
    print(f"  {'id':>4} {'city':<13} {'side':<5} {'entry':>5} {'notl':>4} "
          f"{'realized':>9}  {'hold_exp':>9}  {'delta':>8}")
    for r in rows:
        entry = r['entry']
        notl = r['notional']
        realized = r['realized_exit_pnl'] or 0
        # The catastrophic exit happened at bid that produced realized loss.
        # Reverse-engineer: shares = notl / entry
        # exit_bid such that shares × (exit_bid − entry) = realized
        # exit_bid = entry + realized / shares
        if entry > 0 and notl > 0:
            shares = notl / entry
            exit_bid = entry + (realized / shares)
            # Hold-to-settlement assuming market right (exit_bid = market p_no_win):
            # NO win prob = exit_bid (for NO trades) or 1−exit_bid (for YES trades)
            if (r['token_side'] or '').upper() == 'NO' or 'NO' in (r['side'] or '').upper():
                # NO trade: win prob ≈ exit_bid
                p_win = max(0, min(1, exit_bid))
                payout_if_win = shares * (1 - entry) * 0.98
                hold_exp = p_win * payout_if_win + (1 - p_win) * (-notl)
            elif 'YES' in (r['side'] or '').upper() or (r['token_side'] or '').upper() == 'YES':
                # YES trade (flipped): win prob ≈ exit_bid (which is now YES bid)
                p_win = max(0, min(1, exit_bid))
                payout_if_win = shares * (1 - entry) * 0.98
                hold_exp = p_win * payout_if_win + (1 - p_win) * (-notl)
            else:
                hold_exp = realized  # don't know side
        else:
            hold_exp = realized
        delta = hold_exp - realized
        total_realized += realized
        total_hold_expected += hold_exp
        ts = r['token_side'] or r['side'] or '-'
        print(f"  #{r['id']:>3} {r['city']:<13} {ts[:5]:<5} {entry:>5.3f} "
              f"{notl:>4.0f} {fmt(realized):>9}  {fmt(hold_exp):>9}  "
              f"{fmt(delta):>8}")

    print()
    print(f"  TOTAL realized (catastrophic exit) : {fmt(total_realized)}")
    print(f"  TOTAL hold expected               : {fmt(total_hold_expected)}")
    print(f"  Catastrophic_flip net effect     : {fmt(total_realized - total_hold_expected)}")
    print(f"  (positive = catastrophic helped, negative = hurt vs hold)")

    # ========== 5. Where the REAL P&L damage actually came from ==========
    hr("5. ALL exit types decomposed (last 48h)")
    rows = c.execute("""
        SELECT
          CASE
            WHEN outcome = 2 THEN COALESCE(exit_reason, 'unknown_rebal')
            WHEN pnl > 0 THEN 'WIN_settle'
            ELSE 'LOSS_settle'
          END AS exit_type,
          COUNT(*) AS n,
          ROUND(SUM(pnl), 2) AS net,
          ROUND(AVG(pnl), 2) AS avg
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        GROUP BY exit_type
        ORDER BY net
    """).fetchall()
    print(f"  {'exit_type':<26} {'n':>3}  {'net':>11}  {'avg':>9}")
    grand = 0.0
    for r in rows:
        grand += r['net'] or 0
        print(f"  {r['exit_type']:<26} {r['n']:>3}  {fmt(r['net']):>11}  "
              f"{fmt(r['avg']):>9}")
    print(f"  {'-'*26} {'-'*3}  {'-'*11}")
    print(f"  TOTAL                                   {fmt(grand):>11}")

    c.close()


if __name__ == "__main__":
    main()
