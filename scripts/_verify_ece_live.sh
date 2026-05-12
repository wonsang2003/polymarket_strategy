#!/usr/bin/env bash
set -e
cd /home/ubuntu/polymarket
venv/bin/python - <<'PY'
from polymarket_strat.domain.weather.reliability import (
    get_city_reliability, reset_reliability_cache_for_tests,
)
reset_reliability_cache_for_tests()
r = get_city_reliability()
print(f"loaded: {r.loaded}, note: {r.diagnostic}")
print()
print(f"  {'city':16s}{'mult':>8}{'brier':>8}{'b_mult':>8}{'n':>6}{'s_mult':>8}{'ece_s':>8}{'e_mult':>8}")
for city in ("nyc","seoul","tokyo","london","miami","wellington","munich","dubai"):
    m, d = r.multiplier(city=city, lead_hours=24)
    es = d.get("ece_shrunk")
    em = d.get("ece_mult")
    print(f"  {city:16s}{m:>8.3f}{d.get('brier',0):>8.3f}"
          f"{d.get('brier_mult',0):>8.3f}{int(d.get('n_samples',0)):>6}"
          f"{d.get('samples_mult',0):>8.3f}"
          f"{(es if es is not None else 0):>8.3f}"
          f"{(em if em is not None else 1):>8.3f}")
PY
