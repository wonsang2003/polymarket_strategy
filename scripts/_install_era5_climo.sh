#!/usr/bin/env bash
# Run on EC2: install /tmp/climatology_era5_merged.json as production climo,
# verify, retrain quantile models. Idempotent.
set -euo pipefail
cd /home/ubuntu/polymarket

[ -f data/weather/climatology.json ] && \
    cp data/weather/climatology.json \
       data/weather/climatology.pre_era5_$(date +%Y%m%d_%H%M%S).json
cp /tmp/climatology_era5_merged.json data/weather/climatology.json

echo "--- climo verify ---"
venv/bin/python - <<'PY'
import json, statistics as s
d = json.load(open("data/weather/climatology.json"))
n = sum(len(v) for v in d["by_city_doy"].values())
samples = [c["n"] for v in d["by_city_doy"].values() for c in v.values()]
print(f"cities={len(d['by_city_doy'])} cells={n} median_samples={s.median(samples)}")
PY

echo "--- starting quantile retrain (background) ---"
nohup venv/bin/python scripts/train_quantile_models.py --leads 24 --skip-climo \
    > /tmp/quantile_retrain_era5.log 2>&1 &
echo "PID=$!"
