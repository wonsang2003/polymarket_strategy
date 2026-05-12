#!/usr/bin/env bash
# Run settle --auto on EC2, capture output, always exit 0 (osascript guard).
REPO=/home/ubuntu/polymarket
cd "$REPO"

echo "===== settle --auto ====="
"$REPO/venv/bin/python3" -m polymarket_strat.main settle --auto 2>&1 | tail -80

echo
echo "===== trade_history post-settle ====="
sqlite3 "$REPO/data/weather/weather.db" "SELECT COUNT(*) || ' total / ' || SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) || ' open / ' || SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) || ' settled, cum_pnl=' || ROUND(SUM(COALESCE(pnl,0)),2) FROM trade_history;"

echo
echo "===== Newly settled rows (last hour) ====="
sqlite3 -header -column "$REPO/data/weather/weather.db" "SELECT city, target_date, side, entry_price, notional, outcome, ROUND(pnl,2) pnl, substr(settled_at,1,19) settled_at FROM trade_history WHERE outcome IS NOT NULL AND settled_at >= datetime('now', '-1 hour') ORDER BY settled_at DESC;"

exit 0
