"""Forensic NO-trade audit, last 36 hours.

Decomposes every NO settled/exited trade by entry price, edge, lead, city,
bracket geometry, and exit type. Then runs counterfactual gate sweeps to
quantify how much we'd have saved with tighter filters.

Goal: replace vibes-based gate adjustment with data-driven thresholds.

Run on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/audit_no_trades_36h.py
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


def is_no_side(row):
    """A trade is NO-side if token_side='NO' OR side contains 'NO'."""
    ts = (row.get("token_side") or "").upper()
    s = (row.get("side") or "").upper()
    return ts == "NO" or "NO" in s


def main():
    c = conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")

    # =====================================================================
    # 1. NO trades in window (settled or exited)
    # =====================================================================
    hr(f"1. ALL NO TRADES IN LAST {WINDOW_HOURS}h (by settled_at)")
    rows = c.execute(f"""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, token_side, ROUND(entry_price,3) AS entry_px,
               notional, outcome, ROUND(pnl,2) AS pnl,
               ROUND(edge,3) AS edge,
               ROUND(model_prob,3) AS model_p,
               ROUND(market_prob,3) AS market_p,
               ROUND(bracket_lower_f,2) AS bk_lo,
               ROUND(bracket_upper_f,2) AS bk_up,
               settled_at, created_at,
               regime
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{WINDOW_HOURS} hours')
    """).fetchall()
    rows = [dict(r) for r in rows]
    no_rows = [r for r in rows if is_no_side(r)]
    yes_rows = [r for r in rows if not is_no_side(r)]

    print(f"  Total events in window  : {len(rows)}")
    print(f"  NO-side events          : {len(no_rows)}")
    print(f"  YES-side events         : {len(yes_rows)}")

    no_pnl = sum(r['pnl'] or 0 for r in no_rows)
    yes_pnl = sum(r['pnl'] or 0 for r in yes_rows)
    print(f"  NO-side net P&L         : {fmt(no_pnl)}")
    print(f"  YES-side net P&L        : {fmt(yes_pnl)}")

    no_settled = [r for r in no_rows if r['outcome'] != 2]  # actual settlements (not rebalance)
    no_exited = [r for r in no_rows if r['outcome'] == 2]
    no_wins = [r for r in no_settled if (r['pnl'] or 0) > 0]
    no_losses = [r for r in no_settled if (r['pnl'] or 0) <= 0]

    print(f"\n  NO settled (won/lost)    : {len(no_settled)} ({len(no_wins)}W/{len(no_losses)}L)")
    print(f"  NO rebalance exits       : {len(no_exited)}")
    if no_settled:
        wr = len(no_wins) / len(no_settled)
        print(f"  NO directional win rate  : {wr*100:.1f}%")

    # =====================================================================
    # 2. Win/loss magnitude asymmetry
    # =====================================================================
    hr("2. WIN/LOSS MAGNITUDE — the asymmetry that kills tail-NO")
    if no_wins:
        avg_win = sum(r['pnl'] for r in no_wins) / len(no_wins)
        max_win = max(r['pnl'] for r in no_wins)
        min_win = min(r['pnl'] for r in no_wins)
    else:
        avg_win = max_win = min_win = 0
    if no_losses:
        avg_loss = sum(r['pnl'] for r in no_losses) / len(no_losses)
        max_loss = min(r['pnl'] for r in no_losses)
    else:
        avg_loss = max_loss = 0

    print(f"  Avg win                 : {fmt(avg_win)}")
    print(f"  Avg loss                : {fmt(avg_loss)}")
    if avg_loss != 0:
        ratio = abs(avg_loss / avg_win) if avg_win > 0 else float('inf')
        print(f"  Loss / win magnitude    : {ratio:.2f}x  "
              f"(losses are {ratio:.1f}× the size of wins)")
        breakeven_wr = abs(avg_loss) / (abs(avg_loss) + avg_win) if avg_win > 0 else 1.0
        print(f"  Required breakeven win% : {breakeven_wr*100:.1f}%")
        print(f"  Realized win rate       : {(len(no_wins)/len(no_settled)*100) if no_settled else 0:.1f}%")
        gap = (len(no_wins)/len(no_settled) - breakeven_wr) * 100 if no_settled else 0
        print(f"  Gap                     : {gap:+.1f}pp")

    # =====================================================================
    # 3. Entry price bucket analysis
    # =====================================================================
    hr("3. NO TRADES BY ENTRY-PRICE BUCKET")
    buckets = [
        ("0.00-0.30", lambda p: 0 <= p < 0.30),
        ("0.30-0.40", lambda p: 0.30 <= p < 0.40),
        ("0.40-0.50", lambda p: 0.40 <= p < 0.50),
        ("0.50-0.60", lambda p: 0.50 <= p < 0.60),
        ("0.60-0.70", lambda p: 0.60 <= p < 0.70),
        ("0.70-0.80", lambda p: 0.70 <= p < 0.80),
        ("0.80-0.90", lambda p: 0.80 <= p < 0.90),
        ("0.90-1.00", lambda p: 0.90 <= p <= 1.00),
    ]
    print(f"  {'bucket':<12}{'n':>4} {'W':>3} {'L':>3} {'RBL':>4} {'win%':>6}  "
          f"{'avg_win':>9} {'avg_loss':>10}  {'net':>11}")
    for label, fn in buckets:
        bsel = [r for r in no_rows if r['entry_px'] is not None and fn(r['entry_px'])]
        if not bsel:
            continue
        bset = [r for r in bsel if r['outcome'] != 2]
        bexit = [r for r in bsel if r['outcome'] == 2]
        bw = [r for r in bset if (r['pnl'] or 0) > 0]
        bl = [r for r in bset if (r['pnl'] or 0) <= 0]
        net = sum(r['pnl'] or 0 for r in bsel)
        wr = len(bw) / len(bset) * 100 if bset else 0
        avg_w = sum(r['pnl'] for r in bw) / len(bw) if bw else 0
        avg_l = sum(r['pnl'] for r in bl) / len(bl) if bl else 0
        print(f"  {label:<12}{len(bsel):>4} {len(bw):>3} {len(bl):>3} {len(bexit):>4} "
              f"{wr:>5.1f}%  {avg_w:>+8.2f} {avg_l:>+9.2f}  {fmt(net):>11}")

    # =====================================================================
    # 4. Edge-size bucket analysis
    # =====================================================================
    hr("4. NO TRADES BY EDGE-SIZE BUCKET (predicted edge at entry)")
    edge_buckets = [
        ("<0.05",   lambda e: e < 0.05),
        ("0.05-0.07", lambda e: 0.05 <= e < 0.07),
        ("0.07-0.10", lambda e: 0.07 <= e < 0.10),
        ("0.10-0.15", lambda e: 0.10 <= e < 0.15),
        ("0.15-0.20", lambda e: 0.15 <= e < 0.20),
        ("0.20-0.30", lambda e: 0.20 <= e < 0.30),
        (">=0.30",  lambda e: e >= 0.30),
    ]
    print(f"  {'edge band':<12}{'n':>4} {'W':>3} {'L':>3} {'RBL':>4} {'win%':>6}  "
          f"{'avg_pnl':>9}  {'net':>11}")
    for label, fn in edge_buckets:
        bsel = [r for r in no_rows if r['edge'] is not None and fn(r['edge'])]
        if not bsel:
            continue
        bset = [r for r in bsel if r['outcome'] != 2]
        bexit = [r for r in bsel if r['outcome'] == 2]
        bw = [r for r in bset if (r['pnl'] or 0) > 0]
        bl = [r for r in bset if (r['pnl'] or 0) <= 0]
        net = sum(r['pnl'] or 0 for r in bsel)
        avg_pnl = (net / len(bsel)) if bsel else 0
        wr = len(bw) / len(bset) * 100 if bset else 0
        print(f"  {label:<12}{len(bsel):>4} {len(bw):>3} {len(bl):>3} {len(bexit):>4} "
              f"{wr:>5.1f}%  {avg_pnl:>+8.2f}  {fmt(net):>11}")

    # =====================================================================
    # 5. Bracket-width analysis (NO trades only)
    # =====================================================================
    hr("5. NO TRADES BY BRACKET WIDTH")
    width_buckets = [
        ("<2°F (1°C)",   lambda w: w < 2.0),
        ("2-3°F",        lambda w: 2.0 <= w < 3.0),
        ("3-5°F",        lambda w: 3.0 <= w < 5.0),
        ("5-10°F",       lambda w: 5.0 <= w < 10.0),
        (">=10°F",       lambda w: w >= 10.0),
    ]
    print(f"  {'width':<14}{'n':>4} {'W':>3} {'L':>3} {'win%':>6}  "
          f"{'avg_pnl':>9}  {'net':>11}")
    for label, fn in width_buckets:
        bsel = [
            r for r in no_rows
            if r['bk_up'] is not None and r['bk_lo'] is not None
               and fn((r['bk_up'] or 0) - (r['bk_lo'] or 0))
        ]
        if not bsel:
            continue
        bset = [r for r in bsel if r['outcome'] != 2]
        bw = [r for r in bset if (r['pnl'] or 0) > 0]
        bl = [r for r in bset if (r['pnl'] or 0) <= 0]
        net = sum(r['pnl'] or 0 for r in bsel)
        avg_pnl = (net / len(bsel)) if bsel else 0
        wr = len(bw) / len(bset) * 100 if bset else 0
        print(f"  {label:<14}{len(bsel):>4} {len(bw):>3} {len(bl):>3} "
              f"{wr:>5.1f}%  {avg_pnl:>+8.2f}  {fmt(net):>11}")

    # =====================================================================
    # 6. Lead-time bucket
    # =====================================================================
    hr("6. NO TRADES BY LEAD TIME (target_date − created_date, in days)")
    lead_buckets = [
        ("0d (same-day)",    lambda d: d == 0),
        ("1d",               lambda d: d == 1),
        ("2d",               lambda d: d == 2),
        (">=3d",             lambda d: d >= 3),
    ]
    print(f"  {'lead':<16}{'n':>4} {'W':>3} {'L':>3} {'win%':>6}  "
          f"{'avg_pnl':>9}  {'net':>11}")
    for label, fn in lead_buckets:
        bsel = []
        for r in no_rows:
            try:
                td = datetime.fromisoformat(r['target_date']).date()
                cd = datetime.fromisoformat(str(r['created_at']).replace("Z","+00:00").replace(" ","T")).date()
                lead_d = (td - cd).days
                if fn(lead_d):
                    bsel.append(r)
            except Exception:
                continue
        if not bsel:
            continue
        bset = [r for r in bsel if r['outcome'] != 2]
        bw = [r for r in bset if (r['pnl'] or 0) > 0]
        bl = [r for r in bset if (r['pnl'] or 0) <= 0]
        net = sum(r['pnl'] or 0 for r in bsel)
        avg_pnl = (net / len(bsel)) if bsel else 0
        wr = len(bw) / len(bset) * 100 if bset else 0
        print(f"  {label:<16}{len(bsel):>4} {len(bw):>3} {len(bl):>3} "
              f"{wr:>5.1f}%  {avg_pnl:>+8.2f}  {fmt(net):>11}")

    # =====================================================================
    # 7. City-level breakdown
    # =====================================================================
    hr("7. NO TRADES BY CITY")
    cities = {}
    for r in no_rows:
        city = r['city']
        cities.setdefault(city, []).append(r)
    print(f"  {'city':<14}{'n':>4} {'W':>3} {'L':>3} {'RBL':>4} {'win%':>6}  "
          f"{'avg_pnl':>9}  {'net':>11}")
    for city, bsel in sorted(cities.items(), key=lambda x: sum(r['pnl'] or 0 for r in x[1])):
        bset = [r for r in bsel if r['outcome'] != 2]
        bexit = [r for r in bsel if r['outcome'] == 2]
        bw = [r for r in bset if (r['pnl'] or 0) > 0]
        bl = [r for r in bset if (r['pnl'] or 0) <= 0]
        net = sum(r['pnl'] or 0 for r in bsel)
        avg_pnl = (net / len(bsel)) if bsel else 0
        wr = len(bw) / len(bset) * 100 if bset else 0
        print(f"  {city:<14}{len(bsel):>4} {len(bw):>3} {len(bl):>3} {len(bexit):>4} "
              f"{wr:>5.1f}%  {avg_pnl:>+8.2f}  {fmt(net):>11}")

    # =====================================================================
    # 8. Counterfactual gate sweep — what would we save with tighter rules?
    # =====================================================================
    hr("8. COUNTERFACTUAL — tighten gates, replay window")
    print("  Using ONLY directional settles (excluding rebalance exits)")
    print("  to isolate the effect of NEW-ENTRY filters.\n")

    no_dir = [r for r in no_rows if r['outcome'] != 2]
    actual_net = sum(r['pnl'] or 0 for r in no_dir)
    print(f"  ACTUAL realized (directional NO only): {fmt(actual_net)} "
          f"({len(no_dir)} trades)\n")

    scenarios = [
        ("min_edge>=0.05 (current)", lambda r: True),
        ("min_edge>=0.10",           lambda r: (r['edge'] or 0) >= 0.10),
        ("min_edge>=0.15",           lambda r: (r['edge'] or 0) >= 0.15),
        ("min_edge>=0.20",           lambda r: (r['edge'] or 0) >= 0.20),
        ("entry>=0.40",              lambda r: (r['entry_px'] or 0) >= 0.40),
        ("entry>=0.50",              lambda r: (r['entry_px'] or 0) >= 0.50),
        ("min_edge>=0.10 AND entry>=0.40",
            lambda r: (r['edge'] or 0) >= 0.10 and (r['entry_px'] or 0) >= 0.40),
        ("min_edge>=0.15 AND entry>=0.40",
            lambda r: (r['edge'] or 0) >= 0.15 and (r['entry_px'] or 0) >= 0.40),
        ("min_edge>=0.10 AND entry>=0.45",
            lambda r: (r['edge'] or 0) >= 0.10 and (r['entry_px'] or 0) >= 0.45),
        ("min_edge>=0.15 AND entry>=0.50 AND width>=2",
            lambda r: (r['edge'] or 0) >= 0.15 and (r['entry_px'] or 0) >= 0.50
                      and ((r['bk_up'] or 0) - (r['bk_lo'] or 0)) >= 2.0),
        ("min_edge>=0.20 AND entry>=0.50",
            lambda r: (r['edge'] or 0) >= 0.20 and (r['entry_px'] or 0) >= 0.50),
    ]

    print(f"  {'scenario':<46}{'kept':>5}  {'wins':>5}  {'losses':>7}  "
          f"{'win%':>6}  {'net':>11}")
    for label, fn in scenarios:
        kept = [r for r in no_dir if fn(r)]
        wins = [r for r in kept if (r['pnl'] or 0) > 0]
        losses = [r for r in kept if (r['pnl'] or 0) <= 0]
        net = sum(r['pnl'] or 0 for r in kept)
        wr = len(wins) / len(kept) * 100 if kept else 0
        print(f"  {label:<46}{len(kept):>5}  {len(wins):>5}  {len(losses):>7}  "
              f"{wr:>5.1f}%  {fmt(net):>11}")

    # =====================================================================
    # 9. Itemize the losses
    # =====================================================================
    hr("9. EVERY NO LOSS IN THE WINDOW (itemized)")
    losses_only = [r for r in no_rows if r['outcome'] != 2 and (r['pnl'] or 0) <= 0]
    losses_only.sort(key=lambda r: r['pnl'] or 0)
    print(f"  {'id':>4} {'city':<14} {'target':<11} {'cat':<18} {'side':<8} "
          f"{'entry':>5} {'edge':>6} {'width':>5} {'notl':>5} {'pnl':>9}")
    for r in losses_only:
        width = (r['bk_up'] or 0) - (r['bk_lo'] or 0)
        print(f"  {r['id']:>4} {r['city']:<14} {r['target_date']:<11} "
              f"{r['cat']:<18} {(r['side'] or '—'):<8} "
              f"{(r['entry_px'] or 0):>5.3f} {(r['edge'] or 0):>+5.3f} "
              f"{width:>5.1f} {r['notional']:>5.0f} {fmt(r['pnl']):>9}")

    # =====================================================================
    # 10. Itemize the wins — what made them work?
    # =====================================================================
    hr("10. EVERY NO WIN IN THE WINDOW (itemized)")
    wins_only = [r for r in no_rows if r['outcome'] != 2 and (r['pnl'] or 0) > 0]
    wins_only.sort(key=lambda r: -(r['pnl'] or 0))
    print(f"  {'id':>4} {'city':<14} {'target':<11} {'cat':<18} {'side':<8} "
          f"{'entry':>5} {'edge':>6} {'width':>5} {'notl':>5} {'pnl':>9}")
    for r in wins_only:
        width = (r['bk_up'] or 0) - (r['bk_lo'] or 0)
        print(f"  {r['id']:>4} {r['city']:<14} {r['target_date']:<11} "
              f"{r['cat']:<18} {(r['side'] or '—'):<8} "
              f"{(r['entry_px'] or 0):>5.3f} {(r['edge'] or 0):>+5.3f} "
              f"{width:>5.1f} {r['notional']:>5.0f} {fmt(r['pnl']):>9}")

    c.close()


if __name__ == "__main__":
    main()
