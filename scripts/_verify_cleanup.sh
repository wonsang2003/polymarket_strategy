#!/usr/bin/env bash
cd /home/ubuntu/polymarket
venv/bin/python <<'PY'
import sqlite3
c = sqlite3.connect("data/weather/weather.db")
print("--- low-temp trades AFTER cleanup ---")
cur = c.execute(
    "SELECT id, city, pnl, exit_reason, question FROM trade_history "
    "WHERE LOWER(question) LIKE '%lowest%' OR LOWER(question) LIKE '%low temp%' "
    "ORDER BY id"
)
for r in cur.fetchall():
    print(f"  id={r[0]:>3}  {r[1]:<14}  pnl={r[2]}  exit={r[3]}  q={r[4][:60]}")

total_pnl = c.execute("SELECT SUM(pnl) FROM trade_history WHERE pnl IS NOT NULL").fetchone()[0]
n_settled = c.execute("SELECT COUNT(*) FROM trade_history WHERE pnl IS NOT NULL").fetchone()[0]
print(f"\n  total settled trades: {n_settled}")
print(f"  total cumulative pnl: ${total_pnl}")
PY
