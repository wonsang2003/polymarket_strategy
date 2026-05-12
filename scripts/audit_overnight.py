"""Overnight activity audit — what happened in the last N hours.

Replicates the dashboard's activity-feed query but enriched with
event-by-event commentary, drawdown computation, and per-city / per-category
splits. Helps answer "what hit while I was asleep".

Run on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/audit_overnight.py [HOURS]
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("/home/ubuntu/polymarket/data/weather/weather.db")
WINDOW_HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def conn():
    c = sqlite3.connect(f"file:{DB_PATH}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(title, char="="):
    print()
    print(char * 78)
    print(f"  {title}")
    print(char * 78)


def fmt(v):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):>9,.2f}"


def kst(ts):
    if ts is None:
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def main():
    c = conn()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    cutoff_kst = cutoff + timedelta(hours=9)

    print(f"\n  WINDOW: last {WINDOW_HOURS} hours")
    print(f"  From   : {cutoff_kst.strftime('%Y-%m-%d %H:%M')} KST  "
          f"({cutoff_iso} UTC)")
    print(f"  To     : {now_kst.strftime('%Y-%m-%d %H:%M')} KST  (now)")

    # =====================================================================
    # 1. Headline numbers
    # =====================================================================
    hr("1. HEADLINE — what changed during the window")
    cum_now = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE outcome IS NOT NULL"
    ).fetchone()
    cum_before = c.execute(
        f"SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        f"FROM trade_history WHERE outcome IS NOT NULL "
        f"AND datetime(settled_at) < datetime('now', '-{WINDOW_HOURS} hours')"
    ).fetchone()

    net_change = (cum_now['net'] or 0) - (cum_before['net'] or 0)
    n_change = (cum_now['n'] or 0) - (cum_before['n'] or 0)

    print(f"  Lifetime cumulative — start of window : {fmt(cum_before['net'])} ({cum_before['n']} settles)")
    print(f"  Lifetime cumulative — now             : {fmt(cum_now['net'])} ({cum_now['n']} settles)")
    print(f"  CHANGE during window                  : {fmt(net_change)} (+{n_change} new settles)")

    # Activity counts in the window
    activity = c.execute(f"""
        SELECT
          (SELECT COUNT(*) FROM trade_history
            WHERE datetime(created_at) >= datetime('now', '-{WINDOW_HOURS} hours')) AS entries,
          (SELECT COUNT(*) FROM trade_history
            WHERE outcome = 1 AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')) AS yes_resolved,
          (SELECT COUNT(*) FROM trade_history
            WHERE outcome = 0 AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')) AS no_resolved,
          (SELECT COUNT(*) FROM trade_history
            WHERE outcome = 2 AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')) AS rebal_exits
    """).fetchone()
    print(f"\n  Window activity:")
    print(f"    Entries          : {activity['entries']}")
    print(f"    YES-resolved     : {activity['yes_resolved']}")
    print(f"    NO-resolved      : {activity['no_resolved']}")
    print(f"    Rebalance exits  : {activity['rebal_exits']}")

    # =====================================================================
    # 2. The good — wins during the window
    # =====================================================================
    hr("2. THE GOOD — wins during the window")
    wins = c.execute(f"""
        SELECT settled_at, city, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional,
               outcome, ROUND(pnl,2) AS pnl, ROUND(edge,3) AS edge
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2 AND pnl > 0
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        ORDER BY pnl DESC
    """).fetchall()
    if wins:
        print(f"  {'when (KST)':<14} {'city':<14} {'cat':<18} {'side':<8} "
              f"{'px':>5} {'notl':>5} {'edge':>7} {'pnl':>10}")
        win_total = 0.0
        for r in wins:
            win_total += r['pnl']
            print(f"  {kst(r['settled_at']):<14} {r['city']:<14} {r['cat']:<18} "
                  f"{(r['side'] or '—'):<8} {r['px']:>5.3f} {r['notional']:>5.0f} "
                  f"{(r['edge'] or 0):>+6.3f} {fmt(r['pnl']):>10}")
        print(f"\n  Total wins: {len(wins)}  |  Net win P&L: {fmt(win_total)}")
    else:
        print("  No directional wins in the window.")

    # Profitable rebalance exits
    good_rebals = c.execute(f"""
        SELECT settled_at, city, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional, ROUND(pnl,2) AS pnl
        FROM trade_history
        WHERE outcome = 2 AND pnl > 0
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        ORDER BY pnl DESC
    """).fetchall()
    if good_rebals:
        print(f"\n  Profitable rebalance exits ({len(good_rebals)}):")
        for r in good_rebals:
            print(f"    {kst(r['settled_at']):<14} {r['city']:<14} "
                  f"{r['cat']:<18} {(r['side'] or '—'):<8} {r['px']:>5.3f} "
                  f"notl={r['notional']:>4.0f} pnl={fmt(r['pnl'])}")

    # =====================================================================
    # 3. The bad — losses + bad rebalance exits
    # =====================================================================
    hr("3. THE BAD — losses during the window")
    losses = c.execute(f"""
        SELECT settled_at, city, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional,
               outcome, ROUND(pnl,2) AS pnl, ROUND(edge,3) AS edge,
               created_at
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2 AND pnl <= 0
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        ORDER BY pnl ASC
    """).fetchall()
    if losses:
        print(f"  Directional losses ({len(losses)}):")
        print(f"  {'when (KST)':<14} {'city':<14} {'cat':<18} {'side':<8} "
              f"{'px':>5} {'notl':>5} {'edge':>7} {'pnl':>10} {'created':<11}")
        loss_total = 0.0
        for r in losses:
            loss_total += r['pnl']
            cr = (str(r['created_at']) or "")[:10]
            print(f"  {kst(r['settled_at']):<14} {r['city']:<14} {r['cat']:<18} "
                  f"{(r['side'] or '—'):<8} {r['px']:>5.3f} {r['notional']:>5.0f} "
                  f"{(r['edge'] or 0):>+6.3f} {fmt(r['pnl']):>10} {cr:<11}")
        print(f"\n  Total losses: {len(losses)}  |  Net loss P&L: {fmt(loss_total)}")
    else:
        print("  No directional losses in the window.")

    # Bad rebalance exits (top 10 worst)
    bad_rebals = c.execute(f"""
        SELECT settled_at, city, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional, ROUND(pnl,2) AS pnl
        FROM trade_history
        WHERE outcome = 2 AND pnl < 0
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        ORDER BY pnl ASC
        LIMIT 15
    """).fetchall()
    bad_rebal_total = c.execute(f"""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome = 2 AND pnl < 0
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
    """).fetchone()
    if bad_rebals:
        print(f"\n  Loss-locking rebalance exits "
              f"({bad_rebal_total['n']} total, sum {fmt(bad_rebal_total['net'])}):")
        print(f"  Top 15 worst:")
        for r in bad_rebals:
            print(f"    {kst(r['settled_at']):<14} {r['city']:<14} "
                  f"{r['cat']:<18} {(r['side'] or '—'):<8} {r['px']:>5.3f} "
                  f"notl={r['notional']:>4.0f} pnl={fmt(r['pnl'])}")

    # =====================================================================
    # 4. Per-category window scorecard
    # =====================================================================
    hr("4. PER-CATEGORY SCORECARD (window, by event)")
    rows = c.execute(f"""
        SELECT COALESCE(category,'<null>') AS cat,
          SUM(CASE WHEN outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN outcome != 2 AND pnl > 0 THEN pnl ELSE 0 END) AS win_pnl,
          SUM(CASE WHEN outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN outcome != 2 AND pnl <= 0 THEN pnl ELSE 0 END) AS loss_pnl,
          SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rebals,
          ROUND(SUM(CASE WHEN outcome = 2 THEN pnl ELSE 0 END),2) AS rebal_pnl,
          ROUND(SUM(pnl),2) AS net,
          COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        GROUP BY cat
        ORDER BY net DESC
    """).fetchall()
    print(f"  {'category':<20}{'n':>4}  {'W':>3} {'win$':>8}  {'L':>3} {'loss$':>8}  "
          f"{'RBL':>4} {'rbl$':>8}  {'net':>11}")
    for r in rows:
        print(f"  {r['cat']:<20}{r['n']:>4}  "
              f"{r['wins']:>3} {fmt(r['win_pnl']):>8}  "
              f"{r['losses']:>3} {fmt(r['loss_pnl']):>8}  "
              f"{r['rebals']:>4} {fmt(r['rebal_pnl']):>8}  "
              f"{fmt(r['net']):>11}")

    # =====================================================================
    # 5. Open book — danger zone (positions at risk in next 24h)
    # =====================================================================
    hr("5. OPEN BOOK NOW — what's at risk in the next 24h")
    has_mp = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
    ).fetchone()
    if has_mp:
        rows = c.execute("""
            SELECT t.id, t.city, t.target_date,
                   COALESCE(t.category,'<null>') AS cat,
                   t.token_side, ROUND(t.entry_price,3) AS entry_px,
                   t.notional, t.created_at,
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
            ORDER BY t.target_date, t.city
        """).fetchall()
    else:
        rows = []

    today_kst_date = now_kst.date()
    danger = []  # bid < 0.10 (near-certain loss)
    at_risk = []  # bid 0.10–0.40 (pricing against us)
    safe_winning = []  # bid > entry (in profit)

    for r in rows:
        entry = r['entry_px'] or 0
        bid = r['bid']
        notional = r['notional'] or 0
        if entry <= 0 or bid is None:
            continue
        shares = notional / entry
        gross = shares * (bid - entry)
        fee = 0.02 * gross if gross > 0 else 0
        upnl = gross - fee
        roi = upnl / notional if notional > 0 else 0

        rec = (r, bid, upnl, roi)
        if bid < 0.10:
            danger.append(rec)
        elif bid < entry * 0.7:
            at_risk.append(rec)
        elif bid > entry:
            safe_winning.append(rec)

    print(f"  DANGER (bid < 0.10 — near-certain full loss): {len(danger)}")
    danger_total = 0.0
    for r, bid, upnl, roi in sorted(danger, key=lambda x: x[2]):
        danger_total += upnl
        print(f"    id {r['id']:>3}  {r['city']:<14} {r['target_date']:<11} "
              f"{r['cat']:<18} entry={r['entry_px']:.3f} bid={bid:.3f} "
              f"upnl={fmt(upnl)} roi={roi*100:>+5.1f}%")
    if danger:
        print(f"    └─ Combined likely-realisation loss: {fmt(danger_total)}")

    print(f"\n  AT RISK (bid below 70% of entry): {len(at_risk)}")
    risk_total = 0.0
    for r, bid, upnl, roi in sorted(at_risk, key=lambda x: x[2])[:10]:
        risk_total += upnl
        print(f"    id {r['id']:>3}  {r['city']:<14} {r['target_date']:<11} "
              f"{r['cat']:<18} entry={r['entry_px']:.3f} bid={bid:.3f} "
              f"upnl={fmt(upnl)} roi={roi*100:>+5.1f}%")

    print(f"\n  WINNING (bid > entry): {len(safe_winning)}")
    win_total = 0.0
    for r, bid, upnl, roi in sorted(safe_winning, key=lambda x: -x[2])[:10]:
        win_total += upnl
        print(f"    id {r['id']:>3}  {r['city']:<14} {r['target_date']:<11} "
              f"{r['cat']:<18} entry={r['entry_px']:.3f} bid={bid:.3f} "
              f"upnl={fmt(upnl)} roi={roi*100:>+5.1f}%")

    # =====================================================================
    # 6. Cycle-by-cycle breakdown — where in the window did the damage hit?
    # =====================================================================
    hr("6. HOURLY CYCLE BREAKDOWN — when did P&L move?")
    rows = c.execute(f"""
        SELECT
          strftime('%m-%d %H', datetime(settled_at, '+9 hours')) AS hour_kst,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          SUM(CASE WHEN outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rebals
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        GROUP BY hour_kst
        ORDER BY hour_kst
    """).fetchall()
    print(f"  {'hour (KST)':<14}{'n':>4}  {'W':>3} {'L':>3} {'RBL':>4}  {'net':>11}")
    cumul = 0.0
    for r in rows:
        cumul += r['net'] or 0
        print(f"  {r['hour_kst']:<14}{r['n']:>4}  {r['wins']:>3} {r['losses']:>3} "
              f"{r['rebals']:>4}  {fmt(r['net']):>11}")
    print(f"\n  Window cumulative                    : {fmt(cumul)}")

    c.close()


if __name__ == "__main__":
    main()
