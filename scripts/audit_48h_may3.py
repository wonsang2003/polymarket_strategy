"""48-hour activity audit since the city_calibration ship (May 1 ~ May 3).

Tracks:
  - Lifetime P&L delta vs the snapshot before
  - Settlements + entries + rebal exits in the last 48h
  - Per-category breakdown (weather, weather_tail_no, weather_tail_no_flipped)
  - Per-city breakdown
  - Whether city_calibration shows visible effect (bias-rejected entries)
  - Open book current state
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


def kst(ts):
    if ts is None:
        return "-"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def main():
    c = conn()
    now = datetime.now(timezone.utc)
    now_kst = (now + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
    cutoff = now - timedelta(hours=48)
    cutoff_kst = (cutoff + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
    print(f"\nNOW: {now_kst}")
    print(f"48h cutoff: {cutoff_kst}")

    # ---- Headline P&L
    hr("1. HEADLINE — lifetime cumulative P&L")
    cum = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history
    """).fetchone()
    print(f"  Lifetime P&L : {fmt(cum['net'])} ({cum['n']} settled events, {cum['open']} open)")

    pnl_48 = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-48 hours')
    """).fetchone()
    print(f"  Last 48h P&L : {fmt(pnl_48['net'])} ({pnl_48['n']} events)")

    # Daily breakdown for May 1, 2, 3 (KST)
    print("\n  Per-day P&L (KST):")
    for day_offset in [-2, -1, 0]:
        day_kst = (now + timedelta(hours=9, days=day_offset)).strftime("%Y-%m-%d")
        r = c.execute(
            "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
            "FROM trade_history WHERE settled_at LIKE ? || '%'",
            (day_kst,)
        ).fetchone()
        print(f"    {day_kst}: {fmt(r['net'])}  ({r['n']} events)")

    # ---- Last 48h activity by category
    hr("2. LAST 48h BREAKDOWN by category × outcome")
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               CASE WHEN outcome=2 THEN 'rebal'
                    WHEN pnl>0 THEN 'WIN'
                    ELSE 'LOSS' END AS r,
               COUNT(*) AS n,
               ROUND(SUM(pnl),2) AS net,
               ROUND(AVG(pnl),2) AS avg
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        GROUP BY cat, r
        ORDER BY cat, r
    """).fetchall()
    print(f"  {'category':<26}{'result':<8}{'n':>3}  {'net':>11}  {'avg':>9}")
    for r in rows:
        print(f"  {r['cat']:<26}{r['r']:<8}{r['n']:>3}  {fmt(r['net']):>11}  "
              f"{fmt(r['avg']):>9}")

    # ---- New entries in last 48h
    hr("3. NEW ENTRIES in last 48h by category")
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               COUNT(*) AS n
        FROM trade_history
        WHERE datetime(created_at) >= datetime('now', '-48 hours')
        GROUP BY cat
        ORDER BY n DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['cat']:<26} new entries: {r['n']}")

    # ---- City-level activity in window
    hr("4. PER-CITY activity (last 48h, settled events)")
    rows = c.execute("""
        SELECT city, COUNT(*) AS n,
               SUM(CASE WHEN outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
               SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
               ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        GROUP BY city
        ORDER BY net
    """).fetchall()
    print(f"  {'city':<14} {'n':>3} {'W':>3} {'L':>3} {'RBL':>4}  {'net':>11}")
    grand_net = 0.0
    for r in rows:
        grand_net += r['net'] or 0
        print(f"  {r['city']:<14} {r['n']:>3} {r['w']:>3} {r['l']:>3} "
              f"{r['rbl']:>4}  {fmt(r['net']):>11}")
    print(f"  {'-'*14}")
    print(f"  TOTAL                          {fmt(grand_net):>11}")

    # ---- Did calibration affect entries? Look for created_at >= May 1 entries
    hr("5. CALIBRATION VISIBLE EFFECT — entries since May 1 by city")
    rows = c.execute("""
        SELECT city, COALESCE(category, '<null>') AS cat,
               COUNT(*) AS n
        FROM trade_history
        WHERE datetime(created_at) >= '2026-05-01'
        GROUP BY city, cat
        ORDER BY city, cat
    """).fetchall()
    by_city = {}
    for r in rows:
        by_city.setdefault(r['city'], []).append((r['cat'], r['n']))
    for city, items in sorted(by_city.items()):
        items_str = ", ".join([f"{c}={n}" for c, n in items])
        print(f"  {city:<14}  {items_str}")

    # ---- New flipped trades in window
    hr("6. NEW FLIPPED TRADES (weather_tail_no_flipped) since May 1")
    rows = c.execute("""
        SELECT id, city, target_date, side, ROUND(entry_price,3) AS px,
               notional, outcome, ROUND(pnl,2) AS pnl,
               created_at, settled_at
        FROM trade_history
        WHERE category = 'weather_tail_no_flipped'
          AND datetime(created_at) >= '2026-05-01'
        ORDER BY created_at
    """).fetchall()
    if rows:
        print(f"  {'id':>4} {'city':<14} {'tgt':<11} {'px':>5} {'out':>3} "
              f"{'pnl':>9} {'created':<13} {'settled':<13}")
        for r in rows:
            outcome_s = "open" if r['outcome'] is None else str(r['outcome'])
            pnl_s = fmt(r['pnl']) if r['pnl'] is not None else "  -"
            print(f"  #{r['id']:>3} {r['city']:<14} {r['target_date']:<11} "
                  f"{r['px']:>5.3f} {outcome_s:>3} {pnl_s:>9} "
                  f"{kst(r['created_at']):<13} "
                  f"{kst(r['settled_at']) if r['settled_at'] else '(open)':<13}")
    else:
        print("  No flipped trades created since May 1.")

    # ---- Open book current state
    hr("7. OPEN BOOK — current state")
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               COUNT(*) AS n,
               ROUND(SUM(notional), 2) AS notional
        FROM trade_history
        WHERE outcome IS NULL
        GROUP BY cat
        ORDER BY notional DESC
    """).fetchall()
    print(f"  {'category':<26} {'n':>3}  {'notional':>10}")
    total_n = 0
    total_notl = 0.0
    for r in rows:
        total_n += r['n']
        total_notl += r['notional'] or 0
        print(f"  {r['cat']:<26} {r['n']:>3}  ${r['notional']:>9.2f}")
    print(f"  {'-'*26} {'-'*3}  {'-'*10}")
    print(f"  TOTAL                      {total_n:>3}  ${total_notl:>9.2f}")

    # Open positions with current MTM
    rows = c.execute("""
        SELECT t.id, t.city, t.target_date, COALESCE(t.category, '<null>') AS cat,
               t.token_side, ROUND(t.entry_price, 3) AS px,
               t.notional, mp.best_bid AS bid
        FROM trade_history t
        LEFT JOIN (
            SELECT mp1.* FROM market_prices mp1
            JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx
                  FROM market_prices GROUP BY token_id) lt
              ON lt.token_id = mp1.token_id
             AND lt.mx = mp1.fetched_at_utc
        ) mp ON mp.token_id = t.token_id
        WHERE t.outcome IS NULL
        ORDER BY t.target_date, t.city
    """).fetchall()
    total_upnl = 0.0
    cruising = []
    danger = []
    for r in rows:
        entry = r['px'] or 0
        bid = r['bid']
        notl = r['notional'] or 0
        if entry > 0 and bid is not None:
            shares = notl / entry
            gross = shares * (bid - entry)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            total_upnl += upnl
            if upnl > 1:
                cruising.append((r, upnl, bid))
            elif bid < 0.10:
                danger.append((r, upnl, bid))

    print(f"\n  Sum unrealized P&L: {fmt(total_upnl)}")
    print(f"  Cruising (in profit): {len(cruising)}")
    print(f"  Danger (bid < 0.10): {len(danger)}")
    if danger:
        for r, upnl, bid in danger:
            print(f"    #{r['id']:>3} {r['city']:<12} {r['target_date']} "
                  f"px={r['px']:.3f} bid={bid:.3f} upnl={fmt(upnl)}")

    # ---- TRUE total
    hr("8. TRUE TOTAL")
    print(f"  Lifetime realized        : {fmt(cum['net'])}")
    print(f"  Open unrealized          : {fmt(total_upnl)}")
    print(f"  TRUE total               : {fmt((cum['net'] or 0) + total_upnl)}")

    # ---- Recent activity timeline (last 24 settles + entries)
    hr("9. RECENT TIMELINE — last 30 events (any type)")
    rows = c.execute("""
        SELECT created_at AS event_time, 'ENTRY' AS evt,
               city, side, COALESCE(category,'<null>') AS cat,
               ROUND(entry_price,3) AS px, notional, NULL AS pnl
        FROM trade_history
        WHERE datetime(created_at) >= datetime('now', '-48 hours')
        UNION ALL
        SELECT settled_at AS event_time,
               CASE WHEN outcome=2 THEN 'REBAL'
                    WHEN pnl>0 THEN 'WIN'
                    ELSE 'LOSS' END AS evt,
               city, side, COALESCE(category,'<null>') AS cat,
               ROUND(entry_price,3) AS px, notional, ROUND(pnl,2) AS pnl
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-48 hours')
        ORDER BY event_time DESC
        LIMIT 40
    """).fetchall()
    print(f"  {'when':<13} {'evt':<6} {'city':<13} {'cat':<25} {'side':<8} "
          f"{'px':>5} {'pnl':>9}")
    for r in rows:
        pnl_s = fmt(r['pnl']) if r['pnl'] is not None else "  -"
        side_s = (r['side'] or '-')[:8]
        print(f"  {kst(r['event_time']):<13} {r['evt']:<6} {r['city']:<13} "
              f"{r['cat']:<25} {side_s:<8} {r['px']:>5.3f} {pnl_s:>9}")

    c.close()


if __name__ == "__main__":
    main()
