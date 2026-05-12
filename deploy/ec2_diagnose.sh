#!/usr/bin/env bash
# Diagnostic sweep: what's settled, what's open, why. Read-only.
set -eu
REPO=/home/ubuntu/polymarket
DB=$REPO/data/weather/weather.db

echo "===== EC2 clocks ====="
echo "  UTC:   $(date -u +'%Y-%m-%d %H:%M:%S %Z')"
echo "  Local: $(date +'%Y-%m-%d %H:%M:%S %Z')"

echo
echo "===== trade_history summary ====="
sqlite3 "$DB" "SELECT COUNT(*) || ' total / ' || SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) || ' open / ' || SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) || ' settled, cum_pnl=' || ROUND(SUM(COALESCE(pnl,0)),2) FROM trade_history;"

echo
echo "===== Open positions by city + target_date ====="
sqlite3 -header -column "$DB" "
  SELECT id, city, target_date, side, entry_price, notional,
         substr(market_id,1,20) mkt_id_prefix
    FROM trade_history
   WHERE outcome IS NULL
   ORDER BY target_date, city;
"

echo
echo "===== Settled in last 24h ====="
sqlite3 -header -column "$DB" "
  SELECT id, city, target_date, side, outcome, ROUND(pnl,2) pnl,
         substr(settled_at,1,19) settled_at
    FROM trade_history
   WHERE settled_at >= datetime('now', '-24 hour')
   ORDER BY settled_at DESC;
"

echo
echo "===== Last 5 cron settle runs ====="
if [ -f "$REPO/logs/cron.log" ]; then
  grep -n "settle" "$REPO/logs/cron.log" | tail -20
else
  echo "  (no $REPO/logs/cron.log found)"
  ls "$REPO/logs/" 2>/dev/null | head -20 || echo "  (no logs dir)"
fi

echo
echo "===== Active crontab ====="
crontab -l 2>/dev/null | grep -E "^[^#[:space:]]" || echo "  (no crontab)"

echo
echo "===== _station_day_ended sanity ====="
cd "$REPO" && "$REPO/venv/bin/python3" -c "
from datetime import datetime, timezone, date
from polymarket_strat.main import _station_day_ended
from polymarket_strat.domain.weather.models import CITY_REGISTRY

now = datetime.now(timezone.utc)
print(f'now (UTC):     {now.isoformat()}')
target = date(2026, 4, 22)

open_cities = ['amsterdam','munich','nyc','toronto','buenos_aires','sao_paulo','hong_kong','wellington','sydney','tokyo','seoul','shanghai','dubai']
print(f'{\"city\":<15}{\"tz\":<25}{\"ended?\":<10}')
for c in open_cities:
    s = CITY_REGISTRY.get(c)
    tz = getattr(s, 'timezone', '(none)') if s else '(unknown city)'
    ended = _station_day_ended(c, target, now=now)
    print(f'{c:<15}{tz:<25}{str(ended):<10}')
"
