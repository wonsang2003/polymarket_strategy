"""Bracket probability computation and Kelly sizing.

Pure math — no I/O, no side effects.  Takes model forecasts + calibrated
error distributions and outputs edge-scored bracket probabilities.
"""
from __future__ import annotations

import math
from typing import Any

from polymarket_strat.domain.weather.models import (
    BracketProbability,
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    TemperatureForecast,
    WeatherModel,
)


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

# How many real-forecast samples a (city, model, regime, lead) bucket needs
# before its MLE σ is trusted enough to drop the floor.
_N_REAL_FORECAST_THRESHOLD: int = 60

# Leads at which the relaxed floor is allowed. 24h has ~91 real-forecast days
# per (city, model) pair. 48h has fewer and is still partly reanalysis-padded,
# so leave it on the conservative floor until coverage catches up.
_RELAXED_FLOOR_LEADS: frozenset[int] = frozenset({24})

# Back-compat alias for anyone importing _SIGMA_FLOOR_F — points to the
# reanalysis-era default so external callers stay on the safe side.
_SIGMA_FLOOR_F: float = _SIGMA_FLOOR_F_REANALYSIS


def _effective_sigma_floor(error_dist: ErrorDistribution) -> float:
    """Choose the σ floor for a given calibrated distribution.

    Returns the relaxed 2.0°F floor only when BOTH conditions hold:
      * bucket is at a lead where relaxation is allowed (24h today)
      * MLE fit was backed by >= _N_REAL_FORECAST_THRESHOLD samples

    Otherwise returns the 2.5°F reanalysis-era guardrail.
    """
    if (
        error_dist.lead_hours in _RELAXED_FLOOR_LEADS
        and error_dist.n_samples >= _N_REAL_FORECAST_THRESHOLD
    ):
        return _SIGMA_FLOOR_F_REAL
    return _SIGMA_FLOOR_F_REANALYSIS


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
    ) -> float:
        """P(lower <= observed_high < upper) for a single model.

        Derivation:
            observed = forecast - error   (since error = forecast - observed)
            P(lower <= observed < upper)
              = P(lower <= forecast - error < upper)
              = P(forecast - upper < error <= forecast - lower)
              = CDF_error(forecast - lower) - CDF_error(forecast - upper)

        σ is clamped to `_effective_sigma_floor(error_dist)` before evaluation.
        That returns 2.0°F for (lead=24h, n_samples >= 60) buckets that have
        accumulated enough real-forecast rows to trust their MLE σ, and 2.5°F
        everywhere else (long leads, thin buckets, reanalysis-heavy fits).
        See the module-level comments for the full rationale.
        """
        floored = ErrorDistribution(
            city=error_dist.city,
            model=error_dist.model,
            regime=error_dist.regime,
            lead_hours=error_dist.lead_hours,
            family=error_dist.family,
            mu=error_dist.mu,
            sigma=max(error_dist.sigma, _effective_sigma_floor(error_dist)),
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
    ) -> tuple[float, float]:
        """Skill-weighted ensemble bracket probability.

        Returns:
            (mean_prob, std_prob).  std_prob measures how much the models
            disagree — high std = high uncertainty = reduce Kelly size.
        """
        if len(forecasts) != len(error_dists):
            raise ValueError("forecasts and error_dists must be same length")
        if not forecasts:
            return (0.0, 1.0)

        weighted_probs: list[float] = []
        raw_probs: list[float] = []
        total_weight = 0.0

        for fc, dist in zip(forecasts, error_dists):
            w = self.weights.get(fc.model, 0.1)
            p = self.bracket_probability(
                forecast_f=fc.forecast_high_f,
                error_dist=dist,
                lower_f=lower_f,
                upper_f=upper_f,
            )
            weighted_probs.append(w * p)
            raw_probs.append(p)
            total_weight += w

        mean_prob = sum(weighted_probs) / total_weight if total_weight > 0 else 0.0

        # std across models (unweighted) as uncertainty proxy
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
    ) -> tuple[float, float, bool]:
        """Compute raw edge, fee-adjusted edge, and tradeability.

        Fee model: Polymarket charges fee_rate on WINNINGS only.
            EV = p * (1 - P) * (1 - fee) - (1 - p) * P
        Simplified: edge_after_fees ≈ raw_edge - fee * p * (1 - P)

        Tradeability gates (Apr 19 2026 refactor — see CLAUDE.md §4.3):
            1. Market price in [0.15, 0.75] — avoid penny artifacts and
               adverse (crushed) payoffs.
            2. Flat min-edge ≥ 5¢ after fees — replaces the previous tiered
               schedule `0.05 + max(0, (P-0.50) * 0.40)` which demanded up
               to 15¢ at P=0.75. Walk-forward verified: at flat 5¢ the
               market-band filter alone already forces a sensible floor
               (worst p×(1-p) → 0.19, smallest edge/stdev = 0.115).

        Gates explicitly REMOVED in this refactor:
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

        # Gate 2: Flat min-edge after fees
        if adjusted < min_edge:
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
