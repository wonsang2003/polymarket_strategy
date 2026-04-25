"""WeatherBracketStrategy — the main orchestrator.

Implements the Strategy Protocol (analyze + backtest) by wiring together:
  - WeatherMarketScanner (discover contracts)
  - GribDataClient (fetch forecasts)
  - RegimeClassifier (classify synoptic regime)
  - ErrorDistributionFitter (calibrate error distributions)
  - BracketProbabilityCalculator (compute model probs, edges, Kelly)
  - WeatherDatabase (persist everything)
"""
from __future__ import annotations

import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
from pathlib import Path
from typing import Any

# Effective settlement moment (station-local) for forecast-horizon computation.
# Polymarket settles at end-of-day in the station's timezone, but the daily
# temperature max is typically locked in by ~17:00 local (heat-of-the-day +
# station thermal lag). Using 17:00 as the "lead zero" point means:
#   - D+0 contract called at 09:00 local → ~8h lead (short-range, trade)
#   - D+0 contract called at 20:00 local → past lock-in, skip (observed)
#   - D+1 contract called at 23:00 local → ~18h lead → 24h bucket
#   - D+2 contract any time → lead > 48h → skip (no calibration coverage)
# Temperate-latitude defensible default; tropical stations (Dubai, Singapore,
# Hong Kong summer) max slightly earlier — 17:00 over-buffers but cost is
# only a few extra "too_close_to_settlement" skips near the boundary.
_LOCK_IN_LOCAL = time(hour=17)

# Cities blocked from producing signals due to demonstrated bracket-parsing /
# calibration-artifact pathologies on live paper data (Apr 24 2026 audit of
# 32 settled trades — see §14 priority 4, §15 Apr 24 strategy-review bullet).
# Evidence per city (avg_p / avg_m / avg_edge across the settled window):
#   - la: avg_p = 1.00 — impossible for a weather bracket; indicates
#        bracket-parse emitted a degenerate bounds pair (e.g. "-inf to +inf")
#        that the CDF integrates to 1.0. Also flagged in §14 priority 4 for
#        marine-layer over-forecast (ECMWF μ=+6.18°F outlier historically,
#        see §5.3). 1 trade, $50 full loss.
#   - seattle: avg_p = 0.71, avg_m = 0.18, avg_edge = +0.52. A 52¢ edge on
#        a 18¢ market is the textbook signature of an artifact market (real
#        edges top out around 15-20¢ — anything above that is the scanner
#        mis-parsing the contract bounds vs. station observation). 1 trade,
#        $50 full loss.
#   - mexico_city: avg_p = 0.64, avg_m = 0.16, avg_edge = +0.47. Documented
#        root cause in §14 "Apr 19 Mexico City 27°C+ false positive": D+2
#        contract hit the 72h bucket before the 48h hard cap landed, σ-scaled
#        a stable_high dist by √3, and the 72h forecast itself ran hotter
#        than the 48h run. The 48h cap prevents the 72h-bucket pathway now,
#        but the hot-forecast bias at this station is persistent enough that
#        blocking is the safer call until we have a station-specific bias
#        correction (§2 backlog: "Station microclimate bias correction").
#        1 trade, $50 full loss.
#   - sf: added Apr 24 2026 per external calibration review ("polymarket
#        analysis.pdf"). SF and LA are the PDF's Tier D ("don't trade on
#        current model, Brier > 0.16") — both West Coast cities where
#        marine-layer and microclimate effects are not captured by global
#        GFS/ECMWF models. Current walk-forward Brier 0.161 (vs Wellington
#        0.113 at the Tier-A frontier). Zero trades placed yet — blocking
#        pre-emptively per the PDF's explicit recommendation. Kept separate
#        from the reliability-weighted sizing soft-block (see §15.3.1 /
#        Tier 2b of the Apr 24 dev plan): this is a hard "don't trade" for
#        cities where we know the physical model is structurally wrong,
#        not just a statistical weight.
# Reinstatement criteria: a city comes off this list only after
#   (a) n >= 30 real-forecast days at 24h lead have accumulated since the
#       last calibration anomaly, AND
#   (b) a walk-forward rerun scoped to that city produces Brier skill > 0
#       vs. the flat 0.50 base-rate predictor.
# Until then the calibration pipeline still collects forecast_errors for
# these cities (so reinstatement is a data question, not a re-scaffolding
# question) — we just refuse to trade them.
# Apr 25 2026 (LATE) — DEPRECATED. Emptied after a careful audit showed the
# old blocks were based on data from a now-fixed pipeline AND were structurally
# duplicative of newer safety layers.
#
# Why this list was emptied:
#
# 1. The losses that triggered the Plan B blocks (-$687 cumulative on
#    wellington / sao_paulo / toronto / amsterdam / munich / buenos_aires /
#    milan) came from the OLD parametric pipeline with the LEAKY 1-year
#    climatology. With only 1 obs per (city, doy) cell, the
#    `forecast_anomaly = forecast - climo_mean` feature ≈ `-error`, giving
#    the model fake confidence on artifact brackets. The new pipeline
#    uses ERA5 30-yr climatology (8052 cells, median 30 obs/cell) — that
#    feature now carries genuine signal.
#
# 2. Today's tail-bin ECE audit (data/weather/tail_ece_audit.json) shows
#    the previously-blocked cities are NOT obviously worse calibrated:
#       sao_paulo: 2.61% (best in dataset)
#       munich:    2.67%
#       amsterdam: 2.68%
#       la:        4.44% (better than nyc 6.61%)
#       sf:        4.96%
#       seattle:   5.18%
#       wellington:6.01%
#    Meanwhile dubai sits at 13.27% — worst calibration, NOT blocked. The
#    block list is internally inconsistent with measured calibration.
#
# 3. Three independent safety layers now do what _BLOCKED_CITIES did,
#    but data-driven:
#      a. Per-city ECE shrinkage (#52 + #76) — bad-calibration cities
#         trade smaller automatically. Munich gets 0.91x sizing,
#         Wellington 0.89x, NYC 0.58x. Naturally-shrinking position
#         scaler proportional to (1 - 1.5 × ECE_shrunk).
#      b. Plan B high-p artifact cap (`p>0.85 + edge>0.20 → reject`) —
#         catches the specific artifact pattern that produced most of
#         the -$687 loss tape.
#      c. BucketBlocklist (#53, scripts/fit_bucket_blocklist.py) —
#         re-blocks (city, lead, regime) buckets nightly based on
#         realized P&L. Data-driven complement to this manual list.
#    Plus structural safeguards: 48h hard lead cap (kills D+2 pathology),
#    fine-bin ECE coverage gate (calibrated bins only), liquidity gate.
#
# 4. The original 4 (la/seattle/mexico_city/sf) were each blocked on
#    n=1 trade with $50 loss — not statistical evidence. SF had 0 trades
#    placed. Mexico City's D+2 pathology is now structurally impossible.
#    Marine-layer for LA/SF is a parametric-pricer concern; the new
#    quantile pricer learns from actual GFS errors at those locations.
#
# Reinstatement path: this list stays as a frozenset() — keeping the
# variable shape lets us re-add cities without re-scaffolding. If a
# specific city's REALIZED P&L on the new pipeline goes negative with
# n>=10 settled trades, BucketBlocklist will auto-block via the nightly
# fit_bucket_blocklist.py cron at 5:15am KST. Manual blocks via this
# variable should be reserved for STRUCTURAL bugs (e.g. parser failure
# making bracket bounds garbage), not for "this city has been losing."
_BLOCKED_CITIES: frozenset[str] = frozenset()

# Apr 24 2026 (Citadel Q1) — hard sample-size gate for TRADING. Distinct
# from `calibration._MIN_SAMPLES_FOR_PARAMETRIC_FIT = 30` which controls
# the *fit family* (Normal vs Skew-Normal), but still emits a fitted
# distribution even at n=5. At inference we're more conservative: a
# Normal(μ, σ) fit from 8 observations has roughly ±40% CI on σ. Trading
# on that is betting on a noise estimate. Below this threshold, REFUSE
# to trade the bracket.
#
# The regime × lead × season slicing (added Apr 24) divides historical
# samples by ~8x (4 seasons × 2 regime mass-buckets). A 700-sample city
# has ~90 per cell after slicing — safely above N_MIN_FOR_TRADING even
# at the tightest cell. Thin regimes (convective, marine_influence) or
# small-history cities will hit this gate and fall back to pooled (-1)
# distributions, then to STABLE_HIGH, then to skip.
_N_MIN_FOR_TRADING: int = 30

# Apr 25 2026 — "high-conviction override" for close-to-settlement trades.
# Background: previously we hard-rejected every contract with lead_hours
# < 6h because our model is PURE FORECAST (no nowcast layer) and gets
# stale as the station observation window opens. User argued this was
# leaving opportunity on the table for low-liquidity mispricings in the
# 3-6h window. Honest answer: most of those "edges" aren't real — the
# market has observation info we don't — BUT on extreme-conviction
# signals (bracket geometry clearly outside observation range) with
# very large edges, the late-entry trade can still be +EV.
#
# Override logic:
#   raw_lead_h in [3, 6)  → allow IFF BOTH:
#       - adj_edge >= _LATE_ENTRY_MIN_EDGE (≥ 20¢, vs default 5¢/10¢)
#       - model_prob extreme (≥ 0.80 or ≤ 0.20) so bracket is clearly
#         in or out of the physical observation envelope
#     AND size at _LATE_ENTRY_SIZE_MULT × normal (half). The halving
#     encodes our reduced confidence vs full-lead entries.
#   raw_lead_h < 3        → hard reject unconditionally. Window too
#     narrow for any forecast-based edge to survive observation info.
#
# Post-ship measurement:
#   Every late-entry signal gets tagged `late_entry=True` in metadata
#   and trade_history. After 2 weeks of paper data, we can measure
#   win-rate on these trades specifically. If realized EV ≥ 0, the
#   override is pulling its weight and we might consider relaxing
#   further (e.g., 0.15 edge floor). If realized EV < 0, tighten or
#   remove the override entirely and build the nowcast (§15.2.2).
_LATE_ENTRY_MIN_LEAD_H: float = 3.0      # hard floor
_LATE_ENTRY_GATE_LEAD_H: float = 6.0     # override active between these
_LATE_ENTRY_MIN_EDGE: float = 0.20       # 20¢ required (vs 5¢/10¢ default)
_LATE_ENTRY_MIN_P_HIGH: float = 0.80     # model prob must exceed this, OR
_LATE_ENTRY_MAX_P_LOW: float = 0.20      #   fall below this
_LATE_ENTRY_SIZE_MULT: float = 0.5       # halve sizing — reduced confidence

