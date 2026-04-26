"""Forecast error calibration and synoptic regime classification.

ErrorDistributionFitter: fits Normal / Skew-Normal / Student-t to a vector
of (forecast - observed) errors, selecting the family by skewness and kurtosis.

RegimeClassifier: classifies the synoptic regime from upper-air fields.
This is a stub — the user implements once GRIB parsing is wired.
"""
from __future__ import annotations

import math
from typing import Any

from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    WeatherModel,
)



# -----------------------------------------------------------------------------
# Fitter guards — motivated by the Apr 24 2026 external calibration review
# (see CLAUDE.md §14 priority 7 and the "polymarket analysis.pdf" audit):
#
#   1. Skew-normal MLE frequently hits optimizer bounds on thin / ill-
#      conditioned samples, producing garbage shape parameters like
#      -7.5e7 or +1.2e6. These are not real fits — they are the optimizer
#      crashing silently. Any |shape| > _MAX_SKEW_SHAPE means the fit is
#      invalid and we should fall back to a better-conditioned family.
#
#   2. Three-parameter fits (skew_normal, student_t) need enough samples to
#      be statistically meaningful. On n < 30 we get parameters dominated
#      by noise — a 3-DoF student_t fitted to 8 samples produces σ values
#      that are ±50% off the true σ with only 30% confidence. Force
#      Normal(μ, σ) in that regime — it's honest and the σ at least
#      inherits the actual sample variance rather than a fabricated fit.
#
# These floors are deliberately conservative. The cost of a slightly-less-
# optimal family choice on well-sampled data is small; the cost of trading
# on a garbage distribution is not.
# -----------------------------------------------------------------------------

# Reject skew_normal fits where |shape| exceeds this — optimizer blew up.
_MAX_SKEW_SHAPE: float = 10.0

# Below this many samples, don't fit skew_normal or student_t (3-param fits).
# Fall back to Normal (2-param) which gracefully degrades.
_MIN_SAMPLES_FOR_PARAMETRIC_FIT: int = 30


