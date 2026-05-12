#!/usr/bin/env bash
# Probe Gamma API with various filter combos to find the 3 missing markets.
set -eu
REPO=/home/ubuntu/polymarket
cd "$REPO"

"$REPO/venv/bin/python3" - <<'PY'
import requests, json, sqlite3

conn = sqlite3.connect("/home/ubuntu/polymarket/data/weather/weather.db")
missing = list(conn.execute("""
    SELECT id, city, market_id, token_id, question FROM trade_history
     WHERE outcome IS NULL AND target_date='2026-04-22'
       AND city IN ('amsterdam','munich','buenos_aires')
     ORDER BY city
"""))

# 1) Compare with a working conditionId from same cohort (hong_kong id=34)
working = conn.execute("SELECT id, city, market_id FROM trade_history WHERE id=34").fetchone()
print(f"=== Working reference (hong_kong) ===  {working[2]!r}")
r = requests.get(f"https://gamma-api.polymarket.com/markets?condition_ids={working[2]}&limit=1", timeout=10)
print(f"  status={r.status_code} body_len={len(r.text)}")
print(f"  body[:300]={r.text[:300]}\n")

for rid, city, mkt_id, token_id, question in missing:
    print(f"=== {city} id={rid} ===")
    print(f"  mkt_id: {mkt_id}")
    print(f"  token_id: {token_id!r}")
    print(f"  question: {question!r}")
    # a) default
    for params in [
        {"condition_ids": mkt_id, "limit": 1},
        {"condition_ids": mkt_id, "limit": 1, "closed": "true"},
        {"condition_ids": mkt_id, "limit": 1, "active": "false"},
        {"condition_ids": mkt_id, "limit": 1, "active": "false", "closed": "true"},
        {"condition_ids": mkt_id, "limit": 1, "archived": "true"},
    ]:
        r = requests.get("https://gamma-api.polymarket.com/markets", params=params, timeout=10)
        try:
            body = r.json()
        except Exception:
            body = r.text[:100]
        n = len(body) if isinstance(body, list) else "?"
        print(f"  params={params}  status={r.status_code}  n={n}")
    # b) try token_id as clobTokenIds
    if token_id:
        for key in ("clob_token_ids", "token_ids"):
            r = requests.get("https://gamma-api.polymarket.com/markets", params={key: token_id, "limit": 1}, timeout=10)
            try:
                body = r.json()
                n = len(body) if isinstance(body, list) else "?"
                print(f"  via {key}={token_id[:20]}...  status={r.status_code}  n={n}")
                if isinstance(body, list) and body:
                    m = body[0]
                    print(f"    FOUND: id={m.get('id')} slug={m.get('slug')} closed={m.get('closed')} outPx={m.get('outcomePrices')}")
            except Exception as e:
                print(f"  via {key}: exc {e}")
    print()
PY