# Apr 25 2026 — PLAN B emergency: high-confidence artifact cap.
# Diagnosed from today's loss tape (5 of 5 fresh entries on Apr 25 had
# p_model >= 0.93, all lost):
#     Toronto    p=1.00 m=0.67 edge=0.32 → -$1.49
#     Toronto    p=1.00 m=0.70 edge=0.29 → -$5.71
#     Sao Paulo  p=0.97 m=0.70 edge=0.26 → -$0.71
#     Shanghai   p=0.93 m=0.71 edge=0.21 → -$8.87
#     Wellington p=0.67 m=0.59 edge=0.08 → -$1.57
# When the model produces p≥0.85 on a market priced at 0.60-0.75 (i.e.
# market disagrees substantially), one of three things is happening:
#   1. Bracket parse degenerated (e.g. unbounded threshold like
#      "Toronto temp >= 0°F" parsed as "always YES"). Already saw this
#      pattern in the LA/Seattle/MexCity blocklist trades.
#   2. Calibration σ is tighter than reality, inflating mid-bracket p
#      to near-1.0 even when forecast uncertainty is meaningful.
#   3. Genuine extreme-confidence signal — but those should match
#      market within 5-10¢, not be priced at 0.65 by an active book.
# All three failure modes lose money. The high-p × high-edge combo is a
# clear artifact signature — refuse to trade it.
#
# Threshold rationale: p_model > 0.80 + edge > 0.15 carves out the
# losing zone without nuking legitimate "high-confidence narrow market"
# signals where m is also near 0.80 (those have small edge by
# construction, so they pass the cap).
#
# Apr 26 2026 — TIGHTENED from (0.85, 0.20) → (0.80, 0.15) after the
# NO-side bleed analysis. Settled NO trades had a long tail of
# small-loss exits on entries with p_model in [0.77, 0.85] (sao_paulo
# 0.86, munich 0.77, hong_kong 0.92). The fine-bin tail audit's
# [0.95, 1.00] bin showed a 9.2% calibration gap, but the (0.77, 0.85]
# band — exactly where the new losses concentrate — was just below the
# old cap and slipped through. The previous (0.85, 0.20) carved out
# only the most extreme artifacts; (0.80, 0.15) covers the realistic
# under-prediction band the model exhibits at the high tail.
_PLAN_B_HIGH_P_CAP: float = 0.80
_PLAN_B_HIGH_P_EDGE_TRIGGER: float = 0.15

# Apr 26 2026 — NARROW-BRACKET NO-SIDE CAP.
#
# Polymarket EU/Asia weather contracts are typically 1°C wide
# (≈ 1.8°F). Our calibrated σ floor is 2.5°F, so bracket width is
# approximately σ/√2 — a regime where bracket P is hyper-sensitive
# to small σ-estimation errors. When the model says P(NO) = 0.85 on
# a 1°F-wide bracket, a 0.5°F under-estimate of σ inflates P(NO) by
# ~5–10pp, which is the entire edge.
#
# Empirically: the recent NO-side losses concentrate exactly here —
# narrow brackets with model_prob in [0.77, 0.92]. The fine-bin ECE
# audit was run on synthetic ±2°F brackets (wider), so the audit
# under-states the true calibration error on the contracts we
# actually trade. Until σ tightens further from real-forecast
# accumulation OR isotonic regression is wired into the NO side,
# refuse NO entries on narrow brackets above this threshold.
#
# Threshold tuned conservative: 0.75 still allows narrow-bracket NO
# trades when the model is moderately confident (e.g. forecast 70°F
# vs bracket [60, 61]°F → P(NO) ~ 0.95 → blocked, correct), but
# blocks the borderline tail entries that have been bleeding (e.g.
# forecast 70°F vs bracket [69, 70.8]°F → P(NO) ~ 0.80 → blocked
# under this rule, was the failure mode).
_NARROW_BRACKET_WIDTH_F: float = 2.0
_NARROW_BRACKET_NO_MAX_P: float = 0.75

# ============================================================================
# Apr 25 2026 — STRATEGY 3: Ensemble-confidence-weighted blend
#
# Background: traditional approach was "model_prob - market_prob = edge".
# This assumes our model is the truth-finder. After -$687 cumulative loss
# we flipped paradigm: the MARKET is the prior; our model is one piece of
# evidence. Edge requires identifiable inefficiency, not raw disagreement.
#
# Key insight: when our forecast models AGREE (low ensemble σ), our
# probability estimate is well-supported. When they DISAGREE (high σ),
# our probability is one of many possibilities and we should DEFER to
# market consensus.
#
# Formula:
#     model_confidence = exp(-σ_ensemble / SIGMA_DECAY)   ∈ [0, 1]
#     w = model_confidence² (squared so low-confidence collapses fast)
#     p_blended = w · p_model + (1 - w) · p_market
#
# At σ=0: w=1, p_blended = p_model (full trust in model)
# At σ=3°F: w=0.135, p_blended ≈ p_market (market dominates)
# At σ=6°F: w=0.018, fully market-following
#
# Trade gate: require model_confidence ≥ MIN_CONFIDENCE AND
# |p_blended - p_market| ≥ MIN_BLENDED_EDGE. The blended edge
# automatically shrinks when ensemble is split, so even if raw model
# claimed 30¢ edge, the blended edge after low confidence might be 5¢
# — below threshold, no trade.
#
# This is the "p=1.00 artifact killer" because:
#   - Toronto p=1.00 / market=0.67 with ensemble σ=4°F (frontal day,
#     models split) → confidence=0.26, w=0.07, p_blended=0.69 ≈ market.
#     Blended edge = 0.69 - 0.67 = 2¢. Below 8¢ threshold → SKIP.
#   - The artifact gets DEFERRED to market consensus instead of fought.
# ============================================================================
_S3_SIGMA_DECAY: float = 3.0       # °F. exp(-σ/3) maps σ to confidence
_S3_MIN_CONFIDENCE: float = 0.50   # require at least 50% confidence to trade
_S3_MIN_BLENDED_EDGE: float = 0.08 # 8¢ on blended edge (vs 5¢/10¢ on raw)

