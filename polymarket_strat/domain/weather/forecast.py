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

# Minimum realistic σ for 24h NWP daily-max temperature forecasts (°F).
# Calibration uses Open-Meteo archive (reanalysis blend), which has seen the
# observations and produces σ ≈ 0.9-1.5°F — 2-4x tighter than real operational
# forecast errors (~2.5-4°F per NWS verification statistics).
# This floor prevents the overconfident bracket probabilities that result from
# comparing reanalysis-to-reanalysis instead of forecast-to-observation.
_SIGMA_FLOOR_F: float = 2.5


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

        σ is clamped to _SIGMA_FLOOR_F before evaluation.  The archive-derived
        distributions have σ ≈ 0.9-1.5°F because reanalysis has assimilated the
        observations; real 24h operational forecast σ is 2.5-4°F.  Without the
        floor, model_probs for narrow exact-degree brackets are overconfident by
        a factor of 4-7x (e.g., 70% becomes 52%).
        """
        floored = ErrorDistribution(
            city=error_dist.city,
            model=error_dist.model,
            regime=error_dist.regime,
            lead_hours=error_dist.lead_hours,
            family=error_dist.family,
            mu=error_dist.mu,
            sigma=max(error_dist.sigma, _SIGMA_FLOOR_F),
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
    ) -> tuple[float, float, bool]:
        """Compute raw edge, fee-adjusted edge, and tradeability.

        Fee model: Polymarket charges fee_rate on WINNINGS only.
            EV = p * (1 - P) * (1 - fee) - (1 - p) * P
        Simplified: edge_after_fees ≈ raw_edge - fee * p * (1 - P)

        Tradeability gates (all must pass):
            1. Market price in [0.15, 0.75] — avoid penny artifacts and adverse payoff
            2. Model probability ≥ 0.55 — must believe we're more likely right than wrong
            3. Tiered edge threshold:
               - P ≤ 0.50: edge ≥ 5¢ (standard)
               - P > 0.50: edge ≥ 5¢ + (P - 0.50) × 0.40
                 (at P=0.70 → 13¢, at P=0.75 → 15¢)
            4. Minimum Sharpe per trade ≥ 0.15:
               sharpe_per_trade = edge / sqrt(p × (1-p))

        Returns:
            (raw_edge, edge_after_fees, is_tradeable)
        """
        raw = model_prob - market_prob
        fee_drag = fee_rate * model_prob * (1.0 - market_prob)
        adjusted = raw - fee_drag

        # Gate 1: Market price band
        if market_prob < 0.15 or market_prob > 0.75:
            return (raw, adjusted, False)

        # Gate 2: Model confidence floor
        if model_prob < 0.55:
            return (raw, adjusted, False)

        # Gate 3: Tiered edge threshold — higher P demands larger edge
        min_edge = 0.05
        if market_prob > 0.50:
            min_edge += (market_prob - 0.50) * 0.40
        if adjusted < min_edge:
            return (raw, adjusted, False)

        # Gate 4: Minimum Sharpe per trade
        trade_vol = math.sqrt(model_prob * (1.0 - model_prob))
        sharpe_per_trade = adjusted / trade_vol if trade_vol > 0 else 0.0
        if sharpe_per_trade < 0.15:
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