class ErrorDistributionFitter:
    """Fit a parametric error distribution to forecast-minus-observed residuals.

    Selection logic (Apr 24 2026 — guarded against optimizer blow-ups and
    small-sample garbage):
        n < 30                                   ->  Normal (unconditional)
        |skew| < 0.3 and excess_kurtosis < 1.0  ->  Normal
        excess_kurtosis >= 1.0                   ->  Student-t
        |skew| >= 0.3                            ->  Skew-Normal (+ shape guard)

    If the skew-normal MLE produces |shape| > _MAX_SKEW_SHAPE, the fit is
    rejected and we fall through to Student-t (the next-best fat-tailed
    family), or Normal if Student-t also fails.

    All fitting uses scipy MLE. A Bayesian variant (PyMC) is provided
    below for when posterior-predictive bracket probabilities are wanted.
    """

    def fit(
        self,
        errors: list[float],
        *,
        city: str = "",
        model: WeatherModel = WeatherModel.GFS,
        regime: SynopticRegime = SynopticRegime.STABLE_HIGH,
        lead_hours: int = 24,
    ) -> ErrorDistribution:
        """MLE fit of the best-matching distribution family.

        Args:
            errors: list of (forecast - observed) values in Fahrenheit.
            city, model, regime, lead_hours: metadata carried through.

        Returns:
            ErrorDistribution with fitted parameters.

        Raises:
            ValueError: if fewer than 5 errors are provided.
        """
        import numpy as np
        from scipy import stats as sp_stats

        arr = np.asarray(errors, dtype=np.float64)
        n = len(arr)
        if n < 5:
            raise ValueError(f"Need >= 5 errors to fit, got {n}")

        mu_empirical = float(np.mean(arr))
        sigma_empirical = float(np.std(arr, ddof=1))
        skewness = float(sp_stats.skew(arr))
        excess_kurt = float(sp_stats.kurtosis(arr))  # Fisher definition (excess)

        # Guard A: below sample-size floor, don't fit 3-param families.
        # Normal with the empirical (μ, σ) at least honestly reflects the
        # sample; a skew_normal fit on 8 data points reflects optimizer noise.
        if n < _MIN_SAMPLES_FOR_PARAMETRIC_FIT:
            return ErrorDistribution(
                city=city, model=model, regime=regime, lead_hours=lead_hours,
                family=DistributionFamily.NORMAL,
                mu=mu_empirical, sigma=max(sigma_empirical, 1e-6),
                shape=0.0, nu=30.0,
                n_samples=n,
            )

        # n >= 30 — family selection by moment test.
        if abs(skewness) < 0.3 and excess_kurt < 1.0:
            # Normal — well-behaved, symmetric, thin-tailed
            return ErrorDistribution(
                city=city, model=model, regime=regime, lead_hours=lead_hours,
                family=DistributionFamily.NORMAL,
                mu=mu_empirical, sigma=max(sigma_empirical, 1e-6),
                shape=0.0, nu=30.0,
                n_samples=n,
            )

        if excess_kurt >= 1.0:
            # Student-t (fat tails)
            try:
                df, loc, scale = sp_stats.t.fit(arr)
                return ErrorDistribution(
                    city=city, model=model, regime=regime, lead_hours=lead_hours,
                    family=DistributionFamily.STUDENT_T,
                    mu=float(loc), sigma=float(scale), shape=0.0,
                    nu=max(float(df), 2.01),
                    n_samples=n,
                )
            except Exception:
                # Student-t optimizer failed — fall through to Normal.
                return ErrorDistribution(
                    city=city, model=model, regime=regime, lead_hours=lead_hours,
                    family=DistributionFamily.NORMAL,
                    mu=mu_empirical, sigma=max(sigma_empirical, 1e-6),
                    shape=0.0, nu=30.0,
                    n_samples=n,
                )

        # Skew-Normal (asymmetric) — with shape guard.
        try:
            a, loc, scale = sp_stats.skewnorm.fit(arr)
        except Exception:
            a, loc, scale = 0.0, mu_empirical, sigma_empirical

        # Guard B: |shape| > 10 means the optimizer hit a boundary and failed.
        # Real skewness in temperature errors rarely exceeds ~3 even in
        # highly skewed regimes (marine layer burn-off, cold-air damming).
        # A shape of 1e6 or -7e7 is pure numerical garbage.
        if abs(float(a)) > _MAX_SKEW_SHAPE:
            # Fall back to Student-t as next-best fat-tailed asymmetric-ish family.
            try:
                df, t_loc, t_scale = sp_stats.t.fit(arr)
                return ErrorDistribution(
                    city=city, model=model, regime=regime, lead_hours=lead_hours,
                    family=DistributionFamily.STUDENT_T,
                    mu=float(t_loc), sigma=float(t_scale), shape=0.0,
                    nu=max(float(df), 2.01),
                    n_samples=n,
                )
            except Exception:
                # Last-resort Normal.
                return ErrorDistribution(
                    city=city, model=model, regime=regime, lead_hours=lead_hours,
                    family=DistributionFamily.NORMAL,
                    mu=mu_empirical, sigma=max(sigma_empirical, 1e-6),
                    shape=0.0, nu=30.0,
                    n_samples=n,
                )

        return ErrorDistribution(
            city=city, model=model, regime=regime, lead_hours=lead_hours,
            family=DistributionFamily.SKEW_NORMAL,
            mu=float(loc), sigma=float(scale), shape=float(a), nu=30.0,
            n_samples=n,
        )

    def fit_bayesian(
        self,
        errors: list[float],
        *,
        city: str = "",
        model: WeatherModel = WeatherModel.GFS,
        regime: SynopticRegime = SynopticRegime.STABLE_HIGH,
        lead_hours: int = 24,
        prior_errors: list[float] | None = None,
        n_draws: int = 2000,
        n_tune: int = 1000,
        n_thin: int = 200,
        random_seed: int = 42,
    ) -> list[ErrorDistribution]:
        """Bayesian fit returning a thinned posterior as a list of ErrorDistributions.

        Each element is one posterior draw of (mu, sigma, nu) for a Student-t
        likelihood. When computing bracket probabilities downstream, integrate
        across these samples (MC average) to get posterior predictive probs:

            P(bracket) ≈ (1/N) Σ_s P_sample_s(bracket)

        This automatically widens predictions when the error history is sparse
        (few posterior samples pile up near any point — wide marginal) and tightens
        when data is plentiful. Replaces the ad-hoc σ floor with honest uncertainty.

        Args:
            errors: (forecast - observed) residuals in °F.
            city, model, regime, lead_hours: metadata carried into each sample.
            prior_errors: optional extra residuals to concatenate (e.g., neighbor
                city of same climate zone) — acts as a weak data-augmentation prior.
            n_draws: samples per chain (default 2000).
            n_tune: warmup samples per chain (default 1000). Discarded.
            n_thin: max posterior distributions returned (default 200).
            random_seed: for reproducibility.

        Returns:
            List of `n_thin` ErrorDistributions, each with family=STUDENT_T.
            Use `summarize_posterior()` if you need a single-row DB representation.

        Raises:
            ValueError if fewer than 5 errors are provided.
            ImportError if PyMC is not installed.

        Model:
            mu     ~ Normal(0, 5)             # bias, °F — prior ±10°F plausible
            sigma  ~ HalfNormal(3)            # scale, °F — prior typical <6°F
            nu     ~ Exponential(1/30) + 2    # d.o.f., bounded below at 2 so variance exists
            obs_i  ~ StudentT(nu, mu, sigma)  # heavy-tailed likelihood, robust to outliers
        """
        try:
            import numpy as np
            import pymc as pm
        except ImportError as exc:
            raise ImportError(
                "Bayesian fit requires PyMC: `pip install pymc arviz` "
                "(also brings pytensor + numpyro). Expect ~2GB of disk."
            ) from exc

        arr = np.asarray(errors, dtype=np.float64)
        if len(arr) < 5:
            raise ValueError(f"Need >= 5 errors to fit, got {len(arr)}")

        # Optional data augmentation from a weakly-related prior pool
        if prior_errors:
            prior_arr = np.asarray(prior_errors, dtype=np.float64)
            arr = np.concatenate([arr, prior_arr])

        with pm.Model():
            mu = pm.Normal("mu", mu=0.0, sigma=5.0)
            sigma = pm.HalfNormal("sigma", sigma=3.0)
            nu_raw = pm.Exponential("nu_raw", lam=1.0 / 30.0)
            nu = pm.Deterministic("nu", nu_raw + 2.0)
            pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=arr)
            trace = pm.sample(
                draws=n_draws,
                tune=n_tune,
                chains=4,
                target_accept=0.9,
                progressbar=False,
                random_seed=random_seed,
            )

        # Flatten across chains × draws
        mus = trace.posterior["mu"].values.flatten()
        sigmas = trace.posterior["sigma"].values.flatten()
        nus = trace.posterior["nu"].values.flatten()

        # Thin to n_thin evenly-spaced samples to keep downstream MC tractable
        n_total = len(mus)
        if n_total > n_thin:
            idx = np.linspace(0, n_total - 1, n_thin).astype(int)
            mus = mus[idx]
            sigmas = sigmas[idx]
            nus = nus[idx]

        distributions: list[ErrorDistribution] = []
        for m, s, v in zip(mus, sigmas, nus):
            distributions.append(ErrorDistribution(
                city=city, model=model, regime=regime, lead_hours=lead_hours,
                family=DistributionFamily.STUDENT_T,
                mu=float(m),
                sigma=float(max(s, 1e-6)),
                shape=0.0,
                nu=float(max(v, 2.01)),
                n_samples=len(arr),
            ))
        return distributions

    def summarize_posterior(
        self,
        posterior: list[ErrorDistribution],
    ) -> ErrorDistribution:
        """Collapse a posterior sample list into a single ErrorDistribution.

        The trick is widening sigma to reflect parameter uncertainty. By the
        law of total variance:

            Var(X) = E[Var(X | params)] + Var(E[X | params])
                   = E[σ² · ν/(ν-2)]   +  Var(μ)

        For ν large (≳30), ν/(ν-2) ≈ 1, so widened_sigma² ≈ mean(σ²) + var(μ).
        This gives a single (μ, σ, ν) triple safe to plug into the existing
        non-Bayesian CDF pipeline while still reflecting the posterior spread.

        Use this when you want to store ONE row in `error_distributions` but
        still capture the Bayesian-derived uncertainty.
        """
        if not posterior:
            raise ValueError("empty posterior")

        import numpy as np

        mus = np.array([d.mu for d in posterior])
        sigmas = np.array([d.sigma for d in posterior])
        nus = np.array([d.nu for d in posterior])

        mean_mu = float(np.mean(mus))
        var_mu = float(np.var(mus, ddof=1)) if len(mus) > 1 else 0.0
        # Student-t conditional variance: σ² · ν/(ν-2), requires ν > 2
        safe_nus = np.maximum(nus, 2.01)
        cond_var = sigmas ** 2 * safe_nus / (safe_nus - 2.0)
        widened_sigma = float(np.sqrt(np.mean(cond_var) + var_mu))
        mean_nu = float(np.mean(safe_nus))

        ref = posterior[0]
        return ErrorDistribution(
            city=ref.city,
            model=ref.model,
            regime=ref.regime,
            lead_hours=ref.lead_hours,
            family=DistributionFamily.STUDENT_T,
            mu=mean_mu,
            sigma=max(widened_sigma, 1e-6),
            shape=0.0,
            nu=max(mean_nu, 2.01),
            n_samples=ref.n_samples,
        )


