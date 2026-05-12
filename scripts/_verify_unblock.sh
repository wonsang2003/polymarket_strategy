#!/usr/bin/env bash
set -e
cd /home/ubuntu/polymarket
venv/bin/python -c "
from polymarket_strat.domain.weather.strategy import _BLOCKED_CITIES
print(f'_BLOCKED_CITIES = {_BLOCKED_CITIES}')
print(f'len = {len(_BLOCKED_CITIES)}')
assert _BLOCKED_CITIES == frozenset(), f'expected empty, got {_BLOCKED_CITIES}'
print('OK: blocklist empty')
"
echo
echo "--- safety layers still active ---"
venv/bin/python -c "
from polymarket_strat.domain.weather.reliability import (
    get_city_reliability, get_bucket_blocklist,
)
rel = get_city_reliability()
bl = get_bucket_blocklist()
print(f'reliability loaded: {rel.loaded}, n_cities: {len(rel._data) if rel._data else 0}')
print(f'bucket blocklist loaded: {bl.loaded}, fine: {len(bl._fine)}, coarse: {len(bl._coarse)}')
"
