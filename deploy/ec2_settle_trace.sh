#!/usr/bin/env bash
# Trace *why* specific open Apr 22 rows aren't settling.
# Read-only except where we run settle --auto (which would be run by cron anyway).
set -eu
REPO=/home/ubuntu/polymarket
DB=$REPO/data/weather/weather.db
cd "$REPO"

echo "===== Tail of autotrade.log (last 100 lines) ====="
tail -100 "$REPO/logs/autotrade.log" 2>/dev/null || echo "  (no autotrade.log)"

echo
echo "===== Polymarket state for each open Apr 22 market ====="
"$REPO/venv/bin/python3" - <<'PY'
import sqlite3
from polymarket_strat.api import PolymarketPublicClient

DB = "/home/ubuntu/polymarket/data/weather/weather.db"
conn = sqlite3.connect(DB)
rows = conn.execute("""
    SELECT id, city, market_id, token_id, side, entry_price
      FROM trade_history
     WHERE outcome IS NULL AND target_date = '2026-04-22'
     ORDER BY city
""").fetchall()

client = PolymarketPublicClient()
print(f"{'id':<4}{'city':<15}{'closed':<8}{'accepting':<11}{'outPx':<20}{'mkt_id_prefix'}")
for row_id, city, mkt_id, token_id, side, entry in rows:
    try:
        m = client.get_market(mkt_id)
    except Exception as e:
        print(f"{row_id:<4}{city:<15}ERR      {str(e)[:40]}")
        continue
    if not m:
        print(f"{row_id:<4}{city:<15}EMPTY    (get_market returned empty)  {mkt_id[:22]}")
        continue
    closed = str(m.get("closed"))
    accepting = str(m.get("acceptingOrders"))
    out_px = m.get("outcomePrices") or m.get("outcome_prices") or "?"
    print(f"{row_id:<4}{city:<15}{closed:<8}{accepting:<11}{str(out_px):<20}{mkt_id[:22]}")
PY

echo
echo "===== Run settle --auto (trace) ====="
"$REPO/venv/bin/polymarket-strat" settle --auto 2>&1 | tail -60

echo
echo "===== Post-settle trade_history summary ====="
sqlite3 "$DB" "SELECT COUNT(*) || ' total / ' || SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) || ' open / ' || SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) || ' settled, cum_pnl=' || ROUND(SUM(COALESCE(pnl,0)),2) FROM trade_history;"

exit 0
