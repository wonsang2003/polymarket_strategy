"""Feature engineering for the quantile-regression bracket pricer.

Apr 25 2026 — replaces the 5-feature parametric model with a 17-feature
ML-ready vector. Features are designed to be:
  1. Computable cheaply at inference time (no extra API calls per cycle)
  2. Available historically for training (joinable on forecast_errors)
  3. Domain-meaningful (each captures a real source of forecast error)

Feature groups:

  TEMPORAL (3): month, day_of_year_sin/cos
    Captures annual cycle of forecast skill — e.g., GFS is more accurate
    in stable summer than in transitional spring.

  STRUCTURAL (5): lead_hours, ensemble_spread_f, regime_one_hot (3)
    What the system "knows" about the forecast situation.
    Regime encoded as 3 indicators (stable_high, frontal_passage, transition)
    rather than ordinal — they're not on a single axis.

  CLIMATOLOGY (4): climo_mean, climo_std, forecast_anomaly, anomaly_zscore
    The 30-year normal high for this date+city, std of historical
    highs, deviation of today's forecast from climo, normalized
    z-score of that deviation. Climo is a strong baseline; the
    anomaly captures "how unusual is this forecast?"

  ENSEMBLE-DERIVED (2): ensemble_std_f, ensemble_skewness
    From the spread (range, max-min) we approximate σ via /4.
    Skewness captures asymmetric ensemble member distributions.

  MODEL_IDENTITY (3): is_gfs, is_ecmwf, model_skill_proxy
    Which model produced this forecast. Skill proxy is a per-(city,
    model) historical brier score used as a soft model-quality
    indicator.

Total: 17 features. All numeric (one-hots are floats 0/1).

Why not more features?
  - Live obs (METAR) requires a separate daemon (Layer 3, deferred)
  - Synoptic features (500mb height, CAPE) require GRIB parsing (deferred)
  - Multi-day persistence requires hourly observation history we don't yet store
  Adding these is the next unlock after Layer 1 ships.

Why no city one-hot?
  - Each (city, lead) gets its own model — city-specific patterns are
    already captured by the per-model partition. One-hots would be
    redundant and consume effective sample size.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence


# Feature vector layout — fixed order so trained models and inference agree.
# DON'T reorder without retraining. NaN-safe defaults so missing data
# doesn't silently corrupt predictions.
FEATURE_NAMES: list[str] = [
    "month",                  # 1-12
    "day_of_year_sin",        # sin(2π · DOY / 366)
    "day_of_year_cos",        # cos(2π · DOY / 366)
    "lead_hours",             # 6, 12, 24, 48, 72
    "ensemble_spread_f",      # max - min across ensemble members (°F)
    "regime_stable_high",     # 0/1
    "regime_frontal_passage", # 0/1
    "regime_transition",      # 0/1
    "climo_mean_f",           # 30-year normal high for date+city
    "climo_std_f",            # std of historical highs
    "forecast_anomaly_f",     # forecast_high - climo_mean
    "forecast_anomaly_z",     # anomaly / climo_std (normalized)
    "ensemble_std_f",         # spread/4 (Gaussian rule of thumb)
    "ensemble_skewness",      # 0 if symmetric (extension hook)
    "is_gfs",                 # 0/1
    "is_ecmwf",               # 0/1
    "model_skill_proxy",      # per-(city, model) historical brier
]
N_FEATURES: int = len(FEATURE_NAMES)


_DEFAULT_CLIMO_PATH = "data/weather/climatology.json"
_DEFAULT_MODEL_SKILL_PATH = "data/weather/model_skill.json"

_climo_cache: "ClimatologyLookup | None" = None


@dataclass(slots=True)
class ClimatologyLookup:
    """Per-(city, day_of_year) historical mean+std of observed highs.

    Built by `scripts/build_climatology.py` from the observations table.
    Stored as JSON: { "city": { "doy": {"mean": ..., "std": ...}, ... } }.

    Lookup never fails — falls back to per-city year-round mean+std if
    a specific day_of_year is missing (e.g., we have only 1 year of
    data and that day didn't have an observation).
    """
    by_city_doy: dict[str, dict[int, dict[str, float]]]
    by_city_yearmean: dict[str, dict[str, float]]

    @classmethod
    def from_json(cls, path: str) -> "ClimatologyLookup":
        if not os.path.exists(path):
            return cls(by_city_doy={}, by_city_yearmean={})
        with open(path, "r") as f:
            payload = json.load(f)
        by_city_doy = {}
        by_city_yearmean = {}
        for city, by_doy in (payload.get("by_city_doy", {}) or {}).items():
            by_city_doy[city] = {int(k): v for k, v in by_doy.items()}
        for city, stats in (payload.get("by_city_yearmean", {}) or {}).items():
            by_city_yearmean[city] = stats
        return cls(by_city_doy=by_city_doy, by_city_yearmean=by_city_yearmean)

    def lookup(self, city: str, doy: int) -> tuple[float, float]:
        """Return (mean, std) for (city, day_of_year). Falls through to
        year-round mean if specific DOY missing, then to global default."""
        per_city = self.by_city_doy.get(city, {})
        # Try exact DOY first
        if doy in per_city:
            entry = per_city[doy]
            return float(entry["mean"]), float(entry["std"])
        # Try ±3 day window (smoothing)
        for delta in range(1, 4):
            for adj in (doy - delta, doy + delta):
                adj = ((adj - 1) % 366) + 1  # wrap
                if adj in per_city:
                    entry = per_city[adj]
                    return float(entry["mean"]), float(entry["std"])
        # Year-round fallback
        if city in self.by_city_yearmean:
            ym = self.by_city_yearmean[city]
            return float(ym.get("mean", 60.0)), float(ym.get("std", 15.0))
        # Last resort
        return 60.0, 15.0


def get_climatology(path: str | None = None) -> ClimatologyLookup:
    """Lazy module-level singleton. First call loads JSON; subsequent
    calls return the cached lookup."""
    global _climo_cache
    if _climo_cache is None:
        _climo_cache = ClimatologyLookup.from_json(path or _DEFAULT_CLIMO_PATH)
    return _climo_cache


def reset_climatology_for_tests() -> None:
    global _climo_cache
    _climo_cache = None


# Per-(city, model) skill proxy — Brier scores from walk-forward.
# Loaded once, cached. Used as a feature so the model can learn "trust
# GFS more for NYC than for Tokyo" implicitly.
_skill_cache: dict[tuple[str, str], float] | None = None


def _load_model_skill(path: str | None = None) -> dict[tuple[str, str], float]:
    global _skill_cache
    if _skill_cache is not None:
        return _skill_cache
    p = path or _DEFAULT_MODEL_SKILL_PATH
    if not os.path.exists(p):
        _skill_cache = {}
        return _skill_cache
    try:
        with open(p, "r") as f:
            payload = json.load(f)
        # Format: {"city": {"gfs": brier, "ecmwf": brier}, ...}
        out = {}
        for city, per_model in (payload or {}).items():
            for model, brier in per_model.items():
                out[(city, model.lower())] = float(brier)
        _skill_cache = out
        return out
    except Exception:
        _skill_cache = {}
        return _skill_cache


def reset_model_skill_for_tests() -> None:
    global _skill_cache
    _skill_cache = None


# Doy index from a date — 1 to 366.
def day_of_year(d: date) -> int:
    return d.timetuple().tm_yday


def build_feature_vector(
    *,
    city: str,
    model: str,            # "gfs" | "ecmwf" | "hrrr" | "nam"
    forecast_high_f: float,
    obs_date: date,
    lead_hours: int,
    regime: str,           # one of "stable_high", "frontal_passage", "transition", etc.
    ensemble_spread_f: float = 0.0,
    ensemble_skewness: float = 0.0,
    climo: ClimatologyLookup | None = None,
    model_skill: dict[tuple[str, str], float] | None = None,
    mask_climatology: bool = False,  # set True to zero out the 4 climo features
                                      # (used by honest_ece.py to isolate climo
                                      # leak contribution to ECE)
) -> list[float]:
    """Build the 17-feature vector for one observation.

    Same function called at TRAINING time (one row per historical
    forecast_error) and at INFERENCE time (one row per candidate trade).
    Feature vector layout MUST match FEATURE_NAMES order exactly.

    NaN-safe: missing data → reasonable defaults, never raise. The
    quantile regression can handle modest amounts of feature noise but
    crashes if NaN bleeds in.
    """
    if climo is None:
        climo = get_climatology()
    if model_skill is None:
        model_skill = _load_model_skill()

    doy = day_of_year(obs_date)
    month = obs_date.month
    doy_sin = math.sin(2 * math.pi * doy / 366.0)
    doy_cos = math.cos(2 * math.pi * doy / 366.0)

    if mask_climatology:
        # Replace climatology features with neutral constants. The mean is
        # zeroed and std=1 so anomaly/anomaly_z are also zero — model can
        # still see the constants but they carry no signal.
        climo_mean, climo_std = 0.0, 1.0
        anomaly = 0.0
        anomaly_z = 0.0
    else:
        climo_mean, climo_std = climo.lookup(city, doy)
        climo_std = max(climo_std, 1.0)  # avoid div-by-zero in z-score
        anomaly = float(forecast_high_f) - climo_mean
        anomaly_z = anomaly / climo_std

    ensemble_std = max(0.0, float(ensemble_spread_f or 0.0)) / 4.0

    regime_lower = (regime or "").lower()
    is_stable = 1.0 if regime_lower == "stable_high" else 0.0
    is_frontal = 1.0 if regime_lower == "frontal_passage" else 0.0
    is_transit = 1.0 if regime_lower == "transition" else 0.0

    model_lower = (model or "").lower()
    is_gfs = 1.0 if model_lower == "gfs" else 0.0
    is_ecmwf = 1.0 if model_lower == "ecmwf" else 0.0

    # Skill proxy — historical Brier for this (city, model). Lower is
    # better; default to 0.15 (mediocre) if unknown.
    skill = float(model_skill.get((city, model_lower), 0.15))

    return [
        float(month),
        doy_sin,
        doy_cos,
        float(lead_hours),
        float(ensemble_spread_f or 0.0),
        is_stable, is_frontal, is_transit,
        float(climo_mean),
        float(climo_std),
        float(anomaly),
        float(anomaly_z),
        ensemble_std,
        float(ensemble_skewness or 0.0),
        is_gfs, is_ecmwf,
        skill,
    ]


def build_feature_matrix(
    rows: Sequence[dict[str, Any]],
    climo: ClimatologyLookup | None = None,
    model_skill: dict[tuple[str, str], float] | None = None,
) -> list[list[float]]:
    """Batch builder for training data. `rows` is a sequence of dicts
    each containing the required keys for build_feature_vector."""
    return [
        build_feature_vector(
            city=r["city"],
            model=r["model"],
            forecast_high_f=r["forecast_high_f"],
            obs_date=r["obs_date"] if isinstance(r["obs_date"], date) else date.fromisoformat(str(r["obs_date"])[:10]),
            lead_hours=r["lead_hours"],
            regime=r["regime"],
            ensemble_spread_f=r.get("ensemble_spread_f", 0.0),
            ensemble_skewness=r.get("ensemble_skewness", 0.0),
            climo=climo,
            model_skill=model_skill,
        )
        for r in rows
    ]
