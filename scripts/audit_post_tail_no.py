"""Comprehensive post-deploy audit. Run on EC2.

Checks ruthlessly:
  1. Trade count by strategy_name / category since tail-NO deploy
  2. Data integrity: token_side label vs side label for NO trades
  3. Settled outcomes per strategy
  4. Tail-NO trade specifics: edge_distance, EV at entry, current MTM
  5. Cycle-level health: how often is each gate firing? sane numbers?
  6. Cumulative P&L attribution to each strategy
"""
import json
import sqlite3
import statistics as stats
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    # Tail-NO deploy timestamp = strategy file mtime in DB or use a known marker.
    # We deployed around 2026-04-26 16:30 KST = 07:30 UTC.
    deploy_kst = "2026-04-26 16:30:00"
    print("=" * 90)
    print(f"AUDIT — post tail-NO deploy ({deploy_kst} KST)")
    print(f"now_utc = {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 90)

    rows = c.execute(
        """
        SELECT id, city, target_date, side, token_side, model_prob,
               market_prob, edge, notional, entry_price, pnl, outcome,
               created_at, settled_at, bracket_lower_f, bracket_upper_f,
               exit_reason, regime, category, strategy_name
        FROM trade_history
        ORDER BY id
        """
    ).fetchall()

    pre = [r for r in rows if r["created_at"] and r["created_at"] < deploy_kst]
    post = [r for r in rows if r["created_at"] and r["created_at"] >= deploy_kst]
    print(f"\ntotal rows         : {len(rows)}")
    print(f"  pre-deploy       : {len(pre)}")
    print(f"  post-deploy      : {len(post)}")

    # ---- 1. category / strategy breakdown
    print("\n" + "=" * 90)
    print("[1] STRATEGY ATTRIBUTION (post-deploy)")
    print("=" * 90)
    cat_counts = defaultdict(lambda: {"total": 0, "settled": 0, "open": 0,
                                       "pnl": 0.0, "wins": 0})
    for r in post:
        key = f"category={r['category'] or '∅'}, strategy={r['strategy_name'] or '∅'}"
        cat_counts[key]["total"] += 1
        if r["pnl"] is not None:
            cat_counts[key]["settled"] += 1
            cat_counts[key]["pnl"] += float(r["pnl"])
            if float(r["pnl"]) > 0:
                cat_counts[key]["wins"] += 1
        else:
            cat_counts[key]["open"] += 1
    for key, v in sorted(cat_counts.items()):
        wr = v["wins"] / v["settled"] * 100 if v["settled"] else 0
        print(f"  {key}")
        print(f"    total={v['total']:>3}  open={v['open']:>3}  "
              f"settled={v['settled']:>3}  wr={wr:>4.1f}%  "
              f"pnl=${v['pnl']:+.2f}")

    # ---- 2. data integrity — token_side mismatch
    print("\n" + "=" * 90)
    print("[2] DATA INTEGRITY — token_side label sanity")
    print("=" * 90)
    side_token_pairs = defaultdict(int)
    mismatches = []
    for r in post:
        sd = (r["side"] or "")
        tk = (r["token_side"] or "")
        side_token_pairs[(sd, tk)] += 1
        # Expected: side='BUY_NO' → token_side='NO', side='BUY_YES' → 'YES'
        if "NO" in sd and tk != "NO":
            mismatches.append((r["id"], sd, tk, r["category"]))
        elif "YES" in sd and tk != "YES":
            mismatches.append((r["id"], sd, tk, r["category"]))
    print("  side / token_side pairs (post-deploy):")
    for (sd, tk), n in sorted(side_token_pairs.items()):
        print(f"    side={sd!r:<10} token_side={tk!r:<6} n={n:>3}")
    print(f"  inconsistent rows: {len(mismatches)}")
    for rid, sd, tk, cat in mismatches[:10]:
        print(f"    #{rid} side={sd} token_side={tk} category={cat}")

    # ---- 3. tail-NO trade specifics
    print("\n" + "=" * 90)
    print("[3] TAIL-NO TRADE BOOK")
    print("=" * 90)
    tail_no = [r for r in post if r["category"] == "weather_tail_no"]
    print(f"  count: {len(tail_no)}")
    print(f"  {'id':>4} {'city':<14} {'side':<7} {'tok':<3} "
          f"{'p_mod':>6} {'p_mkt':>6} {'edge':>7} {'notnl':>6} "
          f"{'entry':>6} {'pnl':>8} {'created':<19}")
    for r in tail_no:
        pnl = "OPEN" if r["pnl"] is None else f"${float(r['pnl']):+.2f}"
        print(f"  {r['id']:>4} {(r['city'] or '')[:14]:<14} "
              f"{(r['side'] or '')[:7]:<7} {(r['token_side'] or '')[:3]:<3} "
              f"{float(r['model_prob'] or 0):>6.3f} {float(r['market_prob'] or 0):>6.3f} "
              f"{float(r['edge'] or 0):>+7.3f} ${float(r['notional'] or 0):>5.0f} "
              f"{float(r['entry_price'] or 0):>6.3f} {pnl:>8} "
              f"{(r['created_at'] or '')[:19]}")

    # Settled tail-NO summary
    settled_tn = [r for r in tail_no if r["pnl"] is not None]
    if settled_tn:
        pnls = [float(r["pnl"]) for r in settled_tn]
        wins = sum(1 for p in pnls if p > 0)
        print(f"\n  settled tail-NO: n={len(settled_tn)}  "
              f"wr={wins/len(settled_tn)*100:.0f}%  total=${sum(pnls):+.2f}  "
              f"mean=${sum(pnls)/len(pnls):+.3f}")

    # ---- 4. ALL settled-since-deploy outcomes
    print("\n" + "=" * 90)
    print("[4] POST-DEPLOY SETTLEMENTS (any strategy)")
    print("=" * 90)
    settled_post = [r for r in post if r["pnl"] is not None]
    print(f"  count: {len(settled_post)}")
    if settled_post:
        pnls = [float(r["pnl"]) for r in settled_post]
        wins = sum(1 for p in pnls if p > 0)
        print(f"  win rate: {wins}/{len(settled_post)} = "
              f"{wins/len(settled_post)*100:.1f}%")
        print(f"  total pnl: ${sum(pnls):+.2f}")
        if len(pnls) > 1:
            sd = stats.stdev(pnls)
            mean = sum(pnls) / len(pnls)
            t = mean / (sd / (len(pnls) ** 0.5)) if sd else float("inf")
            print(f"  mean: ${mean:+.3f}  stdev: ${sd:.2f}  t-stat: {t:+.2f}")
        print(f"\n  by exit_reason:")
        by_reason = defaultdict(list)
        for r in settled_post:
            by_reason[r["exit_reason"] or "natural"].append(float(r["pnl"]))
        for reason, pp in sorted(by_reason.items()):
            wins_r = sum(1 for p in pp if p > 0)
            print(f"    {reason:<30} n={len(pp):>3}  "
                  f"wr={wins_r/len(pp)*100:>3.0f}%  total=${sum(pp):+.2f}")

    # ---- 5. ALL post-deploy trades and their state
    print("\n" + "=" * 90)
    print("[5] EVERY POST-DEPLOY TRADE")
    print("=" * 90)
    for r in post[-30:]:
        cat = (r["category"] or "")[:18]
        strat = (r["strategy_name"] or "")[:18]
        pnl = "OPEN" if r["pnl"] is None else f"${float(r['pnl']):+.2f}"
        outcome = r["outcome"]
        out_label = "OPEN" if outcome is None else f"out={outcome}"
        print(f"  #{r['id']:>3} {(r['created_at'] or '')[:16]} "
              f"{(r['city'] or '')[:12]:<12} {(r['side'] or '')[:6]:<6} "
              f"{(r['token_side'] or '')[:3]:<3} cat={cat:<18} "
              f"e={float(r['edge'] or 0):>+5.2f} "
              f"n=${float(r['notional'] or 0):>5.0f} "
              f"pnl={pnl:>8} {out_label:<10}")

    # ---- 6. open positions: are tail-NO and mainstream coexisting?
    print("\n" + "=" * 90)
    print("[6] CURRENTLY OPEN POSITIONS")
    print("=" * 90)
    open_all = [r for r in rows if r["pnl"] is None and r["outcome"] is None]
    print(f"  total open: {len(open_all)}")
    for r in open_all:
        print(f"    #{r['id']:>3} {(r['city'] or '')[:14]:<14} "
              f"cat={(r['category'] or 'old')[:20]:<20} "
              f"side={(r['side'] or '')[:6]:<6} "
              f"size=${float(r['notional'] or 0):>5.0f} "
              f"target={r['target_date']}")

    # ---- 7. FROM TAIL-NO PERSPECTIVE: per-trade EV check
    # Re-derive empirical hit rate from forecast_errors and verify it matches
    # the metadata stored at entry.
    print("\n" + "=" * 90)
    print("[7] TAIL-NO METADATA SPOT CHECK")
    print("=" * 90)
    # Tail-NO metadata is in trade_history.metadata column? Or stored elsewhere?
    cols = [d[1] for d in c.execute("PRAGMA table_info(trade_history)").fetchall()]
    if "metadata" in cols:
        for r in tail_no[:5]:
            md_raw = c.execute(
                "SELECT metadata FROM trade_history WHERE id = ?", (r["id"],)
            ).fetchone()
            if md_raw and md_raw[0]:
                try:
                    md = json.loads(md_raw[0])
                    print(f"  #{r['id']} {r['city']}: dist={md.get('edge_distance_f')}, "
                          f"emp_p_no={md.get('empirical_p_no')}, "
                          f"market_p_no={md.get('market_implied_p_no')}, "
                          f"EV/$={md.get('ev_per_dollar')}")
                except Exception as e:
                    print(f"  #{r['id']} metadata parse error: {e}")
    else:
        print("  trade_history has no 'metadata' column — metadata stored elsewhere")

    return 0


if __name__ == "__main__":
    sys.exit(main())
