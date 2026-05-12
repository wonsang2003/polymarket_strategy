#!/usr/bin/env bash
cd /home/ubuntu/polymarket
venv/bin/python <<'PY'
import sqlite3
from datetime import datetime, timezone
c = sqlite3.connect("data/weather/weather.db")
cur = c.cursor()

# Find OPEN low-temp trades (pnl IS NULL)
rows = cur.execute(
    "SELECT id, city, notional, question FROM trade_history "
    "WHERE pnl IS NULL AND ("
    "    LOWER(question) LIKE '%lowest%' OR "
    "    LOWER(question) LIKE '%low temp%')"
).fetchall()

print(f"open low-temp trades to force-close: {len(rows)}")
for r in rows:
    print(f"  id={r[0]} {r[1]} notional=${r[2]} q={r[3][:60]}")

if not rows:
    print("none — all clean")
else:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        cur.execute(
            "UPDATE trade_history SET pnl = -notional, outcome = 0, "
            "settled_at = ?, exit_reason = COALESCE(exit_reason,'') || 'force_close_low_temp;' "
            "WHERE id = ?",
            (now_iso, r[0]),
        )
        print(f"  force-closed id={r[0]} → pnl=-${r[2]}")
    c.commit()

c.close()
print("done")
PY
