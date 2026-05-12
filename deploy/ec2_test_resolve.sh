#!/usr/bin/env bash
set -eu
REPO=/home/ubuntu/polymarket
cd "$REPO"

"$REPO/venv/bin/python3" - <<'PY'
import sqlite3
from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.main import _resolve_via_polymarket, _station_day_ended
from datetime import datetime, timezone, date

client = PolymarketPublicClient()
conn = sqlite3.connect("/home/ubuntu/polymarket/data/weather/weather.db")
rows = list(conn.execute("""
    SELECT id, city, market_id, side, entry_price, notional FROM trade_history
     WHERE outcome IS NULL AND target_date='2026-04-22'
     ORDER BY city
"""))

print(f"now UTC: {datetime.now(timezone.utc).isoformat()}")
print(f"{'id':<4}{'city':<14}{'get_mkt_keys':<30}{'closed':<8}{'accept':<8}{'outPx':<18}{'resolve':<9}{'day_ended':<10}")
for rid, city, mkt, side, entry, notional in rows:
    m = client.get_market(mkt)
    if not m:
        print(f"{rid:<4}{city:<14}EMPTY                         -       -       -                 None     {_station_day_ended(city, date(2026,4,22))}")
        continue
    closed = m.get('closed')
    accepting = m.get('acceptingOrders')
    outpx = str(m.get('outcomePrices'))[:16]
    outcome = _resolve_via_polymarket({"market_id": mkt, "token_id": None}, client)
    day_ended = _station_day_ended(city, date(2026,4,22))
    print(f"{rid:<4}{city:<14}{'(ok)':<30}{str(closed):<8}{str(accepting):<8}{outpx:<18}{str(outcome):<9}{str(day_ended):<10}")
PY
