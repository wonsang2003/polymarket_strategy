"""Dashboard-perspective audit: what the dashboard ACTUALLY shows.

Replicates the exact queries from tools/dashboard/app.py so we know what the
user sees in each panel. Filters by EVENT TIME (settled_at), not creation
time, to match the activity feed and 'PnL today' metric.

Run on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/quant_audit_dashboard_view.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("/home/ubuntu/polymarket/data/weather/weather.db")
WINDOW_HOURS = 36


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


def main():
    c = conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # =====================================================================
    # 1. THE TICKER (Live tab) — exact dashboard logic
    # =====================================================================
    hr("1. WHAT THE TICKER SHOWS RIGHT NOW")
    # Mirrors:
    #   open_trades = SELECT * FROM trade_history WHERE outcome IS NULL
    #   settled = SELECT * FROM trade_history WHERE outcome IS NOT NULL
    #   settled_today = settled.filter(settled_at startswith today_kst)
    #   total_pnl_realised = sum(settled.pnl)  ← LIFETIME
    #   pnl_today_realised = sum(settled_today.pnl)  ← TODAY ONLY

    # KST today = UTC + 9 hours
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
    print(f"  KST 'today' boundary: {today_kst}")

    open_count = c.execute(
        "SELECT COUNT(*) AS n, ROUND(SUM(notional),2) AS notional "
        "FROM trade_history WHERE outcome IS NULL"
    ).fetchone()
    print(f"  Open positions          : {open_count['n']}")
    print(f"  Open notional           : ${open_count['notional']:,.2f}")

    cum = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE outcome IS NOT NULL"
    ).fetchone()
    print(f"  Cumulative P&L (all settled) : {fmt(cum['net'])} ({cum['n']} settles)")

    today = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE settled_at LIKE ? || '%'",
        (today_kst,),
    ).fetchone()
    print(f"  P&L today (KST)         : {fmt(today['net'])} ({today['n']} events)")

    signals_today = c.execute(
        "SELECT COUNT(*) AS n FROM trade_history WHERE date(created_at)=?",
        (today_kst,),
    ).fetchone()
    print(f"  Signals today (KST)     : {signals_today['n']}")

    # Unrealized P&L on open book
    has_mp = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
    ).fetchone()
    upnl_total = 0.0
    upnl_priced = 0
    if has_mp:
        for r in c.execute("""
            SELECT t.entry_price, t.notional, mp.best_bid
            FROM trade_history t
            LEFT JOIN (
                SELECT mp1.* FROM market_prices mp1
                JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx FROM market_prices GROUP BY token_id) lt
                  ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
            ) mp ON mp.token_id = t.token_id
            WHERE t.outcome IS NULL
        """):
            ep, nt, bid = r["entry_price"], r["notional"], r["best_bid"]
            if ep and bid is not None and nt:
                shares = nt / ep
                gross = shares * (bid - ep)
                fee = 0.02 * gross if gross > 0 else 0
                upnl_total += gross - fee
                upnl_priced += 1
    print(f"  Unrealized P&L (open)   : {fmt(upnl_total)} ({upnl_priced} priced)")

    # =====================================================================
    # 2. ACTIVITY IN LAST 36h — same UNION the dashboard runs
    # =====================================================================
    hr(f"2. ACTIVITY IN LAST {WINDOW_HOURS}h (event-time filter, NOT creation)")
    print(f"  Cutoff: {cutoff} UTC = {(datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS) + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')} KST")
    rows = c.execute(f"""
        SELECT created_at AS event_time,
               'ENTRY' AS event_type,
               city, side, COALESCE(category,'<null>') AS cat,
               edge, entry_price, notional, NULL AS pnl
        FROM trade_history
        WHERE datetime(created_at) >= datetime('now', '-{WINDOW_HOURS} hours')

        UNION ALL

        SELECT settled_at AS event_time,
               CASE
                   WHEN outcome = 2 THEN 'REBALANCE_EXIT'
                   WHEN pnl > 0 THEN 'WIN'
                   ELSE 'LOSS'
               END AS event_type,
               city, side, COALESCE(category,'<null>') AS cat,
               edge, entry_price, notional, pnl
        FROM trade_history
        WHERE outcome IS NOT NULL AND settled_at IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')

        ORDER BY event_time DESC
    """).fetchall()

    by_type = {}
    pnl_by_type = {}
    for r in rows:
        by_type[r["event_type"]] = by_type.get(r["event_type"], 0) + 1
        pnl_by_type[r["event_type"]] = pnl_by_type.get(r["event_type"], 0.0) + (r["pnl"] or 0.0)

    print(f"\n  Event-type histogram:")
    for t in ("ENTRY", "WIN", "LOSS", "REBALANCE_EXIT"):
        n = by_type.get(t, 0)
        net = pnl_by_type.get(t, 0.0)
        print(f"    {t:<18} n={n:>3}  net={fmt(net)}")

    activity_pnl = sum(pnl_by_type.get(t, 0.0) for t in ("WIN", "LOSS", "REBALANCE_EXIT"))
    print(f"\n  TOTAL realised P&L in window (all exit events): {fmt(activity_pnl)}")

    # Full feed
    print(f"\n  FULL ACTIVITY FEED (last {WINDOW_HOURS}h, newest first):")
    print(f"  {'when':<22}  {'event':<16} {'city':<14} {'cat':<18} {'pnl':>10}")
    for r in rows:
        ts = r["event_time"]
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kst = dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M KST")
        except Exception:
            kst = str(ts)[:16]
        pnl_str = fmt(r["pnl"]) if r["pnl"] is not None else "      —   "
        print(f"  {kst:<22}  {r['event_type']:<16} {r['city']:<14} {r['cat']:<18} {pnl_str:>10}")

    # =====================================================================
    # 3. RECENT SETTLEMENTS (Analytics tab) — last 50 by settled_at
    # =====================================================================
    hr("3. RECENT SETTLEMENTS PANEL — last 50 by settled_at, all-time")
    rows = c.execute("""
        SELECT settled_at, city, target_date, side, ROUND(entry_price,3) AS px,
               notional, outcome, ROUND(pnl,2) AS pnl, COALESCE(category,'<null>') AS cat
        FROM trade_history
        WHERE outcome IS NOT NULL
        ORDER BY settled_at DESC
        LIMIT 50
    """).fetchall()

    cumul = 0.0
    print(f"  {'settled_at (UTC)':<22} {'city':<14} {'cat':<18} {'side':<8} "
          f"{'px':>5} {'notl':>5} {'out':>3} {'pnl':>10} {'cumul-50':>11}")
    for r in rows[::-1]:  # oldest first for cumul
        cumul += r["pnl"] or 0
    cumul_running = 0.0
    for r in rows:
        cumul_running += r["pnl"] or 0
    # Display top 25 newest with running cumul of last 50
    cumul_back = cumul
    for r in rows[:25]:
        ts = (str(r["settled_at"])[:16]).replace("T", " ")
        cumul_back -= (r["pnl"] or 0)  # undo
        out = "WIN" if r["outcome"] == 1 else ("NO" if r["outcome"] == 0 else ("RBL" if r["outcome"] == 2 else "?"))
        print(f"  {ts:<22} {r['city']:<14} {r['cat']:<18} {(r['side'] or '—'):<8} "
              f"{r['px']:>5.3f} {r['notional']:>5.0f} {out:>3} "
              f"{fmt(r['pnl']):>10}")
    print(f"\n  Sum of last-50 pnl: {fmt(cumul)}")

    # =====================================================================
    # 4. P&L IN LAST 36h — by SETTLED_AT (event time)
    # =====================================================================
    hr(f"4. SETTLEMENT PERFORMANCE — last {WINDOW_HOURS}h (filtered by settled_at)")
    rows = c.execute(f"""
        SELECT
          CASE
            WHEN outcome = 2 THEN 'REBALANCE_EXIT'
            WHEN pnl > 0 THEN 'WIN'
            ELSE 'LOSS'
          END AS result,
          COALESCE(category,'<null>') AS cat,
          COUNT(*) AS n,
          ROUND(SUM(pnl),2) AS net,
          ROUND(AVG(pnl),2) AS avg,
          ROUND(MIN(pnl),2) AS worst,
          ROUND(MAX(pnl),2) AS best
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        GROUP BY result, cat
        ORDER BY result, cat
    """).fetchall()

    print(f"  {'result':<18}{'category':<20}{'n':>4}  {'net':>11}  {'avg':>8}  "
          f"{'worst':>8}  {'best':>8}")
    grand_total = 0.0
    grand_n = 0
    for r in rows:
        print(f"  {r['result']:<18}{r['cat']:<20}{r['n']:>4}  "
              f"{fmt(r['net']):>11}  {(r['avg'] or 0):>+7.2f}  "
              f"{(r['worst'] or 0):>+7.2f}  {(r['best'] or 0):>+7.2f}")
        grand_total += r["net"] or 0
        grand_n += r["n"]
    print(f"\n  GRAND TOTAL (last {WINDOW_HOURS}h, by settled_at): "
          f"n={grand_n}  net={fmt(grand_total)}")

    # =====================================================================
    # 5. ALL settlements/exits in last 36h — itemized
    # =====================================================================
    hr(f"5. EVERY SETTLEMENT/EXIT IN LAST {WINDOW_HOURS}h — ITEMIZED")
    rows = c.execute(f"""
        SELECT settled_at, city, target_date,
               COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px,
               notional, outcome, ROUND(pnl,2) AS pnl,
               ROUND(edge,3) AS edge, created_at
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
        ORDER BY settled_at DESC
    """).fetchall()

    print(f"  {'settled (KST)':<18} {'city':<14} {'cat':<18} {'side':<8} "
          f"{'px':>5} {'notl':>5} {'out':>3} {'pnl':>10} {'created':<11}")
    running = 0.0
    for r in rows[::-1]:
        running += r["pnl"] or 0
    running_back = running
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["settled_at"]).replace("Z", "+00:00").replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kst = dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")
        except Exception:
            kst = str(r["settled_at"])[:16]
        out = "WIN" if r["outcome"] == 1 else ("NO" if r["outcome"] == 0 else ("RBL" if r["outcome"] == 2 else "?"))
        cr = (str(r["created_at"]) or "")[:10]
        print(f"  {kst:<18} {r['city']:<14} {r['cat']:<18} {(r['side'] or '—'):<8} "
              f"{r['px']:>5.3f} {r['notional']:>5.0f} {out:>3} {fmt(r['pnl']):>10} {cr:<11}")
        running_back -= r["pnl"] or 0

    print(f"\n  TOTAL settled+exited in last {WINDOW_HOURS}h: n={len(rows)}  net={fmt(running)}")

    # =====================================================================
    # 6. RECONCILE: dashboard PnL today vs my 36h window
    # =====================================================================
    hr("6. RECONCILIATION — what the dashboard headline says")
    today_pnl = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE settled_at LIKE ? || '%'",
        (today_kst,),
    ).fetchone()
    last_36h_event = c.execute(f"""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
    """).fetchone()
    last_36h_create = c.execute(f"""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(created_at) >= datetime('now', '-{WINDOW_HOURS} hours')
    """).fetchone()

    lifetime = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE outcome IS NOT NULL"
    ).fetchone()

    print(f"  Lifetime cumulative P&L      : {fmt(lifetime['net'])} ({lifetime['n']} events)")
    print(f"  P&L today (KST {today_kst})  : {fmt(today_pnl['net'])} ({today_pnl['n']} events)")
    print(f"  P&L last 36h by SETTLED_AT   : {fmt(last_36h_event['net'])} ({last_36h_event['n']} events)")
    print(f"  P&L last 36h by CREATED_AT   : {fmt(last_36h_create['net'])} ({last_36h_create['n']} events)")
    print(f"  Unrealized on open book      : {fmt(upnl_total)}")
    print(f"  TRUE 36h-by-settled + UPnL   : {fmt((last_36h_event['net'] or 0) + upnl_total)}")

    c.close()


if __name__ == "__main__":
    main()
