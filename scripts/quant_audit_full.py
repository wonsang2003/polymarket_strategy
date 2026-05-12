"""Comprehensive quant-style audit of the trading book.

Decomposes:
  1. Lifetime P&L by category × side
  2. Hit rate, expectancy, profit factor
  3. Weekly P&L trend (is it getting better or worse?)
  4. Edge → realized PnL relationship (is our model edge predictive?)
  5. Worst-trade topology (where's the tail risk?)
  6. City × lead breakdown (which buckets bleed?)
  7. Open positions exposure + unrealized P&L
  8. Rebalance exit effectiveness vs counterfactual hold-to-settlement

Run against weather.db on EC2:
    python /home/ubuntu/polymarket/scripts/quant_audit_full.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/home/ubuntu/polymarket/data/weather/weather.db")


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

    # =====================================================================
    # 0. Top-line numbers
    # =====================================================================
    hr("0. TOP-LINE")
    row = c.execute("""
        SELECT
          COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS n_settled,
          COUNT(*) FILTER (WHERE outcome IS NULL) AS n_open,
          ROUND(SUM(pnl) FILTER (WHERE outcome IS NOT NULL), 2) AS lifetime_pnl,
          ROUND(SUM(notional) FILTER (WHERE outcome IS NULL), 2) AS open_notional,
          ROUND(AVG(notional) FILTER (WHERE outcome IS NOT NULL), 2) AS avg_settled_notional
        FROM trade_history
    """).fetchone()
    print(f"  Settled trades        : {row['n_settled']}")
    print(f"  Open positions        : {row['n_open']}")
    print(f"  Lifetime realised P&L : {fmt_money(row['lifetime_pnl'])}")
    print(f"  Open notional         : ${row['open_notional']:,.2f}")
    print(f"  Avg notional/trade    : ${row['avg_settled_notional']:,.2f}")

    # =====================================================================
    # 1. Lifetime by category × side
    # =====================================================================
    hr("1. P&L BY (CATEGORY × SIDE × OUTCOME-TYPE)")
    rows = c.execute("""
        SELECT
          COALESCE(category,'<null>') AS cat,
          COALESCE(side,'<null>') AS side,
          CASE
            WHEN outcome = 2 THEN 'rebalance_exit'
            WHEN pnl > 0 THEN 'WIN'
            ELSE 'LOSS'
          END AS result,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS pnl_sum,
          ROUND(AVG(pnl),2) AS pnl_avg,
          ROUND(MIN(pnl),2) AS pnl_min,
          ROUND(MAX(pnl),2) AS pnl_max
        FROM trade_history
        WHERE outcome IS NOT NULL
        GROUP BY cat, side, result
        ORDER BY cat, side, result
    """).fetchall()
    print(f"  {'category':<18}{'side':<10}{'result':<16}{'n':>4}  "
          f"{'pnl_sum':>11}  {'avg':>8}  {'min':>8}  {'max':>8}")
    for r in rows:
        print(f"  {r['cat']:<18}{r['side']:<10}{r['result']:<16}{r['n']:>4}  "
              f"{fmt_money(r['pnl_sum']):>11}  {r['pnl_avg']:>+8.2f}  "
              f"{r['pnl_min']:>+8.2f}  {r['pnl_max']:>+8.2f}")

    # =====================================================================
    # 2. Hit rate, expectancy, profit factor by category
    # =====================================================================
    hr("2. HIT RATE & EXPECTANCY BY CATEGORY")
    rows = c.execute("""
        SELECT
          COALESCE(category,'<null>') AS cat,
          COUNT(*) AS n,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
          SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) AS gross_loss,
          AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,
          AVG(CASE WHEN pnl <= 0 THEN pnl END) AS avg_loss,
          SUM(pnl) AS net
        FROM trade_history
        WHERE outcome IS NOT NULL
        GROUP BY cat
        ORDER BY net DESC
    """).fetchall()
    print(f"  {'category':<18}{'n':>4} {'win%':>7} {'avg_win':>9} {'avg_loss':>10}  "
          f"{'PF':>6}  {'expectancy':>11}  {'net':>11}")
    for r in rows:
        wr = r['wins'] / r['n'] if r['n'] else 0
        pf = abs(r['gross_profit'] / r['gross_loss']) if r['gross_loss'] else float('inf')
        exp = r['net'] / r['n'] if r['n'] else 0
        avg_win = r['avg_win'] or 0
        avg_loss = r['avg_loss'] or 0
        print(f"  {r['cat']:<18}{r['n']:>4} {wr*100:>6.1f}% "
              f"{avg_win:>+8.2f} {avg_loss:>+9.2f}  "
              f"{pf:>5.2f}  {fmt_money(exp):>11}  {fmt_money(r['net']):>11}")

    # =====================================================================
    # 3. Weekly P&L trend
    # =====================================================================
    hr("3. WEEKLY P&L TREND (UTC week of settled_at)")
    rows = c.execute("""
        SELECT
          strftime('%Y-W%W', settled_at) AS wk,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 3) AS wr
        FROM trade_history
        WHERE outcome IS NOT NULL AND settled_at IS NOT NULL
        GROUP BY wk
        ORDER BY wk
    """).fetchall()
    print(f"  {'week':<12}{'n':>4}  {'win%':>7}  {'net':>11}  {'cumul':>11}")
    cumul = 0.0
    for r in rows:
        cumul += r['net'] or 0
        print(f"  {r['wk']:<12}{r['n']:>4}  {(r['wr'] or 0)*100:>6.1f}%  "
              f"{fmt_money(r['net']):>11}  {fmt_money(cumul):>11}")

    # =====================================================================
    # 4. Edge → realized PnL: decile bucketing (is edge predictive?)
    # =====================================================================
    hr("4. EDGE-DECILE → REALIZED PnL  (does our edge translate to money?)")
    edges_rows = c.execute("""
        SELECT edge, pnl, notional, category
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2 AND edge IS NOT NULL
        ORDER BY edge
    """).fetchall()
    if edges_rows:
        n = len(edges_rows)
        deciles = []
        for i in range(10):
            lo = i * n // 10
            hi = (i + 1) * n // 10 if i < 9 else n
            chunk = edges_rows[lo:hi]
            if not chunk:
                continue
            edges = [r['edge'] for r in chunk]
            pnls = [r['pnl'] for r in chunk]
            notionals = [r['notional'] for r in chunk]
            wins = sum(1 for p in pnls if p > 0)
            roi_sum = sum(p / nt if nt else 0 for p, nt in zip(pnls, notionals))
            deciles.append({
                "decile": i + 1,
                "edge_lo": min(edges),
                "edge_hi": max(edges),
                "n": len(chunk),
                "win_rate": wins / len(chunk),
                "avg_pnl": sum(pnls) / len(chunk),
                "avg_roi": roi_sum / len(chunk),
                "net": sum(pnls),
            })
        print(f"  {'decile':>6}  {'edge range':<22}  {'n':>4}  {'win%':>6}  "
              f"{'avg_pnl':>9}  {'avg_roi':>8}  {'net':>11}")
        for d in deciles:
            erange = f"[{d['edge_lo']*100:>+5.1f}, {d['edge_hi']*100:>+5.1f}]"
            print(f"  {d['decile']:>6}  {erange:<22}  {d['n']:>4}  "
                  f"{d['win_rate']*100:>5.1f}%  {d['avg_pnl']:>+8.2f}  "
                  f"{d['avg_roi']*100:>+7.2f}%  {fmt_money(d['net']):>11}")

        # Spearman-ish: does avg_pnl monotonically rise with edge decile?
        avg_pnls = [d['avg_pnl'] for d in deciles]
        positive_steps = sum(
            1 for i in range(1, len(avg_pnls)) if avg_pnls[i] > avg_pnls[i-1]
        )
        print(f"\n  Edge → avg_pnl monotonic up? "
              f"{positive_steps}/{len(avg_pnls)-1} adjacent decile pairs increase.")

    # =====================================================================
    # 5. Worst trade topology
    # =====================================================================
    hr("5. WORST 15 SETTLED TRADES (tail-risk source)")
    rows = c.execute("""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional, ROUND(pnl,2) AS pnl,
               ROUND(edge,3) AS edge, settled_at
        FROM trade_history
        WHERE outcome IS NOT NULL
        ORDER BY pnl ASC
        LIMIT 15
    """).fetchall()
    print(f"  {'id':>4} {'city':<14} {'cat':<18} {'side':<7} "
          f"{'px':>6} {'notl':>6} {'edge':>7} {'pnl':>10}  {'settled':<11}")
    for r in rows:
        print(f"  {r['id']:>4} {r['city']:<14} {r['cat']:<18} "
              f"{(r['side'] or '—'):<7} {r['px']:>6.3f} {r['notional']:>6.0f} "
              f"{(r['edge'] or 0):>+6.3f} {fmt_money(r['pnl']):>10}  "
              f"{(r['settled_at'] or '')[:10]}")

    # =====================================================================
    # 6. City × lead breakdown
    # =====================================================================
    hr("6. CITY × LEAD BREAKDOWN (which buckets bleed?)")
    rows = c.execute("""
        SELECT
          city,
          CAST(julianday(target_date) - julianday(date(created_at)) AS INT) AS lead_d,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
        FROM trade_history
        WHERE outcome IS NOT NULL
        GROUP BY city, lead_d
        HAVING n >= 3
        ORDER BY net ASC
        LIMIT 25
    """).fetchall()
    print(f"  {'city':<14} {'lead_d':>6}  {'n':>4}  {'win%':>6}  "
          f"{'avg':>8}  {'net':>11}")
    for r in rows:
        wr = r['wins'] / r['n'] if r['n'] else 0
        print(f"  {r['city']:<14} {r['lead_d']:>6}  {r['n']:>4}  "
              f"{wr*100:>5.1f}%  {r['avg']:>+7.2f}  "
              f"{fmt_money(r['net']):>11}")

    # =====================================================================
    # 7. Rebalance exit effectiveness
    # =====================================================================
    hr("7. REBALANCE-EXIT EFFECTIVENESS (outcome=2)")
    row = c.execute("""
        SELECT
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
          ROUND(MIN(pnl),2) AS worst,
          ROUND(MAX(pnl),2) AS best,
          ROUND(AVG(notional),2) AS avg_notional
        FROM trade_history
        WHERE outcome = 2
    """).fetchone()
    print(f"  Rebalance exits   : {row['n']}")
    print(f"  Net P&L           : {fmt_money(row['net'])}")
    print(f"  Avg / exit        : {fmt_money(row['avg'])}")
    print(f"  Win/loss exits    : {row['wins']}W / {row['losses']}L")
    print(f"  Best/worst exit   : {fmt_money(row['best'])} / {fmt_money(row['worst'])}")
    print(f"  Avg notional      : ${row['avg_notional']:,.2f}")

    # =====================================================================
    # 8. Open positions: exposure + latest MTM
    # =====================================================================
    hr("8. OPEN POSITIONS — EXPOSURE + UNREALIZED P&L")
    has_mp = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
    ).fetchone()
    if has_mp:
        rows = c.execute("""
            SELECT
              t.id, t.city, t.target_date, COALESCE(t.category,'<null>') AS cat,
              t.side, t.token_side, ROUND(t.entry_price,3) AS entry_px,
              ROUND(t.notional,2) AS notional,
              mp.best_bid AS bid, mp.best_ask AS ask,
              mp.fetched_at_utc AS fetched
            FROM trade_history t
            LEFT JOIN (
                SELECT mp1.*
                FROM market_prices mp1
                JOIN (
                    SELECT token_id, MAX(fetched_at_utc) AS mx
                    FROM market_prices GROUP BY token_id
                ) lt
                  ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
            ) mp ON mp.token_id = t.token_id
            WHERE t.outcome IS NULL
            ORDER BY t.target_date, t.city
        """).fetchall()
    else:
        rows = []

    total_notional = 0.0
    total_mtm_value = 0.0
    total_unrealized = 0.0
    by_target = {}
    by_cat = {}
    by_city = {}
    n_priced = 0

    print(f"  {'id':>4} {'city':<14} {'tgt':<10} {'cat':<18} "
          f"{'tk':>3} {'entry':>6} {'bid':>6} "
          f"{'notl':>6} {'shares':>7} {'mtm$':>8} {'upnl':>9} {'roi%':>7}")
    for r in rows:
        entry = r['entry_px'] or 0
        bid = r['bid']
        notional = r['notional'] or 0
        cat = r['cat']
        target = r['target_date']
        city = r['city']
        token_side = r['token_side'] or "—"
        total_notional += notional

        if entry > 0 and notional > 0:
            shares = notional / entry
        else:
            shares = 0

        if bid is not None:
            n_priced += 1
            mtm = shares * bid
            gross = shares * (bid - entry)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            roi = upnl / notional if notional > 0 else 0
        else:
            mtm = upnl = roi = None

        if mtm is not None:
            total_mtm_value += mtm
        if upnl is not None:
            total_unrealized += upnl

        by_target.setdefault(target, {"n": 0, "notional": 0.0, "upnl": 0.0})
        by_target[target]["n"] += 1
        by_target[target]["notional"] += notional
        if upnl is not None:
            by_target[target]["upnl"] += upnl

        by_cat.setdefault(cat, {"n": 0, "notional": 0.0, "upnl": 0.0})
        by_cat[cat]["n"] += 1
        by_cat[cat]["notional"] += notional
        if upnl is not None:
            by_cat[cat]["upnl"] += upnl

        by_city.setdefault(city, {"n": 0, "notional": 0.0, "upnl": 0.0})
        by_city[city]["n"] += 1
        by_city[city]["notional"] += notional
        if upnl is not None:
            by_city[city]["upnl"] += upnl

        bid_str = f"{bid:.3f}" if bid is not None else "—"
        mtm_str = f"{mtm:>7.2f}" if mtm is not None else "    —  "
        upnl_str = f"{upnl:>+8.2f}" if upnl is not None else "    —   "
        roi_str = f"{roi*100:>+6.1f}%" if roi is not None else "    —"
        print(f"  {r['id']:>4} {city:<14} {target:<10} {cat:<18} "
              f"{token_side:>3} {entry:>6.3f} {bid_str:>6} "
              f"{notional:>6.0f} {shares:>7.1f} {mtm_str:>8} "
              f"{upnl_str:>9} {roi_str:>7}")

    print(f"\n  TOTAL notional        : ${total_notional:,.2f}")
    print(f"  TOTAL MTM value       : ${total_mtm_value:,.2f}")
    print(f"  TOTAL unrealised P&L  : {fmt_money(total_unrealized)}")
    print(f"  Priced positions      : {n_priced}/{len(rows)}")

    print("\n  By target_date:")
    for d, agg in sorted(by_target.items()):
        print(f"    {d}: n={agg['n']:>3}  notional=${agg['notional']:>7.2f}  "
              f"upnl={fmt_money(agg['upnl'])}")

    print("\n  By category:")
    for k, agg in sorted(by_cat.items(), key=lambda x: x[1]["upnl"]):
        print(f"    {k:<20} n={agg['n']:>3}  notional=${agg['notional']:>7.2f}  "
              f"upnl={fmt_money(agg['upnl'])}")

    print("\n  By city (top losers):")
    for k, agg in sorted(by_city.items(), key=lambda x: x[1]["upnl"])[:10]:
        print(f"    {k:<14} n={agg['n']:>3}  notional=${agg['notional']:>7.2f}  "
              f"upnl={fmt_money(agg['upnl'])}")

    # =====================================================================
    # 9. Counterfactual: would NO-rebalance have done better?
    # =====================================================================
    hr("9. REBALANCE COUNTERFACTUAL (rebalance vs hold-to-settlement)")
    print("  Note: this only compares trades that ACTUALLY exited via rebalance")
    print("  (outcome=2). We can't replay the alt history; we can only flag the")
    print("  realized rebalance-exit P&L sum and ask whether holding would have")
    print("  resolved YES (counterfactual loss = -notional + small price move).")
    row = c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl),2) AS net, ROUND(AVG(pnl),2) AS avg
        FROM trade_history WHERE outcome=2
    """).fetchone()
    print(f"  Realized rebalance-exit net : {fmt_money(row['net'])} over {row['n']} exits")
    print(f"  If all those positions had instead resolved 50/50 (worst-case naive),")
    print(f"  expected hold P&L per exit ≈ -0.5×notional + 0.5×payout.")

    # =====================================================================
    # 10. Sharpe-ish + drawdown
    # =====================================================================
    hr("10. RISK STATS")
    rows = c.execute("""
        SELECT settled_at, pnl FROM trade_history
        WHERE outcome IS NOT NULL AND settled_at IS NOT NULL
        ORDER BY settled_at
    """).fetchall()
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
        sharpe = (mean / std) if std > 0 else 0  # per-trade Sharpe
        print(f"  Settled trades       : {n}")
        print(f"  Mean P&L / trade     : {fmt_money(mean)}")
        print(f"  Std P&L / trade      : ${std:,.2f}")
        print(f"  Per-trade Sharpe     : {sharpe:.3f}")
        print(f"  Cumulative final     : {fmt_money(cumul[-1])}")
        print(f"  Cumulative peak      : {fmt_money(peak)}")
        print(f"  Max drawdown         : {fmt_money(max_dd)}")
        print(f"  Best trade           : {fmt_money(max(pnls))}")
        print(f"  Worst trade          : {fmt_money(min(pnls))}")

    c.close()


if __name__ == "__main__":
    main()
