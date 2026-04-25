"""Bracket probability computation and Kelly sizing.

Pure math — no I/O, no side effects.  Takes model forecasts + calibrated
error distributions and outputs edge-scored bracket probabilities.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable

from polymarket_strat.domain.weather.models import (
    BracketProbability,
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    TemperatureForecast,
    WeatherModel,
)


def forecast_content_hash(forecasts: Iterable[TemperatureForecast]) -> str:
    """Deterministic fingerprint of the set of model forecasts feeding p_model.

    Hashing the (model, rounded forecast_f) tuples — sorted so order is
    irrelevant — lets `run_rebalance` detect when Open-Meteo has served a
    fresh GFS/ECMWF run (hash changes) vs. returned the same cached bytes
    (hash unchanged). We don't have access to model `init_time` on every
    Open-Meteo endpoint so a content hash is the reliable signal.

    Rounded to 0.01°F — sub-hundredth differences are numerical noise
    from Open-Meteo's interpolation and would spuriously flip the hash
    even when the underlying forecast didn't actually refresh. The 0.01°F
    resolution is well within NWP precision.
    """
    # Sort so the hash is invariant to iteration order. Include lead_hours
    # because the same (city, model, forecast_f) at different leads should
    # not collide.
    items = sorted(
        (
            fc.model.value,
            int(round(fc.forecast_high_f * 100)),
            int(fc.lead_hours),
        )
        for fc in forecasts
    )
    if not items:
        return ""
    blob = "|".join(f"{m}:{t}:{l}" for (m, t, l) in items)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# Default ensemble weights (tunable per city/season in WeatherConfig)
DEFAULT_WEIGHTS: dict[WeatherModel, float] = {
    WeatherModel.GFS: 0.30,
    WeatherModel.ECMWF: 0.40,
    WeatherModel.HRRR: 0.20,
    WeatherModel.NAM: 0.10,
}

# Minimum realistic σ for NWP daily-max temperature forecasts (°F).
#
# Historical motivation (Apr 2026, pre-real-forecast era):
# Calibration used Open-Meteo *archive* (reanalysis blend), which has seen the
# observations and produces σ ≈ 0.9-1.5°F — 2-4x tighter than real operational
# forecast errors (~2.5-4°F per NWS verification statistics). Without a floor,
# bracket probabilities were systematically overconfident by 4-7x on narrow
# exact-degree brackets (70% spuriously becoming 52%).
#
# Current motivation (Apr 21 2026, after regime backfill at both leads):
# 91 days of real-forecast previous-runs data now exists per (city, model, 24h)
# bucket. The 24h walk-forward reliability diagram shows the OPPOSITE problem:
# model is systematically UNDER-confident in middle bins (mean_pred 0.514 vs
# hit_rate 0.664 at pm_2F) and OVER-confident at tails (mean_pred 0.039 vs
# hit_rate 0.014 at above_F+5). Classic spread-too-wide signature — σ is too
# high, not too low. Relaxing the floor to 2.0°F on buckets that have enough
# real-forecast samples narrows the distribution toward the truth without
# regressing thin-data buckets.
#
# The conditional floor (see _effective_sigma_floor) keeps the 2.5°F guardrail
# at leads or buckets that haven't accumulated enough real-forecast rows to
# earn the relaxation.
_SIGMA_FLOOR_F_REANALYSIS: float = 2.5   # default / safety net
_SIGMA_FLOOR_F_REAL: float = 2.0         # relaxed floor once bucket is "earned"

# Apr 24 2026 (Citadel fix #4) — third tier. After the Apr-21 regime
# backfill + Apr-24 isotonic integration, our best cities have 700+ real-
# forecast samples per (city, model, 24h) bucket. Walk-forward reliability
# confirms the MLE σ on these buckets is close to the true forward-looking
# σ; the 2.0°F floor is now inflating σ on calm stable days where genuine σ
# is ~1.3-1.6°F. Concrete consequence: a calm-day narrow bracket forecast
# that would legitimately read 94% confidence at true-σ 1.3°F gets clamped
# to 82% at floor σ 2.0°F, which smooshes the edge toward zero and kills
# the signal via the edge gate. The 1.5°F "ultra" tier unlocks those
# signals ONLY on buckets that have accumulated enough evidence (n >= 150)
# to trust the MLE σ fit all the way down to realistic values.
_SIGMA_FLOOR_F_ULTRA: float = 1.5        # "earned it many times over"

# How many real-forecast samples a (city, model, regime, lead) bucket needs
# before its MLE σ is trusted enough to drop the floor.
_N_REAL_FORECAST_THRESHOLD: int = 60
# Additional threshold for the 1.5°F ultra floor — we need this much more
# data because dropping σ that far has higher downside if the fit is still
# overtight from residual reanalysis pollution.
_N_REAL_FORECAST_THRESHOLD_ULTRA: int = 150

# Leads at which the relaxed floor is allowed. 24h has ~91 real-forecast days
# per (city, model) pair. 48h has fewer and is still partly reanalysis-padded,
# so leave it on the conservative floor until coverage catches up.
_RELAXED_FLOOR_LEADS: frozenset[int] = frozenset({24})

# Back-compat alias for anyone importing _SIGMA_FLOOR_F — points to the
# reanalysis-era default so external callers stay on the safe side.
_SIGMA_FLOOR_F: float = _SIGMA_FLOOR_F_REANALYSIS


def _effective_sigma_floor(error_dist: ErrorDistribution) -> float:
    """Choose the σ floor for a given calibrated distribution.

    Three-tier schedule (Apr 24 2026 — Citadel fix #4):
      * ULTRA (1.5°F): 24h lead AND n_samples >= 150 — MLE σ is trusted
        down to realistic calm-day levels (~1.3-1.6°F).
      * REAL  (2.0°F): 24h lead AND n_samples >= 60 — real-forecast data
        has accumulated enough to drop reanalysis-era 2.5°F guardrail.
      * REANALYSIS (2.5°F): everything else — 48h leads, thin buckets,
        reanalysis-padded fits.

    Tiering is STRICT and monotonic: lower floor requires MORE evidence,
    never less. Floor is always at least `MLE σ` (we can widen but not
    shrink the fit value).
    """
    if (
        error_dist.lead_hours in _RELAXED_FLOOR_LEADS
        and error_dist.n_samples >= _N_REAL_FORECAST_THRESHOLD_ULTRA
    ):
        return _SIGMA_FLOOR_F_ULTRA
    if (
        error_dist.lead_hours in _RELAXED_FLOOR_LEADS
        and error_dist.n_samples >= _N_REAL_FORECAST_THRESHOLD
    ):
        return _SIGMA_FLOOR_F_REAL
    return _SIGMA_FLOOR_F_REANALYSIS


def ensemble_aware_sigma(mle_sigma: float, ensemble_std_f: float, alpha: float = 1.0) -> float:
    """Widen the historical MLE σ using today's ensemble disagreement.

    Motivation (Apr 24 2026 — Tier 3a of Apr 24 dev plan):
      MLE σ is a HISTORICAL average of forecast-vs-observed error across
      all past days — backward-looking. On a calm day (ensemble members
      agree), the actual forecast uncertainty is probably *below* the
      historical average. On a frontal-passage day (members disagree by
      5°F+), the actual uncertainty is *above* it. Using fixed MLE σ on
      both day-types is the under-dispersed error from which the bulk of
      our overconfidence comes (see walk-forward reliability diagrams +
      live paper 3-4x overconfidence, §14 priority 7).

      Combining MLE with ensemble gives a real-time σ estimate:
        σ_effective = sqrt(σ_mle² + (α × σ_ensemble)²)

      where α ≈ 1.0 matches the rough 2:3 ratio of ensemble spread to
      true forecast error documented in NWP literature (model spread
      under-dispersed relative to actual error).

      Decomposition intuition:
        σ_mle       ≈ systematic error the model doesn't know about
        σ_ensemble  ≈ stochastic error the model's members capture
        σ_effective ≈ their quadrature sum (independent sources)

      When σ_ensemble is 0 (ensemble data unavailable or perfectly
      aligned), this reduces to σ_mle exactly → NO behavior change from
      pre-ensemble code. The feature is strictly additive.

    Args:
        mle_sigma: σ from MLE fit of historical forecast errors (°F).
        ensemble_std_f: standard deviation of ensemble member forecasts (°F).
            Pass 0.0 or a negative value to disable the widening.
        alpha: weight on the ensemble contribution (1.0 default; tune
            between 0.5 and 1.5 as more data accumulates).

    Returns:
        σ_effective >= σ_mle (never shrinks).
    """
    if ensemble_std_f <= 0 or not math.isfinite(ensemble_std_f):
        return mle_sigma
    if mle_sigma <= 0:
        return max(alpha * ensemble_std_f, 1e-6)
    return math.sqrt(mle_sigma * mle_sigma + (alpha * ensemble_std_f) ** 2)


def _error_cdf(x: float, dist: ErrorDistribution) -> float:
    """CDF of the fitted error distribution evaluated at x.

    error = forecast - observed.  To get P(observed <= T), we compute
    P(error >= forecast - T) = 1 - CDF(forecast - T).
    """
    from scipy import stats as sp

    if dist.family == DistributionFamily.NORMAL:
        return float(sp.norm.cdf(x, loc=dist.mu, scale=dist.sigma))
    if dist.family == DistributionFamily.STUDENT_T:
        return float(sp.t.cdf(x, df=dist.nu, loc=dist.mu, scale=dist.sigma))
    if dist.family == DistributionFamily.SKEW_NORMAL:
        return float(sp.skewnorm.cdf(x, a=dist.shape, loc=dist.mu, scale=dist.sigma))
    raise ValueError(f"Unknown distribution family: {dist.family}")


# -----------------------------------------------------------------------------
# Isotonic post-hoc calibration (Apr 24 2026 — §14 priority 7c, Tier 2a of
# Apr 24 dev plan).
#
# Motivation: live paper showed 3-4x overconfidence on low-p trades and
# walk-forward reliability diagrams confirm the same pattern at 24h. The
# structural fixes (σ floor relaxation, sample-weighted MLE, regime backfill)
# are in-flight but take weeks to validate. Isotonic regression is the
# post-hoc safety net that works today: learn a monotonic remapping
# `calibrated_p = isotonic(predicted_p)` from (predicted, outcome) pairs in
# the walk-forward backtest, and apply it as the LAST step in the ensemble
# probability pipeline. Monotonic-only, so it can never reorder predictions
# — it only shrinks/expands the probability scale.
#
# Data file: `data/weather/isotonic_calibration.json`, produced by
# `scripts/fit_isotonic.py`. The file persists piecewise-linear knots
# (X_thresholds_, y_thresholds_) from sklearn.isotonic; at inference we use
# `np.interp(raw_p, x, y)` which is bit-for-bit identical to what sklearn
# would return. No sklearn dependency at inference.
#
# Lookup order for a given (city, lead_hours):
#   1. Per-city calibration (if fit has n >= min_samples_per_city in JSON)
#   2. Global per-lead calibration
#   3. Identity (no change) — if JSON is missing entirely
# This order means small-data cities still benefit from the global curve,
# and a missing/corrupt JSON never hurts inference (fails closed to identity).
# -----------------------------------------------------------------------------

_DEFAULT_ISOTONIC_JSON = "data/weather/isotonic_calibration.json"
_DEFAULT_ISOTONIC_NO_JSON = "data/weather/isotonic_no_calibration.json"
_NARROW_BRACKET_THRESHOLD_F = 2.0  # mirrors strategy.py _NARROW_BRACKET_WIDTH_F
_isotonic_cache: "IsotonicCalibrator | None" = None


class IsotonicCalibrator:
    """Apply isotonic (monotonic) post-hoc probability calibration.

    Safe to call even when the JSON is missing or malformed — in that case
    `calibrate()` returns the input unchanged (identity fallback).

    Thread-safety: load once at module init, cache the knot arrays. No
    mutation after load. np.interp is thread-safe.
    """

    def __init__(self, json_path: str | None = None, no_json_path: str | None = None):
        self.json_path = json_path or _DEFAULT_ISOTONIC_JSON
        self.no_json_path = no_json_path or _DEFAULT_ISOTONIC_NO_JSON
        self._per_city: dict[str, dict[int, tuple[list[float], list[float]]]] = {}
        self._global: dict[int, tuple[list[float], list[float]]] = {}
        # Apr 26 2026 — fix #2 NO-side calibration. Two curves: a global
        # NO-side fit (always pooled across cities, since NO trade volume
        # is too low for per-city) and an optional narrow-bracket-specific
        # curve. Both are loaded from `isotonic_no_calibration.json`.
        self._no_global: tuple[list[float], list[float]] | None = None
        self._no_narrow: tuple[list[float], list[float]] | None = None
        self._no_loaded = False
        self._no_note: str = ""
        self._loaded = False
        self._note: str = ""
        self._try_load()
        self._try_load_no()

    def _try_load(self) -> None:
        """Attempt to load the calibration JSON. On any failure, leave the
        calibrator in identity-fallback mode with a diagnostic note."""
        import json
        import os

        if not os.path.exists(self.json_path):
            self._note = f"no calibration file at {self.json_path}"
            return
        try:
            with open(self.json_path, "r") as f:
                payload = json.load(f)
        except Exception as e:
            self._note = f"failed to load {self.json_path}: {type(e).__name__}"
            return

        for lead_str, fit in (payload.get("global", {}) or {}).items():
            try:
                lead_h = int(lead_str)
                xs = list(fit.get("x", []))
                ys = list(fit.get("y", []))
                if len(xs) >= 2 and len(ys) == len(xs):
                    self._global[lead_h] = (xs, ys)
            except Exception:
                continue

        for city, per_lead in (payload.get("per_city", {}) or {}).items():
            city_map: dict[int, tuple[list[float], list[float]]] = {}
            for lead_str, fit in (per_lead or {}).items():
                try:
                    lead_h = int(lead_str)
                    xs = list(fit.get("x", []))
                    ys = list(fit.get("y", []))
                    if len(xs) >= 2 and len(ys) == len(xs):
                        city_map[lead_h] = (xs, ys)
                except Exception:
                    continue
            if city_map:
                self._per_city[city] = city_map

        self._loaded = True
        self._note = (
            f"loaded {len(self._per_city)} cities, "
            f"{len(self._global)} global lead(s)"
        )

    def calibrate(self, raw_p: float, *, city: str = "", lead_hours: int = 24) -> float:
        """Apply isotonic remapping and return the corrected probability.

        Lookup order: per-city → global → identity.
        """
        # NaN / out-of-[0,1] input → return as-is (caller handles bounds)
        if not (raw_p == raw_p) or raw_p < 0.0 or raw_p > 1.0:
            return raw_p
        if not self._loaded:
            return raw_p

        # Bucket lead to the nearest calibrated bucket (24 or 48).
        # Leads beyond 48 fall back to the 48h curve; leads below 24 fall
        # back to the 24h curve. Matches the _CALIBRATION_LEAD_SCHEDULE
        # contract and avoids silent extrapolation.
        bucket_lead = 24 if lead_hours < 36 else 48

        # Try per-city first
        city_fit = self._per_city.get(city, {}).get(bucket_lead)
        if city_fit is not None:
            xs, ys = city_fit
            import numpy as np
            return float(np.interp(raw_p, xs, ys))

        # Fall through to global
        global_fit = self._global.get(bucket_lead)
        if global_fit is not None:
            xs, ys = global_fit
            import numpy as np
            return float(np.interp(raw_p, xs, ys))

        # Identity fallback
        return raw_p

    # ------------------------------------------------------------------
    # NO-side calibration (Apr 26 2026 — fix #2)
    # ------------------------------------------------------------------
    def _try_load_no(self) -> None:
        """Load the NO-side isotonic calibration from
        `isotonic_no_calibration.json` (produced by `scripts/fit_no_isotonic.py`).

        On any failure (missing file, malformed payload, identity-fallback
        record because n < 20) we leave the NO-side calibrator inactive
        and return raw probabilities unchanged. Same fail-closed semantics
        as YES-side — never make things worse.
        """
        import json
        import os

        if not os.path.exists(self.no_json_path):
            self._no_note = f"no NO-side calibration at {self.no_json_path}"
            return
        try:
            with open(self.no_json_path, "r") as f:
                payload = json.load(f)
        except Exception as e:
            self._no_note = (
                f"failed to load {self.no_json_path}: {type(e).__name__}"
            )
            return

        def _extract(record: dict | None) -> tuple[list[float], list[float]] | None:
            if not record or record.get("identity_fallback"):
                return None
            xs = list(record.get("x", []))
            ys = list(record.get("y", []))
            if len(xs) >= 2 and len(ys) == len(xs):
                return (xs, ys)
            return None

        self._no_global = _extract(payload.get("global"))
        self._no_narrow = _extract(payload.get("narrow"))

        if self._no_global is None and self._no_narrow is None:
            self._no_note = (
                f"loaded {self.no_json_path} but neither global nor narrow "
                "is active (likely n < 20 — identity fallback)"
            )
            return
        self._no_loaded = True
        parts = []
        if self._no_global is not None:
            parts.append("global")
        if self._no_narrow is not None:
            parts.append("narrow")
        self._no_note = f"loaded NO-side curves: {', '.join(parts)}"

    def calibrate_no(
        self,
        raw_p_no: float,
        *,
        bracket_width_f: float = 0.0,
        narrow_threshold_f: float = _NARROW_BRACKET_THRESHOLD_F,
    ) -> float:
        """Apply NO-side isotonic remapping for a NO-bet probability.

        Args:
            raw_p_no: model's P(NO) for the contract
            bracket_width_f: bracket span in °F. When < narrow_threshold_f
                AND a narrow-specific curve is loaded, that curve is used.
                Otherwise the global NO curve applies.
            narrow_threshold_f: width below which "narrow" applies (default
                2.0°F, matching strategy.py:_NARROW_BRACKET_WIDTH_F).

        Returns:
            calibrated P(NO). Identity-fallback when no curve is loaded
            or input is out-of-bounds — never raises.

        Note: this is INDEPENDENT of `calibrate()` (the YES-side curve).
        Callers operating on NO-side `side_model_prob` should call this
        directly; do NOT compose with `calibrate(1 - p_yes)` because the
        YES-side curve was fit on synthetic walk-forward outcomes (1 if
        observed-in-bracket) which is geometrically the YES side, not NO.
        """
        if not (raw_p_no == raw_p_no) or raw_p_no < 0.0 or raw_p_no > 1.0:
            return raw_p_no
        if not self._no_loaded:
            return raw_p_no

        # Prefer narrow curve when bracket is narrow AND curve exists.
        if (
            bracket_width_f > 0.0
            and bracket_width_f < narrow_threshold_f
            and self._no_narrow is not None
        ):
            xs, ys = self._no_narrow
            import numpy as np
            return float(np.interp(raw_p_no, xs, ys))

        # Fall back to global NO curve.
        if self._no_global is not None:
            xs, ys = self._no_global
            import numpy as np
            return float(np.interp(raw_p_no, xs, ys))

        return raw_p_no

    @property
    def no_loaded(self) -> bool:
        return self._no_loaded

    @property
    def no_diagnostic(self) -> str:
        return self._no_note

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def diagnostic(self) -> str:
        return self._note


def _get_isotonic_calibrator() -> IsotonicCalibrator:
    """Lazy module-level singleton. First call loads the JSON (if present)
    and caches it. Subsequent calls are cheap."""
    global _isotonic_cache
    if _isotonic_cache is None:
        _isotonic_cache = IsotonicCalibrator()
    return _isotonic_cache


def _reset_isotonic_cache_for_tests() -> None:
    """Test-only helper — forces the next _get_isotonic_calibrator() call to
    re-read the JSON. Use in tests that write a temp calibration file."""
    global _isotonic_cache
    _isotonic_cache = None


class BracketProbabilityCalculator:
    """Compute model bracket probabilities and edge vs. market."""

    def __init__(self, model_weights: dict[WeatherModel, float] | None = None):
        self.weights = model_weights or dict(DEFAULT_WEIGHTS)

    def bracket_probability(
        self,
        *,
        forecast_f: float,
        error_dist: ErrorDistribution,
        lower_f: float,
        upper_f: float,
        ensemble_std_f: float = 0.0,
    ) -> float:
        """P(lower <= observed_high < upper) for a single model.

        Derivation:
            observed = forecast - error   (since error = forecast - observed)
            P(lower <= observed < upper)
              = P(lower <= forecast - error < upper)
              = P(forecast - upper < error <= forecast - lower)
              = CDF_error(forecast - lower) - CDF_error(forecast - upper)

        σ pipeline (Apr 24 2026 — Tier 3a):
            σ_mle       ← error_dist.sigma                  (MLE on history)
            σ_eff       ← ensemble_aware_sigma(σ_mle, ...)  (widens when
                              ensemble members disagree)
            σ_final     ← max(σ_eff, _effective_sigma_floor)
        σ_final is used for the CDF evaluation. Default ensemble_std_f=0
        disables the widening → behavior identical to pre-Tier-3a code.

        See `_effective_sigma_floor` docstring for the 2.0 vs 2.5°F floor
        conditions, and `ensemble_aware_sigma` for the widening math.
        """
        widened_sigma = ensemble_aware_sigma(error_dist.sigma, ensemble_std_f)
        floored = ErrorDistribution(
            city=error_dist.city,
            model=error_dist.model,
            regime=error_dist.regime,
            lead_hours=error_dist.lead_hours,
            family=error_dist.family,
            mu=error_dist.mu,
            sigma=max(widened_sigma, _effective_sigma_floor(error_dist)),
            shape=error_dist.shape,
            nu=error_dist.nu,
            n_samples=error_dist.n_samples,
        )
        p = _error_cdf(forecast_f - lower_f, floored) - _error_cdf(forecast_f - upper_f, floored)
        return max(min(p, 1.0), 0.0)

    def ensemble_bracket_probability(
        self,
        *,
        forecasts: list[TemperatureForecast],
        error_dists: list[ErrorDistribution],
        lower_f: float,
        upper_f: float,
        apply_isotonic: bool = True,
    ) -> tuple[float, float]:
        """Skill-weighted ensemble bracket probability.

        Apr 24 2026 — `apply_isotonic` (default True) remaps the ensemble
        mean through the per-(city, lead) isotonic calibration curve fit
        from historical walk-forward pairs. This corrects the 3-4x
        overconfidence measured on live low-p trades (§14 priority 7c).
        Pass `apply_isotonic=False` to get the raw ensemble probability
        — useful for walk-forward backtesting (we don't want to calibrate
        against a curve that was fit from the same data).

        Std is computed on the RAW per-model probabilities (not calibrated)
        because it measures model disagreement, which is a physical property
        independent of calibration. Applying isotonic to std would double-
        discount: the mean already shrinks via isotonic, the gate then also
        shrinks Kelly via uncertainty.

        Returns:
            (mean_prob, std_prob). std_prob measures how much the models
            disagree — high std = high uncertainty = reduce Kelly size.
        """
        if len(forecasts) != len(error_dists):
            raise ValueError("forecasts and error_dists must be same length")
        if not forecasts:
            return (0.0, 1.0)

        weighted_probs: list[float] = []
        raw_probs: list[float] = []
        total_weight = 0.0
        # We take the first forecast's (city, lead_hours) as the anchor for
        # calibration lookup. Ensemble members for the same bracket target
        # the same city and lead by construction — if they diverge that's
        # a caller bug, not something we should paper over here.
        anchor_city = forecasts[0].city if hasattr(forecasts[0], "city") else ""
        anchor_lead = forecasts[0].lead_hours if hasattr(forecasts[0], "lead_hours") else 24

        for fc, dist in zip(forecasts, error_dists):
            w = self.weights.get(fc.model, 0.1)
            # Apr 24 2026 — Tier 3a: widen σ using the ensemble spread
            # (if populated on the TemperatureForecast). When the ensemble
            # fetch fails or isn't done, `ensemble_spread_f` defaults to 0
            # and `bracket_probability` short-circuits to pure MLE σ.
            # Spread (max-min) is converted to an approximate std via
            # range/4 — the standard rule for Gaussian-ish 31/51-member
            # ensembles. Not perfect but cheap and directionally right.
            ens_std = max(0.0, float(getattr(fc, "ensemble_spread_f", 0.0)) / 4.0)
            p = self.bracket_probability(
                forecast_f=fc.forecast_high_f,
                error_dist=dist,
                lower_f=lower_f,
                upper_f=upper_f,
                ensemble_std_f=ens_std,
            )
            weighted_probs.append(w * p)
            raw_probs.append(p)
            total_weight += w

        mean_prob = sum(weighted_probs) / total_weight if total_weight > 0 else 0.0

        # Apply isotonic post-hoc calibration. This is the single biggest
        # leverage fix in the Apr 24 dev plan — see module docstring.
        if apply_isotonic:
            calibrator = _get_isotonic_calibrator()
            # Fall back gracefully if calibrator has no data for this
            # (city, lead) — returns mean_prob unchanged.
            mean_prob = calibrator.calibrate(
                mean_prob, city=anchor_city, lead_hours=int(anchor_lead)
            )

        # std across models (unweighted) as uncertainty proxy. Computed
        # on the RAW per-model probs (see docstring above).
        if len(raw_probs) >= 2:
            m = sum(raw_probs) / len(raw_probs)
            var = sum((p - m) ** 2 for p in raw_probs) / (len(raw_probs) - 1)
            std_prob = math.sqrt(var)
        else:
            std_prob = 0.1  # default uncertainty when only 1 model available

        return (max(min(mean_prob, 1.0), 0.0), std_prob)

    @staticmethod
    def edge(
        *,
        model_prob: float,
        market_prob: float,
        fee_rate: float = 0.02,
        min_edge: float = 0.05,
        low_p_min_edge: float = 0.10,
        low_p_threshold: float = 0.50,
    ) -> tuple[float, float, bool]:
        """Compute raw edge, fee-adjusted edge, and tradeability.

        Fee model: Polymarket charges fee_rate on WINNINGS only.
            EV = p * (1 - P) * (1 - fee) - (1 - p) * P
        Simplified: edge_after_fees ≈ raw_edge - fee * p * (1 - P)

        Tradeability gates (Apr 24 2026 refinement of Apr 19 refactor —
        see CLAUDE.md §4.3):
            1. Market price in [0.15, 0.75] — avoid penny artifacts and
               adverse (crushed) payoffs.
            2. P-scaled min-edge after fees:
                   required = min_edge                    if model_prob ≥ 0.50
                   required = max(min_edge, low_p_min_edge)  otherwise
               Defaults: 5¢ at p≥0.50, 10¢ at p<0.50.

        Motivation for p-scaling (Apr 24 audit of 32 settled paper trades):
        the Apr 19 flat-5¢ refactor was calibrated assuming the model's
        probability estimates are unbiased. Live paper data says they
        aren't — the model is systematically ~2x overconfident on low-p
        brackets (avg_p 0.30 paired with ~15% realized hit rate vs. the
        model's claimed 30%). Under 2x overconfidence, a flat 5¢ gate at
        p=0.30 has negative EV. Tightening the threshold to 10¢ at p<0.50
        restores positive EV in that bucket:
            At p_true = 0.5 * p_model = 0.15 (2x overconfident),
            market = 0.20, edge = 0.10:
                EV/$1 = 0.15 * (4 * 0.98) - 0.85 * 1.0 ≈ -0.262 (still LOSS)
            At edge = 0.15 (tighter market_prob = 0.15):
                EV/$1 = 0.15 * (5.67 * 0.98) - 0.85 * 1.0 ≈ -0.017 (near 0)
        (The low-p bucket still needs calibration fixes — §14 priority 7 —
        but 10¢ buys us time to collect more real-forecast data without
        bleeding capital on every low-p signal.)

        High-p brackets (Tokyo-style spread markets where no bracket
        crosses 50%) retain the 5¢ flat floor — those were the explicit
        motivation for dropping `P_model ≥ 0.55` in the Apr 19 refactor,
        and the live data doesn't yet have enough of them to contradict
        that decision.

        Gates explicitly REMOVED in the Apr 19 refactor (unchanged here):
            - P_model ≥ 0.55 (killed multi-bracket spread trades where no
              single bracket crosses 55% — see Tokyo Apr 20 example).
            - Sharpe-per-trade ≥ 0.15 (redundant given market band + flat
              edge: can never reject a signal that passed both).

        Side-bias: currently one-sided (buy-YES only). Negative-edge
        markets (market overpricing YES → buy-NO opportunity) are NOT
        flagged; shorting is a separate implementation pass.

        Returns:
            (raw_edge, edge_after_fees, is_tradeable)
        """
        raw = model_prob - market_prob
        fee_drag = fee_rate * model_prob * (1.0 - market_prob)
        adjusted = raw - fee_drag

        # Gate 1: Market price band
        if market_prob < 0.15 or market_prob > 0.75:
            return (raw, adjusted, False)

        # Gate 2: P-scaled min-edge after fees.
        required_edge = (
            max(min_edge, low_p_min_edge)
            if model_prob < low_p_threshold
            else min_edge
        )
        if adjusted < required_edge:
            return (raw, adjusted, False)

        return (raw, adjusted, True)

    @staticmethod
    def kelly_fraction(
        *,
        model_prob: float,
        market_prob: float,
        prob_std: float,
        fee_rate: float = 0.02,
        quarter_kelly: bool = True,
    ) -> tuple[float, float]:
        """Uncertainty-adjusted fractional Kelly.

        1. Compute raw Kelly: f* = (p*b - q) / b
           where b = (1-P)*(1-fee)/P (net odds), q = 1-p
        2. Apply quarter-Kelly (multiply by 0.25)
        3. Apply uncertainty shrinkage: 1/(1+CV^2)
           where CV = prob_std / model_prob

        Returns:
            (kelly_fraction, shrinkage_factor)
        """
        p = model_prob
        P = market_prob
        if P <= 0 or P >= 1 or p <= 0:
            return (0.0, 0.0)

        # Net odds for a YES bracket at price P (paying P, winning 1-P minus fee)
        b = (1.0 - P) * (1.0 - fee_rate) / P
        q = 1.0 - p
        raw_kelly = (p * b - q) / b if b > 0 else 0.0
        raw_kelly = max(raw_kelly, 0.0)

        if quarter_kelly:
            raw_kelly *= 0.25

        # Uncertainty shrinkage
        cv = prob_std / max(p, 1e-6)
        shrinkage = 1.0 / (1.0 + cv * cv)

        return (raw_kelly * shrinkage, shrinkage)

    def price_all_brackets(
        self,
        *,
        forecasts: list[TemperatureForecast],
        error_dists: list[ErrorDistribution],
        brackets: list[tuple[float, float]],
        market_probs: list[float],
        fee_rate: float = 0.02,
        regime: SynopticRegime = SynopticRegime.STABLE_HIGH,
    ) -> list[BracketProbability]:
        """Price a full set of brackets for one city on one date.

        Args:
            forecasts: one TemperatureForecast per model available.
            error_dists: matching ErrorDistribution per model.
            brackets: list of (lower_f, upper_f) bracket bounds.
            market_probs: corresponding Polymarket YES prices.
            fee_rate: Polymarket fee on winnings.
            regime: current synoptic regime.

        Returns:
            List of BracketProbability with edges and Kelly fractions.
        """
        if len(brackets) != len(market_probs):
            raise ValueError("brackets and market_probs must be same length")

        results: list[BracketProbability] = []
        city = forecasts[0].city if forecasts else ""
        target_date = forecasts[0].valid_time.date() if forecasts else None

        # Compute model probs for all brackets, then normalize to sum=1
        raw_probs: list[tuple[float, float]] = []
        for lower, upper in brackets:
            mean_p, std_p = self.ensemble_bracket_probability(
                forecasts=forecasts,
                error_dists=error_dists,
                lower_f=lower,
                upper_f=upper,
            )
            raw_probs.append((mean_p, std_p))

        # Only normalize when brackets cover most of the probability mass (>85%).
        # Polymarket typically lists a few brackets per city/date, not the full
        # exhaustive set. Normalizing an incomplete set inflates single-bracket
        # groups to 1.0, which is wrong. Use raw CDF probabilities instead.
        total = sum(p for p, _ in raw_probs)
        if total > 0.85:
            normalized = [(p / total, s) for p, s in raw_probs]
        else:
            normalized = raw_probs

        for i, ((lower, upper), mkt_p) in enumerate(zip(brackets, market_probs)):
            model_p, prob_std = normalized[i]
            raw_edge, edge_adj, tradeable = self.edge(
                model_prob=model_p, market_prob=mkt_p, fee_rate=fee_rate,
            )
            kelly, shrinkage = self.kelly_fraction(
                model_prob=model_p, market_prob=mkt_p, prob_std=prob_std,
                fee_rate=fee_rate,
            )
            results.append(BracketProbability(
                city=city,
                target_date=target_date,
                lower_f=lower,
                upper_f=upper,
                model_prob=model_p,
                prob_std=prob_std,
                market_prob=mkt_p,
                raw_edge=raw_edge,
                edge_after_fees=edge_adj,
                kelly_fraction=kelly,
                uncertainty_shrinkage=shrinkage,
                regime=regime,
                contributing_models=[fc.model for fc in forecasts],
            ))

        return results
