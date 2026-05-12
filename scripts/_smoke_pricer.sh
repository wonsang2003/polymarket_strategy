#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/polymarket
venv/bin/python - <<'PY'
from datetime import date
from polymarket_strat.config import TradingConstraints
from polymarket_strat.domain.weather.quantile_pricing import get_quantile_pricer
from polymarket_strat.domain.weather.features import (
    reset_climatology_for_tests, reset_model_skill_for_tests,
)
reset_climatology_for_tests()
reset_model_skill_for_tests()

tc = TradingConstraints()
print(f"use_quantile_pricing default: {tc.use_quantile_pricing}")

p = get_quantile_pricer()
print(f"pricer.loaded: {p.loaded}, n_artifacts: {p.n_artifacts}")
print(f"24h coverage: nyc={p.has_model('nyc',24)} seoul={p.has_model('seoul',24)} tokyo={p.has_model('tokyo',24)}")

# Smoke: predict for a synthetic forecast
prob = p.bracket_probability(
    city="nyc", model="gfs", forecast_high_f=70.0,
    obs_date=date(2026, 4, 25), lead_hours=24, regime="stable_high",
    lower_f=68.0, upper_f=72.0, ensemble_spread_f=2.0,
    apply_conformal=False,
)
print(f"NYC ±2°F bracket centered on 70°F (Apr 25): P={prob:.4f}")

prob_wide = p.bracket_probability(
    city="nyc", model="gfs", forecast_high_f=70.0,
    obs_date=date(2026, 4, 25), lead_hours=24, regime="stable_high",
    lower_f=60.0, upper_f=80.0, ensemble_spread_f=2.0,
    apply_conformal=False,
)
print(f"NYC ±10°F wide bracket: P={prob_wide:.4f} (should be near 1.0)")

prob_far = p.bracket_probability(
    city="nyc", model="gfs", forecast_high_f=70.0,
    obs_date=date(2026, 4, 25), lead_hours=24, regime="stable_high",
    lower_f=85.0, upper_f=90.0, ensemble_spread_f=2.0,
    apply_conformal=False,
)
print(f"NYC bracket far above forecast (85-90°F): P={prob_far:.4f} (should be near 0)")

# 48h should NOT have a model (we only retrained 24h)
print(f"48h coverage: nyc={p.has_model('nyc',48)} (using stale model if True)")
PY
