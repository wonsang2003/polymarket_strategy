"""Quant audit, scoped to the post-fix window.

Cutoff: 36 hours ago (i.e., trades created since the late-Apr-26 fixes:
Plan B tightening, narrow-bracket NO cap, low-temp category bug, smart
token_side fallback, tail-NO production rollout).

Compares post-fix performance against pre-fix history so we can decide
whether the curated strategy is actually working.

Run on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/quant_audit_post_fix.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("/home/ubuntu/polymarket/data/weather/weather.db")
WINDOW_HOURS = 36


def conn():
    c = sqlite3.connect(f"file:{DB_PATH}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(title: str, char: str = "="):
    print()
    print(char * 78)
    print(f"  {title}")
    print(char * 78)


def fmt_money(v):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):>9,.2f}"


def main():
    c = conn()
    cutoff_utc = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    # =====================================================================
    # 0. Window definition + counts
    # =====================================================================
    hr(f"0. WINDOW — last {WINDOW_HOURS}h (created_at ≥ {cutoff_utc} UTC)")
    row = c.execute("""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE outcome IS NULL) AS still_open,
          COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS settled_or_exited,
          COUNT(*) FILTER (WHERE outcome = 1) AS yes_resolved,
          COUNT(*) FILTER (WHERE outcome = 0) AS no_resolved,
          COUNT(*) FILTER (WHERE outcome = 2) AS rebalance_exited,
          ROUND(SUM(notional) FILTER (WHERE outcome IS NULL),2) AS open_notional,
          ROUND(SUM(notional),2) AS total_notional_traded
        FROM trade_history
        WHERE created_at >= ?
    """, (cutoff_utc,)).fetchone()
    print(f"  Trades created           : {row['total']}")
    print(f"  Still open               : {row['still_open']}")
    print(f"  Settled / exited         : {row['settled_or_exited']}")
    print(f"    YES resolved           : {row['yes_resolved']}")
    print(f"    NO resolved            : {row['no_resolved']}")
    print(f"    Rebalance exited       : {row['rebalance_exited']}")
    print(f"  Open notional            : ${row['open_notional'] or 0:,.2f}")
    print(f"  Total notional in window : ${row['total_notional_traded'] or 0:,.2f}")

    # Headline P&L for the window
    row = c.execute("""
        SELECT
          ROUND(SUM(pnl),2) AS realized_pnl,
          COUNT(*) AS n_realized
        FROM trade_history
        WHERE created_at >= ? AND outcome IS NOT NULL
    """, (cutoff_utc,)).fetchone()
    print(f"  Realized P&L (window)    : {fmt_money(row['realized_pnl'])} over {row['n_realized']} trades")

    # =====================================================================
    # 1. Pre/post-fix comparison
    # =====================================================================
    hr("1. PRE-FIX vs POST-FIX SIDE-BY-SIDE")
    pre = c.execute("""
        SELECT
          COUNT(*) AS n,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg,
          ROUND(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END),2) AS gross_profit,
          ROUND(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END),2) AS gross_loss
        FROM trade_history
        WHERE outcome IS NOT NULL AND created_at < ?
    """, (cutoff_utc,)).fetchone()
    post = c.execute("""
        SELECT
          COUNT(*) AS n,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg,
          ROUND(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END),2) AS gross_profit,
          ROUND(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END),2) AS gross_loss
        FROM trade_history
        WHERE outcome IS NOT NULL AND created_at >= ?
    """, (cutoff_utc,)).fetchone()

    def pct(w, n):
        return (w / n * 100) if n else 0.0

    def pf(p, l):
        return abs(p / l) if l else float('inf')

    print(f"  {'metric':<22}{'pre-fix':>14}{'post-fix':>14}")
    print(f"  {'-'*22}{'-'*14}{'-'*14}")
    print(f"  {'n trades':<22}{pre['n']:>14}{post['n']:>14}")
    print(f"  {'wins':<22}{pre['wins']:>14}{post['wins']:>14}")
    print(f"  {'win rate':<22}"
          f"{pct(pre['wins'], pre['n']):>13.1f}%"
          f"{pct(post['wins'], post['n']):>13.1f}%")
    print(f"  {'net P&L':<22}{fmt_money(pre['net']):>14}{fmt_money(post['net']):>14}")
    print(f"  {'avg / trade':<22}{fmt_money(pre['avg']):>14}{fmt_money(post['avg']):>14}")
    print(f"  {'gross profit':<22}{fmt_money(pre['gross_profit']):>14}{fmt_money(post['gross_profit']):>14}")
    print(f"  {'gross loss':<22}{fmt_money(pre['gross_loss']):>14}{fmt_money(post['gross_loss']):>14}")
    print(f"  {'profit factor':<22}"
          f"{pf(pre['gross_profit'], pre['gross_loss']):>14.2f}"
          f"{pf(post['gross_profit'], post['gross_loss']):>14.2f}")

    # =====================================================================
    # 2. Post-fix decomposition by (category × outcome)
    # =====================================================================
    hr("2. POST-FIX BREAKDOWN BY (CATEGORY × OUTCOME)")
    rows = c.execute("""
        SELECT
          COALESCE(category,'<null>') AS cat,
          CASE
            WHEN outcome = 2 THEN 'rebalance_exit'
            WHEN pnl > 0 THEN 'WIN'
            ELSE 'LOSS'
          END AS result,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg,
          ROUND(MIN(pnl),2) AS worst,
          ROUND(MAX(pnl),2) AS best
        FROM trade_history
        WHERE outcome IS NOT NULL AND created_at >= ?
        GROUP BY cat, result
        ORDER BY cat, result
    """, (cutoff_utc,)).fetchall()
    print(f"  {'category':<20}{'result':<18}{'n':>4}  {'net':>11}  {'avg':>8}  "
          f"{'worst':>8}  {'best':>8}")
    for r in rows:
        print(f"  {r['cat']:<20}{r['result']:<18}{r['n']:>4}  "
              f"{fmt_money(r['net']):>11}  {(r['avg'] or 0):>+7.2f}  "
              f"{(r['worst'] or 0):>+7.2f}  {(r['best'] or 0):>+7.2f}")

    # =====================================================================
    # 3. Post-fix hit rate / expectancy / PF by category
    # =====================================================================
    hr("3. POST-FIX HIT RATE & EXPECTANCY BY CATEGORY")
    rows = c.execute("""
        SELECT
          COALESCE(category,'<null>') AS cat,
          COUNT(*) AS n,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gp,
          SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) AS gl,
          AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,
          AVG(CASE WHEN pnl <= 0 THEN pnl END) AS avg_loss,
          SUM(pnl) AS net
        FROM trade_history
        WHERE outcome IS NOT NULL AND created_at >= ?
        GROUP BY cat
        ORDER BY net DESC
    """, (cutoff_utc,)).fetchall()
    print(f"  {'category':<20}{'n':>4}  {'win%':>6}  {'avg_win':>8}  {'avg_loss':>9}  "
          f"{'PF':>6}  {'exp':>9}  {'net':>11}")
    for r in rows:
        wr = r['wins'] / r['n'] if r['n'] else 0
        pf = abs(r['gp'] / r['gl']) if r['gl'] else float('inf')
        avg_win = r['avg_win'] or 0
        avg_loss = r['avg_loss'] or 0
        exp = r['net'] / r['n'] if r['n'] else 0
        print(f"  {r['cat']:<20}{r['n']:>4}  {wr*100:>5.1f}%  "
              f"{avg_win:>+7.2f}  {avg_loss:>+8.2f}  {pf:>5.2f}  "
              f"{fmt_money(exp):>9}  {fmt_money(r['net']):>11}")

    # =====================================================================
    # 4. Post-fix edge-decile → realized P&L
    # =====================================================================
    hr("4. POST-FIX EDGE-DECILE → REALIZED P&L (settled non-rebalance only)")
    edges_rows = c.execute("""
        SELECT edge, pnl, notional
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2 AND edge IS NOT NULL
          AND created_at >= ?
        ORDER BY edge
    """, (cutoff_utc,)).fetchall()
    if edges_rows:
        n = len(edges_rows)
        # Use quintiles instead of deciles since post-fix sample may be small
        n_buckets = 5 if n >= 15 else min(3, n)
        print(f"  Using {n_buckets} buckets (n={n})")
        for i in range(n_buckets):
            lo = i * n // n_buckets
            hi = (i + 1) * n // n_buckets if i < n_buckets - 1 else n
            chunk = edges_rows[lo:hi]
            if not chunk:
                continue
            edges = [r['edge'] for r in chunk]
            pnls = [r['pnl'] for r in chunk]
            notionals = [r['notional'] for r in chunk]
            wins = sum(1 for p in pnls if p > 0)
            roi_sum = sum(p / nt if nt else 0 for p, nt in zip(pnls, notionals))
            erange = f"[{min(edges)*100:>+5.1f}, {max(edges)*100:>+6.1f}]"
            print(f"  bucket {i+1}  edge {erange}   n={len(chunk):>2}  "
                  f"win%={wins/len(chunk)*100:>5.1f}  "
                  f"avg_pnl={sum(pnls)/len(chunk):>+7.2f}  "
                  f"avg_roi={(roi_sum/len(chunk))*100:>+6.1f}%  "
                  f"net={fmt_money(sum(pnls))}")
    else:
        print("  No non-rebalance settled trades in window.")

    # =====================================================================
    # 5. Post-fix worst trades
    # =====================================================================
    hr("5. POST-FIX WORST 10 SETTLED TRADES")
    rows = c.execute("""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional, ROUND(pnl,2) AS pnl,
               ROUND(edge,3) AS edge, settled_at,
               CASE WHEN outcome=2 THEN 'rebal_exit' ELSE 'settle' END AS exit_type
        FROM trade_history
        WHERE outcome IS NOT NULL AND created_at >= ?
        ORDER BY pnl ASC
        LIMIT 10
    """, (cutoff_utc,)).fetchall()
    print(f"  {'id':>4} {'city':<14} {'cat':<18} {'side':<7} "
          f"{'px':>6} {'notl':>5} {'edge':>7} {'pnl':>10} {'exit':<11}")
    for r in rows:
        print(f"  {r['id']:>4} {r['city']:<14} {r['cat']:<18} "
              f"{(r['side'] or '—'):<7} {r['px']:>6.3f} {r['notional']:>5.0f} "
              f"{(r['edge'] or 0):>+6.3f} {fmt_money(r['pnl']):>10} "
              f"{r['exit_type']:<11}")

    # =====================================================================
    # 6. Post-fix best trades
    # =====================================================================
    hr("6. POST-FIX BEST 10 SETTLED TRADES")
    rows = c.execute("""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional, ROUND(pnl,2) AS pnl,
               ROUND(edge,3) AS edge, settled_at,
               CASE WHEN outcome=2 THEN 'rebal_exit' ELSE 'settle' END AS exit_type
        FROM trade_history
        WHERE outcome IS NOT NULL AND created_at >= ?
        ORDER BY pnl DESC
        LIMIT 10
    """, (cutoff_utc,)).fetchall()
    print(f"  {'id':>4} {'city':<14} {'cat':<18} {'side':<7} "
          f"{'px':>6} {'notl':>5} {'edge':>7} {'pnl':>10} {'exit':<11}")
    for r in rows:
        print(f"  {r['id']:>4} {r['city']:<14} {r['cat']:<18} "
              f"{(r['side'] or '—'):<7} {r['px']:>6.3f} {r['notional']:>5.0f} "
              f"{(r['edge'] or 0):>+6.3f} {fmt_money(r['pnl']):>10} "
              f"{r['exit_type']:<11}")

    # =====================================================================
    # 7. Post-fix rebalance effectiveness
    # =====================================================================
    hr("7. POST-FIX REBALANCE-EXIT EFFECTIVENESS")
    row = c.execute("""
        SELECT
          COUNT(*) AS n, ROUND(SUM(pnl),2) AS net, ROUND(AVG(pnl),2) AS avg,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
          ROUND(MIN(pnl),2) AS worst, ROUND(MAX(pnl),2) AS best,
          ROUND(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END),2) AS gross_loss,
          ROUND(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END),2) AS gross_profit
        FROM trade_history
        WHERE outcome = 2 AND created_at >= ?
    """, (cutoff_utc,)).fetchone()
    print(f"  Rebalance exits   : {row['n']}")
    print(f"  Net P&L           : {fmt_money(row['net'])}")
    print(f"  Avg / exit        : {fmt_money(row['avg'])}")
    print(f"  Win/loss exits    : {row['wins']}W / {row['losses']}L")
    print(f"  Best/worst exit   : {fmt_money(row['best'])} / {fmt_money(row['worst'])}")
    pf_rebal = abs(row['gross_profit'] / row['gross_loss']) if row['gross_loss'] else float('inf')
    print(f"  Profit factor     : {pf_rebal:.2f}")

    # Compare pre vs post rebalance:
    pre_rebal = c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl),2) AS net, ROUND(AVG(pnl),2) AS avg,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
        FROM trade_history WHERE outcome = 2 AND created_at < ?
    """, (cutoff_utc,)).fetchone()
    post_rebal = row
    print()
    print(f"  {'metric':<20}{'pre-fix rebal':>16}{'post-fix rebal':>16}")
    print(f"  {'-'*20}{'-'*16}{'-'*16}")
    print(f"  {'n exits':<20}{pre_rebal['n']:>16}{post_rebal['n']:>16}")
    print(f"  {'net':<20}{fmt_money(pre_rebal['net']):>16}{fmt_money(post_rebal['net']):>16}")
    print(f"  {'avg / exit':<20}{fmt_money(pre_rebal['avg']):>16}{fmt_money(post_rebal['avg']):>16}")
    pre_wr = pre_rebal['wins'] / pre_rebal['n'] * 100 if pre_rebal['n'] else 0
    post_wr = post_rebal['wins'] / post_rebal['n'] * 100 if post_rebal['n'] else 0
    print(f"  {'win % of exits':<20}{pre_wr:>15.1f}%{post_wr:>15.1f}%")

    # =====================================================================
    # 8. Post-fix risk stats
    # =====================================================================
    hr("8. POST-FIX RISK STATS")
    rows = c.execute("""
        SELECT settled_at, pnl FROM trade_history
        WHERE outcome IS NOT NULL AND settled_at IS NOT NULL
          AND created_at >= ?
        ORDER BY settled_at
    """, (cutoff_utc,)).fetchall()
    if rows:
        pnls = [r['pnl'] for r in rows]
        cumul = []
        running = 0.0
        for p in pnls:
            running += p
            cumul.append(running)
        peak = cumul[0]
        max_dd = 0.0
        for v in cumul:
            if v > peak:
                peak = v
            dd = v - peak
            if dd < max_dd:
                max_dd = dd
        n = len(pnls)
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / max(1, n - 1)
        std = var ** 0.5
        sharpe = (mean / std) if std > 0 else 0
        print(f"  Settled in window    : {n}")
        print(f"  Mean P&L / trade     : {fmt_money(mean)}")
        print(f"  Std P&L / trade      : ${std:,.2f}")
        print(f"  Per-trade Sharpe     : {sharpe:.3f}")
        print(f"  Cumulative final     : {fmt_money(cumul[-1])}")
        print(f"  Max drawdown         : {fmt_money(max_dd)}")
        print(f"  Best trade           : {fmt_money(max(pnls))}")
        print(f"  Worst trade          : {fmt_money(min(pnls))}")
    else:
        print("  No settled trades in window.")

    # =====================================================================
    # 9. Open positions: same MTM analysis but flag post-fix entry
    # =====================================================================
    hr("9. OPEN BOOK STATE (all open are post-fix-window entries)")
    has_mp = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
    ).fetchone()
    if has_mp:
        rows = c.execute("""
            SELECT
              t.id, t.city, t.target_date, COALESCE(t.category,'<null>') AS cat,
              t.token_side, ROUND(t.entry_price,3) AS entry_px,
              ROUND(t.notional,2) AS notional, t.created_at,
              mp.best_bid AS bid
            FROM trade_history t
            LEFT JOIN (
                SELECT mp1.* FROM market_prices mp1
                JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx FROM market_prices GROUP BY token_id) lt
                  ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
            ) mp ON mp.token_id = t.token_id
            WHERE t.outcome IS NULL AND t.created_at >= ?
            ORDER BY t.created_at
        """, (cutoff_utc,)).fetchall()
    else:
        rows = []

    total_notional = 0.0
    total_unrealized = 0.0
    big_winners = []
    big_losers = []
    walking_dead = []  # bid < 0.05, near-certain loss
    for r in rows:
        entry = r['entry_px'] or 0
        bid = r['bid']
        notional = r['notional'] or 0
        total_notional += notional
        if entry > 0 and bid is not None:
            shares = notional / entry
            gross = shares * (bid - entry)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            roi = upnl / notional if notional > 0 else 0
            total_unrealized += upnl
            if roi > 0.20:
                big_winners.append((r, upnl, roi, bid))
            if roi < -0.30:
                big_losers.append((r, upnl, roi, bid))
            if bid < 0.05:
                walking_dead.append((r, upnl, roi, bid))

    print(f"  Open positions          : {len(rows)}")
    print(f"  Total notional          : ${total_notional:,.2f}")
    print(f"  Sum unrealized P&L      : {fmt_money(total_unrealized)}")
    print(f"  Big winners (ROI>+20%)  : {len(big_winners)}")
    print(f"  Big losers  (ROI<-30%)  : {len(big_losers)}")
    print(f"  Walking-dead (bid<0.05) : {len(walking_dead)}")

    if walking_dead:
        print(f"\n  WALKING-DEAD POSITIONS (would be caught by emergency-exit):")
        for r, upnl, roi, bid in walking_dead:
            print(f"    id {r['id']:>3}  {r['city']:<14} entry={r['entry_px']:.3f} "
                  f"bid={bid:.3f} upnl={fmt_money(upnl)} roi={roi*100:+.1f}%")

    if big_winners:
        print(f"\n  BIG WINNERS (ROI > +20%):")
        for r, upnl, roi, bid in big_winners:
            print(f"    id {r['id']:>3}  {r['city']:<14} entry={r['entry_px']:.3f} "
                  f"bid={bid:.3f} upnl={fmt_money(upnl)} roi={roi*100:+.1f}%")

    if big_losers:
        print(f"\n  BIG LOSERS (ROI < -30%):")
        for r, upnl, roi, bid in big_losers:
            print(f"    id {r['id']:>3}  {r['city']:<14} entry={r['entry_px']:.3f} "
                  f"bid={bid:.3f} upnl={fmt_money(upnl)} roi={roi*100:+.1f}%")

    # =====================================================================
    # 10. Forward-looking: settle the realized + unrealized into one number
    # =====================================================================
    hr("10. POST-FIX TRUE NET (realized + unrealized)")
    realized = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net FROM trade_history
        WHERE outcome IS NOT NULL AND created_at >= ?
    """, (cutoff_utc,)).fetchone()['net'] or 0.0
    print(f"  Realized P&L (window)    : {fmt_money(realized)}")
    print(f"  Unrealized P&L (open)    : {fmt_money(total_unrealized)}")
    print(f"  TRUE net (window)        : {fmt_money(realized + total_unrealized)}")
    print(f"  Open notional at risk    : ${total_notional:,.2f}")

    c.close()


if __name__ == "__main__":
    main()
