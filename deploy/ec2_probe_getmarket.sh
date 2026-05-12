#!/usr/bin/env bash
# Probe: does the Apr 23 conditionId dispatch fix actually work for the 3
# "EMPTY" markets, and what does the raw Gamma API say about them?
set -eu
REPO=/home/ubuntu/polymarket
cd "$REPO"

echo "===== 1) Verify api.py has the Apr 23 conditionId dispatch ====="
grep -n -B1 -A4 'condition_ids\|get_market' "$REPO/polymarket_strat/api.py" | head -60

echo
echo "===== 2) Call get_market() directly on each EMPTY conditionId ====="
"$REPO/venv/bin/python3" - <<'PY'
from polymarket_strat.api import PolymarketPublicClient
import json, traceback

client = PolymarketPublicClient()
# The three that returned EMPTY in the trace
probes = [
    ("amsterdam id=31",    "0x3d524d34335fdfe0e76e8bc40dc4a72a4fbe1a7c3cf4cf38ac4e6c8e8ea99e83"),
    ("buenos_aires id=20", "0xefcb5ff6dc6a4cda8e36"),   # truncated in DB display — let's grab full from DB
    ("munich id=35",       "0xeeb2481db1808b69dfd2"),
]
# Replace with real full IDs from DB
import sqlite3
conn = sqlite3.connect("/home/ubuntu/polymarket/data/weather/weather.db")
for row in conn.execute("""
    SELECT id, city, market_id FROM trade_history
     WHERE outcome IS NULL AND target_date='2026-04-22'
       AND city IN ('amsterdam','munich','buenos_aires')
     ORDER BY city
"""):
    rid, city, mkt = row
    print(f"\n--- {city} id={rid} market_id={mkt!r}  len={len(mkt)}")
    try:
        m = client.get_market(mkt)
        if not m:
            print(f"    get_market -> EMPTY")
        else:
            print(f"    get_market -> keys={list(m.keys())[:12]}")
            print(f"      closed={m.get('closed')}  accepting={m.get('acceptingOrders')}")
            print(f"      outcomePrices={m.get('outcomePrices')}")
            print(f"      endDateIso={m.get('endDateIso')}")
            print(f"      slug={m.get('slug')}")
    except Exception as e:
        print(f"    get_market EXC: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
PY

echo
echo "===== 3) Raw HTTP probe: /markets?condition_ids=<id>&limit=1 ====="
"$REPO/venv/bin/python3" - <<'PY'
import requests, json, sqlite3
conn = sqlite3.connect("/home/ubuntu/polymarket/data/weather/weather.db")
for row in conn.execute("""
    SELECT id, city, market_id FROM trade_history
     WHERE outcome IS NULL AND target_date='2026-04-22'
       AND city IN ('amsterdam','munich','buenos_aires')
     ORDER BY city
"""):
    rid, city, mkt = row
    url = f"https://gamma-api.polymarket.com/markets?condition_ids={mkt}&limit=1"
    try:
        r = requests.get(url, timeout=10)
        print(f"\n{city} id={rid}  status={r.status_code}  body_len={len(r.text)}")
        try:
            body = r.json()
            if isinstance(body, list) and body:
                m = body[0]
                print(f"  list[0]: slug={m.get('slug')!r} closed={m.get('closed')} accepting={m.get('acceptingOrders')} outPx={m.get('outcomePrices')}")
            else:
                print(f"  body: {json.dumps(body)[:200]}")
        except Exception:
            print(f"  raw: {r.text[:200]}")
    except Exception as e:
        print(f"  EXC: {e}")
PY
