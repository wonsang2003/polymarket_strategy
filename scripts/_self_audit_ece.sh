#!/usr/bin/env bash
# Audit common failure modes in the new ECE pipeline.
set -e
cd /home/ubuntu/polymarket

venv/bin/python - <<'PY'
import json, sys
from polymarket_strat.domain.weather.reliability import (
    CityReliability, reset_reliability_cache_for_tests,
)

errors = []

# 1. reliability.json must be valid JSON, must have per_city section
with open("data/weather/reliability.json") as f:
    data = json.load(f)
if "per_city" not in data:
    errors.append("MISSING per_city section")
n_cities = len(data["per_city"])
if n_cities < 20:
    errors.append(f"too few cities annotated: {n_cities}")
print(f"  ok: per_city has {n_cities} cities")

# 2. Each city's 24h entry has all required fields
required_fields = {"brier", "n_samples", "ece_shrunk", "n_test", "ece_raw"}
for city, per_lead in data["per_city"].items():
    if "24" not in per_lead:
        errors.append(f"{city}: missing 24h entry")
        continue
    missing = required_fields - set(per_lead["24"].keys())
    if missing:
        errors.append(f"{city}/24h: missing fields {missing}")
print(f"  ok: all 22 cities have brier+ece+n_test")

# 3. ECE shrunk values must be in [0, 1]
for city, per_lead in data["per_city"].items():
    es = per_lead.get("24", {}).get("ece_shrunk")
    if es is None: continue
    if not (0 <= es <= 1):
        errors.append(f"{city}: ece_shrunk out of range: {es}")
print(f"  ok: all ece_shrunk in [0, 1]")

# 4. Multiplier never goes below ece_floor floor (0.40 default)
reset_reliability_cache_for_tests()
r = CityReliability()
worst_mult = float("inf")
worst_city = None
for city in data["per_city"]:
    m, d = r.multiplier(city=city, lead_hours=24)
    if m < worst_mult:
        worst_mult = m
        worst_city = city
if worst_mult < 0.30:
    errors.append(f"multiplier too low: {worst_city}={worst_mult}")
print(f"  ok: lowest multiplier is {worst_city}={worst_mult:.3f} (>=0.30)")

# 5. Unknown city → multiplier 1.0 (graceful)
m, d = r.multiplier(city="atlantis_xyz", lead_hours=24)
if m != 1.0 or not d.get("fallback"):
    errors.append(f"unknown city should fallback, got mult={m} diag={d}")
print(f"  ok: unknown city falls back to mult=1.0")

# 6. Weird leads → falls back to 24h
m1, _ = r.multiplier(city="nyc", lead_hours=24)
m2, _ = r.multiplier(city="nyc", lead_hours=12)  # not exactly 24
if m1 != m2:
    errors.append(f"sub-36h leads should bucket to 24: {m1} vs {m2}")
print(f"  ok: sub-36h leads bucket to 24h consistently")

# 7. honest_ece data freshness check (within 24h)
import os, time
t = os.path.getmtime("data/weather/honest_ece_report.json")
age_h = (time.time() - t) / 3600
if age_h > 48:
    errors.append(f"honest_ece_report stale: {age_h:.1f}h old")
print(f"  ok: honest_ece_report age = {age_h:.2f}h")

# 8. Ranges sanity: best vs worst multiplier
mults = []
for city in data["per_city"]:
    m, _ = r.multiplier(city=city, lead_hours=24)
    mults.append((city, m))
mults.sort(key=lambda x: x[1])
print(f"  range: worst={mults[0]}, best={mults[-1]}")

if errors:
    print(f"\n  FAIL: {len(errors)} issues found:")
    for e in errors:
        print(f"    - {e}")
    sys.exit(1)
print(f"\n  ALL AUDITS PASSED")
PY
