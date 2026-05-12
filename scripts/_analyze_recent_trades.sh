#!/usr/bin/env bash
cd /home/ubuntu/polymarket
venv/bin/python <<'PY'
import sqlite3
from datetime import datetime, timedelta

c = sqlite3.connect("data/weather/weather.db")
cur = c.cursor()

# Get column names
cols = [r[1] for r in c.execute("PRAGMA table_info(trade_history)")]
print(f"trade_history columns: {len(cols)}")

# All trades with key fields
all_trades = c.execute("""
    SELECT id, city, target_date, side, model_prob, market_prob, edge,
           notional, entry_price, pnl, outcome, created_at, settled_at,
           question, regime, exit_reason
    FROM trade_history
    ORDER BY created_at DESC
""").fetchall()

print(f"Total trades: {len(all_trades)}")
print()

# Identify "recent" — trades made today after major strategy changes
# Cutoffs (KST):
#   Pre-fixes: created_at < '2026-04-25 12:00'
#   Post-ERA5/quantile: created_at >= '2026-04-25 12:00' but < 16:00
#   Post-unblock/tail:  created_at >= '2026-04-25 16:00'
recent_cutoff = '2026-04-25 12:00:00'
post_cutoff = '2026-04-25 16:00:00'

pre = [t for t in all_trades if t[11] and t[11] < recent_cutoff]
mid = [t for t in all_trades if t[11] and recent_cutoff <= t[11] < post_cutoff]
post = [t for t in all_trades if t[11] and t[11] >= post_cutoff]

print(f"=== Phase distribution ===")
print(f"  Before fixes (<12:00 KST):           n={len(pre)}")
print(f"  ERA5+quantile era (12:00–16:00):     n={len(mid)}")
print(f"  Post-unblock/tail (≥16:00 KST):      n={len(post)}")
print()

# For each phase, compute summary
def summarize(label, trades):
    if not trades:
        print(f"  {label}: 0 trades")
        return
    settled = [t for t in trades if t[9] is not None]
    open_ = [t for t in trades if t[9] is None and t[10] is None]
    n = len(trades)
    n_set = len(settled)
    if settled:
        pnls = [float(t[9]) for t in settled]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(settled) * 100 if settled else 0
        total_pnl = sum(pnls)
        cities = sorted(set(t[1] for t in settled))
        print(f"  --- {label} ({n} trades, {n_set} settled, {len(open_)} open) ---")
        print(f"      win rate: {wr:.0f}%   total PnL: ${total_pnl:.2f}")
        print(f"      cities: {', '.join(cities)}")
        if wins:
            print(f"      avg win:  ${sum(wins)/len(wins):.2f}  (n={len(wins)})")
        if losses:
            print(f"      avg loss: ${sum(losses)/len(losses):.2f}  (n={len(losses)})")
    else:
        print(f"  --- {label} ({n} trades, 0 settled, {len(open_)} open) ---")

print(f"=== Performance by phase ===")
summarize("Pre-12:00", pre)
print()
summarize("ERA5/Quantile era (12-16)", mid)
print()
summarize("Post-unblock/tail (16+)", post)
print()

# Latest 15 trades — show the actual recent activity
print(f"=== 15 most recent trades (any phase) ===")
print(f"  {'id':>4} {'created':<19} {'city':<14} {'side':<6} {'p_mod':>6} {'p_mkt':>6} {'edge':>7} {'notnl':>5} {'entry':>6} {'pnl':>8} {'q':<55}")
for t in all_trades[:15]:
    pnl_str = f"${t[9]:.2f}" if t[9] is not None else "OPEN"
    q = (t[13] or "")[:55]
    print(f"  {t[0]:>4} {t[11][:19]} {t[1][:14]:<14} {(t[3] or '')[:6]:<6} "
          f"{float(t[4] or 0):>6.3f} {float(t[5] or 0):>6.3f} "
          f"{float(t[6] or 0):>7.3f} {float(t[7] or 0):>5.1f} "
          f"{float(t[8] or 0):>6.3f} {pnl_str:>8} {q}")

print()
# By city
print(f"=== Performance by city (settled only) ===")
by_city = {}
for t in all_trades:
    if t[9] is None:
        continue
    city = t[1]
    if city not in by_city:
        by_city[city] = []
    by_city[city].append(float(t[9]))

print(f"  {'city':<16} {'n':>3} {'wins':>4} {'wr%':>5} {'total':>8} {'avg':>8}")
for city in sorted(by_city.keys()):
    pnls = by_city[city]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  {city:<16} {len(pnls):>3} {wins:>4} {wins/len(pnls)*100:>4.0f}% "
          f"${sum(pnls):>7.2f} ${sum(pnls)/len(pnls):>7.2f}")

print()
# By exit reason / category
print(f"=== Trades by exit reason (settled) ===")
by_reason = {}
for t in all_trades:
    if t[9] is None:
        continue
    reason = t[15] or "natural_settlement"
    if reason not in by_reason:
        by_reason[reason] = []
    by_reason[reason].append(float(t[9]))

for reason in sorted(by_reason.keys()):
    pnls = by_reason[reason]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  {reason:<35} n={len(pnls):>3}  wins={wins:>2}  total=${sum(pnls):>7.2f}")

print()
# Currently open positions
print(f"=== Currently open positions ===")
opens = [t for t in all_trades if t[9] is None and t[10] is None]
print(f"  {len(opens)} open")
for t in opens[:20]:
    print(f"    id={t[0]:>3} {t[1]:<14} target={t[2]} {(t[3] or ''):<6} "
          f"notional=${float(t[7] or 0):.0f}  edge={float(t[6] or 0):.3f}  "
          f"q={(t[13] or '')[:55]}")
PY
