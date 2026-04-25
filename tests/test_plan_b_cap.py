"""Pin the Apr 26 2026 Plan B + narrow-bracket cap contract.

Why this test exists:
  After the Apr 25 NO-side bleed analysis (sao_paulo p=0.86 / -$2.21,
  munich p=0.77 / -$1.87, hong_kong p=0.92 / -$1.35, all on 1°C-wide
  EU/Asia brackets exiting via rebalance "breakeven_triggered"), two
  knobs were tightened in `polymarket_strat.domain.weather.strategy`:

    (1) Plan B high-p artifact cap thresholds dropped from
        (p > 0.85 AND edge > 0.20) → (p > 0.80 AND edge > 0.15).
        The losing band concentrated in p ∈ [0.77, 0.85], just below
        the old cap. The fine-bin tail audit's [0.95, 1.00] bin had a
        9.2% calibration gap, but the (0.77, 0.85] band is also
        miscalibrated and was the primary leak.

    (2) New narrow-bracket NO-side cap: bracket width < 2°F
        (≈ 1°C width) AND token_side == "NO" AND model_prob > 0.75 →
        reject. Bracket P math is hyper-sensitive to σ estimation
        errors when width is on the order of σ. Real Polymarket
        EU/Asia weather contracts are 1°C wide, narrower than the
        ±2°F synthetic brackets the fine-bin ECE audit was run on,
        so honest_ece under-states real calibration error here.

  This file locks in the constants. Loosening them later should be
  deliberate and accompanied by either (a) NO-side isotonic regression
  measurably reducing the high-P calibration gap or (b) > 60 days of
  real-forecast data per (city, model, lead) bucket relaxing σ.
"""
from __future__ import annotations

from polymarket_strat.domain.weather import strategy as ws


# ---------------------------------------------------------------------------
# Plan B high-p cap constants — tightened Apr 26 2026
# ---------------------------------------------------------------------------


def test_plan_b_high_p_cap_is_080() -> None:
    """Plan B fires at p_model > 0.80 (was 0.85 pre-Apr-26)."""
    assert ws._PLAN_B_HIGH_P_CAP == 0.80


def test_plan_b_high_p_edge_trigger_is_015() -> None:
    """Plan B fires at edge_after_fees > 15¢ (was 20¢ pre-Apr-26)."""
    assert ws._PLAN_B_HIGH_P_EDGE_TRIGGER == 0.15


def test_plan_b_constants_are_floats() -> None:
    """Type contract: both constants must remain float literals so
    runtime comparison is unambiguous."""
    assert isinstance(ws._PLAN_B_HIGH_P_CAP, float)
    assert isinstance(ws._PLAN_B_HIGH_P_EDGE_TRIGGER, float)


# ---------------------------------------------------------------------------
# Narrow-bracket NO-side cap constants — added Apr 26 2026
# ---------------------------------------------------------------------------


def test_narrow_bracket_width_threshold_is_2f() -> None:
    """Brackets narrower than 2°F (i.e. typical 1°C-wide EU/Asia
    contracts) trigger the narrow-bracket NO cap."""
    assert ws._NARROW_BRACKET_WIDTH_F == 2.0


def test_narrow_bracket_no_max_p_is_075() -> None:
    """On narrow brackets, NO entries with model_prob > 0.75 are
    rejected. Tuned conservative — still allows narrow-bracket NO at
    moderate confidence (p ∈ (0.50, 0.75]) where bracket geometry
    isn't pathologically tail-sensitive."""
    assert ws._NARROW_BRACKET_NO_MAX_P == 0.75


def test_narrow_bracket_constants_are_floats() -> None:
    """Type contract for the narrow-bracket gate."""
    assert isinstance(ws._NARROW_BRACKET_WIDTH_F, float)
    assert isinstance(ws._NARROW_BRACKET_NO_MAX_P, float)


# ---------------------------------------------------------------------------
# Logical relationships — invariants the gate logic depends on
# ---------------------------------------------------------------------------


def test_plan_b_p_cap_above_narrow_bracket_p_cap() -> None:
    """The Plan B cap (0.80) sits above the narrow-bracket NO cap
    (0.75). This ordering means narrow-bracket NO is the FIRST cap
    that fires on the (0.75, 0.80] band, giving us a more specific
    diagnostic counter (`narrow_bracket_no_cap`) for the failure mode
    that motivated this patch. Plan B then catches everything > 0.80
    on top of any width."""
    assert ws._NARROW_BRACKET_NO_MAX_P < ws._PLAN_B_HIGH_P_CAP


def test_narrow_bracket_threshold_below_typical_us_widths() -> None:
    """US weather contracts are typically 5°F-wide; EU/Asia are 1°C
    (~1.8°F). The threshold at 2.0°F catches the latter but not the
    former, which is the intended behavior."""
    typical_us_width_f = 5.0
    typical_eu_asia_width_f = 1.8  # 1°C
    assert typical_eu_asia_width_f < ws._NARROW_BRACKET_WIDTH_F
    assert typical_us_width_f >= ws._NARROW_BRACKET_WIDTH_F
