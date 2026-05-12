"""Comprehensive 'today' audit — what got placed, what settled, what's open,
how much PnL, and how does it look mathematically.

Splits results by:
  - category (mainstream/weather, weather_tail_no, legacy)
  - exit_reason (rebalance vs natural)
  - settled vs open
"""
import sqlite3
import sys
import math
from collections import defaultdict
from datetime import date, datetime, timezone


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    # Today in UTC and KST
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date().isoformat()
    today_kst = (now_utc.astimezone()).date().isoformat() if now_utc.tzinfo else today_utc

    print(f"now_utc          : {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"today (utc date) : {today_utc}")
    print()

    # All trades with created_at on today (UTC)
    rows = c.execute(
        """
        SELECT id, city, side, token_side, model_prob, market_prob,
               edge, entry_edge, notional, entry_price, pnl, outcome,
               created_at, settled_at, bracket_lower_f, bracket_upper_f,
               exit_reason, category, strategy_name
        FROM trade_history
        WHERE date(created_at) = ?
        ORDER BY id
        """,
        (today_utc,),
    ).fetchall()

    print(f"{'─'*100}")
    print(f"TRADES CREATED TODAY ({today_utc})")
    print(f"{'─'*100}")
    print(f"  total: {len(rows)}")
    print()

    # Categories
    cat_counts = defaultdict(lambda: {"placed": 0, "settled": 0, "open": 0,
                                       "wins": 0, "pnl": 0.0,
                                       "exit_reasons": defaultdict(int)})
    for r in rows:
        cat = r["category"] or "legacy_or_mainstream"
        cat_counts[cat]["placed"] += 1
        if r["pnl"] is not None:
            cat_counts[cat]["settled"] += 1
            cat_counts[cat]["pnl"] += float(r["pnl"])
            if float(r["pnl"]) > 0:
                cat_counts[cat]["wins"] += 1
            cat_counts[cat]["exit_reasons"][r["exit_reason"] or "natural"] += 1
        else:
            cat_counts[cat]["open"] += 1

    print("[1] BY CATEGORY")
    print(f"  {'category':<22} {'placed':>7} {'settled':>8} {'open':>5} "
          f"{'wins':>5} {'wr%':>5} {'pnl_$':>8}")
    for cat, v in sorted(cat_counts.items()):
        wr = v["wins"] / v["settled"] * 100 if v["settled"] else 0
        print(f"  {cat:<22} {v['placed']:>7} {v['settled']:>8} "
              f"{v['open']:>5} {v['wins']:>5} {wr:>4.1f}% "
              f"${v['pnl']:>+7.2f}")
    print()

    # Exit reasons summary
    print("[2] EXIT REASONS (settled rows)")
    all_reasons = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for r in rows:
        if r["pnl"] is None:
            continue
        reason = r["exit_reason"] or "natural"
        all_reasons[reason]["n"] += 1
        all_reasons[reason]["pnl"] += float(r["pnl"])
        if float(r["pnl"]) > 0:
            all_reasons[reason]["wins"] += 1
    print(f"  {'reason':<28} {'n':>4} {'wr%':>5} {'pnl_$':>9} {'avg_$':>8}")
    for reason, v in sorted(all_reasons.items()):
        wr = v["wins"] / v["n"] * 100 if v["n"] else 0
        avg = v["pnl"] / v["n"] if v["n"] else 0
        print(f"  {reason:<28} {v['n']:>4} {wr:>4.1f}% ${v['pnl']:>+8.2f} ${avg:>+7.2f}")
    print()

    # Hold-time distribution
    print("[3] HOLD TIMES — were trades closed quickly or held?")
    holds_min = []
    holds_by_reason = defaultdict(list)
    for r in rows:
        if r["settled_at"] is None or r["created_at"] is None:
            continue
        cr = r["created_at"].replace("Z", "").split("+")[0].split(".")[0]
        st = r["settled_at"].replace("Z", "").split("+")[0].split(".")[0]
        try:
            cdt = datetime.fromisoformat(cr)
            sdt = datetime.fromisoformat(st)
            h = (sdt - cdt).total_seconds() / 60
            holds_min.append(h)
            holds_by_reason[r["exit_reason"] or "natural"].append(h)
        except Exception:
            continue
    if holds_min:
        holds_min.sort()
        n = len(holds_min)
        p50 = holds_min[n // 2]
        p90 = holds_min[int(n * 0.9)]
        p10 = holds_min[int(n * 0.1)]
        print(f"  n={n}  p10={p10:.0f}m  p50={p50:.0f}m  p90={p90:.0f}m  "
              f"min={holds_min[0]:.0f}m  max={holds_min[-1]:.0f}m")
        print(f"\n  by exit_reason:")
        for reason, hs in sorted(holds_by_reason.items()):
            hs_sorted = sorted(hs)
            n2 = len(hs_sorted)
            print(f"    {reason:<28} n={n2:>3}  p50={hs_sorted[n2//2]:.0f}m  "
                  f"max={hs_sorted[-1]:.0f}m")
    else:
        print("  (no settled trades with hold-time data)")
    print()

    # Math coherence: edge vs PnL realized
    print("[4] EDGE COHERENCE — does positive edge correlate with positive PnL?")
    edge_pnl_pairs = []
    for r in rows:
        if r["pnl"] is None or r["edge"] is None:
            continue
        e = float(r["edge"])
        # Filter outliers from the legacy USD-EV bug
        if abs(e) > 1.0:
            continue
        edge_pnl_pairs.append((e, float(r["pnl"]) / max(float(r["notional"] or 1), 1)))
    if len(edge_pnl_pairs) >= 5:
        # Pearson correlation
        ex = sum(e for e, _ in edge_pnl_pairs) / len(edge_pnl_pairs)
        py = sum(p for _, p in edge_pnl_pairs) / len(edge_pnl_pairs)
        num = sum((e - ex) * (p - py) for e, p in edge_pnl_pairs)
        denom_e = math.sqrt(sum((e - ex) ** 2 for e, _ in edge_pnl_pairs))
        denom_p = math.sqrt(sum((p - py) ** 2 for _, p in edge_pnl_pairs))
        corr = num / (denom_e * denom_p) if denom_e and denom_p else 0
        print(f"  n={len(edge_pnl_pairs)}, mean_edge={ex:.4f}, mean_pnl_pct={py*100:.2f}%")
        print(f"  Pearson(edge, realized_return): {corr:+.3f}")
        if corr > 0.3:
            print("  ✓ Positive correlation — high-edge trades won more often")
        elif corr < -0.3:
            print("  ✗ NEGATIVE correlation — high-edge trades LOST more often")
        else:
            print("  ~ near-zero — sample noise dominates the signal")
    else:
        print(f"  insufficient samples ({len(edge_pnl_pairs)} valid pairs)")
    print()

    # All today's trades — line by line
    print(f"{'─'*100}")
    print(f"FULL TODAY'S TRADES")
    print(f"{'─'*100}")
    print(f"  {'id':>4} {'created':<19} {'city':<13} {'side':<6} {'tok':<3} "
          f"{'p_mod':>6} {'p_mkt':>6} {'edge':>6} {'notnl':>5} "
          f"{'pnl':>8} {'reason':<26} {'cat':<6}")
    for r in rows:
        cat = (r["category"] or "—")[:6]
        pnl = "OPEN" if r["pnl"] is None else f"${float(r['pnl']):+.2f}"
        edge = float(r["edge"]) if r["edge"] is not None else 0
        edge_str = f"{edge:+.3f}" if abs(edge) <= 1 else f"{edge:.0f}!"
        print(f"  #{r['id']:>3} {r['created_at'][:19]} "
              f"{(r['city'] or '')[:13]:<13} "
              f"{(r['side'] or '')[:6]:<6} "
              f"{(r['token_side'] or '')[:3]:<3} "
              f"{float(r['model_prob'] or 0):>6.3f} "
              f"{float(r['market_prob'] or 0):>6.3f} "
              f"{edge_str:>6} ${float(r['notional'] or 0):>4.0f} "
              f"{pnl:>8} {(r['exit_reason'] or 'natural')[:26]:<26} {cat:<6}")
    print()

    # Aggregate today's totals
    settled_today = [r for r in rows if r["pnl"] is not None]
    if settled_today:
        pnls = [float(r["pnl"]) for r in settled_today]
        wins = sum(1 for p in pnls if p > 0)
        notional_total = sum(float(r["notional"] or 0) for r in settled_today)
        print(f"[5] TODAY'S TOTAL ({len(settled_today)} settled)")
        print(f"  total notional risked  : ${notional_total:,.2f}")
        print(f"  total PnL              : ${sum(pnls):+,.2f}")
        print(f"  win rate               : {wins}/{len(settled_today)} = "
              f"{wins/len(settled_today)*100:.1f}%")
        print(f"  PnL / notional         : "
              f"{sum(pnls)/max(notional_total,1)*100:+.2f}%")
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1))
            t = mean / (sd / math.sqrt(len(pnls))) if sd > 0 else float("inf")
            print(f"  mean PnL               : ${mean:+.3f}")
            print(f"  stdev                  : ${sd:.2f}")
            print(f"  t-stat vs $0           : {t:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
