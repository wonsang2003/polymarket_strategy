"""Cross-bracket coherence arbitrage detector.

Apr 25 2026 — STRATEGY 1 of the post-paradigm-flip refactor. The user
insight: the market is mostly efficient, so betting against its
consensus is usually wrong. But MARKET PRICES MUST OBEY ARITHMETIC.
For monotonic bracket families like "Highest temp ≥ T at city X on
day D" with multiple thresholds T₁ < T₂ < T₃, the prices must satisfy
a partial order:

    P(obs ≥ T₁) ≥ P(obs ≥ T₂) ≥ P(obs ≥ T₃)

When markets violate this, it's a free arbitrage — you can buy the
cheaper bracket (with strictly higher payout probability) and either
hold to settlement or sell into a market that eventually corrects.

NOT a model-based edge. We never claim to know the temperature better
than the market. We only spot the market's own pricing inconsistencies.

Detection rules:
  Family 1 (LOWER-BOUND, "or higher"): lower_f varies, upper_f = +inf.
    Market prices should DECREASE as lower_f increases.
    Violation: P(lower=T₁) < P(lower=T₂) for T₁ < T₂.

  Family 2 (UPPER-BOUND, "or lower"): upper_f varies, lower_f = -inf.
    Market prices should INCREASE as upper_f increases.
    Violation: P(upper=T₁) > P(upper=T₂) for T₁ < T₂.

Range brackets ("between A and B" with both A and B finite) are not
in scope here — they form a different topology that requires
distribution-shape analysis (Strategy 4, deferred).

Output: CoherenceOpportunity records that the strategy layer turns
into trade signals. Each opportunity captures the LONG side (buy the
cheaper bracket — guaranteed higher P payout) and the implied
arbitrage size (how much the market is mis-pricing).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from polymarket_strat.domain.weather.models import BracketContract


# Sentinel temperature values used by the bracket parser to indicate
# unbounded sides (see infrastructure/weather/market_scanner.py).
_LOWER_INF_F: float = -50.0
_UPPER_INF_F: float = 200.0

# Minimum mispricing magnitude to consider an arbitrage worth trading.
# Below this, the gap is likely just market microstructure noise (1¢
# bid-ask wobble across two thinly-traded brackets) — not real
# arbitrage, since transaction costs eat any 1-2¢ spread before
# settlement.
_MIN_VIOLATION_MAGNITUDE: float = 0.03   # 3¢
# When violation is identified, also require the LONG side's market
# price to be within the tradeable band [0.10, 0.90]. Outside this band
# we can't trade meaningfully due to penny-artifact / crushed-payoff
# economics (the cheap-side bracket might be at $0.005, fill is
# unrealistic).
_MIN_LONG_PRICE: float = 0.10
_MAX_LONG_PRICE: float = 0.90


@dataclass(slots=True)
class CoherenceOpportunity:
    """A detected arithmetic violation in market bracket prices.

    The LONG bracket is the one to BUY — it's currently mispriced
    LOWER than another bracket whose YES outcome is strictly less
    likely. Holding to settlement guarantees at least the relative
    payout — if the SHORT bracket would have paid, the LONG one
    DEFINITELY pays (by monotonicity).

    Pure arbitrage: long_price < short_price BUT P(long YES) ≥
    P(short YES) is mathematically forced. Worst-case zero edge
    (both lose), best-case = full price gap.
    """
    city: str
    target_date: str  # ISO format
    family: str       # "lower_bound" or "upper_bound"
    long_bracket: BracketContract   # buy this
    short_bracket: BracketContract  # the over-priced sibling
    long_price: float               # market_price_yes of long
    short_price: float              # market_price_yes of short
    violation_magnitude: float      # short_price - long_price (always > 0)


def _is_lower_bound_bracket(b: BracketContract) -> bool:
    """A bracket is "lower-bound only" (≥ threshold) when its upper
    bound is the +inf sentinel and lower is finite.
    """
    return (
        b.upper_f >= _UPPER_INF_F - 0.01   # at sentinel
        and b.lower_f > _LOWER_INF_F + 0.01  # not at -inf
    )


def _is_upper_bound_bracket(b: BracketContract) -> bool:
    """A bracket is "upper-bound only" (≤ threshold) when its lower
    bound is the -inf sentinel and upper is finite.
    """
    return (
        b.lower_f <= _LOWER_INF_F + 0.01   # at sentinel
        and b.upper_f < _UPPER_INF_F - 0.01  # not at +inf
    )


def detect_coherence_violations(
    contracts: Sequence[BracketContract],
) -> list[CoherenceOpportunity]:
    """Scan a flat list of bracket contracts; return opportunities.

    The list typically comes from the market scanner pre-grouping; we
    re-group internally by (city, target_date) since that's the level
    at which arbitrage applies — different days are independent
    events.

    For each (city, date) group:
      1. Filter to lower-bound brackets, sort by lower_f.
         Walk pairwise: if price[i] < price[i+1], violation.
      2. Same for upper-bound brackets, sorted by upper_f, expecting
         INCREASING prices.
      3. Emit one CoherenceOpportunity per violation.

    Range brackets (both bounds finite) are skipped here. They have
    legitimate non-monotonic prices and need distribution-shape
    arbitrage instead.
    """
    # Group by (city, target_date)
    groups: dict[tuple[str, str], list[BracketContract]] = {}
    for c in contracts:
        key = (c.city, c.target_date.isoformat() if hasattr(c.target_date, "isoformat") else str(c.target_date))
        groups.setdefault(key, []).append(c)

    opportunities: list[CoherenceOpportunity] = []
    for (city, target_date), group in groups.items():
        # ---- Lower-bound family ("or higher") ----
        lower_family = [b for b in group if _is_lower_bound_bracket(b)]
        # Sort ascending by threshold so pairwise comparison checks
        # the monotonicity rule directly: P(≥T_i) should be ≥ P(≥T_{i+1}).
        lower_family.sort(key=lambda b: b.lower_f)
        for i in range(len(lower_family) - 1):
            cheaper_threshold = lower_family[i]   # T_i (lower threshold → easier to hit)
            stricter_threshold = lower_family[i + 1]  # T_{i+1}
            p_cheap = cheaper_threshold.market_price_yes
            p_strict = stricter_threshold.market_price_yes
            # Violation: stricter (higher T) priced HIGHER than easier (lower T).
            # Real-world: "≥75°F" priced at 0.45 while "≥73°F" priced at 0.40
            # — impossible. Buy the cheaper bracket (T₁=73°F) — it has
            # strictly higher win probability AND lower entry price.
            violation = p_strict - p_cheap
            if violation < _MIN_VIOLATION_MAGNITUDE:
                continue
            # Skip if the long-side price is in penny-artifact territory.
            if p_cheap < _MIN_LONG_PRICE or p_cheap > _MAX_LONG_PRICE:
                continue
            opportunities.append(
                CoherenceOpportunity(
                    city=city,
                    target_date=target_date,
                    family="lower_bound",
                    long_bracket=cheaper_threshold,   # buy the easier bracket
                    short_bracket=stricter_threshold,  # the overpriced sibling
                    long_price=p_cheap,
                    short_price=p_strict,
                    violation_magnitude=round(violation, 4),
                )
            )

        # ---- Upper-bound family ("or lower") ----
        # For "≤T" brackets: P(obs ≤ T) should INCREASE as T increases.
        # Tighter ceiling (T_i) → smaller probability set → lower price
        # expected. If price[i] > price[i+1] for T_i < T_{i+1}, violation.
        upper_family = [b for b in group if _is_upper_bound_bracket(b)]
        upper_family.sort(key=lambda b: b.upper_f)
        for i in range(len(upper_family) - 1):
            tighter = upper_family[i]      # T_i (smaller threshold → tighter ceiling)
            looser = upper_family[i + 1]   # T_{i+1}
            p_tight = tighter.market_price_yes
            p_loose = looser.market_price_yes
            # Violation: tighter (lower T) priced HIGHER than looser
            # (higher T). Buy the looser one — strictly higher win prob.
            violation = p_tight - p_loose
            if violation < _MIN_VIOLATION_MAGNITUDE:
                continue
            if p_loose < _MIN_LONG_PRICE or p_loose > _MAX_LONG_PRICE:
                continue
            opportunities.append(
                CoherenceOpportunity(
                    city=city,
                    target_date=target_date,
                    family="upper_bound",
                    long_bracket=looser,        # buy the looser bracket (higher P)
                    short_bracket=tighter,
                    long_price=p_loose,
                    short_price=p_tight,
                    violation_magnitude=round(violation, 4),
                )
            )

    return opportunities
