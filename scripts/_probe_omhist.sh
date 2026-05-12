#!/usr/bin/env bash
set -e
BASE="https://historical-forecast-api.open-meteo.com/v1/forecast"
ARC="https://archive-api.open-meteo.com/v1/archive"

echo "=== A: historical-forecast for NYC 2023-04-01..15 ==="
curl -sS "$BASE?latitude=40.78&longitude=-73.87&start_date=2023-04-01&end_date=2023-04-15&daily=temperature_2m_max&models=gfs_seamless&temperature_unit=fahrenheit" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['daily']['temperature_2m_max'])"

echo
echo "=== B: archive ERA5 same dates ==="
curl -sS "$ARC?latitude=40.78&longitude=-73.87&start_date=2023-04-01&end_date=2023-04-15&daily=temperature_2m_max&models=era5&temperature_unit=fahrenheit" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['daily']['temperature_2m_max'])"

echo
echo "=== C: historical-forecast 2018-01 (probe earliest year) ==="
curl -sS "$BASE?latitude=40.78&longitude=-73.87&start_date=2018-01-01&end_date=2018-01-03&daily=temperature_2m_max&models=gfs_seamless&temperature_unit=fahrenheit" | head -c 400
echo
echo
echo "=== D: historical-forecast 2021-04 ==="
curl -sS "$BASE?latitude=40.78&longitude=-73.87&start_date=2021-04-01&end_date=2021-04-03&daily=temperature_2m_max&models=gfs_seamless&temperature_unit=fahrenheit" | head -c 400
echo
echo
echo "=== E: historical-forecast with ECMWF ==="
curl -sS "$BASE?latitude=40.78&longitude=-73.87&start_date=2023-04-01&end_date=2023-04-03&daily=temperature_2m_max&models=ecmwf_ifs025&temperature_unit=fahrenheit" | head -c 400
echo
echo
echo "=== F: forecast lead time documented? probe with 'previous_day' suffix ==="
curl -sS "$BASE?latitude=40.78&longitude=-73.87&start_date=2024-04-01&end_date=2024-04-03&hourly=temperature_2m,temperature_2m_previous_day1&models=gfs_seamless&temperature_unit=fahrenheit" | head -c 600