from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.domain.models import (
    BacktestResult,
    BacktestTrade,
    StrategyAnalysis,
    StrategySignal,
    TradePlan,
)
from polymarket_strat.domain.weather.calibration import ErrorDistributionFitter, RegimeClassifier
from polymarket_strat.domain.weather.coherence import detect_coherence_violations
from polymarket_strat.domain.weather.forecast import BracketProbabilityCalculator
from polymarket_strat.domain.weather.reliability import (
    get_bucket_blocklist,
    get_city_reliability,
)
from polymarket_strat.domain.weather.models import (
    CITY_REGISTRY,
    BracketContract,
    CorrelationGroup,
    ErrorDistribution,
    ForecastError,
    StationObservation,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
from polymarket_strat.infrastructure.weather.station_client import StationObservationClient
from polymarket_strat.risk import PortfolioRiskManager


class WeatherBracketStrategy:
    name = "weather_bracket"

    def __init__(
        self,
        *,
        grib_client: GribDataClient,
        station_client: StationObservationClient,
        market_scanner: WeatherMarketScanner,
        db: WeatherDatabase,
        min_edge: float = 0.05,
        fee_rate: float = 0.02,
        max_positions_per_group: int = 2,
        model_weights: dict[WeatherModel, float] | None = None,
    ):
        self.grib = grib_client
        self.stations = station_client
        self.scanner = market_scanner
        self.db = db
        self.min_edge = min_edge
        self.fee_rate = fee_rate
        self.max_positions_per_group = max_positions_per_group
        self.calc = BracketProbabilityCalculator(model_weights)
        self.regime_clf = RegimeClassifier()
        self.fitter = ErrorDistributionFitter()

    # ------------------------------------------------------------------
    # Strategy Protocol
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        constraints: TradingConstraints,
        portfolio_state: PortfolioState,
    ) -> StrategyAnalysis:
        """Live signal generation: discover contracts, fetch forecasts, price brackets."""
        contracts = self.scanner.find_weather_bracket_markets()
        if not contracts:
            return StrategyAnalysis(strategy_name=self.name, signals=[], trade_plan=[],
                                   diagnostics={"reason": "no_weather_contracts_found"})

        # Group contracts by (city, target_date) — normalization must be per-date,
        # not across all dates for a city. Mixing April 16 and April 17 brackets
        # in a single normalization is meaningless.
        by_city_date: dict[tuple[str, date], list[BracketContract]] = defaultdict(list)
        for c in contracts:
            by_city_date[(c.city, c.target_date)].append(c)

        signals: list[StrategySignal] = []
        plans: list[TradePlan] = []

        # Apr 25 2026 — STRATEGY 1: cross-bracket coherence arbitrage.
        # Run BEFORE the model-based pipeline. These are pure-arithmetic
        # arbitrage signals: monotonicity violations across "or higher"
        # / "or lower" bracket families. No forecast model needed; the
        # market's own prices are inconsistent. Each opportunity is
        # emitted as a coherence_arb signal with full diagnostic info.
        # Filter out blocked cities defensively (we don't trade them
        # even on arithmetic arb — too much risk that the artifact
        # pricing is informative-but-broken at the source).
        unblocked_contracts = [
            c for c in contracts if c.city not in _BLOCKED_CITIES
        ]
        coherence_opps = detect_coherence_violations(unblocked_contracts)
        coherence_signal_count = 0
        for opp in coherence_opps:
            # Sizing: arbitrage is risk-free in expectation, but capital
            # is still tied up to settlement. Use a flat per-arb cap of
            # the standard min_position_notional_usd — not Kelly-based,
            # because the arithmetic guarantee is binary, not
            # probabilistic. At $5 per arb across maybe 5-10 ops/day,
            # this is risk-isolated from the main pipeline.
            arb_size = constraints.min_position_notional_usd
            sig_meta = {
                "city": opp.city,
                "target_date": opp.target_date,
                "strategy_subtype": "coherence_arb",
                "family": opp.family,
                "long_bracket_lower_f": opp.long_bracket.lower_f,
                "long_bracket_upper_f": opp.long_bracket.upper_f,
                "short_bracket_lower_f": opp.short_bracket.lower_f,
                "short_bracket_upper_f": opp.short_bracket.upper_f,
                "long_price": opp.long_price,
                "short_price": opp.short_price,
                "violation_magnitude": opp.violation_magnitude,
                "token_id": opp.long_bracket.token_id_yes,
                "token_side": "YES",
                "edge_after_fees": opp.violation_magnitude,  # rough; arbitrage is bounded
            }
            signals.append(StrategySignal(
                market=opp.long_bracket.market_id,
                question=opp.long_bracket.question,
                category="weather",
                outcome=f"COHERENCE_{opp.family.upper()}",
                side="BUY_YES",
                signal_score=opp.violation_magnitude * 30,  # weight by mispricing size
                reference_price=opp.long_price,
                metadata=sig_meta,
            ))
            plans.append(TradePlan(
                strategy_name=self.name,
                market=opp.long_bracket.market_id,
                question=opp.long_bracket.question,
                category="weather",
                outcome=f"COHERENCE_{opp.family.upper()}",
                token_id=opp.long_bracket.token_id_yes,
                side="BUY_YES",
                signal_score=opp.violation_magnitude * 30,
                target_notional=arb_size,
                reference_price=opp.long_price,
                best_ask=opp.long_bracket.best_ask_yes,
                best_bid=opp.long_bracket.best_bid_yes,
                spread=opp.long_bracket.spread,
                top_ask_size=opp.long_bracket.top_ask_size,
                top_bid_size=opp.long_bracket.top_bid_size,
                risk_score=0.1,  # arbitrage is low-risk
                expected_value=opp.violation_magnitude,
                executable=opp.long_bracket.spread <= constraints.max_spread,
                rationale=[
                    f"COHERENCE {opp.family}: {opp.city} {opp.target_date}",
                    f"Long  ${opp.long_price:.2f} bracket [{opp.long_bracket.lower_f:.0f}, {opp.long_bracket.upper_f:.0f}]°F",
                    f"Short ${opp.short_price:.2f} bracket [{opp.short_bracket.lower_f:.0f}, {opp.short_bracket.upper_f:.0f}]°F",
                    f"Violation magnitude: {opp.violation_magnitude:.2%}",
                ],
                metadata=sig_meta,
            ))
            coherence_signal_count += 1
        risk_mgr = PortfolioRiskManager(constraints, portfolio_state)
        # Correlation-group cap is now notional-based (CLAUDE.md §4.3).
        # Tracks cumulative $-exposure per correlation group within this
        # analyze() cycle; each new plan must leave room under
        # `constraints.bankroll * constraints.max_correlation_group_fraction`
        # (default 15%). Replaces the prior count-based cap of 2 positions.
        group_notional: dict[str, float] = defaultdict(float)
        # Precompute once — these do not change within a cycle.
        per_position_cap = max(
            constraints.bankroll * constraints.max_position_fraction,
            constraints.min_position_notional_usd,
        )
        group_notional_cap = (
            constraints.bankroll * constraints.max_correlation_group_fraction
        )
        # Cache forecasts per (city, lead_bucket) so tomorrow's 48h contracts and
        # today's 24h contracts each get the correct-horizon forecast.
        forecast_cache: dict[tuple[str, int], Any] = {}
        # Parallel cache of `forecast_content_hash(forecasts)` — computed once
        # per (city, lead_bucket) after the HRRR/NAM drop so the rebalance job
        # can detect a fresh Open-Meteo response by comparing the stored hash
        # from entry time against a re-fetch hash. SHA256 of the sorted
        # (model, round(°F*100), lead_h) triples per the forecast_content_hash
        # helper in domain.weather.forecast.
        forecast_hash_cache: dict[tuple[str, int], str] = {}

        # City reliability index (Apr 24 2026 — Tier 2b). Singleton, loads
        # data/weather/reliability.json once. When the JSON is missing we
        # silently get multiplier=1.0 on every lookup — no behavior change
        # from pre-reliability code. When present, low-brier / low-sample
        # cities get sized down rather than blocklisted.
        city_reliability = get_city_reliability()

        # Bucket-level EV auto-block (Apr 24 2026 — Tier 2c). Reads
        # data/weather/blocked_buckets.json (produced by nightly
        # scripts/fit_bucket_blocklist.py). Complementary to _BLOCKED_CITIES:
        # that's manual structural blocks, this is data-driven per-bucket
        # blocks from realized paper P&L. Graceful fallback: no file → no
        # blocks, behavior unchanged.
        bucket_blocklist = get_bucket_blocklist()

        # Gate rejection histogram — propagates into StrategyAnalysis.diagnostics
        # so run_autotrade can surface exact failure mode (which gate ate each
        # candidate contract) in CloudWatch + Telegram. Without this every
        # "no_executable_signals" cycle is an opaque black box.
        gate_rejects: dict[str, int] = defaultdict(int)
        no_forecast_contracts = 0
        no_dists_contracts = 0
        hrrr_dropped_city_leads: set[tuple[str, int]] = set()

        for (city, target_date), date_contracts in by_city_date.items():
            station = CITY_REGISTRY.get(city)
            if not station:
                continue

            # Hard block: LA, Seattle, Mexico City are on the bracket-artifact
            # blocklist (see _BLOCKED_CITIES docstring for evidence). Skip
            # BEFORE any forecast fetch / regime classification so we don't
            # waste Open-Meteo quota on trades we'll never take. Counted
            # separately in diagnostics so the dashboard can show which
            # cities were suppressed vs. which failed legitimate gates.
            if city in _BLOCKED_CITIES:
                gate_rejects["blocked_city"] += len(date_contracts)
                continue

            # Compute lead_hours in station-local wall-clock time, not a UTC
            # date-diff. The prior UTC-date-diff formula had two compounding
            # bugs:
            #   (a) Asian stations at 09:00 local = 00:00 UTC were read as
            #       "yesterday" and silently bumped +1 day.
            #   (b) A D+0 contract at 06:00 local (10+h of real forecast
            #       horizon) and the same contract at 20:00 local (daily max
            #       already observed) received identical lead_hours.
            # Plus an off-by-one `+ 1` that shifted every contract one bucket
            # beyond its true horizon, routing D+2 to the 72h bucket that has
            # no calibration data. Observed fallout: Apr 19 2026 Mexico City
            # 27°C+ contract for Apr 21 priced at 72.7% model vs 20% market
            # because the 72h bucket hit the √(lead/24) σ-scaling fallback
            # against a hotter 72h forecast.
            try:
                tz = ZoneInfo(station.timezone)
            except Exception:
                gate_rejects["missing_timezone"] += len(date_contracts)
                continue
            now_local = datetime.now(tz)
            settlement_local = datetime.combine(
                target_date, _LOCK_IN_LOCAL, tzinfo=tz
            )
            raw_lead_h = (settlement_local - now_local).total_seconds() / 3600.0

            # Past-settlement: observation already captured. No model edge
            # possible; any price movement is late-info / noise trading.
            if raw_lead_h <= 0:
                gate_rejects["past_settlement"] += len(date_contracts)
                continue
            # Apr 25 2026 — two-tier close-to-settlement gate.
            # Hard floor at 3h: observation window dominates, never trade.
            # Between 3h and 6h: allow as "late entry" but apply stricter
            # downstream gates (see _LATE_ENTRY_* constants above). The
            # flag flows into the per-bracket signal loop below.
            if raw_lead_h < _LATE_ENTRY_MIN_LEAD_H:
                gate_rejects["too_close_to_settlement"] += len(date_contracts)
                continue
            is_late_entry = raw_lead_h < _LATE_ENTRY_GATE_LEAD_H
            # Apr 25 2026 — Hard cap LOWERED to 30h (was 48h). User decision:
            # only trade today/tomorrow contracts (lead ≤ 24h, plus a 6h timezone
            # buffer). The 48h bucket has known weaker calibration:
            #   - Quantile pinball at 48h was 0.27-0.73 vs 0.05-0.12 at 24h
            #     (training metrics, Apr 25 2026)
            #   - Conformal widening at 48h reached 5.99°F (Toronto), 5.44°F
            #     (Seoul) — equivalent to ±3σ uncertainty bands
            #   - Only n=192 real-forecast samples per (city, 48h) bucket vs
            #     n=1478 at 24h, so 48h fits are noisy
            #   - Walk-forward Brier skill: 24h +0.479 vs 48h +0.421 (12% worse)
            # Better to skip the bucket entirely than trade with weaker priors.
            # Revisit when we have more real-forecast 48h samples accumulated.
            if raw_lead_h > 30.0:
                gate_rejects["beyond_calibration_horizon"] += len(date_contracts)
                continue

            lead_hours = _bucket_lead(raw_lead_h)

            cache_key = (city, lead_hours)
            # Fetch latest model forecasts at the correct horizon for this contract.
            if cache_key not in forecast_cache:
                fetched = self.grib.fetch_all_models(station, lead_hours=lead_hours)
                if not fetched:
                    print(
                        f"[weather] No forecasts available for {city} @ {lead_hours}h lead, skipping.",
                        file=sys.stderr,
                    )
                    forecast_cache[cache_key] = []
                else:
                    forecast_cache[cache_key] = fetched
            forecasts = forecast_cache[cache_key]
            if not forecasts:
                no_forecast_contracts += len(date_contracts)
                continue

            # HRRR/NAM inference gate: these short-range models are designed for
            # lead horizons up to ~18h (HRRR) / ~36h (NAM). Beyond that Open-Meteo
            # returns degraded or sentinel values (observed Apr 19 2026:
            # NYC/Chicago/Toronto/Atlanta HRRR at 48h lead were 14-28°F too cold
            # vs GFS/ECMWF agreement, contaminating the 0.10-weighted ensemble
            # vote and crushing p_model for borderline brackets). GFS+ECMWF carry
            # the signal past 36h; at weight-0.10 the short-range models provide
            # no marginal information at those leads anyway.
            if lead_hours > 36:
                kept: list[Any] = []
                for fc in forecasts:
                    if fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                        hrrr_dropped_city_leads.add((city, lead_hours))
                        print(
                            f"[weather] Dropping {fc.model.value} for {city} at "
                            f"{lead_hours}h lead (beyond short-range horizon, "
                            f"value={fc.forecast_high_f:.1f}°F).",
                            file=sys.stderr,
                        )
                        continue
                    kept.append(fc)
                forecasts = kept
                if not forecasts:
                    no_forecast_contracts += len(date_contracts)
                    continue

            # Content hash of the post-HRRR-drop forecast set. Stamped on every
            # trade plan downstream so run_rebalance can detect whether the
            # Open-Meteo response has changed since entry (fresh info → tighter
            # exit threshold) vs stayed the same (market-only drift → looser
            # exit threshold). Must match the rebalance-side ordering: compute
            # AFTER HRRR/NAM drop so the same city+lead+model set is hashed
            # at entry and at rebalance time.
            if cache_key not in forecast_hash_cache:
                from polymarket_strat.domain.weather.forecast import (
                    forecast_content_hash,
                )
                forecast_hash_cache[cache_key] = forecast_content_hash(forecasts)
            content_hash_for_bracket = forecast_hash_cache[cache_key]

            # Classify regime — prefer ensemble-based (82 members) over heuristic
            try:
                ens_stats = self.grib.fetch_ensemble_spread_stats(
                    station, target_date=target_date
                )
                if ens_stats.get("n_members", 0) >= 3:
                    regime = self.regime_clf.classify_from_ensemble(
                        spread_f=ens_stats["spread"],
                        std_f=ens_stats["std"],
                        skewness=ens_stats["skewness"],
                        cape_max=ens_stats["cape_max"],
                        n_members=ens_stats["n_members"],
                    )
                else:
                    raise ValueError("too few members")
            except Exception:
                # Fallback to heuristic spread-based classification
                spreads = [fc.ensemble_spread_f for fc in forecasts if fc.ensemble_spread_f > 0]
                max_spread = max(spreads) if spreads else 0.0
                regime = self.regime_clf.classify_from_spread(model_spread_f=max_spread)

            # Apr 24 2026 (Citadel Q5-A, per-city update) — derive target
            # season from (target_date, city) via climate-aware schedule.
            # SH cities get flipped calendar, tropical cities get 2-season
            # wet/dry partition. Within-city consistent across training
            # and inference.
            from polymarket_strat.domain.weather.season import season_from_date
            target_season = season_from_date(target_date, city)

            # Get calibrated error distributions, with fallback to STABLE_HIGH
            # if the current regime hasn't been calibrated yet.
            def _load_dists(r: SynopticRegime) -> tuple[list[ErrorDistribution], list[Any]]:
                dists, fcs = [], []
                for fc in forecasts:
                    fc_bucket = _bucket_lead(fc.lead_hours)
                    # Try season-specific fit first. If it's missing (no
                    # row) or n_samples too thin (checked below), fall
                    # back to the pooled fit.
                    dist = self.db.get_error_distribution(
                        city, fc.model, r, fc_bucket, season=target_season
                    )
                    if dist is None or dist.n_samples < _N_MIN_FOR_TRADING:
                        pooled = self.db.get_error_distribution(
                            city, fc.model, r, fc_bucket, season=-1
                        )
                        if pooled is not None and pooled.n_samples >= _N_MIN_FOR_TRADING:
                            dist = pooled
                    # HRRR/NAM share gfs_seamless in the archive so we calibrate only GFS.
                    # At inference we use live HRRR/NAM forecasts (real independent signal)
                    # but borrow the GFS error distribution as the uncertainty estimate.
                    if dist is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                        dist = self.db.get_error_distribution(
                            city, WeatherModel.GFS, r, fc_bucket, season=target_season
                        )
                        if dist is None or dist.n_samples < _N_MIN_FOR_TRADING:
                            dist = self.db.get_error_distribution(
                                city, WeatherModel.GFS, r, fc_bucket, season=-1
                            )
                    # Lead-hours safety net: calibration now fetches 24h + 48h
                    # previous-runs forecasts and stores per-lead forecast_errors,
                    # so the direct lookup above should hit for today (24h) and
                    # tomorrow (48h) contracts. This √(lead/24) σ-scaling fallback
                    # only fires if (a) calibration hasn't been re-run since the
                    # multi-lead fix, (b) the 48h previous-runs fetch failed for
                    # this city/model, or (c) a 72h+ contract is in scope (not yet
                    # on the calibration schedule). Random-walk growth approximation
                    # is the standard ansatz for well-calibrated NWP error.
                    if dist is None and fc_bucket != 24:
                        # Season-aware fallback: try season-specific 24h first,
                        # then pooled 24h. Scaled by √(lead/24) for the longer
                        # bucket under random-walk assumption.
                        base = self.db.get_error_distribution(
                            city, fc.model, r, 24, season=target_season
                        )
                        if base is None or base.n_samples < _N_MIN_FOR_TRADING:
                            base = self.db.get_error_distribution(
                                city, fc.model, r, 24, season=-1
                            )
                        if base is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                            base = self.db.get_error_distribution(
                                city, WeatherModel.GFS, r, 24, season=-1
                            )
                        if base is not None:
                            scale = (fc_bucket / 24.0) ** 0.5
                            dist = ErrorDistribution(
                                city=base.city,
                                model=base.model,
                                regime=base.regime,
                                lead_hours=fc_bucket,
                                family=base.family,
                                mu=base.mu,
                                sigma=base.sigma * scale,
                                shape=base.shape,
                                nu=base.nu,
                                n_samples=base.n_samples,
                            )
                    if dist is None:
                        continue
                    # Skip outlier distributions: large bias (|μ|>5°F) or extreme spread
                    # (σ>5°F) indicate poor calibration data (marine layer artifacts,
                    # IEM station gaps, urban microclimate effects not resolved by 25km models).
                    if abs(dist.mu) > 5.0 or dist.sigma > 5.0:
                        print(
                            f"[weather] Skipping outlier dist {city}/{fc.model.value}: "
                            f"μ={dist.mu:.2f}°F σ={dist.sigma:.2f}°F",
                            file=sys.stderr,
                        )
                        continue
                    # Apr 24 2026 (Citadel Q1) — hard sample-size gate.
                    # Distributions with n_samples < _N_MIN_FOR_TRADING are
                    # statistically noisy (σ CI > ±40% at n<30). Drop them
                    # here so the caller's STABLE_HIGH fallback kicks in
                    # with higher-n data if available, or the whole bracket
                    # skips if even the pooled fallback is thin.
                    if dist.n_samples < _N_MIN_FOR_TRADING:
                        continue
                    dists.append(dist)
                    fcs.append(fc)
                return dists, fcs

            error_dists, matching_forecasts = _load_dists(regime)
            if not error_dists and regime != SynopticRegime.STABLE_HIGH:
                # Regime-specific data not yet calibrated OR all distributions
                # failed the sample-size gate; fall back to stable_high which
                # typically has the largest sample count.
                error_dists, matching_forecasts = _load_dists(SynopticRegime.STABLE_HIGH)

            if not error_dists:
                no_dists_contracts += len(date_contracts)
                gate_rejects["insufficient_samples"] += len(date_contracts)
                print(
                    f"[weather] No calibrated distributions for {city} (tried {regime.value} + stable_high). "
                    f"Run: polymarket-strat weather-calibrate --cities {city}",
                    file=sys.stderr,
                )
                continue

            # Apr 24 2026 (Tier 2c) — data-driven bucket-level EV auto-block.
            # If trade_history says this (city, lead, regime) has been a net
            # loser over n >= N_MIN settled trades, short-circuit before the
            # bracket-pricing loop. Distinct from _BLOCKED_CITIES which is a
            # manual structural block; this is automatic and refit nightly.
            blocked, block_reason = bucket_blocklist.is_blocked(
                city=city,
                lead_hours=int(lead_hours),
                regime=regime.value,
            )
            if blocked:
                gate_rejects["bucket_ev_blocked"] += len(date_contracts)
                continue

            # Price all brackets for this (city, date) independently.
            #
            # Apr 24 2026 (Citadel-review fix #1): market_prob for the edge
            # gate is the LIVE best_ask, not Polymarket's last-trade price
            # from `outcomePrices[0]`. Rationale:
            #   - `outcomePrices[0]` is stale (updates only on fills) and is
            #     roughly mid; on a 6¢-spread market our real entry is at
            #     best_ask = mid + 3¢. Using mid overstates edge by half the
            #     spread on every trade.
            #   - edge() is supposed to answer "if I enter at the available
            #     price, do I still have positive EV after fees?". The only
            #     honest answer is `p_model - best_ask`.
            # Fall back to market_price_yes when best_ask is missing or 0 so
            # this change is strictly additive (won't wipe out signals on
            # legacy rows where best_ask wasn't captured).
            brackets = [(c.lower_f, c.upper_f) for c in date_contracts]
            market_probs = [
                c.best_ask_yes if (c.best_ask_yes and 0 < c.best_ask_yes < 1)
                else c.market_price_yes
                for c in date_contracts
            ]

            priced = self.calc.price_all_brackets(
                forecasts=matching_forecasts,
                error_dists=error_dists,
                brackets=brackets,
                market_probs=market_probs,
                fee_rate=self.fee_rate,
                regime=regime,
            )

            # Apr 25 2026 — Layer 1 ML pricing override.
            # When `constraints.use_quantile_pricing` is True AND we have
            # a trained quantile model for this (city, lead), override
            # bp.model_prob with the quantile-based estimate. The override
            # is per-bracket and per-side-agnostic (we still recompute
            # both YES and NO sides downstream from the new model_prob).
            #
            # Falls back to parametric silently when:
            #   - Feature flag is off
            #   - No trained model for this (city, lead)
            #   - The quantile pricer raises an exception
            # So this addition is strictly safe — at worst, behavior is
            # unchanged from pre-Layer-1 code.
            if getattr(constraints, "use_quantile_pricing", False):
                try:
                    from polymarket_strat.domain.weather.quantile_pricing import (
                        get_quantile_pricer,
                    )
                    qpricer = get_quantile_pricer()
                    if qpricer.has_model(city, int(lead_hours)):
                        for bp_idx, bp in enumerate(priced):
                            # Compute bracket prob via quantile model for
                            # each contributing forecast then average via
                            # the same skill weights used in parametric.
                            ml_probs = []
                            ml_weights = []
                            for fc in matching_forecasts:
                                w = self.calc.weights.get(fc.model, 0.1)
                                # Apr 25 2026 (post-ECE measurement): conformal
                                # OFF in production. Aggregate ECE (raw)=5.44%
                                # beats the 7% target; conformal (10.61%) hurts
                                # because the quantile model is already well-
                                # calibrated and widening pushes probabilities
                                # too far in the "inside bracket" direction.
                                # Re-enable only if a future ECE measurement
                                # shows raw drifting > 7% — conformal then
                                # provides the safety net it was designed for.
                                p = qpricer.bracket_probability(
                                    city=city, model=fc.model.value,
                                    forecast_high_f=fc.forecast_high_f,
                                    obs_date=target_date,
                                    lead_hours=int(lead_hours),
                                    regime=regime.value,
                                    lower_f=bp.lower_f, upper_f=bp.upper_f,
                                    ensemble_spread_f=fc.ensemble_spread_f,
                                    apply_conformal=False,
                                )
                                if p is not None:
                                    ml_probs.append(p)
                                    ml_weights.append(w)
                            if ml_probs and sum(ml_weights) > 0:
                                ml_avg = sum(p * w for p, w in zip(ml_probs, ml_weights)) / sum(ml_weights)
                                bp.model_prob = float(ml_avg)
                                # Mark this bp as quantile-priced for
                                # diagnostics (used in metadata below).
                                if hasattr(bp, "metadata"):
                                    bp.metadata["pricer"] = "quantile_v1"
                except Exception as exc:
                    print(
                        f"[strategy] Quantile pricer override failed for {city} "
                        f"@{lead_hours}h: {exc!r}; falling back to parametric.",
                        file=sys.stderr,
                    )

            # Emit signals and plans for tradeable edges
            group_key = station.correlation_group.value
            # Effective min_edge for this cycle: the strategy-level threshold
            # (self.min_edge) and the constraints-level threshold
            # (constraints.min_edge_flat) should normally agree at 5¢, but
            # we take the tighter of the two so either knob can raise the bar.
            effective_min_edge = max(self.min_edge, constraints.min_edge_flat)
            for bp, contract in zip(priced, date_contracts):
                # Apr 24 2026 (Citadel fix #5) — evaluate BOTH buy-YES and
                # buy-NO sides for each bracket. Polymarket weather markets
                # are binary: token_yes pays $1 if the bracket resolves TRUE,
                # token_no pays $1 otherwise. For any bracket where our model
                # and the market disagree, edge exists on exactly ONE side
                # (the sides are mutually exclusive by construction:
                # `(p_model - p_market)_yes = -(p_model - p_market)_no`).
                #
                # Before this fix we only scanned BUY-YES, which silently
                # dropped "market overprices YES → buy NO" opportunities.
                # The Apr 24 production log showed 252 / 519 contracts (48%)
                # rejected by the market_band [0.15, 0.75] gate on the YES
                # side — a substantial fraction of those are the NO-side
                # opportunities we were missing (market YES price < 0.15
                # means market NO price > 0.85, which is still outside the
                # band but closer to it, and rules still apply symmetrically).
                #
                # For NO side we approximate best_ask_no and best_bid_no
                # from the YES-side orderbook via the no-arbitrage relation:
                #     best_ask_no ≈ 1 - best_bid_yes
                #     best_bid_no ≈ 1 - best_ask_yes
                # Polymarket's two orderbooks drift slightly (~1¢ at worst),
                # so this introduces small entry-price noise. A cleaner fix
                # would be to fetch each NO token's CLOB book separately —
                # future work once we've validated NO-side edges are real.

                # Compute ensemble spread (max across model members) up-front
                # so Strategy 3 confidence gate can reference it. Used both
                # in the gate logic and persisted to signal metadata below.
                max_ensemble_spread = 0.0
                for fc in matching_forecasts:
                    max_ensemble_spread = max(
                        max_ensemble_spread, float(fc.ensemble_spread_f or 0.0)
                    )

                yes_side = {
                    "label": "YES",
                    "model_prob": bp.model_prob,
                    "market_prob": bp.market_prob,  # already best_ask_yes per fix #1
                    "entry_price": contract.best_ask_yes,
                    "best_bid_for_exit": contract.best_bid_yes,
                    "token_id": contract.token_id_yes,
                }
                # Apr 26 2026 — fix #2: NO-side post-hoc isotonic
                # calibration. The YES-side `bp.model_prob` is already
                # corrected by the YES isotonic curve fit from synthetic
                # walk-forward outcomes, but NO-side has its own failure
                # mode (narrow-bracket bleed on real settled trades) that
                # the YES curve doesn't see. Apply a separate NO-side
                # curve fit from `trade_history` itself
                # (scripts/fit_no_isotonic.py). Identity fallback when
                # the curve isn't loaded — never makes things worse.
                from polymarket_strat.domain.weather.forecast import (
                    _get_isotonic_calibrator,
                )
                _no_calibrator = _get_isotonic_calibrator()
                _raw_p_no = 1.0 - bp.model_prob
                _bracket_width_f = float(bp.upper_f - bp.lower_f)
                _calibrated_p_no = _no_calibrator.calibrate_no(
                    _raw_p_no, bracket_width_f=_bracket_width_f
                )

                no_side = {
                    "label": "NO",
                    "model_prob": _calibrated_p_no,
                    "model_prob_raw_no": _raw_p_no,  # for diagnostics
                    # best_ask_no ≈ 1 - best_bid_yes (the "ask" on NO is the
                    # complement of the "bid" on YES under no-arb).
                    "market_prob": max(
                        0.0, min(1.0, 1.0 - contract.best_bid_yes)
                    ),
                    "entry_price": max(
                        0.0, min(1.0, 1.0 - contract.best_bid_yes)
                    ),
                    "best_bid_for_exit": max(
                        0.0, min(1.0, 1.0 - contract.best_ask_yes)
                    ),
                    "token_id": contract.token_id_no,
                }

                # Pick the side with the better (larger) fee-adjusted edge.
                # Both pass `self.calc.edge` independently. At most one can
                # be tradeable due to the mutual-exclusivity of the sides.
                side_picks: list[dict[str, Any]] = []
                for side in (yes_side, no_side):
                    if not side["token_id"]:
                        # NO token_id missing (some legacy BracketContract
                        # rows don't populate token_id_no). Skip NO in that
                        # case; YES always has a token_id.
                        continue
                    raw_edge, adj_edge, tradeable = self.calc.edge(
                        model_prob=side["model_prob"],
                        market_prob=side["market_prob"],
                        fee_rate=self.fee_rate,
                        min_edge=effective_min_edge,
                    )
                    side["raw_edge"] = raw_edge
                    side["edge_after_fees"] = adj_edge
                    side["is_tradeable"] = tradeable
                    if tradeable:
                        side_picks.append(side)

                if not side_picks:
                    # Neither side passes the gate. Attribute the rejection
                    # to whichever side came CLOSER to passing — typically
                    # YES — so the diagnostics histogram reflects the same
                    # behavior as pre-fix-5 runs (when YES was the only
                    # side). For tie-breaking, take the side with higher
                    # (less-negative) adjusted edge.
                    primary = max(
                        (yes_side, no_side),
                        key=lambda s: s.get("edge_after_fees", -1.0),
                    )
                    if primary["market_prob"] < 0.15 or primary["market_prob"] > 0.75:
                        gate_rejects["gate1_market_band"] += 1
                    elif primary["model_prob"] < 0.50:
                        gate_rejects["gate2_min_edge_low_p"] += 1
                    else:
                        gate_rejects["gate2_min_edge"] += 1
                    continue

                # Pick the best-edge side (only one in practice).
                picked = max(side_picks, key=lambda s: s["edge_after_fees"])
                token_side = picked["label"]

                # Apr 24 2026 — rename of legacy variables so the block
                # below can use the side-agnostic view. `bp.market_prob`,
                # `bp.model_prob`, etc. still reflect YES; we override with
                # picked-side values when threading into signal/plan.
                side_model_prob = picked["model_prob"]
                side_market_prob = picked["market_prob"]
                side_entry_price = picked["entry_price"]
                side_token_id = picked["token_id"]
                side_edge_after_fees = picked["edge_after_fees"]

                # Skip near-zero prices: unrealistic fills and 50x+ implied leverage.
                # Applied to the SIDE entry price so NO-side trades with
                # market_no < 0.02 (i.e. YES-side nearly 1.0) are also filtered.
                if side_entry_price < constraints.min_entry_price:
                    gate_rejects["min_entry_price"] += 1
                    continue

                # Apr 25 2026 — PLAN B emergency artifact cap.
                # Reject any side where p_model > 0.80 AND edge > 0.15
                # (TIGHTENED Apr 26 from 0.85/0.20).
                # See _PLAN_B_HIGH_P_CAP comment for rationale. This catches
                # bracket-parsing artifacts (Toronto p=1.00 / m=0.67) and
                # calibration σ blow-ups while preserving legitimate
                # high-conviction signals where market broadly agrees
                # (small edge implies near-correct market price).
                if (
                    side_model_prob > _PLAN_B_HIGH_P_CAP
                    and side_edge_after_fees > _PLAN_B_HIGH_P_EDGE_TRIGGER
                ):
                    gate_rejects["plan_b_high_p_artifact"] += 1
                    continue

                # Apr 26 2026 — NARROW-BRACKET NO-SIDE CAP.
                # On 1°C-wide Polymarket contracts (≈ 1.8°F width), the
                # bracket P math is hyper-sensitive to σ estimation
                # errors. NO-side losses concentrate in [0.77, 0.92] on
                # exactly these bracket widths. Refuse NO entries when
                # bracket width < 2°F AND model_prob > 0.75. YES-side
                # is unaffected (different failure mode — see honest_ece
                # report). When σ tightens further or isotonic is wired
                # into NO side, this cap can be loosened or removed.
                bracket_width_f = float(bp.upper_f - bp.lower_f)
                if (
                    token_side == "NO"
                    and bracket_width_f < _NARROW_BRACKET_WIDTH_F
                    and side_model_prob > _NARROW_BRACKET_NO_MAX_P
                ):
                    gate_rejects["narrow_bracket_no_cap"] += 1
                    continue

                # Apr 25 2026 (LATE) — ABSURD-EDGE CAP. Real Polymarket
                # weather edges top out around 15-20¢ after fees. Anything
                # > 50¢ after fees is a category-error trade — likely the
                # quantile pricer was asked about a market type it wasn't
                # trained for (e.g. "lowest temperature" markets when our
                # model is purely for daily HIGH temp). Triggered today by
                # London "lowest temperature in London = 5°C" → fake
                # +$7489 settlement. Defense in depth: market_scanner.py
                # filters lowest-temp markets, but this is a model-level
                # safety net for any other "model says 0% / market says
                # something" mismatch.
                if abs(side_edge_after_fees) > 0.50:
                    gate_rejects["absurd_edge_cap"] += 1
                    continue

                # Apr 25 2026 — STRATEGY 3: ensemble-confidence-weighted
                # blend. Pull the ensemble spread from the contributing
                # forecasts (already plumbed via Tier 3a). Compute model
                # confidence and blend p_model with p_market.
                #
                # Per-forecast ensemble_spread_f is range (max - min). For
                # σ-equivalent we divide by ~4 (rule for Gaussian-ish
                # ensembles). Take the MAX across models for conservative
                # estimate — if any single model's ensemble disagrees, we
                # treat the whole signal as low-confidence.
                ensemble_std_f = max_ensemble_spread / 4.0 if max_ensemble_spread > 0 else 0.0
                model_confidence_s3 = math.exp(-ensemble_std_f / _S3_SIGMA_DECAY)
                # w² collapses fast when confidence drops — at confidence
                # 0.5, w=0.25, market gets 75% weight.
                w_blend = model_confidence_s3 ** 2
                p_blended = (
                    w_blend * side_model_prob
                    + (1.0 - w_blend) * side_market_prob
                )
                # Blended edge after fees (same fee model as raw edge).
                blended_raw = p_blended - side_market_prob
                blended_edge = (
                    blended_raw
                    - self.fee_rate * p_blended * (1.0 - side_market_prob)
                )

                # Gate 1: minimum confidence floor — if models disagree
                # by more than 6°F (σ_eq > 1.5°F → confidence < 0.60),
                # we have no business overriding the market.
                if model_confidence_s3 < _S3_MIN_CONFIDENCE:
                    gate_rejects["s3_low_model_confidence"] += 1
                    continue
                # Gate 2: blended edge threshold — must clear 8¢ AFTER
                # the market-following adjustment. This automatically
                # filters out raw-edge artifacts where the model
                # disagreed wildly but ensemble confirmed it was noise.
                if blended_edge < _S3_MIN_BLENDED_EDGE:
                    gate_rejects["s3_blended_edge_too_small"] += 1
                    continue
                # Bookkeeping: record blended values on the side dict so
                # downstream (signal metadata, kelly recompute) can use
                # them. We DON'T overwrite side_model_prob — that stays
                # raw for posthoc analysis. Blended values are the
                # decision basis going forward.
                picked["model_confidence"] = round(model_confidence_s3, 4)
                picked["w_blend"] = round(w_blend, 4)
                picked["p_blended"] = round(p_blended, 4)
                picked["blended_edge"] = round(blended_edge, 4)

                # Apr 25 2026 — late-entry high-conviction override.
                # Contracts with 3h ≤ raw_lead_h < 6h were already flagged
                # `is_late_entry=True` at the city-level gate. Here we
                # apply the SIDE-specific conviction test: the side we
                # picked must show an extreme-conviction p_model (≥ 0.80
                # or ≤ 0.20) AND a large edge (≥ 20¢). Without both we
                # don't trust the signal enough to overcome the
                # observation-info disadvantage.
                #
                # Why extreme-p matters: at T-4h, the market is partially
                # informed by live station obs. If our model still says
                # 0.85 on a bracket and the station has been tracking
                # toward it all day, that's bracket-geometry edge
                # (forecast is clearly inside or outside the observed
                # envelope). A mid-p signal at T-4h more likely means we
                # don't know what's going on yet — the market does.
                #
                # Trades that pass this gate get `late_entry=True` tagged
                # on the signal metadata and half-sized below. That lets
                # us measure their performance separately after 2 weeks.
                if is_late_entry:
                    is_extreme_p = (
                        side_model_prob >= _LATE_ENTRY_MIN_P_HIGH
                        or side_model_prob <= _LATE_ENTRY_MAX_P_LOW
                    )
                    if not is_extreme_p:
                        gate_rejects["late_entry_not_extreme"] += 1
                        continue
                    if side_edge_after_fees < _LATE_ENTRY_MIN_EDGE:
                        gate_rejects["late_entry_insufficient_edge"] += 1
                        continue

                # Compute reliability multiplier now so we can log it on the
                # signal even before the position-sizing step reads it.
                reliability_mult_preview, reliability_diag_preview = (
                    city_reliability.multiplier(city=city, lead_hours=int(lead_hours))
                )

                # Apr 24 2026 (fix #5) — recompute Kelly fraction for the
                # picked side. `bp.kelly_fraction` was computed for the YES
                # side only in price_all_brackets(); on a NO-side pick we
                # need p=(1-p_yes) and market=(1-best_bid_yes). The
                # uncertainty shrinkage (CV²) stays symmetric under the flip.
                if token_side == "YES":
                    side_kelly = bp.kelly_fraction
                else:
                    side_kelly_raw, _shrink = self.calc.kelly_fraction(
                        model_prob=side_model_prob,
                        market_prob=side_market_prob,
                        prob_std=max(0.0, getattr(bp, "prob_std", 0.05)),
                        fee_rate=self.fee_rate,
                        quarter_kelly=True,
                    )
                    side_kelly = side_kelly_raw

                # Apr 24 2026 (Citadel Q4 + data expansion) — extract rich
                # per-model diagnostics for posthoc analysis. Every trade
                # gets stamped with what GFS said, what ECMWF said, what
                # ensemble spread was, which model init_time we used, etc.
                # Nullable in the signal metadata — `run_execute` reads
                # these and persists them into trade_history columns.
                per_model_forecasts = {}
                per_model_init_times = {}
                # max_ensemble_spread already computed above at side-pick
                # time so Strategy 3 confidence gate could reference it.
                # Reuse the same value here for metadata.
                for fc in matching_forecasts:
                    model_key = fc.model.value
                    per_model_forecasts[model_key] = float(fc.forecast_high_f)
                    # init_time is the timestamp of the model run we traded
                    # against. Useful for answering "was this trade on the
                    # 00Z or 12Z GFS run?" when diagnosing a bad call.
                    init_iso = fc.init_time.isoformat() if fc.init_time else None
                    per_model_init_times[model_key] = init_iso
                    max_ensemble_spread = max(max_ensemble_spread, float(fc.ensemble_spread_f or 0.0))

                signal = StrategySignal(
                    market=contract.market_id,
                    question=contract.question,
                    category="weather",
                    outcome=f"{bp.lower_f:.0f}-{bp.upper_f:.0f}F",
                    side=f"BUY_{token_side}",
                    signal_score=side_edge_after_fees * 20,
                    reference_price=side_market_prob,
                    metadata={
                        "city": city,
                        "regime": regime.value,
                        "token_side": token_side,
                        "model_prob": round(side_model_prob, 4),
                        "model_prob_yes": round(bp.model_prob, 4),  # always log YES for reference
                        "edge_after_fees": round(side_edge_after_fees, 4),
                        "kelly_fraction": round(side_kelly, 4),
                        "shrinkage": round(bp.uncertainty_shrinkage, 4),
                        "contributing_models": [m.value for m in bp.contributing_models],
                        "token_id": side_token_id,
                        "bracket_lower_f": bp.lower_f,
                        "bracket_upper_f": bp.upper_f,
                        "target_date": contract.target_date.isoformat(),
                        # Stamped on every plan so run_rebalance can look up
                        # the entry-time hash without re-computing it from a
                        # potentially-refreshed forecast response.
                        "forecast_content_hash": content_hash_for_bracket,
                        # Apr 24 2026 — reliability diagnostics so the
                        # dashboard can show why a trade got its size.
                        "reliability_multiplier": round(reliability_mult_preview, 4),
                        "reliability_diag": reliability_diag_preview,
                        # Apr 24 2026 (Citadel Q4) — per-model forecast
                        # diagnostics. run_execute unpacks these into
                        # trade_history columns for posthoc analysis.
                        "forecast_high_f_per_model": per_model_forecasts,
                        "init_time_per_model": per_model_init_times,
                        "ensemble_spread_f": round(max_ensemble_spread, 2),
                        "season": target_season,
                        # Apr 25 2026 — late-entry override diagnostic. If
                        # True, this trade fired in the 3-6h lead window
                        # under the conviction gate (edge ≥ 20¢ AND p in
                        # extreme tails). Sized at 0.5x normal. Track in
                        # trade_history for empirical win-rate validation.
                        "late_entry": is_late_entry,
                        "raw_lead_hours": round(raw_lead_h, 2),
                    },
                )
                signals.append(signal)

                # Position sizing: quarter-Kelly × CV² shrinkage (already baked
                # into bp.kelly_fraction) capped at
                #   max(bankroll * max_position_fraction, min_position_notional).
                # At $500 bankroll → $25 cap, at $1k → $50, at $5k → $250.
                # The floor prevents the cap from degenerating to <$10 on very
                # small bankrolls (where Polymarket's order-size mechanics bite).
                #
                # Apr 24 2026 — layered on top: reliability-weighted shrinkage
                # per §15.3.1 (Tier 2b of Apr 24 dev plan). Formula:
                #   multiplier = min(1, 0.12/city_brier) × min(1, n_samples/50)
                # Effect on cities:
                #   Wellington (Brier 0.098, n=732) → 1.0 × 1.0 = 1.0x sizing
                #   NYC        (Brier 0.149, n=1466) → 0.81 × 1.0 = 0.81x
                #   Toronto    (Brier 0.132, n=??)  → 0.91 × ... = ~0.9x
                #   A hypothetical new-city at n=25 → multiplier×0.5 from samples
                # Graceful fallback: if no reliability.json file, multiplier=1.0
                # so pre-reliability code behavior is preserved.
                reliability_mult, reliability_diag = city_reliability.multiplier(
                    city=city,
                    lead_hours=int(lead_hours),
                )
                # Apr 24 2026 (fix #5) — use side-specific Kelly (recomputed
                # for NO-side trades above), not bp.kelly_fraction which is
                # always YES-side.
                # Apr 25 2026 — late-entry half-sizing. Trades in the 3-6h
                # window have reduced information vs full-lead trades
                # (observation window is open, we don't have nowcast).
                # Even when the conviction gate passes, halving size
                # encodes the uncertainty and caps downside if the
                # override turns out to be noise after paper data.
                late_size_mult = _LATE_ENTRY_SIZE_MULT if is_late_entry else 1.0
                target_notional = min(
                    side_kelly * constraints.bankroll * reliability_mult * late_size_mult,
                    per_position_cap,
                )
                if target_notional < constraints.min_order_size:
                    gate_rejects["min_order_size"] += 1
                    continue

                # Correlation-group notional cap: reject if this new trade
                # would push group $-exposure past 15% of bankroll. Tracked
                # per-cycle; persistent cross-cycle group exposure is handled
                # by the generic PortfolioRiskManager at fill-time.
                if group_notional[group_key] + target_notional > group_notional_cap:
                    gate_rejects["group_cap"] += 1
                    continue

                # Apr 24 2026 (Citadel fix #3) — liquidity depth gate.
                #
                # On Polymarket weather markets, middle brackets often have
                # <$300 aggregate liquidity and best_ask that only covers
                # a handful of shares before walking the book up 3-5¢. Our
                # TradePlan previously set top_ask_size=0 (unknown), which
                # meant execution would blindly send a $15 order against a
                # book that only had $3 available at top — rest would fill
                # 3-5¢ worse and nuke the edge.
                #
                # Gate logic:
                #   - If `top_ask_size > 0` (came from a CLOB book fetch),
                #     require `target_notional ≤ 0.5 × top_ask_size × best_ask`.
                #     The 0.5x multiplier reserves half the top-of-book as
                #     buffer against a simultaneous fill from another bot.
                #   - If `top_ask_size == 0` (unknown — no CLOB fetch yet),
                #     fall back to the aggregate `liquidity` field from
                #     Polymarket Gamma. Require `target_notional ≤ 0.15 ×
                #     liquidity`. Heuristic calibrated from observing that
                #     `liquidity` is typically 5-10x the top-of-book depth
                #     on weather markets, so 0.15 of aggregate ≈ 0.75-1.5
                #     of top-of-book. Still conservative vs. the 0.5x top
                #     rule, but avoids hard-zero-gating every contract
                #     whose book we haven't fetched yet.
                #   - If BOTH are zero (unknown top size AND unknown
                #     liquidity — shouldn't happen with Gamma's response
                #     but possible), permit the trade and flag it in
                #     diagnostics. Fail-open avoids total signal loss on
                #     API hiccup.
                # Depth gate uses the picked side's entry price. For YES we
                # enter at best_ask_yes (known). For NO the synthetic entry
                # price is (1 - best_bid_yes); liquidity at that level comes
                # from the YES-side best_bid depth (which is what we'd be
                # hitting, in inverse). Approximated via aggregate
                # `contract.liquidity` in the fallback path.
                top_ask_notional_usd = contract.top_ask_size * side_entry_price
                if top_ask_notional_usd > 0:
                    # We have real L2 depth — strict gate
                    max_depth_notional = 0.5 * top_ask_notional_usd
                    if target_notional > max_depth_notional:
                        gate_rejects["depth_top_of_book"] += 1
                        continue
                elif contract.liquidity > 0:
                    # Fall back to aggregate liquidity proxy
                    max_depth_notional = 0.15 * contract.liquidity
                    if target_notional > max_depth_notional:
                        gate_rejects["depth_aggregate_liquidity"] += 1
                        continue
                # else: both zero → permit (flagged by setting a sentinel
                # the dashboard can filter on; we don't gate aggressively
                # because it's likely a Gamma response anomaly, not a
                # genuinely illiquid book).

                plan_executable = contract.spread <= constraints.max_spread
                if not plan_executable:
                    gate_rejects["spread_too_wide"] += 1

                # Build side-appropriate best_ask / best_bid for the plan.
                # For NO-side, these are the synthetic prices derived from
                # the no-arb relationship on the YES-side orderbook.
                if token_side == "YES":
                    plan_best_ask = contract.best_ask_yes
                    plan_best_bid = contract.best_bid_yes
                else:
                    plan_best_ask = side_entry_price  # = 1 - best_bid_yes
                    plan_best_bid = picked["best_bid_for_exit"]  # = 1 - best_ask_yes

                plans.append(TradePlan(
                    strategy_name=self.name,
                    market=contract.market_id,
                    question=contract.question,
                    category="weather",
                    outcome=signal.outcome,
                    token_id=side_token_id,
                    side=f"BUY_{token_side}",
                    signal_score=signal.signal_score,
                    target_notional=round(target_notional, 2),
                    reference_price=side_market_prob,
                    best_ask=plan_best_ask,
                    best_bid=plan_best_bid,
                    spread=contract.spread,
                    top_ask_size=contract.top_ask_size,
                    top_bid_size=contract.top_bid_size,
                    risk_score=1.0 - bp.uncertainty_shrinkage,
                    expected_value=side_edge_after_fees,
                    executable=plan_executable,
                    rationale=[
                        f"BUY {token_side}: Model prob {side_model_prob:.1%} vs market {side_market_prob:.1%}",
                        f"Edge after fees: {side_edge_after_fees:.1%}",
                        f"Regime: {regime.value}, shrinkage: {bp.uncertainty_shrinkage:.2f}",
                    ],
                    metadata=signal.metadata,
                ))
                group_notional[group_key] += target_notional

        plans.sort(key=lambda p: (-p.expected_value, p.risk_score))
        return StrategyAnalysis(
            strategy_name=self.name,
            signals=signals,
            trade_plan=plans,
            diagnostics={
                "cities_scanned": len({city for city, _ in by_city_date}),
                "contracts_found": len(contracts),
                "signals_generated": len(signals),
                "plans_generated": len(plans),
                "executable_count": sum(1 for p in plans if p.executable),
                "correlation_group_notional": {
                    k: round(v, 2) for k, v in group_notional.items()
                },
                "per_position_cap": round(per_position_cap, 2),
                "group_notional_cap": round(group_notional_cap, 2),
                "gate_rejects": dict(gate_rejects),
                "no_forecast_contracts": no_forecast_contracts,
                "no_dists_contracts": no_dists_contracts,
                "hrrr_dropped_city_leads": [
                    f"{c}@{l}h" for c, l in sorted(hrrr_dropped_city_leads)
                ],
            },
        )

    def backtest(self) -> BacktestResult:
        """Backtest using historical forecasts, observations, and error distributions.

        Loads data from the SQLite DB.  You must run calibrate() first to
        populate forecast_errors and error_distributions.
        """
        trades: list[BacktestTrade] = []
        all_errors = self.db.get_trades(limit=5000)

        if not all_errors:
            return BacktestResult(
                strategy_name=self.name,
                trade_count=0, mean_return=0, median_return=0, win_rate=0,
                max_drawdown=0, expected_value=0, calibration_error=None,
                passed=False,
                diagnostics={"reason": "no_historical_trades_in_db"},
            )

        returns: list[float] = []
        for t in all_errors:
            pnl = t.get("pnl")
            notional = t.get("notional", 1)
            if pnl is not None and notional > 0:
                ret = pnl / notional
                returns.append(ret)
                trades.append(BacktestTrade(
                    market=t.get("city", ""),
                    side=t.get("side", "BUY"),
                    entry_price=t.get("market_prob", 0.5),
                    exit_price=t.get("market_prob", 0.5) + (pnl / max(notional, 1)),
                    expected_value=t.get("edge", 0),
                    pnl=pnl,
                    return_pct=ret,
                    metadata={"city": t.get("city"), "regime": t.get("regime")},
                ))

        if not returns:
            return BacktestResult(
                strategy_name=self.name,
                trade_count=0, mean_return=0, median_return=0, win_rate=0,
                max_drawdown=0, expected_value=0, calibration_error=None,
                passed=False,
                diagnostics={"reason": "no_settled_trades"},
            )

        max_dd = _max_drawdown(returns)
        mean_ret = statistics.fmean(returns)
        sharpe = (mean_ret / statistics.stdev(returns) * len(returns) ** 0.5) if len(returns) > 1 else 0

        return BacktestResult(
            strategy_name=self.name,
            trade_count=len(trades),
            mean_return=mean_ret,
            median_return=statistics.median(returns),
            win_rate=sum(1 for r in returns if r > 0) / len(returns),
            max_drawdown=max_dd,
            expected_value=mean_ret,
            calibration_error=None,
            passed=mean_ret > 0 and max_dd < 0.15 and sharpe > 1.0,
            diagnostics={"sharpe": round(sharpe, 3)},
            trades=trades,
        )

    # ------------------------------------------------------------------
    # Calibration pipeline
    # ------------------------------------------------------------------

    def calibrate(
        self,
        *,
        cities: list[str] | None = None,
        lookback_days: int = 365,
    ) -> dict[str, Any]:
        """Offline calibration: fetch observations, compute errors, fit distributions.

        1. For each city, fetch station observations for the lookback window.
        2. Load matching historical forecasts from DB.
        3. Compute forecast - observed errors.
        4. Group by (city, model, regime, lead_bucket).
        5. Fit error distributions via scipy MLE.
        6. Store fitted distributions in DB.

        Returns a summary of what was calibrated.
        """
        target_cities = cities or list(CITY_REGISTRY.keys())
        end = date.today() - timedelta(days=5)   # archive API has ~5-day lag
        start = end - timedelta(days=lookback_days)
        summary: dict[str, Any] = {"cities": {}, "total_distributions_fitted": 0}

        for city_key in target_cities:
            station = CITY_REGISTRY.get(city_key)
            if not station:
                continue

            print(f"[calibrate] {city_key}: fetching observations {start} to {end}...", file=sys.stderr)
            obs_list = self.stations.fetch_daily_highs(station, start=start, end=end)
            for obs in obs_list:
                self.db.save_observation(obs)
            obs_lookup = {obs.obs_date: obs.observed_high_f for obs in obs_list}

            if not obs_lookup:
                # IEM ASOS has limited coverage outside North America.
                # Fall back to ERA5 reanalysis (~0.3-0.5°C RMSE vs station).
                print(
                    f"[calibrate] {city_key}: IEM returned no data, "
                    f"falling back to ERA5 reanalysis.",
                    file=sys.stderr,
                )
                era5_highs = self.grib.fetch_era5_observations(station, start=start, end=end)
                if not era5_highs:
                    print(f"[calibrate] {city_key}: ERA5 also empty, skipping.", file=sys.stderr)
                    continue
                for obs_date, high_f in era5_highs.items():
                    era5_obs = StationObservation(
                        city=city_key,
                        station_id=station.station_id,
                        obs_date=obs_date,
                        observed_high_f=high_f,
                        source="ERA5",
                    )
                    self.db.save_observation(era5_obs)
                obs_lookup = era5_highs
                print(f"[calibrate] {city_key}: Using {len(era5_highs)} ERA5 observations.", file=sys.stderr)

            # Backfill forecast errors from Open-Meteo archive for GFS and ECMWF only.
            # HRRR and NAM both map to gfs_seamless in the archive, producing identical
            # data as GFS — calibrating them separately creates 3x GFS weighting in the
            # ensemble and artificially inflates confidence.  At inference time, HRRR/NAM
            # will fall back to the GFS distribution via the regime fallback.
            _ARCHIVE_CALIBRATION_MODELS = {WeatherModel.GFS, WeatherModel.ECMWF}
            # Purge any stale HRRR/NAM distributions that may exist from earlier runs.
            self.db.delete_distributions_for_models(
                city_key, [WeatherModel.HRRR, WeatherModel.NAM]
            )

            city_fitted = 0
            # Calibration leads: 24h (today contracts) + 48h (tomorrow contracts).
            # Each lead needs its own previous-runs fetch because the model's
            # error distribution grows with lead time (σ_48h ≈ √2 × σ_24h for
            # well-calibrated NWP). Reanalysis fallback is 24h-only because
            # archive is observation-assimilated — a "48h reanalysis" is
            # conceptually undefined.
            _CALIBRATION_LEAD_SCHEDULE = [
                (24, 1, True),   # (lead_hours, lead_days, allow_reanalysis_fallback)
                (48, 2, False),
            ]
            for model in WeatherModel:
                if model not in _ARCHIVE_CALIBRATION_MODELS:
                    continue

                for lead_hours, lead_days, allow_reanalysis in _CALIBRATION_LEAD_SCHEDULE:
                    # Prefer the previous-runs API (true operational forecasts,
                    # ~90-day rolling archive). For 48h lead, the previous-runs
                    # API exposes `temperature_2m_max_previous_day2` etc.
                    prev_runs_start = max(start, end - timedelta(days=90))
                    print(
                        f"[calibrate]   {city_key}/{model.value}/{lead_hours}h: "
                        f"fetching real forecasts {prev_runs_start}→{end} "
                        f"(previous-runs lead_days={lead_days})...",
                        file=sys.stderr,
                    )
                    archive_prev = self.grib.fetch_archived_forecasts(
                        station, model,
                        start=prev_runs_start, end=end,
                        lead_days=lead_days,
                    )

                    # Fill older dates from reanalysis archive — 24h-lead only.
                    # σ floor in BracketProbabilityCalculator (2.5°F) prevents
                    # overconfidence from reanalysis samples.
                    archive_hist: dict[date, float] = {}
                    if allow_reanalysis and start < prev_runs_start:
                        print(
                            f"[calibrate]   {city_key}/{model.value}/{lead_hours}h: "
                            f"filling {start}→{prev_runs_start - timedelta(days=1)} "
                            f"from reanalysis archive...",
                            file=sys.stderr,
                        )
                        archive_hist = self.grib.fetch_historical_highs(
                            station, model,
                            start=start,
                            end=prev_runs_start - timedelta(days=1),
                        )

                    # Merge: real forecasts override reanalysis where both exist
                    archive = {**archive_hist, **archive_prev}
                    print(
                        f"[calibrate]   {city_key}/{model.value}/{lead_hours}h: "
                        f"{len(archive_prev)} real-forecast days + "
                        f"{len(archive_hist)} reanalysis days = {len(archive)} total",
                        file=sys.stderr,
                    )

                    for obs_date, observed_f in obs_lookup.items():
                        forecast_f = archive.get(obs_date)
                        if forecast_f is None:
                            continue
                        error = forecast_f - observed_f
                        # Use STABLE_HIGH as the default regime (upper-air
                        # classification requires GRIB fields not yet fetched
                        # during historical runs). lead_hours tagged per the
                        # schedule above so _load_dists can look up the right
                        # bucket at inference — no more √(lead/24) σ-scaling
                        # fallback needed for 24h + 48h cases.
                        self.db.save_forecast_error(
                            ForecastError(
                                city=city_key,
                                model=model,
                                regime=SynopticRegime.STABLE_HIGH,
                                lead_hours=lead_hours,
                                error_f=error,
                                obs_date=obs_date,
                            )
                        )

                # Apr 24 2026 (Citadel Q5-A) — fit BOTH pooled and per-season.
                # Pooled (season=-1) preserves legacy behavior and is the
                # fallback when a given (regime, lead, season) bucket lacks
                # enough samples. Per-season (season=0..3) captures the
                # non-stationarity of forecast errors across the year —
                # winter frontal events have very different error shapes
                # than stable summer days, and pooling them averages away
                # both regimes.
                #
                # Sample-size budget: 700 real-forecast days / (city,
                # model, 24h) × 1 pooled fit + 4 season fits = 5 total
                # fits per regime×lead. Most cells have 100-200 samples
                # per season, safely above our 5-sample hard minimum.
                # Thin cells (tropical stations × winter, e.g. Dubai
                # winter convective) will have <5 samples and get skipped
                # — inference falls back to pooled.
                for regime in SynopticRegime:
                    for lead_bucket in [6, 12, 24, 48, 72]:
                        # Pooled fit (season=-1 sentinel on the stored dist)
                        errors_pooled = self.db.get_forecast_errors(
                            city_key, model, regime, lead_bucket, season=None
                        )
                        if len(errors_pooled) >= 5:
                            try:
                                dist = self.fitter.fit(
                                    errors_pooled,
                                    city=city_key,
                                    model=model,
                                    regime=regime,
                                    lead_hours=lead_bucket,
                                )
                                # season=-1 for pooled (default on new
                                # ErrorDistribution). Save writes both
                                # pooled and per-season to coexist via
                                # the composite (city, model, regime,
                                # lead, season) PK.
                                self.db.save_error_distribution(dist)
                                city_fitted += 1
                            except (ValueError, Exception) as exc:
                                print(f"[calibrate]   {city_key}/{model.value}/{regime.value}/{lead_bucket}h/pooled: {exc}", file=sys.stderr)

                        # Per-season fits. Range depends on city climate
                        # (4 for NH/SH temperate, 2 for tropical/arid).
                        from polymarket_strat.domain.weather.season import n_seasons as _n_seasons_for_city
                        for season_bucket in range(_n_seasons_for_city(city_key)):
                            errors_season = self.db.get_forecast_errors(
                                city_key, model, regime, lead_bucket, season=season_bucket
                            )
                            if len(errors_season) < 5:
                                continue
                            try:
                                dist = self.fitter.fit(
                                    errors_season,
                                    city=city_key,
                                    model=model,
                                    regime=regime,
                                    lead_hours=lead_bucket,
                                )
                                # Override the season on the fitted dist
                                # so save_error_distribution writes under
                                # the season-specific key.
                                dist.season = season_bucket
                                self.db.save_error_distribution(dist)
                                city_fitted += 1
                            except (ValueError, Exception) as exc:
                                print(f"[calibrate]   {city_key}/{model.value}/{regime.value}/{lead_bucket}h/S{season_bucket}: {exc}", file=sys.stderr)

            summary["cities"][city_key] = {
                "observations": len(obs_list),
                "distributions_fitted": city_fitted,
            }
            summary["total_distributions_fitted"] += city_fitted

        print(f"[calibrate] Done. {summary['total_distributions_fitted']} distributions fitted.", file=sys.stderr)
        return summary


def _bucket_lead(lead_hours: int) -> int:
    """Round lead time to nearest calibration bucket."""
    for bucket in [6, 12, 24, 48, 72]:
        if lead_hours <= bucket:
            return bucket
    return 72


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= 1.0 + r
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd
