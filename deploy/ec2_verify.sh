#!/usr/bin/env bash
# Diagnostic: confirm today's fixes are present on EC2, show DB state.
REPO=/home/ubuntu/polymarket
DB=$REPO/data/weather/weather.db

echo "===== A) api.py conditionId dispatch present ====="
grep -n 'condition_ids' "$REPO/polymarket_strat/api.py" || echo "  MISSING"

echo
echo "===== B) market_scanner.py prefers numeric id ====="
grep -n -A1 'market_id = ' "$REPO/polymarket_strat/infrastructure/weather/market_scanner.py" | head -10

echo
echo "===== C) trade_history rows (pre-settle) ====="
sqlite3 "$DB" "SELECT COUNT(*) || ' total / ' || SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) || ' open / ' || SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) || ' settled' FROM trade_history;"

echo
echo "===== D) Sample open positions ====="
sqlite3 -header -column "$DB" "SELECT substr(market_id,1,22) as mkt_id, city, target_date, side, entry_price, notional FROM trade_history WHERE outcome IS NULL ORDER BY target_date LIMIT 5;"

echo
echo "===== E) Target-date distribution of open positions ====="
sqlite3 -header -column "$DB" "SELECT target_date, COUNT(*) n FROM trade_history WHERE outcome IS NULL GROUP BY target_date ORDER BY target_date;"
