#!/usr/bin/env bash
set -eu
REPO=/home/ubuntu/polymarket
"$REPO/venv/bin/python3" - <<'PY'
import requests, json, sqlite3

conn = sqlite3.connect("/home/ubuntu/polymarket/data/weather/weather.db")
for row in conn.execute("""
    SELECT id, city, market_id, side, entry_price, notional FROM trade_history
     WHERE outcome IS NULL AND target_date='2026-04-22'
     ORDER BY city
"""):
    rid, city, mkt, side, entry, notional = row
    print(f"\n=== {city} id={rid} side={side} entry={entry} notional=${notional} ===")
    # Try both
    for closed_flag in (None, "true"):
        p = {"condition_ids": mkt, "limit": 1}
        if closed_flag: p["closed"] = closed_flag
        r = requests.get("https://gamma-api.polymarket.com/markets", params=p, timeout=10)
        body = r.json() if r.ok else []
        if body:
            m = body[0]
            print(f"  closed={closed_flag}: id={m.get('id')} closed={m.get('closed')} accepting={m.get('acceptingOrders')} outPx={m.get('outcomePrices')} umaResolutionStatus={m.get('umaResolutionStatus')}")
            break
        else:
            print(f"  closed={closed_flag}: EMPTY")
PY
