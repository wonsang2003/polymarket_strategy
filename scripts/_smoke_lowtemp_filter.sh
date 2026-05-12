#!/usr/bin/env bash
set -e
cd /home/ubuntu/polymarket
echo "--- scanner test: confirm low-temp markets filtered ---"
venv/bin/python <<'PY'
from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner

scanner = WeatherMarketScanner(PolymarketPublicClient())
contracts = scanner.find_weather_bracket_markets()
print(f"total bracket contracts found: {len(contracts)}")

# Check none are low-temp
low_temp = [c for c in contracts if "lowest" in c.question.lower() or "low temp" in c.question.lower()]
print(f"low-temp contracts (should be 0): {len(low_temp)}")
if low_temp:
    print("LEAKED LOW-TEMP MARKETS:")
    for c in low_temp[:5]:
        print(f"  {c.market_id} {c.city} {c.question[:80]}")

# Check all kept are high-temp
high_temp = [c for c in contracts if "highest" in c.question.lower() or "high temp" in c.question.lower()]
print(f"high-temp contracts: {len(high_temp)}")
print(f"other (no high/low keyword): {len(contracts) - len(high_temp) - len(low_temp)}")
PY
