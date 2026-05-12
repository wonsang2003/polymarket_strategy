"""Morning status report — what the dashboard shows right now.
Apr 29 ~08:00 KST audit.
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
    now_utc = datetime.now(timezone.utc)
    now_kst = (now_utc + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
    print(f"NOW: {now_kst}")

    # ---- Cron freshness
    autotrade_log = Path("/home/ubuntu/polymarket/logs/autotrade.log")
    snap_log = Path("/home/ubuntu/polymarket/logs/snap_mtm.log")

    def age(p):
        if not p.exists():
            return "missing"
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        s = (now_utc - mtime).total_seconds()
        if s < 60:
            return f"{int(s)}s ago"
        if s < 3600:
            return f"{int(s/60)}m ago"
        return f"{int(s/3600)}h ago"

    print(f"  autotrade.log  last write: {age(autotrade_log)}")
    print(f"  snap_mtm.log   last write: {age(snap_log)}")

    last_mp = c.execute("SELECT MAX(fetched_at_utc) AS x FROM market_prices").fetchone()
    if last_mp["x"]:
        dt = datetime.fromisoformat(str(last_mp["x"]).replace("Z","+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        print(f"  last market_prices write: {int((now_utc-dt).total_seconds())}s ago "
              f"({dt.astimezone(timezone(timedelta(hours=9))).strftime('%H:%M:%S KST')})")

    # ---- Headline P&L
    hr("1. HEADLINE")
    today_kst_str = (now_utc + timedelta(hours=9)).strftime("%Y-%m-%d")
    yesterday_kst_str = (now_utc + timedelta(hours=9) - timedelta(days=1)).strftime("%Y-%m-%d")

    lifetime = c.execute("""
        SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n
        FROM trade_history WHERE outcome IS NOT NULL
    """).fetchone()
    today = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE settled_at LIKE ? || '%'",
        (today_kst_str,)
    ).fetchone()
    yesterday = c.execute(
        "SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE settled_at LIKE ? || '%'",
        (yesterday_kst_str,)
    ).fetchone()

    print(f"  Lifetime cumulative : {fmt(lifetime['net'])} ({lifetime['n']} settles)")
    print(f"  P&L today (KST {today_kst_str})    : {fmt(today['net'])} ({today['n']} events)")
    print(f"  P&L yesterday (KST {yesterday_kst_str}): {fmt(yesterday['net'])} ({yesterday['n']} events)")

    # ---- Last 12h activity
    hr("2. LAST 12 HOURS — entries + exits")
    rows = c.execute("""
        SELECT created_at AS event_time, 'ENTRY' AS evt,
               city, side, COALESCE(category,'<null>') AS cat,
               ROUND(entry_price,3) AS px, notional,
               NULL AS pnl, exit_reason
        FROM trade_history
        WHERE datetime(created_at) >= datetime('now', '-12 hours')
        UNION ALL
        SELECT settled_at AS event_time,
               CASE WHEN outcome=2 THEN 'REBAL' WHEN pnl>0 THEN 'WIN' ELSE 'LOSS' END AS evt,
               city, side, COALESCE(category,'<null>') AS cat,
               ROUND(entry_price,3) AS px, notional,
               ROUND(pnl,2) AS pnl, exit_reason
        FROM trade_history
        WHERE outcome IS NOT NULL AND settled_at IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-12 hours')
        ORDER BY event_time DESC
    """).fetchall()

    n_entry = sum(1 for r in rows if r["evt"] == "ENTRY")
    n_win = sum(1 for r in rows if r["evt"] == "WIN")
    n_loss = sum(1 for r in rows if r["evt"] == "LOSS")
    n_rebal = sum(1 for r in rows if r["evt"] == "REBAL")
    pnl_total = sum(r["pnl"] or 0 for r in rows)

    print(f"  ENTRIES: {n_entry}   WINS: {n_win}   LOSSES: {n_loss}   REBALS: {n_rebal}")
    print(f"  Realized in 12h: {fmt(pnl_total)}")
    print()
    print(f"  {'when':<13} {'evt':<6} {'city':<13} {'cat':<25} {'side':<8} "
          f"{'px':>5} {'pnl':>8} {'reason':<22}")
    for r in rows[:30]:
        pnl_s = fmt(r["pnl"]) if r["pnl"] is not None else "    -"
        print(f"  {kst(r['event_time']):<13} {r['evt']:<6} {r['city']:<13} "
              f"{r['cat']:<25} {(r['side'] or '-'):<8} {r['px']:>5.3f} "
              f"{pnl_s:>8} {(r['exit_reason'] or '-')[:22]:<22}")

    # ---- FLIP EXPERIMENT — did it fire yet?
    hr("3. FLIP EXPERIMENT (weather_tail_no_flipped) — did it fire?")
    flip_rows = c.execute("""
        SELECT id, city, target_date, side, token_side,
               ROUND(entry_price,3) AS px, notional, outcome,
               ROUND(pnl,2) AS pnl, created_at, settled_at
        FROM trade_history
        WHERE category = 'weather_tail_no_flipped'
        ORDER BY created_at
    """).fetchall()
    if not flip_rows:
        print("  No flipped trades yet (experiment was deployed at ~22:00 KST Apr 28).")
    else:
        for r in flip_rows:
            print(f"  #{r['id']:>3} {r['city']:<13} tgt={r['target_date']} "
                  f"side={r['side']} px={r['px']:.3f} notl=${r['notional']:.0f} "
                  f"out={r['outcome']} pnl={fmt(r['pnl'])} "
                  f"created={kst(r['created_at'])} settled={kst(r['settled_at'])}")

    # ---- Open positions snapshot
    hr("4. OPEN POSITIONS — current state")
    rows = c.execute("""
        SELECT t.id, t.city, t.target_date,
               COALESCE(t.category,'<null>') AS cat,
               t.token_side, ROUND(t.entry_price,3) AS px,
               t.notional, mp.best_bid AS bid
        FROM trade_history t
        LEFT JOIN (
            SELECT mp1.* FROM market_prices mp1
            JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx
                  FROM market_prices GROUP BY token_id) lt
              ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
        ) mp ON mp.token_id = t.token_id
        WHERE t.outcome IS NULL
        ORDER BY t.target_date, t.city
    """).fetchall()

    total_notl = 0.0
    total_upnl = 0.0
    by_target = {}
    by_cat = {}
    danger = []
    cruising = []
    for r in rows:
        entry = r["px"] or 0
        bid = r["bid"]
        notl = r["notional"] or 0
        total_notl += notl
        upnl = None
        if entry > 0 and bid is not None:
            shares = notl / entry
            gross = shares * (bid - entry)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            total_upnl += upnl
        by_target.setdefault(r["target_date"], {"n": 0, "notl": 0.0, "upnl": 0.0})
        by_target[r["target_date"]]["n"] += 1
        by_target[r["target_date"]]["notl"] += notl
        if upnl is not None:
            by_target[r["target_date"]]["upnl"] += upnl
        by_cat.setdefault(r["cat"], {"n": 0, "notl": 0.0, "upnl": 0.0})
        by_cat[r["cat"]]["n"] += 1
        by_cat[r["cat"]]["notl"] += notl
        if upnl is not None:
            by_cat[r["cat"]]["upnl"] += upnl
        if bid is not None and bid < 0.10:
            danger.append((r, upnl, bid))
        if upnl is not None and upnl > 0:
            cruising.append((r, upnl, bid))

    print(f"  Open positions: {len(rows)}")
    print(f"  Total notional: ${total_notl:,.2f}")
    print(f"  Sum unrealized: {fmt(total_upnl)}")
    print(f"\n  By target date:")
    for d, agg in sorted(by_target.items()):
        print(f"    {d}: n={agg['n']:>2}  notl=${agg['notl']:>6.2f}  upnl={fmt(agg['upnl'])}")
    print(f"\n  By category:")
    for k, agg in sorted(by_cat.items()):
        print(f"    {k:<26}: n={agg['n']:>2}  notl=${agg['notl']:>6.2f}  upnl={fmt(agg['upnl'])}")

    if danger:
        print(f"\n  DANGER (bid < 0.10) — likely full losses:")
        for r, upnl, bid in danger:
            print(f"    #{r['id']:>3} {r['city']:<13} {r['target_date']} "
                  f"px={r['px']:.3f} bid={bid:.3f} upnl={fmt(upnl)}")

    if cruising:
        print(f"\n  CRUISING (in profit, top 5):")
        for r, upnl, bid in sorted(cruising, key=lambda x: -x[1])[:5]:
            print(f"    #{r['id']:>3} {r['city']:<13} {r['target_date']} "
                  f"px={r['px']:.3f} bid={bid:.3f} upnl={fmt(upnl)}")

    # ---- TRUE total
    hr("5. TRUE NET")
    true_net = (lifetime["net"] or 0) + total_upnl
    print(f"  Lifetime realized        : {fmt(lifetime['net'])}")
    print(f"  Open unrealized          : {fmt(total_upnl)}")
    print(f"  TRUE total (lifetime+UPnL): {fmt(true_net)}")

    c.close()


if __name__ == "__main__":
    main()
