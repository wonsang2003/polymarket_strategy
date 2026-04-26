"""Comprehensive post-deploy audit. Run on EC2.

V2 (Apr 26 evening): UTC vs KST fix + rebalance-eaten tail-NO surface.
"""
import json
import sqlite3
import statistics as stats
import sys
from collections import defaultdict
from datetime import datetime, timezone


# Tail-NO trades placed at 07:32 UTC and 11:07 UTC on 2026-04-26
DEPLOY_UTC = "2026-04-26 07:30:00"

KNOWN_TAIL_NO_MARKETS = {
    "2064941", "2064826", "2074435", "2074503", "2065058", "2074468",
    "2064881", "2074477", "2064827", "2064860", "2074311", "2064848",
    "2074469", "2082313",
}


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    print("=" * 90)
    print(f"AUDIT — post tail-NO deploy ({DEPLOY_UTC} UTC)")
    print(f"now_utc = {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 90)

    rows = c.execute(
        """
        SELECT id, city, target_date, side, token_side, model_prob,
               market_prob, edge, notional, entry_price, pnl, outcome,
               created_at, settled_at, bracket_lower_f, bracket_upper_f,
               exit_reason, regime, category, strategy_name, entry_edge
        FROM trade_history
        ORDER BY id
        """
    ).fetchall()

    pre = [r for r in rows if r["created_at"] and r["created_at"] < DEPLOY_UTC]
    post = [r for r in rows if r["created_at"] and r["created_at"] >= DEPLOY_UTC]
    print(f"\ntotal rows         : {len(rows)}")
    print(f"  pre-deploy       : {len(pre)}")
    print(f"  post-deploy      : {len(post)}")

    # --- 1. Tail-NO outcome attribution
    print("\n" + "=" * 90)
    print("[1] TAIL-NO TRADES (matched by known market_ids)")
    print("=" * 90)
    tail_no = [r for r in rows if r["market_id"] in KNOWN_TAIL_NO_MARKETS] if False else []
    # Need market_id; re-fetch.
    tail_no_rows = c.execute(
        "SELECT id, city, side, token_side, market_id, notional, entry_price, "
        "pnl, outcome, created_at, settled_at, exit_reason, "
        "category, strategy_name, entry_edge "
        "FROM trade_history WHERE market_id IN ({}) "
        "AND created_at >= '2026-04-26 07:30:00' "
        "ORDER BY created_at, id".format(",".join(["?"] * len(KNOWN_TAIL_NO_MARKETS))),
        list(KNOWN_TAIL_NO_MARKETS),
    ).fetchall()
    print(f"  count: {len(tail_no_rows)}")
    print(f"  {'id':>4} {'created':<19} {'city':<14} {'side':<7} "
          f"{'tok':<3} {'notnl':>5} {'entry':>6} {'pnl':>8} "
          f"{'outcome':<8} {'exit_reason':<24} {'cat':<6} "
          f"{'entry_edge':>10}")
    for r in tail_no_rows:
        pnl = "OPEN" if r["pnl"] is None else f"${float(r['pnl']):+.2f}"
        out_lbl = ("OPEN" if r["outcome"] is None
                   else f"out={r['outcome']}")
        cat = (r["category"] or "—")[:6]
        ee = f"{r['entry_edge']:.2f}" if r['entry_edge'] is not None else "—"
        print(f"  {r['id']:>4} {r['created_at'][:19]} "
              f"{(r['city'] or '')[:14]:<14} "
              f"{(r['side'] or '')[:7]:<7} "
              f"{(r['token_side'] or '')[:3]:<3} "
              f"${float(r['notional'] or 0):>4.0f} "
              f"{float(r['entry_price'] or 0):>6.3f} "
              f"{pnl:>8} {out_lbl:<8} "
              f"{(r['exit_reason'] or 'natural')[:24]:<24} "
              f"{cat:<6} {ee:>10}")

    settled_tn = [r for r in tail_no_rows if r["pnl"] is not None]
    if settled_tn:
        pnls = [float(r["pnl"]) for r in settled_tn]
        wins = sum(1 for p in pnls if p > 0)
        # Exit reason breakdown
        exit_reasons = defaultdict(list)
        for r in settled_tn:
            exit_reasons[r["exit_reason"] or "natural"].append(float(r["pnl"]))
        print(f"\n  settled tail-NO: n={len(settled_tn)}  wr={wins/len(settled_tn)*100:.1f}%  "
              f"total=${sum(pnls):+.2f}  mean=${sum(pnls)/len(pnls):+.3f}")
        print(f"  exit reason breakdown:")
        for reason, pp in sorted(exit_reasons.items()):
            wins_r = sum(1 for p in pp if p > 0)
            print(f"    {reason:<30} n={len(pp):>2}  wr={wins_r/len(pp)*100:>3.0f}%  "
                  f"total=${sum(pp):+.2f}")

    # --- 2. Pre-deploy mainstream comparison (sanity)
    print("\n" + "=" * 90)
    print("[2] ALL recent trades (last 25 by id)")
    print("=" * 90)
    for r in rows[-25:]:
        is_tn = r["market_id"] in KNOWN_TAIL_NO_MARKETS
        marker = "[TN]" if is_tn else "    "
        pnl = "OPEN" if r["pnl"] is None else f"${float(r['pnl']):+.2f}"
        out = ("OPEN" if r["outcome"] is None
               else f"out={r['outcome']}")
        print(f"  {marker} #{r['id']:>3} {r['created_at'][:19]} "
              f"{(r['city'] or '')[:14]:<14} "
              f"{(r['side'] or '')[:6]:<6} {pnl:>8} {out:<8} "
              f"reason={(r['exit_reason'] or 'natural')[:22]:<22} "
              f"cat={r['category'] or '—'}")

    # --- 3. Aggregate post-deploy by category
    print("\n" + "=" * 90)
    print("[3] POST-DEPLOY AGGREGATE BY (inferred_)category")
    print("=" * 90)
    cat_stats = defaultdict(lambda: {"total": 0, "settled": 0, "wins": 0, "pnl": 0.0})
    for r in post:
        # Infer category: explicit DB column, or "tail_no" if known market_id
        if r["market_id"] in KNOWN_TAIL_NO_MARKETS:
            cat = "weather_tail_no"
        else:
            cat = r["category"] or "mainstream"
        cat_stats[cat]["total"] += 1
        if r["pnl"] is not None:
            cat_stats[cat]["settled"] += 1
            cat_stats[cat]["pnl"] += float(r["pnl"])
            if float(r["pnl"]) > 0:
                cat_stats[cat]["wins"] += 1
    for cat, v in sorted(cat_stats.items()):
        wr = v["wins"] / v["settled"] * 100 if v["settled"] else 0
        print(f"  {cat:<22}: total={v['total']:>3}  settled={v['settled']:>3}  "
              f"wr={wr:>4.1f}%  total_pnl=${v['pnl']:+.2f}")

    # --- 4. Open positions
    print("\n" + "=" * 90)
    print("[4] CURRENTLY OPEN")
    print("=" * 90)
    open_all = [r for r in rows if r["pnl"] is None and r["outcome"] is None]
    print(f"  count: {len(open_all)}")
    for r in open_all:
        is_tn = r["market_id"] in KNOWN_TAIL_NO_MARKETS
        marker = "[TN]" if is_tn else "    "
        print(f"    {marker} #{r['id']:>3} {(r['city'] or '')[:14]:<14} "
              f"side={(r['side'] or '')[:6]:<6} "
              f"size=${float(r['notional'] or 0):>4.0f} "
              f"target={r['target_date']} "
              f"created={r['created_at'][:16]} "
              f"cat={r['category'] or '—'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