class RegimeClassifier:
    """Classify synoptic regime from ensemble spread statistics or upper-air fields.

    Three classification modes (best → worst):
      1. classify_from_ensemble() — uses 82-member ensemble stats + CAPE
      2. classify_from_grib()     — uses upper-air GRIB fields (requires parsing)
      3. classify_from_spread()   — heuristic from 2-point model disagreement

    The ensemble classifier (mode 1) is the recommended default since v2.
    It uses Open-Meteo's ensemble API (31 GFS + 51 ECMWF members) to detect
    all five regimes without GRIB parsing.
    """

    def classify_from_ensemble(
        self,
        *,
        spread_f: float,
        std_f: float,
        skewness: float,
        cape_max: float,
        n_members: int = 0,
    ) -> SynopticRegime:
        """Classify regime from ensemble member statistics.

        Uses 82 ensemble members (31 GFS + 51 ECMWF) via Open-Meteo.
        This replaces the 2-point spread heuristic with proper uncertainty
        quantification.

        Args:
            spread_f: max - min across all ensemble members (°F)
            std_f: standard deviation of ensemble members (°F)
            skewness: Fisher skewness of ensemble member distribution
            cape_max: maximum CAPE (J/kg) for the day
            n_members: number of ensemble members (for sanity checks)

        Decision tree:
            CAPE > 1000 J/kg                    → CONVECTIVE
            spread > 10°F AND std > 3°F         → FRONTAL_PASSAGE
            |skewness| > 0.8 AND std > 1.5°F    → MARINE_INFLUENCE
            spread > 6°F AND std > 2°F          → TRANSITION
            else                                → STABLE_HIGH
        """
        if n_members < 3:
            # Not enough members; fall back to heuristic
            return self.classify_from_spread(model_spread_f=spread_f)

        if cape_max > 1000:
            return SynopticRegime.CONVECTIVE
        if spread_f > 10.0 and std_f > 3.0:
            return SynopticRegime.FRONTAL_PASSAGE
        if abs(skewness) > 0.8 and std_f > 1.5:
            return SynopticRegime.MARINE_INFLUENCE
        if spread_f > 6.0 and std_f > 2.0:
            return SynopticRegime.TRANSITION
        return SynopticRegime.STABLE_HIGH

    def classify_from_spread(self, *, model_spread_f: float) -> SynopticRegime:
        """Quick classification from ensemble forecast spread (max - min).

        Good enough for v1.  Replace with classify_from_grib() once upper-air
        parsing is implemented.

        Thresholds (Fahrenheit):
            spread < 2.7°F (~1.5°C)  -> STABLE_HIGH
            spread > 7.2°F (~4.0°C)  -> FRONTAL_PASSAGE
            spread > 4.5°F (~2.5°C)  -> TRANSITION
            else                      -> STABLE_HIGH
        """
        if model_spread_f < 2.7:
            return SynopticRegime.STABLE_HIGH
        if model_spread_f > 7.2:
            return SynopticRegime.FRONTAL_PASSAGE
        if model_spread_f > 4.5:
            return SynopticRegime.TRANSITION
        return SynopticRegime.STABLE_HIGH

    def classify_from_grib(
        self,
        *,
        pressure_tendency_hpa_12h: float,
        cape_jkg: float,
        is_coastal: bool,
        wind_onshore: bool,
        front_distance_km: float,
        model_spread_f: float,
    ) -> SynopticRegime:
        """Full classification from upper-air GRIB fields.

        YOU IMPLEMENT — extract these features from GRIB data:

        - pressure_tendency_hpa_12h: surface pressure change over 12h
          (GRIB key: msl at valid_time vs valid_time-12h)
        - cape_jkg: Convective Available Potential Energy
          (GRIB key: cape or cin)
        - is_coastal: whether the city is within 50km of coast
          (static per city — lookup from CityStation)
        - wind_onshore: whether 10m wind is from ocean to land
          (GRIB key: u10/v10 + coastline geometry)
        - front_distance_km: distance to nearest front
          (derived from 850mb theta-e gradient maxima)
        - model_spread_f: ensemble spread as above

        Decision tree:
            front_distance_km < 200  -> FRONTAL_PASSAGE
            cape_jkg > 1000          -> CONVECTIVE
            is_coastal and wind_onshore -> MARINE_INFLUENCE
            abs(pressure_tendency) > 4 -> TRANSITION
            else                       -> STABLE_HIGH
        """
        if front_distance_km < 200:
            return SynopticRegime.FRONTAL_PASSAGE
        if cape_jkg > 1000:
            return SynopticRegime.CONVECTIVE
        if is_coastal and wind_onshore:
            return SynopticRegime.MARINE_INFLUENCE
        if abs(pressure_tendency_hpa_12h) > 4:
            return SynopticRegime.TRANSITION
        return SynopticRegime.STABLE_HIGH
