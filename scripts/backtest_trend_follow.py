"""Backtest the trend-follow hypothesis: does bid-at-T predict outcome?

For every settled (non-rebalance) trade in the post-Apr-24 era (when
market_prices started writing), reconstruct the bid trajectory and ask:

  (a) At time T before settlement, did bid > entry predict eventual win?
  (b) Does the strength of that signal grow as T → 0 (closer to settle)?
  (c) What's the optimal threshold for a profit-take / stop-loss rule?

We use the latest market_prices snapshot BEFORE settled_at to classify
the position's terminal state. We bucket by hours-to-settlement.

Run on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/backtest_trend_follow.py
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


def parse_iso(s):
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def main():
    c = conn()

    # =====================================================================
    # 1. Pull every settled (non-rebal) trade with its bid trajectory
    # =====================================================================
    hr("1. POPULATION — settled trades with bid trajectory data")
    trades = c.execute("""
        SELECT id, city, target_date, COALESCE(category,'<null>') AS cat,
               side, token_side, ROUND(entry_price,4) AS entry_px,
               notional, edge, outcome, ROUND(pnl,2) AS pnl,
               settled_at, created_at, token_id,
               ROUND(bracket_upper_f - bracket_lower_f, 1) AS w
        FROM trade_history
        WHERE outcome IS NOT NULL AND outcome != 2  -- exclude rebal exits
          AND token_id IS NOT NULL
          AND settled_at IS NOT NULL
        ORDER BY settled_at DESC
    """).fetchall()
    print(f"  Total settled (non-rebal) trades: {len(trades)}")

    # Filter to those with at least one market_prices row
    trades_with_data = []
    for t in trades:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM market_prices WHERE token_id=?",
            (t["token_id"],)
        ).fetchone()["n"]
        if n > 0:
            trades_with_data.append(t)
    print(f"  With market_prices data         : {len(trades_with_data)}")

    if not trades_with_data:
        print("  No trades with bid trajectory data. Exiting.")
        return

    # =====================================================================
    # 2. For each trade, find LAST snapshot before settlement
    # =====================================================================
    hr("2. CLASSIFY — terminal-state bid vs eventual outcome")
    rows = []
    for t in trades_with_data:
        settled_dt = parse_iso(t["settled_at"])
        if settled_dt is None:
            continue
        last_snap = c.execute("""
            SELECT best_bid, fetched_at_utc
            FROM market_prices
            WHERE token_id = ? AND datetime(fetched_at_utc) <= ?
            ORDER BY fetched_at_utc DESC
            LIMIT 1
        """, (t["token_id"], settled_dt.isoformat())).fetchone()
        if last_snap is None or last_snap["best_bid"] is None:
            continue
        snap_dt = parse_iso(last_snap["fetched_at_utc"])
        if snap_dt is None:
            continue
        hours_before = (settled_dt - snap_dt).total_seconds() / 3600.0
        bid = float(last_snap["best_bid"])
        entry = float(t["entry_px"])
        is_cruising = bid > entry
        is_dead = bid < 0.10
        is_win = (t["pnl"] or 0) > 0
        rows.append({
            "id": t["id"], "city": t["city"], "cat": t["cat"],
            "entry": entry, "bid_at_T": bid,
            "hours_before_settle": hours_before,
            "is_cruising": is_cruising, "is_dead": is_dead, "is_win": is_win,
            "pnl": t["pnl"] or 0, "side": t["side"], "token_side": t["token_side"],
            "w": t["w"], "notional": t["notional"],
        })

    if not rows:
        print("  No trades with valid bid+settle timing. Exiting.")
        return

    # =====================================================================
    # 3. Headline: P(win | cruising_at_T) vs P(win | losing_at_T)
    # =====================================================================
    hr("3. HEADLINE — terminal-bid vs outcome (n={})".format(len(rows)))

    cruising = [r for r in rows if r["is_cruising"]]
    losing = [r for r in rows if not r["is_cruising"] and not r["is_dead"]]
    dead = [r for r in rows if r["is_dead"]]

    def stats(name, sub):
        if not sub:
            print(f"  {name:<26}: n=0")
            return
        wins = sum(1 for r in sub if r["is_win"])
        wr = wins / len(sub) * 100
        net = sum(r["pnl"] for r in sub)
        print(f"  {name:<26}: n={len(sub):>3}  win%={wr:>5.1f}  "
              f"avg_pnl={net/len(sub):>+7.2f}  total=${net:+,.2f}")

    stats("CRUISING (bid > entry)", cruising)
    stats("LOSING (entry > bid > 0.10)", losing)
    stats("DEAD (bid < 0.10)", dead)
    stats("ALL", rows)

    # Conditional probabilities
    p_win_cruising = (sum(1 for r in cruising if r["is_win"]) / len(cruising)) if cruising else 0
    p_win_losing = (sum(1 for r in losing if r["is_win"]) / len(losing)) if losing else 0
    p_win_dead = (sum(1 for r in dead if r["is_win"]) / len(dead)) if dead else 0
    p_win_all = (sum(1 for r in rows if r["is_win"]) / len(rows))

    print(f"\n  P(win | cruising)     = {p_win_cruising:.3f}")
    print(f"  P(win | losing)       = {p_win_losing:.3f}")
    print(f"  P(win | dead)         = {p_win_dead:.3f}")
    print(f"  P(win) baseline       = {p_win_all:.3f}")
    if p_win_cruising > 0 and p_win_losing > 0:
        lift = p_win_cruising / p_win_all
        print(f"  Lift from cruising signal: {lift:.2f}x baseline")

    # =====================================================================
    # 4. Bucket by HOURS-BEFORE-SETTLEMENT
    # =====================================================================
    hr("4. SIGNAL STRENGTH BY TIME-TO-SETTLEMENT")
    buckets = [
        ("0-1h",   lambda h: 0 <= h < 1),
        ("1-3h",   lambda h: 1 <= h < 3),
        ("3-6h",   lambda h: 3 <= h < 6),
        ("6-12h",  lambda h: 6 <= h < 12),
        ("12-24h", lambda h: 12 <= h < 24),
        (">=24h",  lambda h: h >= 24),
    ]
    print(f"  {'window':<10}{'n':>4}  {'P(win|cruising)':>15}  {'P(win|losing)':>14}  "
          f"{'lift':>6}")
    for label, fn in buckets:
        sub = [r for r in rows if fn(r["hours_before_settle"])]
        if not sub:
            continue
        sub_cruise = [r for r in sub if r["is_cruising"]]
        sub_lose = [r for r in sub if not r["is_cruising"] and not r["is_dead"]]
        p_c = (sum(1 for r in sub_cruise if r["is_win"]) / len(sub_cruise)) if sub_cruise else 0
        p_l = (sum(1 for r in sub_lose if r["is_win"]) / len(sub_lose)) if sub_lose else 0
        baseline = sum(1 for r in sub if r["is_win"]) / len(sub)
        lift = p_c / baseline if baseline > 0 else 0
        print(f"  {label:<10}{len(sub):>4}  "
              f"{p_c:>5.3f} (n={len(sub_cruise):>3})  "
              f"{p_l:>5.3f} (n={len(sub_lose):>3})  "
              f"{lift:>5.2f}x")

    # =====================================================================
    # 5. STRATEGY simulation: profit-take + stop-loss at various thresholds
    # =====================================================================
    hr("5. PROFIT-TAKE + STOP-LOSS COUNTERFACTUAL")
    print("  For each trade, compute hold-to-settle pnl vs early-exit pnl")
    print("  under different bid-relative-to-entry thresholds.")
    print()
    print("  Realized hold-to-settle:")
    actual_net = sum(r["pnl"] for r in rows)
    print(f"    Total: ${actual_net:+.2f} over {len(rows)} trades")
    print()

    # Simulation: at terminal snapshot, exit if bid > entry + take_threshold
    # OR bid < entry - stop_threshold. Compare to actual settlement P&L.
    scenarios = [
        ("hold-to-settle (baseline)", None, None),
        ("take +0.10",            0.10, None),
        ("take +0.15",            0.15, None),
        ("take +0.20",            0.20, None),
        ("stop -0.10",            None, 0.10),
        ("stop -0.15",            None, 0.15),
        ("stop -0.20",            None, 0.20),
        ("take +0.15 / stop -0.15", 0.15, 0.15),
        ("take +0.20 / stop -0.20", 0.20, 0.20),
        ("take +0.10 / stop -0.10", 0.10, 0.10),
    ]
    print(f"  {'scenario':<32}{'kept-to-settle':>16}{'exited':>9}{'exit_pnl':>11}{'total':>11}")
    for label, take, stop in scenarios:
        exited = 0
        exit_pnl = 0.0
        kept_pnl = 0.0
        kept = 0
        for r in rows:
            bid_diff = r["bid_at_T"] - r["entry"]
            should_exit = False
            sim_exit_pnl = 0.0
            if take is not None and bid_diff >= take:
                should_exit = True
                # Exit pnl ≈ (bid - entry) × shares, fee on gain
                shares = r["notional"] / r["entry"] if r["entry"] > 0 else 0
                gross = shares * (r["bid_at_T"] - r["entry"])
                fee = 0.02 * gross if gross > 0 else 0
                sim_exit_pnl = gross - fee
            elif stop is not None and bid_diff <= -stop:
                should_exit = True
                shares = r["notional"] / r["entry"] if r["entry"] > 0 else 0
                gross = shares * (r["bid_at_T"] - r["entry"])
                fee = 0.02 * gross if gross > 0 else 0
                sim_exit_pnl = gross - fee
            if should_exit:
                exited += 1
                exit_pnl += sim_exit_pnl
            else:
                kept += 1
                kept_pnl += r["pnl"]
        total = kept_pnl + exit_pnl
        print(f"  {label:<32}{kept:>16}{exited:>9}${exit_pnl:>+8.2f}${total:>+8.2f}")

    c.close()


if __name__ == "__main__":
    main()
