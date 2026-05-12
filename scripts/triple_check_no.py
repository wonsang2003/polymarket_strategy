"""Triple-check NO trades — pull every raw fact the dashboard sees,
no interpretation."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")
WIN_H = 36


def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(t):
    print()
    print("=" * 78)
    print(f"  {t}")
    print("=" * 78)


def main():
    c = conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WIN_H)).strftime("%Y-%m-%dT%H:%M:%S")
    short_cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
    print(f"\nNOW: {now_kst}")
    print(f"36h cutoff (UTC): {cutoff}")
    print(f"6h cutoff  (UTC): {short_cutoff}")

    # =====================================================================
    hr("Q1: milan #187 raw row — is it really a parsing artifact?")
    r = c.execute("""SELECT id, city, target_date, question, bracket_lower_f,
        bracket_upper_f, side, token_side, entry_price, edge, model_prob,
        market_prob, outcome, pnl, category, market_id, token_id
        FROM trade_history WHERE id=187""").fetchone()
    if r:
        for k in r.keys():
            print(f"  {k:<22}: {r[k]!r}")
    else:
        print("  not found")

    # =====================================================================
    hr("Q2: ALL wide-bracket NO trades EVER (width > 10F) — pattern?")
    rows = c.execute("""
        SELECT id, city, target_date,
               ROUND(bracket_upper_f - bracket_lower_f, 1) AS w,
               ROUND(bracket_lower_f, 2) AS lo,
               ROUND(bracket_upper_f, 2) AS up,
               ROUND(entry_price, 3) AS px,
               ROUND(edge, 3) AS edge,
               outcome, ROUND(pnl, 2) AS pnl,
               side, token_side,
               SUBSTR(question, 1, 70) AS q
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND (bracket_upper_f - bracket_lower_f) > 10
          AND (token_side='NO' OR side LIKE '%NO%')
        ORDER BY id
    """).fetchall()
    print(f"  {'id':>4} {'city':<12} {'target':<11} {'w':>6} {'lo':>7} "
          f"{'up':>7} {'px':>5} {'edge':>6} {'out':>3} {'pnl':>7}")
    for r in rows:
        print(f"  #{r['id']:>3} {r['city']:<12} {r['target_date']:<11} "
              f"{r['w']:>6}F {r['lo']:>7} {r['up']:>7} {r['px']:>5.3f} "
              f"{r['edge']:>+5.3f} {r['outcome']:>3} {r['pnl']:>+6.2f}")
        print(f"        q={r['q']!r}")
    if rows:
        net = sum(r['pnl'] or 0 for r in rows)
        wins = sum(1 for r in rows if r['outcome'] != 2 and (r['pnl'] or 0) > 0)
        losses = sum(1 for r in rows if r['outcome'] != 2 and (r['pnl'] or 0) <= 0)
        print(f"\n  Total: {len(rows)} wide-NO trades, {wins}W/{losses}L, net=${net:+.2f}")

    # =====================================================================
    hr(f"Q3: Every NO event in last {WIN_H}h — settles + rebals, itemized")
    rows = c.execute(f"""
        SELECT id, city, target_date,
               ROUND(bracket_upper_f - bracket_lower_f, 1) AS w,
               ROUND(entry_price, 3) AS px,
               ROUND(edge, 3) AS edge,
               outcome, ROUND(pnl, 2) AS pnl,
               side, token_side, COALESCE(category, '<null>') AS cat,
               settled_at, exit_reason
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND (token_side='NO' OR side LIKE '%NO%')
          AND datetime(settled_at) >= datetime('now', '-{WIN_H} hours')
        ORDER BY settled_at
    """).fetchall()
    n_settle_w = sum(1 for r in rows if r['outcome'] != 2 and (r['pnl'] or 0) > 0)
    n_settle_l = sum(1 for r in rows if r['outcome'] != 2 and (r['pnl'] or 0) <= 0)
    n_rebal_w = sum(1 for r in rows if r['outcome'] == 2 and (r['pnl'] or 0) > 0)
    n_rebal_l = sum(1 for r in rows if r['outcome'] == 2 and (r['pnl'] or 0) <= 0)
    pnl_settle_w = sum(r['pnl'] or 0 for r in rows if r['outcome'] != 2 and (r['pnl'] or 0) > 0)
    pnl_settle_l = sum(r['pnl'] or 0 for r in rows if r['outcome'] != 2 and (r['pnl'] or 0) <= 0)
    pnl_rebal_w = sum(r['pnl'] or 0 for r in rows if r['outcome'] == 2 and (r['pnl'] or 0) > 0)
    pnl_rebal_l = sum(r['pnl'] or 0 for r in rows if r['outcome'] == 2 and (r['pnl'] or 0) <= 0)

    print(f"  SETTLE-WIN  : n={n_settle_w:>3} sum=${pnl_settle_w:+.2f}")
    print(f"  SETTLE-LOSS : n={n_settle_l:>3} sum=${pnl_settle_l:+.2f}")
    print(f"  REBAL-WIN   : n={n_rebal_w:>3} sum=${pnl_rebal_w:+.2f}")
    print(f"  REBAL-LOSS  : n={n_rebal_l:>3} sum=${pnl_rebal_l:+.2f}")
    print(f"  TOTAL       : n={len(rows):>3} sum=${(pnl_settle_w+pnl_settle_l+pnl_rebal_w+pnl_rebal_l):+.2f}")

    print(f"\n  Itemized:")
    print(f"  {'when (KST)':<14} {'id':>4} {'city':<12} {'cat':<18} "
          f"{'px':>5} {'edge':>6} {'w':>5} {'out':>3} {'pnl':>7} {'reason':<22}")
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r['settled_at']).replace("Z", "+00:00").replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kst = dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")
        except Exception:
            kst = str(r['settled_at'])[:16]
        print(f"  {kst:<14} #{r['id']:>3} {r['city']:<12} {r['cat']:<18} "
              f"{r['px']:>5.3f} {r['edge']:>+5.3f} {r['w']:>4}F "
              f"{r['outcome']:>3} {r['pnl']:>+6.2f} {(r['exit_reason'] or '-'):<22}")

    # =====================================================================
    hr("Q4: Did 06:00 KST tick fire catastrophic exits? Last 6h all exits")
    rows = c.execute(f"""
        SELECT id, city, ROUND(entry_price, 3) AS px, ROUND(pnl, 2) AS pnl,
               settled_at, exit_reason, outcome,
               COALESCE(category, '<null>') AS cat
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-6 hours')
        ORDER BY settled_at
    """).fetchall()
    print(f"  {len(rows)} exits/settles in the last 6 hours")
    for r in rows:
        print(f"    #{r['id']:>3} {r['city']:<12} {r['cat']:<18} "
              f"px={r['px']:.3f} out={r['outcome']} pnl={r['pnl']:+.2f} "
              f"reason={r['exit_reason']!r} at={r['settled_at']}")

    # =====================================================================
    hr("Q5: Counterfactual without rebalance — would NO trades net more?")
    print("  HYPOTHESIS: rebalance bleed accounts for the gap between")
    print("  directional-net (+$X) and total-NO-net (+$Y).")
    settle_pnl = c.execute(f"""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2
          AND (token_side='NO' OR side LIKE '%NO%')
          AND datetime(settled_at) >= datetime('now', '-{WIN_H} hours')
    """).fetchone()
    rebal_pnl = c.execute(f"""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome = 2
          AND (token_side='NO' OR side LIKE '%NO%')
          AND datetime(settled_at) >= datetime('now', '-{WIN_H} hours')
    """).fetchone()
    print(f"  Directional NO net : ${settle_pnl['net']:+.2f}  ({settle_pnl['n']} trades)")
    print(f"  Rebalance NO net   : ${rebal_pnl['net']:+.2f}  ({rebal_pnl['n']} trades)")
    print(f"  Total NO net       : ${(settle_pnl['net'] or 0) + (rebal_pnl['net'] or 0):+.2f}")
    avg_settle = (settle_pnl['net'] or 0) / settle_pnl['n'] if settle_pnl['n'] else 0
    avg_rebal = (rebal_pnl['net'] or 0) / rebal_pnl['n'] if rebal_pnl['n'] else 0
    print(f"  Avg per directional: ${avg_settle:+.2f}")
    print(f"  Avg per rebal exit : ${avg_rebal:+.2f}")

    # =====================================================================
    hr("Q6: NO trades grouped by category × outcome (definitive cut)")
    rows = c.execute(f"""
        SELECT COALESCE(category, '<null>') AS cat,
               CASE WHEN outcome = 2 THEN 'rebal'
                    WHEN pnl > 0 THEN 'WIN'
                    ELSE 'LOSS' END AS r,
               COUNT(*) AS n,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(pnl), 2) AS avg
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND (token_side='NO' OR side LIKE '%NO%')
          AND datetime(settled_at) >= datetime('now', '-{WIN_H} hours')
        GROUP BY cat, r
        ORDER BY cat, r
    """).fetchall()
    print(f"  {'category':<22} {'result':<6} {'n':>3} {'net':>9} {'avg':>8}")
    for r in rows:
        print(f"  {r['cat']:<22} {r['r']:<6} {r['n']:>3} "
              f"${r['net']:+8.2f} ${r['avg']:+7.2f}")

    # =====================================================================
    hr("Q7: NO open positions right now — what's projected to settle?")
    rows = c.execute("""
        SELECT t.id, t.city, t.target_date,
               COALESCE(t.category, '<null>') AS cat,
               t.token_side,
               ROUND(t.bracket_upper_f - t.bracket_lower_f, 1) AS w,
               ROUND(t.entry_price, 3) AS px,
               ROUND(t.edge, 3) AS edge,
               t.notional,
               mp.best_bid AS bid
        FROM trade_history t
        LEFT JOIN (
            SELECT mp1.* FROM market_prices mp1
            JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx
                  FROM market_prices GROUP BY token_id) lt
              ON lt.token_id = mp1.token_id
             AND lt.mx = mp1.fetched_at_utc
        ) mp ON mp.token_id = t.token_id
        WHERE t.outcome IS NULL
          AND (t.token_side='NO' OR t.side LIKE '%NO%')
        ORDER BY t.target_date, t.city
    """).fetchall()

    danger = []  # bid < 0.10
    cruising = []  # bid > entry
    middle = []
    for r in rows:
        if r['bid'] is None:
            continue
        if r['bid'] < 0.10:
            danger.append(r)
        elif r['bid'] > (r['px'] or 0):
            cruising.append(r)
        else:
            middle.append(r)
    print(f"  Total open NO: {len(rows)}")
    print(f"  Walking-dead (bid<0.10): {len(danger)}")
    for r in danger:
        likely_loss = r['notional']
        print(f"    #{r['id']:>3} {r['city']:<12} {r['target_date']} "
              f"{r['cat']:<18} px={r['px']:.3f} bid={r['bid']:.3f} "
              f"notional=${r['notional']:.0f} "
              f"likely_loss=${likely_loss:.0f}")
    print(f"  Cruising (bid>entry): {len(cruising)}")
    for r in cruising[:10]:
        shares = r['notional'] / r['px'] if r['px'] else 0
        gross = shares * (r['bid'] - r['px'])
        upnl = gross - 0.02 * gross if gross > 0 else gross
        print(f"    #{r['id']:>3} {r['city']:<12} {r['target_date']} "
              f"{r['cat']:<18} px={r['px']:.3f} bid={r['bid']:.3f} "
              f"upnl=${upnl:+.2f}")
    print(f"  In-between: {len(middle)}")

    c.close()


if __name__ == "__main__":
    main()
