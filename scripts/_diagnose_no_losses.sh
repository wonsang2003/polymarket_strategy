#!/usr/bin/env bash
cd /home/ubuntu/polymarket
venv/bin/python <<'PY'
import sqlite3
from datetime import datetime, timezone, timedelta

c = sqlite3.connect("data/weather/weather.db")

# 1. Latest trades since 22:00 KST (= 13:00 UTC today)
print("=== Trades created since 22:00 KST (13:00 UTC) ===")
recent = c.execute("""
    SELECT id, city, target_date, side, token_side, model_prob, market_prob, edge,
           notional, entry_price, pnl, outcome, created_at, settled_at,
           bracket_lower_f, bracket_upper_f, exit_reason, regime
    FROM trade_history
    WHERE created_at >= '2026-04-25 13:00:00'
    ORDER BY created_at DESC
""").fetchall()
print(f"  count: {len(recent)}")
print(f"  {'id':>4} {'created':<19} {'city':<14} {'side':<8} {'tok':<4} {'p_mod':>6} {'p_mkt':>6} {'edge':>6} "
      f"{'notnl':>5} {'entry':>6} {'pnl':>8} {'br':<14}")
for t in recent[:25]:
    pnl = "OPEN" if t[10] is None else f"${t[10]:.2f}"
    bracket = f"{t[14]:.1f}-{t[15]:.1f}F"
    print(f"  {t[0]:>4} {t[12][:19]} {(t[1] or '')[:14]:<14} "
          f"{(t[3] or '')[:8]:<8} {(t[4] or '')[:4]:<4} "
          f"{float(t[5] or 0):>6.3f} {float(t[6] or 0):>6.3f} "
          f"{float(t[7] or 0):>6.3f} {float(t[8] or 0):>5.0f} "
          f"{float(t[9] or 0):>6.3f} {pnl:>8} {bracket:<14}")

# 2. NO-bet pattern analysis
print()
print("=== ALL settled BUY_NO trades — model vs reality ===")
no_trades = c.execute("""
    SELECT id, city, side, token_side, model_prob, market_prob,
           bracket_lower_f, bracket_upper_f, pnl, outcome, exit_reason
    FROM trade_history
    WHERE (side LIKE '%NO%' OR token_side = 'NO')
      AND pnl IS NOT NULL
    ORDER BY id DESC
""").fetchall()
print(f"  count: {len(no_trades)}")

# Compute realized win rate at each model_prob bin
import collections
bins = collections.defaultdict(lambda: {"n": 0, "wins": 0, "total_pnl": 0.0})
for t in no_trades:
    pmod = float(t[4] or 0)
    pnl = float(t[8] or 0)
    if pmod < 0.7:
        bin_label = "<0.70"
    elif pmod < 0.8:
        bin_label = "0.70-0.80"
    elif pmod < 0.9:
        bin_label = "0.80-0.90"
    elif pmod < 0.95:
        bin_label = "0.90-0.95"
    else:
        bin_label = ">=0.95"
    bins[bin_label]["n"] += 1
    if pnl > 0:
        bins[bin_label]["wins"] += 1
    bins[bin_label]["total_pnl"] += pnl

print(f"  {'model_p':<12} {'n':>4} {'wins':>5} {'wr%':>5} {'total_pnl':>10}")
for label in ["<0.70", "0.70-0.80", "0.80-0.90", "0.90-0.95", ">=0.95"]:
    b = bins.get(label, {"n": 0, "wins": 0, "total_pnl": 0})
    if b["n"]:
        print(f"  {label:<12} {b['n']:>4} {b['wins']:>5} "
              f"{b['wins']/b['n']*100:>4.0f}% ${b['total_pnl']:>9.2f}")

# 3. Bracket width analysis
print()
print("=== NO trades by bracket width (1°C narrow vs >2°C wide) ===")
narrow = [t for t in no_trades if (float(t[7]) - float(t[6])) <= 2.5]
wide = [t for t in no_trades if (float(t[7]) - float(t[6])) > 2.5]
def stats(label, ts):
    if not ts:
        print(f"  {label}: 0 trades")
        return
    pnls = [float(t[8]) for t in ts]
    wins = [p for p in pnls if p > 0]
    print(f"  {label}: n={len(ts)}, wins={len(wins)}, "
          f"wr={len(wins)/len(ts)*100:.0f}%, total=${sum(pnls):.2f}, "
          f"avg=${sum(pnls)/len(ts):.2f}")
stats("Narrow (≤2.5°F width)", narrow)
stats("Wide (>2.5°F width)", wide)

# 4. Exit reason breakdown for NO trades
print()
print("=== NO trade exits ===")
exits = collections.defaultdict(lambda: {"n": 0, "wins": 0, "total": 0.0})
for t in no_trades:
    reason = t[10] or "natural_settlement"
    exits[reason]["n"] += 1
    pnl = float(t[8])
    if pnl > 0:
        exits[reason]["wins"] += 1
    exits[reason]["total"] += pnl
for reason, e in sorted(exits.items()):
    print(f"  {reason:<35} n={e['n']:>3}  wins={e['wins']:>2}  "
          f"wr={e['wins']/e['n']*100:>3.0f}%  total=${e['total']:.2f}")

# 5. The CRITICAL question: is model_prob_NO matching realized NO outcome?
print()
print("=== Reliability of model_prob on settled NO trades ===")
# For NO trades: model_prob = P(NO outcome). Win = NO outcome happened.
# We want: average model_prob vs realized win rate.
for label in ["<0.70", "0.70-0.80", "0.80-0.90", "0.90-0.95", ">=0.95"]:
    b = bins.get(label, {"n": 0})
    if b["n"]:
        in_bin = []
        for t in no_trades:
            pmod = float(t[4] or 0)
            in_label = "<0.70" if pmod < 0.7 else "0.70-0.80" if pmod < 0.8 else \
                "0.80-0.90" if pmod < 0.9 else "0.90-0.95" if pmod < 0.95 else ">=0.95"
            if in_label == label:
                in_bin.append(t)
        avg_pmod = sum(float(t[4] or 0) for t in in_bin) / len(in_bin)
        wr = sum(1 for t in in_bin if float(t[8]) > 0) / len(in_bin)
        gap = avg_pmod - wr
        print(f"  bin {label:<12} n={len(in_bin):>3}  "
              f"model_says_NO={avg_pmod*100:.1f}%  reality={wr*100:.0f}%  "
              f"gap={gap*100:+.1f}%")

# 6. Check token_side sanity
print()
print("=== Token side consistency check ===")
for side, token_side in c.execute("""
    SELECT side, token_side, COUNT(*) FROM trade_history
    WHERE pnl IS NOT NULL GROUP BY side, token_side
""").fetchall():
    print(f"  side={side or '(null)'}, token_side={token_side or '(null)'}")
PY
