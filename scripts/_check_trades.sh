#!/usr/bin/env bash
cd /home/ubuntu/polymarket
venv/bin/python <<'PY'
import sqlite3
c = sqlite3.connect("data/weather/weather.db")
cols = [r[1] for r in c.execute("PRAGMA table_info(trade_history)")]
print("trade_history columns:", cols)
n = c.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
print(f"total rows: {n}")
ts_col = "settled_at" if "settled_at" in cols else cols[-1]
print(f"--- last 5 rows by {ts_col} ---")
recent = c.execute(f"SELECT * FROM trade_history ORDER BY rowid DESC LIMIT 5").fetchall()
for r in recent:
    print(dict(zip(cols, r)))
PY
