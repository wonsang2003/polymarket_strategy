"""Reconcile the dashboard's PnL number with reality.

Dashboard reports $+166 today; audit_today_full.py reported +$72.12.
Hypothesis: timezone mismatch — dashboard uses date.today() (host-local =
KST on EC2), audit used UTC date filter. Trades that settle 'today KST'
may have settled_at strings that start with yesterday's UTC date.
"""
import sqlite3
import sys
from datetime import datetime, timezone, date


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    now_utc = datetime.now(timezone.utc)
    print(f"now_utc       : {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"date.today()  : {date.today().isoformat()}  ← what host (KST) thinks 'today' is")
    print(f"UTC today     : {now_utc.date().isoformat()}")
    print()

    # 1. EXACT reproduction of dashboard's pnl_today calc (host-local 'today',
    #    string prefix on settled_at).
    today_local = date.today().isoformat()
    rows = c.execute(
        "SELECT id, settled_at, pnl, city, side, token_side "
        "FROM trade_history WHERE outcome IS NOT NULL"
    ).fetchall()
    dashboard_today = [
        r for r in rows
        if r["settled_at"] and str(r["settled_at"]).startswith(today_local)
    ]
    pnl_dashboard = sum(float(r["pnl"]) for r in dashboard_today
                        if r["pnl"] is not None)
    print(f"[1] DASHBOARD-style filter (settled_at startswith host-today)")
    print(f"    today_local = {today_local}")
    print(f"    matched     : {len(dashboard_today)}")
    print(f"    pnl total   : ${pnl_dashboard:+.2f}")
    print()

    # 2. UTC-correct: filter by date(created_at) = today UTC
    today_utc = now_utc.date().isoformat()
    rows_created_utc = c.execute(
        "SELECT id, created_at, settled_at, pnl FROM trade_history "
        "WHERE date(created_at) = ? AND outcome IS NOT NULL",
        (today_utc,),
    ).fetchall()
    pnl_created_utc = sum(float(r["pnl"]) for r in rows_created_utc
                          if r["pnl"] is not None)
    print(f"[2] My audit's filter (date(created_at)=today UTC)")
    print(f"    today_utc   = {today_utc}")
    print(f"    matched     : {len(rows_created_utc)}")
    print(f"    pnl total   : ${pnl_created_utc:+.2f}")
    print()

    # 3. UTC-correct on settled_at
    rows_settled_utc = c.execute(
        "SELECT id, settled_at, pnl FROM trade_history "
        "WHERE date(settled_at) = ? AND outcome IS NOT NULL",
        (today_utc,),
    ).fetchall()
    pnl_settled_utc = sum(float(r["pnl"]) for r in rows_settled_utc
                          if r["pnl"] is not None)
    print(f"[3] UTC date(settled_at) = today UTC")
    print(f"    today_utc   = {today_utc}")
    print(f"    matched     : {len(rows_settled_utc)}")
    print(f"    pnl total   : ${pnl_settled_utc:+.2f}")
    print()

    # 4. List the discrepancy rows: settled today (host-local) but with
    #    settled_at strings starting with a different date prefix.
    print(f"[4] SAMPLE: rows that settled 'today' by various definitions")
    print(f"  {'id':>4} {'created_at':<19} {'settled_at':<35} {'pnl':>8} "
          f"{'host_today?':<12} {'utc_today?':<10}")
    all_settled = c.execute(
        "SELECT id, created_at, settled_at, pnl, side "
        "FROM trade_history WHERE outcome IS NOT NULL "
        "ORDER BY settled_at DESC LIMIT 30"
    ).fetchall()
    for r in all_settled:
        sa = str(r["settled_at"] or "")
        host_today_match = sa.startswith(today_local)
        utc_today_match = bool(r["settled_at"]) and (
            sa.startswith(today_utc) or
            (lambda s: s and s[:10] == today_utc)(sa[:10])
        )
        # Check if SQLite date(settled_at) = today_utc
        sqlite_check = c.execute(
            "SELECT date(?) = ?", (r["settled_at"], today_utc)
        ).fetchone()[0]
        utc_today_match = bool(sqlite_check)
        host_lbl = "✓" if host_today_match else "✗"
        utc_lbl = "✓" if utc_today_match else "✗"
        print(f"  #{r['id']:>3} {(r['created_at'] or '')[:19]:<19} "
              f"{sa[:35]:<35} ${float(r['pnl'] or 0):>+7.2f} "
              f"{host_lbl:<12} {utc_lbl:<10}")

    # 5. Reconciliation summary
    print()
    print(f"[5] RECONCILIATION")
    print(f"    Dashboard ($+166?)         shows : ${pnl_dashboard:+.2f}")
    print(f"    My audit ($+72.12)         shows : ${pnl_created_utc:+.2f}")
    print(f"    UTC settled_at filter      shows : ${pnl_settled_utc:+.2f}")
    if abs(pnl_dashboard - pnl_created_utc) > 0.01:
        print(f"    ⇒ DISCREPANCY: ${abs(pnl_dashboard - pnl_created_utc):+.2f}")
        print(f"    Dashboard counts settlements that crossed the LOCAL day boundary")
        print(f"    but were created on a different day. My audit used created_at;")
        print(f"    a trade made yesterday-UTC that settles today-KST counts in")
        print(f"    dashboard but not in mine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
