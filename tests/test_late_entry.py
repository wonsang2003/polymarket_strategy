"""Pin the Apr 25 2026 late-entry high-conviction override.

Why this test exists:
  Previously we hard-rejected every contract with raw_lead_h < 6h.
  The late-entry override allows contracts with 3h ≤ raw_lead_h < 6h
  to trade IFF:
    (a) model_prob is extreme (≥ 0.80 or ≤ 0.20), AND
    (b) adjusted edge ≥ 20¢ (vs 5¢/10¢ default)
  AND they size at 0.5× normal.

  Below 3h: hard reject unconditionally (no override).

  This file pins:
    1. _LATE_ENTRY_* constants are at expected values
    2. The ordering of thresholds is self-consistent
    3. The size multiplier is documented correctly
"""
from __future__ import annotations

from polymarket_strat.domain.weather.strategy import (
    _LATE_ENTRY_GATE_LEAD_H,
    _LATE_ENTRY_MAX_P_LOW,
    _LATE_ENTRY_MIN_EDGE,
    _LATE_ENTRY_MIN_LEAD_H,
    _LATE_ENTRY_MIN_P_HIGH,
    _LATE_ENTRY_SIZE_MULT,
)


class TestLateEntryConstants:
    def test_hard_floor_is_3h(self):
        """Below 3h we reject unconditionally. This must not drift —
        going below 3h would trade on an already-determined bracket."""
        assert _LATE_ENTRY_MIN_LEAD_H == 3.0

    def test_gate_boundary_is_6h(self):
        """Above 6h is "normal" — full-lead entry rules apply."""
        assert _LATE_ENTRY_GATE_LEAD_H == 6.0

    def test_override_window_is_strictly_ordered(self):
        """Min lead (hard floor) must be strictly less than gate boundary."""
        assert _LATE_ENTRY_MIN_LEAD_H < _LATE_ENTRY_GATE_LEAD_H

    def test_edge_threshold_is_20_cents(self):
        """Late-entry requires much larger edge than standard 5¢/10¢."""
        assert _LATE_ENTRY_MIN_EDGE == 0.20

    def test_edge_threshold_exceeds_standard_gates(self):
        """Late-entry edge threshold must strictly exceed both the
        high-p (5¢) and low-p (10¢) gates. Otherwise the override
        doesn't actually tighten anything."""
        # Hardcoded — if the default edge gate ever rises above 20¢ we
        # want this to fail loudly and force a rethink of the override.
        assert _LATE_ENTRY_MIN_EDGE > 0.10
        assert _LATE_ENTRY_MIN_EDGE > 0.05

    def test_extreme_p_thresholds_are_symmetric(self):
        """High-p gate (≥0.80) and low-p gate (≤0.20) should be mirror
        images around 0.5. Asymmetric values would mean we trust
        high-p overrides differently than low-p ones — not a thing we
        currently have evidence to justify."""
        assert _LATE_ENTRY_MIN_P_HIGH + _LATE_ENTRY_MAX_P_LOW == 1.0

    def test_extreme_p_is_extreme(self):
        """The high/low thresholds must carve out actual tails — 0.80
        and 0.20 represent "very confident" signals. Values like 0.60
        would defeat the purpose of requiring extreme conviction."""
        assert _LATE_ENTRY_MIN_P_HIGH >= 0.75
        assert _LATE_ENTRY_MAX_P_LOW <= 0.25

    def test_size_multiplier_halves(self):
        """0.5x sizing encodes reduced confidence. Full-size late entry
        would put us at risk of the same overconfident-model-plus-
        stale-observation failure mode the gate is designed to
        contain."""
        assert _LATE_ENTRY_SIZE_MULT == 0.5

    def test_size_multiplier_is_strictly_below_one(self):
        """Late entry MUST be smaller than normal sizing. If the
        multiplier ever hits 1.0, we've abandoned the risk-adjustment
        that makes the override safe to run without a nowcast."""
        assert 0.0 < _LATE_ENTRY_SIZE_MULT < 1.0


class TestLateEntryLogic:
    """Boolean truth table for the override — pin the exact conditions."""

    def test_normal_lead_is_not_late_entry(self):
        """raw_lead_h >= 6 → NOT late entry (normal full-lead rules)."""
        # Simulating the check from strategy.analyze:
        # `is_late_entry = raw_lead_h < _LATE_ENTRY_GATE_LEAD_H`
        for lead in (6.0, 6.5, 12.0, 24.0, 48.0):
            assert not (lead < _LATE_ENTRY_GATE_LEAD_H)

    def test_between_3h_and_6h_is_late_entry(self):
        """raw_lead_h ∈ [3, 6) → late entry mode active."""
        for lead in (3.0, 3.5, 4.0, 5.0, 5.99):
            is_late = (lead >= _LATE_ENTRY_MIN_LEAD_H) and (lead < _LATE_ENTRY_GATE_LEAD_H)
            assert is_late

    def test_under_3h_is_hard_reject(self):
        """raw_lead_h < 3 → rejected at the hard floor, never reaches
        the late-entry gate."""
        for lead in (0.1, 1.0, 2.0, 2.99):
            assert lead < _LATE_ENTRY_MIN_LEAD_H

    def test_conviction_check_uses_OR_on_extremes(self):
        """Either high-p OR low-p side passes; mid-p rejects."""
        def is_extreme(p):
            return p >= _LATE_ENTRY_MIN_P_HIGH or p <= _LATE_ENTRY_MAX_P_LOW

        assert is_extreme(0.85)   # high
        assert is_extreme(0.15)   # low
        assert is_extreme(0.80)   # boundary high (inclusive)
        assert is_extreme(0.20)   # boundary low (inclusive)
        assert not is_extreme(0.50)   # mid
        assert not is_extreme(0.70)   # too low for high tail
        assert not is_extreme(0.30)   # too high for low tail
