"""Triple-check audit: pull every NO trade signal across multiple time windows.

Designed to catch what shallow filters miss. Specifically:
  - Lifetime / 72h / 36h / 24h / 8h / 1h splits side-by-side
  - Win-rate vs breakeven-win-rate at each entry price (asymmetry math)
  - 06:00 KST tick activity post-gate-tightening deploy
  - Catastrophic-flip exit verification
  - Bracket-width artifact identification
  - NO_ASK_MAX counterfactual (what about high-entry-price entries?)

Run on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/audit_no_thorough.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("/home/ubuntu/polymarket/data/weather/weather.db")
LOG_PATH = Path("/home/ubuntu/polymarket/logs/autotrade.log")


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


def is_no(row):
    ts = (row.get("token_side") or "").upper()
    s = (row.get("side") or "").upper()
    return ts == "NO" or "NO" in s


def main():
    c = conn()

    # =====================================================================
    # 0. CURRENT SNAPSHOT — lifetime + open + most recent activity
    # =====================================================================
    hr("0. CURRENT STATE — what the dashboard says NOW")
    row = c.execute("""
        SELECT
          COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS settled_n,
          COUNT(*) FILTER (WHERE outcome IS NULL) AS open_n,
          ROUND(SUM(pnl) FILTER (WHERE outcome IS NOT NULL), 2) AS lifetime_pnl,
          ROUND(SUM(notional) FILTER (WHERE outcome IS NULL), 2) AS open_notional,
          MAX(settled_at) FILTER (WHERE outcome IS NOT NULL) AS last_settle,
          MAX(created_at) AS last_created
        FROM trade_history
    """).fetchone()
    print(f"  Settled         : {row['settled_n']}")
    print(f"  Open            : {row['open_n']}")
    print(f"  Lifetime P&L    : {fmt(row['lifetime_pnl'])}")
    print(f"  Open notional   : ${row['open_notional']:,.2f}")
    print(f"  Last settle     : {row['last_settle']}")
    print(f"  Last entry      : {row['last_created']}")

    # =====================================================================
    # 1. NO P&L across multiple time windows
    # =====================================================================
    hr("1. NO P&L ACROSS WINDOWS (settled_at, all NO-side events)")
    windows = [("1h",1), ("4h",4), ("8h",8), ("12h",12), ("24h",24),
               ("36h",36), ("48h",48), ("72h",72), ("7d",168), ("lifetime", None)]
    print(f"  {'window':<10}{'NO n':>6}{'NO wins':>9}{'NO losses':>11}"
          f"{'NO rebals':>11}{'win%':>7}{'NO net':>14}")
    for label, hrs in windows:
        if hrs is None:
            sql = "WHERE outcome IS NOT NULL"
            params = ()
        else:
            sql = (f"WHERE outcome IS NOT NULL AND settled_at IS NOT NULL "
                   f"AND datetime(settled_at) >= datetime('now', '-{hrs} hours')")
            params = ()
        rows = c.execute(f"""
            SELECT side, token_side, outcome, pnl FROM trade_history {sql}
        """, params).fetchall()
        no_rows = [dict(r) for r in rows if is_no(dict(r))]
        n = len(no_rows)
        directional = [r for r in no_rows if r['outcome'] != 2]
        wins = [r for r in directional if (r['pnl'] or 0) > 0]
        losses = [r for r in directional if (r['pnl'] or 0) <= 0]
        rebals = [r for r in no_rows if r['outcome'] == 2]
        wr = len(wins) / len(directional) * 100 if directional else 0
        net = sum(r['pnl'] or 0 for r in no_rows)
        print(f"  {label:<10}{n:>6}{len(wins):>9}{len(losses):>11}"
              f"{len(rebals):>11}{wr:>6.1f}%{fmt(net):>14}")

    # =====================================================================
    # 2. STRUCTURAL EV by entry-price bucket — MATHEMATICAL breakeven
    # =====================================================================
    hr("2. STRUCTURAL EV BY ENTRY PRICE — breakeven win rate per bucket")
    print("  For NO @ entry price p:")
    print("    win payout per dollar = (1-p) * 0.98")
    print("    breakeven_wr = p / (p + (1-p)*0.98)")
    print("  Lifetime NO trades only.\n")
    print(f"  {'entry':<14}{'be_wr':>7}{'n':>5}{'W':>4}{'L':>4}{'realized_wr':>13}"
          f"{'gap_vs_be':>11}{'net':>11}{'verdict':<10}")
    buckets = [
        (0.00, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60),
        (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01),
    ]
    rows = c.execute("""
        SELECT side, token_side, entry_price, pnl, outcome
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2 AND entry_price IS NOT NULL
    """).fetchall()
    no_settled = [dict(r) for r in rows if is_no(dict(r))]
    for lo, hi in buckets:
        bsel = [r for r in no_settled if lo <= (r['entry_price'] or 0) < hi]
        if not bsel:
            continue
        be_wr = ((lo + hi) / 2) / ((lo + hi) / 2 + (1 - (lo + hi) / 2) * 0.98)
        wins = [r for r in bsel if (r['pnl'] or 0) > 0]
        losses = [r for r in bsel if (r['pnl'] or 0) <= 0]
        wr = len(wins) / len(bsel)
        net = sum(r['pnl'] or 0 for r in bsel)
        gap = (wr - be_wr) * 100
        verdict = "GOOD" if gap > 5 else ("MARGINAL" if gap > -5 else "BAD")
        print(f"  {lo:.2f}-{hi:.2f}    {be_wr*100:>6.1f}%{len(bsel):>5}"
              f"{len(wins):>4}{len(losses):>4}{wr*100:>12.1f}%"
              f"{gap:>+10.1f}{fmt(net):>11}  {verdict}")

    # =====================================================================
    # 3. NO_ASK_MAX counterfactual — what if we capped high-price entries?
    # =====================================================================
    hr("3. NO_ASK_MAX COUNTERFACTUAL — last 36h, NO trades only")
    cutoff_36 = (datetime.now(timezone.utc) - timedelta(hours=36)
                 ).strftime("%Y-%m-%dT%H:%M:%S")
    rows = c.execute(f"""
        SELECT side, token_side, entry_price, pnl, outcome
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2 AND entry_price IS NOT NULL
          AND datetime(settled_at) >= datetime('{cutoff_36}')
    """).fetchall()
    no_36 = [dict(r) for r in rows if is_no(dict(r))]
    actual_net = sum(r['pnl'] or 0 for r in no_36)
    print(f"  Actual realized (NO directional, 36h): {fmt(actual_net)}"
          f" over {len(no_36)} trades\n")

    scenarios = [
        ("current: NO_ASK_MIN=0.40, NO_ASK_MAX=0.95",
            lambda r: 0.40 <= (r['entry_price'] or 0) <= 0.95),
        ("REVERT to NO_ASK_MIN=0.05, NO_ASK_MAX=0.95",
            lambda r: 0.05 <= (r['entry_price'] or 0) <= 0.95),
        ("NO_ASK_MAX=0.80 (cap top end)",
            lambda r: 0.05 <= (r['entry_price'] or 0) <= 0.80),
        ("NO_ASK_MAX=0.75",
            lambda r: 0.05 <= (r['entry_price'] or 0) <= 0.75),
        ("NO_ASK 0.40-0.75 (sweet spot)",
            lambda r: 0.40 <= (r['entry_price'] or 0) <= 0.75),
        ("NO_ASK 0.50-0.75",
            lambda r: 0.50 <= (r['entry_price'] or 0) <= 0.75),
        ("NO_ASK 0.50-0.70",
            lambda r: 0.50 <= (r['entry_price'] or 0) <= 0.70),
        ("NO_ASK 0.55-0.70",
            lambda r: 0.55 <= (r['entry_price'] or 0) <= 0.70),
    ]
    print(f"  {'scenario':<48}{'kept':>5}{'wins':>6}{'losses':>9}{'win%':>7}{'net':>11}")
    for label, fn in scenarios:
        kept = [r for r in no_36 if fn(r)]
        wins = [r for r in kept if (r['pnl'] or 0) > 0]
        losses = [r for r in kept if (r['pnl'] or 0) <= 0]
        wr = len(wins)/len(kept)*100 if kept else 0
        net = sum(r['pnl'] or 0 for r in kept)
        print(f"  {label:<48}{len(kept):>5}{len(wins):>6}{len(losses):>9}"
              f"{wr:>6.1f}%{fmt(net):>11}")

    # =====================================================================
    # 4. POST-DEPLOY VERIFICATION — did the 06:00 KST tick respect new gates?
    # =====================================================================
    hr("4. POST-DEPLOY: ENTRIES SINCE GATE TIGHTENING (created_at >= 05:53 UTC)")
    deploy_cutoff = "2026-04-27T20:53:00"  # rough deploy time UTC
    rows = c.execute(f"""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, token_side, ROUND(entry_price,3) AS entry_px,
               ROUND(edge,3) AS edge, notional, created_at, outcome
        FROM trade_history
        WHERE datetime(created_at) >= datetime('{deploy_cutoff}')
        ORDER BY created_at DESC
    """).fetchall()
    print(f"  New entries since deploy: {len(rows)}")
    for r in rows[:30]:
        cr_kst = (str(r['created_at']) or "")[:19]
        try:
            dt = datetime.fromisoformat(str(r['created_at']).replace("Z","+00:00").replace(" ","T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            cr_kst = dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M KST")
        except Exception:
            pass
        out = "open" if r['outcome'] is None else f"out={r['outcome']}"
        print(f"    {cr_kst:<18} id={r['id']:>3} {r['city']:<14} {r['cat']:<18} "
              f"side={r['side']:<8} entry={(r['entry_px'] or 0):.3f} "
              f"edge={(r['edge'] or 0):+.3f} notl={r['notional']:>4.0f}  {out}")

    # Was the tightened gate respected?
    bad_entries = [
        r for r in rows
        if (r['edge'] is not None and r['edge'] < 0.10)
           or ((r['token_side'] or "") == "NO" and (r['entry_px'] or 0) < 0.40)
    ]
    if bad_entries:
        print(f"\n  WARN: {len(bad_entries)} entries violated the new gates "
              f"(edge<0.10 or NO<0.40):")
        for r in bad_entries:
            print(f"    id={r['id']} {r['city']} edge={r['edge']:+.3f} "
                  f"entry={r['entry_px']:.3f} side={r['side']}")
    else:
        print(f"\n  All {len(rows)} new entries respected the tightened gates.")

    # =====================================================================
    # 5. CATASTROPHIC-FLIP VERIFICATION — did the 06:00 tick exit anything?
    # =====================================================================
    hr("5. CATASTROPHIC-FLIP EXITS (post-deploy)")
    rows = c.execute(f"""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, token_side, ROUND(entry_price,3) AS entry_px,
               notional, ROUND(pnl,2) AS pnl, settled_at, outcome
        FROM trade_history
        WHERE outcome = 2
          AND datetime(settled_at) >= datetime('{deploy_cutoff}')
        ORDER BY settled_at DESC
    """).fetchall()
    print(f"  Rebalance exits since deploy: {len(rows)}")
    for r in rows[:20]:
        kst = ""
        try:
            dt = datetime.fromisoformat(str(r['settled_at']).replace("Z","+00:00").replace(" ","T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kst = dt.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M KST")
        except Exception:
            pass
        print(f"    {kst:<18} id={r['id']:>3} {r['city']:<14} {r['cat']:<18} "
              f"side={r['side']:<8} entry={(r['entry_px'] or 0):.3f} "
              f"pnl={fmt(r['pnl'])}")

    # Look for catastrophic_flip exit_reason in autotrade.log
    print("\n  autotrade.log mentions of 'CATASTROPHIC FLIP' (post-deploy):")
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH, 'r') as f:
                lines = f.readlines()
            mentions = [l for l in lines if "CATASTROPHIC" in l.upper()]
            for m in mentions[-15:]:
                print(f"    {m.rstrip()}")
            if not mentions:
                print("    (no catastrophic-flip mentions found)")
        except Exception as e:
            print(f"    log read error: {e}")
    else:
        print("    log not present")

    # =====================================================================
    # 6. ENTRY-PRICE × WIN-RATE on OPEN BOOK — what's at risk?
    # =====================================================================
    hr("6. OPEN BOOK BY ENTRY PRICE — projected risk")
    has_mp = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
    ).fetchone()
    rows = []
    if has_mp:
        rows = c.execute("""
            SELECT t.id, t.city, t.target_date, COALESCE(t.category,'<null>') AS cat,
                   t.token_side, ROUND(t.entry_price,3) AS entry_px,
                   t.notional, mp.best_bid AS bid
            FROM trade_history t
            LEFT JOIN (
                SELECT mp1.* FROM market_prices mp1
                JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx FROM market_prices GROUP BY token_id) lt
                  ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
            ) mp ON mp.token_id = t.token_id
            WHERE t.outcome IS NULL
        """).fetchall()
    open_no = [dict(r) for r in rows if (r['token_side'] or '').upper() == 'NO']
    print(f"  Open NO positions: {len(open_no)}")
    by_bucket = {}
    for r in open_no:
        ep = r['entry_px'] or 0
        bucket = f"{int(ep*10)/10:.1f}-{int(ep*10)/10 + 0.1:.1f}"
        by_bucket.setdefault(bucket, []).append(r)
    print(f"  {'bucket':<12}{'n':>4}{'notional':>11}{'avg_entry':>12}{'avg_bid':>10}{'projected':>12}")
    for bucket in sorted(by_bucket.keys()):
        ps = by_bucket[bucket]
        avg_entry = sum(r['entry_px'] or 0 for r in ps) / len(ps)
        priced = [r for r in ps if r['bid'] is not None]
        avg_bid = sum(r['bid'] for r in priced) / len(priced) if priced else 0
        notional = sum(r['notional'] or 0 for r in ps)
        # Crude projected P&L: shares × (bid - entry) for priced positions
        proj = 0.0
        for r in ps:
            if r['bid'] is not None and r['entry_px']:
                shares = r['notional'] / r['entry_px']
                gross = shares * (r['bid'] - r['entry_px'])
                fee = 0.02 * gross if gross > 0 else 0
                proj += gross - fee
        print(f"  {bucket:<12}{len(ps):>4}{notional:>11.0f}{avg_entry:>12.3f}"
              f"{avg_bid:>10.3f}{fmt(proj):>12}")

    # =====================================================================
    # 7. BRACKET-WIDTH ARTIFACT CHECK
    # =====================================================================
    hr("7. BRACKET-WIDTH ARTIFACT CHECK (NO trades only)")
    rows = c.execute("""
        SELECT id, city, target_date, ROUND(entry_price,3) AS px,
               ROUND(bracket_lower_f,1) AS bk_lo, ROUND(bracket_upper_f,1) AS bk_up,
               outcome, ROUND(pnl,2) AS pnl, side, token_side
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND bracket_upper_f - bracket_lower_f > 10.0
    """).fetchall()
    artifacts = [dict(r) for r in rows if is_no(dict(r))]
    print(f"  NO trades with bracket width > 10°F (likely artifacts): {len(artifacts)}")
    for r in artifacts:
        width = (r['bk_up'] or 0) - (r['bk_lo'] or 0)
        print(f"    id={r['id']:>4} {r['city']:<14} target={r['target_date']} "
              f"width={width:.1f}°F entry={r['px']:.3f} pnl={fmt(r['pnl'])}")

    # =====================================================================
    # 8. THE BREAKEVEN MATH IN ONE TABLE — most important
    # =====================================================================
    hr("8. BREAKEVEN-WR vs REALIZED-WR per entry-price (CRITICAL)")
    print("  This is the math that determines if a strategy is structurally +EV.\n")
    print("  At NO entry price p, breakeven win rate = p / (p + (1-p)*0.98)")
    print()
    for p in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        be_wr = p / (p + (1 - p) * 0.98)
        win_payout = (1 - p) * 0.98
        loss_payout = p
        ratio = win_payout / loss_payout if loss_payout > 0 else 0
        print(f"  p={p:.2f}  breakeven_wr={be_wr*100:>5.1f}%  "
              f"win/$={win_payout*100:>5.1f}¢  loss/$={loss_payout*100:>5.1f}¢  "
              f"win/loss ratio={ratio:.2f}")

    c.close()


if __name__ == "__main__":
    main()
