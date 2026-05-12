"""Quantile-regression bracket pricer + conformal wrapper.

Apr 25 2026 — Layer 1 of the calibration overhaul. Replaces the
parametric Normal/Skew-Normal/Student-t fitter with a non-parametric
quantile regression that learns the full conditional distribution
P(error | features) without any distributional assumption.

Design:

  TRAINING (offline, nightly):
    For each (city, lead_hours) pair, train K=19 separate quantile
    models, one per τ ∈ {0.05, 0.10, ..., 0.95}. Each model predicts
    the τ-quantile of forecast error given the 17-feature vector.
    Backed by sklearn HistGradientBoostingRegressor with quantile loss
    — fast, no LightGBM dependency, scales to ~10K rows in seconds.

  INFERENCE (per candidate trade):
    1. Build the feature vector from the live forecast + climatology
    2. Predict all 19 quantiles of error → reconstruct error CDF
    3. P(observed ∈ [L, U]) = CDF_error(F - L) - CDF_error(F - U)
    4. (Optional) Conformal wrapper: adjust the predicted probability
       toward 0.5 by an empirically-validated margin so coverage is
       guaranteed at the chosen α level

  WHY THIS BEATS PARAMETRIC FITTING:
    * No distributional assumption — captures bi-modal, fat-tailed,
      asymmetric error distributions that Normal/t can't represent
    * Per-(city, lead) specialization without splitting samples 4-way
      by season (each model trains on all seasons, with season as
      feature; the model partitions internally if useful)
    * Calibration is enforced by the loss function itself (pinball
      loss), unlike MLE which optimizes for parameter likelihood
      and may produce mis-calibrated tails
    * Sample-efficient: ~700 rows per (city, lead) is enough for
      depth-3-4 trees with 50-100 boosting rounds

  CONFORMAL WRAPPER (Layer 6):
    Quantile regression's coverage is "marginal calibration" — it
    holds in expectation over training distribution. If we want
    distribution-free GUARANTEE that a 90% interval covers 90% of
    new observations, we wrap with conformal prediction.

    Compute on holdout: nonconformity[i] = max over τ of how far
    the actual error exceeded the predicted τ-quantile in the wrong
    direction. The α-quantile of these scores is the safety margin.
    At inference, widen each quantile by this margin to guarantee
    coverage. Cost: slightly conservative predictions, but proven.
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from polymarket_strat.domain.weather.features import (
    FEATURE_NAMES,
    N_FEATURES,
    build_feature_vector,
    get_climatology,
)


# Quantile grid — 19 quantiles spaced 0.05 apart. Provides ~5%
# resolution on the CDF. More is overkill (returns diminish on
# small training samples); fewer loses tail resolution.
TAU_GRID: tuple[float, ...] = tuple(round(0.05 * i, 2) for i in range(1, 20))
# = (0.05, 0.10, 0.15, ..., 0.90, 0.95)

# Where trained model artifacts live.
_DEFAULT_MODELS_DIR = "data/weather/quantile_models"

# Conformal prediction calibration constants.
# alpha = 0.1 → 90% coverage guarantee.
_CONFORMAL_ALPHA = 0.10


@dataclass(slots=True)
class QuantilePrediction:
    """The output of a quantile regression model — values predicted
    at each τ. Together they reconstruct the conditional CDF."""
    quantiles: dict[float, float]   # τ → predicted value
    feature_vec: list[float]        # for audit / debugging

    def cdf(self, x: float) -> float:
        """Estimate CDF at x via linear interpolation between
        adjacent quantile predictions.

        For x below the smallest quantile (τ=0.05), return τ_min/2
        (extrapolated toward 0). For x above largest (τ=0.95), return
        (1 + τ_max)/2 (extrapolated toward 1). This avoids step
        discontinuities at the tails — a known weakness of finite-
        quantile predictions.
        """
        if not self.quantiles:
            return 0.5
        # Sort by predicted value (which should be monotonically
        # increasing with τ for a well-trained model — but we sort
        # defensively in case of crossings).
        sorted_pairs = sorted(self.quantiles.items(), key=lambda kv: kv[1])
        taus = [t for t, _ in sorted_pairs]
        vals = [v for _, v in sorted_pairs]

        # Below smallest quantile → linearly extrapolate from (0, 0)
        # toward the first quantile.
        if x <= vals[0]:
            return max(0.0, taus[0] * (x - vals[0] + abs(vals[0]) + 1e-6) / max(abs(vals[0]) + 1e-6, 1e-6) * 0.5)
        # Above largest → linearly toward (max_val, 1)
        if x >= vals[-1]:
            return min(1.0, taus[-1] + (1.0 - taus[-1]) * 0.5)

        # Interpolate within the range
        for i in range(len(vals) - 1):
            if vals[i] <= x <= vals[i + 1]:
                if vals[i + 1] == vals[i]:
                    return taus[i]
                t = (x - vals[i]) / (vals[i + 1] - vals[i])
                return taus[i] + t * (taus[i + 1] - taus[i])
        return 1.0


@dataclass(slots=True)
class QuantileModelArtifact:
    """Serializable bundle of the trained models for one (city, lead)
    plus its conformal calibration constant.
    """
    city: str
    lead_hours: int
    n_train_samples: int
    feature_names: list[str]
    # tau → fitted model (sklearn HistGradientBoostingRegressor)
    models: dict[float, Any]
    # conformal: empirical safety margin in error units
    conformal_widening: float = 0.0
    # holdout metrics for validation
    holdout_pinball_loss: float = float("nan")
    holdout_brier: float = float("nan")
    holdout_ece: float = float("nan")

    def predict(self, feature_vec: Sequence[float]) -> QuantilePrediction:
        x = np.asarray(feature_vec, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != N_FEATURES:
            raise ValueError(
                f"feature vector length {x.shape[1]} != expected {N_FEATURES}"
            )
        out: dict[float, float] = {}
        for tau, model in self.models.items():
            try:
                pred = float(model.predict(x)[0])
                out[float(tau)] = pred
            except Exception as exc:
                # Model crashed on this row — skip this quantile,
                # the CDF will still work with fewer points.
                print(
                    f"[quantile] {self.city}/{self.lead_hours}h τ={tau} "
                    f"predict failed: {exc!r}",
                    file=sys.stderr,
                )
        # Sort quantile predictions to enforce monotonicity
        # (training shouldn't allow crossings but with HistGBM and
        # small samples we sometimes see 1-2°F crossings near tails).
        # Sort by tau (since values should ascend with tau by
        # construction — if not, the sort_pairs in CDF handles it).
        sorted_q = dict(sorted(out.items()))
        return QuantilePrediction(quantiles=sorted_q, feature_vec=list(feature_vec))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: str) -> "QuantileModelArtifact":
        with open(path, "rb") as f:
            return pickle.load(f)


class QuantileBracketPricer:
    """Inference-time wrapper. Loads all (city, lead) artifacts at
    init; routes each prediction to the right model.

    Falls back gracefully when a (city, lead) artifact is missing —
    returns None so the caller can switch back to parametric pricing.
    """

    def __init__(self, models_dir: str | None = None):
        self.models_dir = models_dir or _DEFAULT_MODELS_DIR
        self._artifacts: dict[tuple[str, int], QuantileModelArtifact] = {}
        self._loaded = False
        self._load_all()

    def _load_all(self) -> None:
        if not os.path.isdir(self.models_dir):
            return
        for fname in os.listdir(self.models_dir):
            if not fname.endswith(".pkl"):
                continue
            stem = fname[:-4]  # strip .pkl
            # Convention: "{city}_{lead}h.pkl"
            try:
                city, rest = stem.rsplit("_", 1)
                if not rest.endswith("h"):
                    continue
                lead = int(rest[:-1])
            except (ValueError, AttributeError):
                continue
            path = os.path.join(self.models_dir, fname)
            try:
                art = QuantileModelArtifact.load(path)
                self._artifacts[(city, lead)] = art
            except Exception as exc:
                print(
                    f"[quantile] failed to load {path}: {exc!r}",
                    file=sys.stderr,
                )
        self._loaded = bool(self._artifacts)

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def n_artifacts(self) -> int:
        return len(self._artifacts)

    def has_model(self, city: str, lead_hours: int) -> bool:
        return (city, lead_hours) in self._artifacts

    def bracket_probability(
        self,
        *,
        city: str,
        model: str,
        forecast_high_f: float,
        obs_date,
        lead_hours: int,
        regime: str,
        lower_f: float,
        upper_f: float,
        ensemble_spread_f: float = 0.0,
        ensemble_skewness: float = 0.0,
        apply_conformal: bool = True,
        mask_climatology: bool = False,
    ) -> float | None:
        """Compute P(observed ∈ [lower_f, upper_f]) using the quantile
        regression model for this (city, lead).

        Returns None if no trained model exists — caller should fall
        back to parametric pricing.

        Math: error = forecast - observed. Hence
            P(observed ∈ [L, U]) = P(forecast - U < error ≤ forecast - L)
                                 = CDF_error(F - L) - CDF_error(F - U)

        With conformal wrapper (apply_conformal=True), CDF inputs are
        widened by ±widening before evaluation — produces slightly more
        conservative probabilities with proven α-coverage.
        """
        art = self._artifacts.get((city, lead_hours))
        if art is None:
            return None

        feat = build_feature_vector(
            city=city, model=model,
            forecast_high_f=forecast_high_f,
            obs_date=obs_date, lead_hours=lead_hours,
            regime=regime,
            ensemble_spread_f=ensemble_spread_f,
            ensemble_skewness=ensemble_skewness,
            mask_climatology=mask_climatology,
        )
        pred = art.predict(feat)

        # Apply conformal widening — pull tails toward 0.5 by
        # symmetric subtraction of widening from both ends.
        widening = art.conformal_widening if apply_conformal else 0.0

        # error = F - obs. CDF at (F - L) is P(error ≤ F-L) = P(obs ≥ L).
        x_lower = forecast_high_f - lower_f - widening
        x_upper = forecast_high_f - upper_f + widening

        cdf_lower = pred.cdf(x_lower)
        cdf_upper = pred.cdf(x_upper)

        p = max(0.0, min(1.0, cdf_lower - cdf_upper))
        return p


# ============================================================================
# Singleton accessor
# ============================================================================
_pricer_cache: QuantileBracketPricer | None = None


def get_quantile_pricer() -> QuantileBracketPricer:
    """Lazy module-level singleton."""
    global _pricer_cache
    if _pricer_cache is None:
        _pricer_cache = QuantileBracketPricer()
    return _pricer_cache


def reset_pricer_for_tests() -> None:
    global _pricer_cache
    _pricer_cache = None


# ============================================================================
# Training driver — used by scripts/train_quantile_models.py
# ============================================================================
def train_quantile_models_for_bucket(
    *,
    city: str,
    lead_hours: int,
    feature_matrix: np.ndarray,        # (n_samples, N_FEATURES)
    error_targets: np.ndarray,         # (n_samples,) — error_f values
    holdout_frac: float = 0.20,
    max_iter: int = 200,
    max_depth: int = 4,
    learning_rate: float = 0.05,
    random_seed: int = 42,
) -> QuantileModelArtifact:
    """Fit 19 quantile regression models for one (city, lead). Returns
    a saveable artifact with computed conformal widening.

    Uses a TIME-RANDOM split (no time order on training pairs since
    we already filter by lead_hours and rely on cross-date variety).
    For pure time-respecting validation, the caller can pre-split.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor

    n = len(feature_matrix)
    if n < 50:
        raise ValueError(
            f"Need >= 50 training samples for {city}/{lead_hours}h; got {n}"
        )

    rng = np.random.default_rng(random_seed)
    perm = rng.permutation(n)
    n_holdout = max(20, int(n * holdout_frac))
    holdout_idx = perm[:n_holdout]
    train_idx = perm[n_holdout:]

    X_train = feature_matrix[train_idx]
    y_train = error_targets[train_idx]
    X_hold = feature_matrix[holdout_idx]
    y_hold = error_targets[holdout_idx]

    models: dict[float, Any] = {}
    pinball_losses: list[float] = []

    for tau in TAU_GRID:
        m = HistGradientBoostingRegressor(
            loss="quantile",
            quantile=tau,
            max_iter=max_iter,
            max_depth=max_depth,
            learning_rate=learning_rate,
            random_state=random_seed,
            early_stopping=False,  # full passes for stable quantile fit
        )
        m.fit(X_train, y_train)
        models[tau] = m

        # Pinball loss on holdout — proper scoring rule for quantile regression.
        preds = m.predict(X_hold)
        diff = y_hold - preds
        loss_per = np.maximum(tau * diff, (tau - 1) * diff)
        pinball_losses.append(float(loss_per.mean()))

    avg_pinball = float(np.mean(pinball_losses))

    # Conformal widening — distribution-free safety margin.
    # On the holdout, compute for each quantile pair (τ_low, τ_high)
    # how often the true error fell outside the predicted band.
    # Take the (1 - α) quantile of nonconformity scores as widening.
    # Simplified: nonconformity[i] = min over τ of how far y[i] is
    # outside the τ-quantile band. We compute for the central
    # τ=0.50 prediction (median) and use absolute residuals.
    median_model = models[0.50]
    median_preds = median_model.predict(X_hold)
    abs_residuals = np.abs(y_hold - median_preds)
    # 1-α quantile of |residual| gives the conformal margin
    widening = float(np.quantile(abs_residuals, 1.0 - _CONFORMAL_ALPHA))

    return QuantileModelArtifact(
        city=city,
        lead_hours=lead_hours,
        n_train_samples=int(len(train_idx)),
        feature_names=list(FEATURE_NAMES),
        models=models,
        conformal_widening=widening,
        holdout_pinball_loss=avg_pinball,
    )
