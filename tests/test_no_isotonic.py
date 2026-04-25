"""Pin the Apr 26 2026 NO-side isotonic calibration contract.

Why this test exists:
  Fix #2 from the NO-side bleed analysis. The existing YES-side isotonic
  curve (`isotonic_calibration.json`) was fit from synthetic walk-forward
  CSV outcomes, but the actual NO-side losses concentrate on narrow
  Polymarket EU/Asia 1°C contracts the synthetic backtest never saw.

  This file pins:
    1. IsotonicCalibrator.calibrate_no() returns identity when the
       NO-side JSON is missing (fail-closed).
    2. When loaded, calibrate_no() applies the global curve.
    3. When a narrow-bracket curve exists AND the bracket width is
       below the threshold, the narrow curve is used instead of global.
    4. The narrow_threshold matches strategy.py:_NARROW_BRACKET_WIDTH_F.
    5. Out-of-bounds inputs (NaN, < 0, > 1) pass through unchanged.
    6. identity_fallback records in the JSON are ignored (treated as
       "no curve").
"""
from __future__ import annotations

import json
import math
import os
import tempfile

import pytest

from polymarket_strat.domain.weather.forecast import (
    IsotonicCalibrator,
    _NARROW_BRACKET_THRESHOLD_F,
)


# ---------------------------------------------------------------------------
# Threshold constant — must match strategy.py
# ---------------------------------------------------------------------------


def test_narrow_threshold_constant_is_2f() -> None:
    """The width threshold for narrow-vs-global selection must be 2.0°F,
    matching `strategy.py:_NARROW_BRACKET_WIDTH_F`. If one is changed
    without the other, the gate and the calibration disagree about
    "narrow" and behavior diverges."""
    assert _NARROW_BRACKET_THRESHOLD_F == 2.0


# ---------------------------------------------------------------------------
# Fail-closed: missing or malformed JSON
# ---------------------------------------------------------------------------


def test_no_json_missing_returns_identity() -> None:
    """When the NO-side JSON does not exist, calibrate_no() returns the
    input unchanged. This is the default behavior on a fresh deploy
    where fit_no_isotonic.py hasn't run yet."""
    cal = IsotonicCalibrator(no_json_path="/tmp/nonexistent_no_iso_xyz.json")
    assert cal.no_loaded is False
    assert cal.calibrate_no(0.85) == 0.85
    assert cal.calibrate_no(0.50) == 0.50
    assert cal.calibrate_no(0.10) == 0.10


def test_no_json_malformed_returns_identity() -> None:
    """Garbage JSON on disk leaves the calibrator in identity mode."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{this is not valid json")
        path = f.name
    try:
        cal = IsotonicCalibrator(no_json_path=path)
        assert cal.no_loaded is False
        assert cal.calibrate_no(0.77) == 0.77
    finally:
        os.unlink(path)


def test_no_json_identity_fallback_record_returns_identity() -> None:
    """If fit_no_isotonic.py wrote an identity_fallback record (n < 20),
    the inference path must NOT apply that pseudo-curve — it should
    treat the record as 'no curve'."""
    payload = {
        "fit_at_utc": "2026-04-26T00:00:00+00:00",
        "global": {
            "x": [0.0, 1.0],
            "y": [0.0, 1.0],
            "n": 5,
            "identity_fallback": True,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        cal = IsotonicCalibrator(no_json_path=path)
        assert cal.no_loaded is False
        assert cal.calibrate_no(0.85) == 0.85
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Active curve behavior
# ---------------------------------------------------------------------------


def test_no_global_curve_shrinks_high_p() -> None:
    """A realistic NO-side curve learned from settled trades. The model
    consistently over-predicts NO at high p (says 0.85, reality 0.75).
    The curve should map 0.85 → 0.75."""
    payload = {
        "fit_at_utc": "2026-04-26T00:00:00+00:00",
        "global": {
            # Realistic 5-knot piecewise-linear curve: identity at low p,
            # shrinkage above 0.7.
            "x": [0.0, 0.3, 0.6, 0.85, 1.0],
            "y": [0.0, 0.3, 0.55, 0.75, 0.86],
            "n": 50,
            "brier_before": 0.18,
            "brier_after": 0.12,
            "identity_fallback": False,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        cal = IsotonicCalibrator(no_json_path=path)
        assert cal.no_loaded is True
        assert cal.calibrate_no(0.85) == pytest.approx(0.75, abs=1e-9)
        # Identity at low p (curve passes through low-p identity by
        # construction in this fixture).
        assert cal.calibrate_no(0.30) == pytest.approx(0.30, abs=1e-9)
        # Linear interp between knots (0.6→0.55 and 0.85→0.75).
        # At p=0.7: x_frac = (0.7-0.6)/(0.85-0.6) = 0.4
        # y       = 0.55 + 0.4 * (0.75 - 0.55) = 0.55 + 0.08 = 0.63
        assert cal.calibrate_no(0.70) == pytest.approx(0.63, abs=1e-9)
    finally:
        os.unlink(path)


def test_no_narrow_curve_overrides_global_when_width_below_threshold() -> None:
    """When both curves are loaded and the bracket is narrow (< 2°F),
    the narrow curve must take precedence over the global curve."""
    payload = {
        "global": {
            "x": [0.0, 1.0],
            "y": [0.0, 1.0],  # identity global
            "n": 100,
            "identity_fallback": False,
        },
        "narrow": {
            "x": [0.0, 1.0],
            "y": [0.0, 0.5],  # narrow shrinks everything by half
            "n": 30,
            "identity_fallback": False,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        cal = IsotonicCalibrator(no_json_path=path)
        assert cal.no_loaded is True
        # Narrow bracket (1°F) → narrow curve applies → shrinks 0.8 → 0.4
        assert cal.calibrate_no(0.8, bracket_width_f=1.0) == pytest.approx(0.4)
        # Wide bracket (5°F) → global curve applies → identity → 0.8
        assert cal.calibrate_no(0.8, bracket_width_f=5.0) == pytest.approx(0.8)
        # Width unspecified → global (identity) applies
        assert cal.calibrate_no(0.8) == pytest.approx(0.8)
    finally:
        os.unlink(path)


def test_no_global_only_applies_regardless_of_width() -> None:
    """When only the global curve is loaded (no narrow-specific fit),
    every bracket — narrow or wide — uses the global curve."""
    payload = {
        "global": {
            "x": [0.0, 1.0],
            "y": [0.0, 0.7],  # global shrinks by 30%
            "n": 100,
            "identity_fallback": False,
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        cal = IsotonicCalibrator(no_json_path=path)
        # Both narrow and wide should use the same global curve.
        assert cal.calibrate_no(0.5, bracket_width_f=1.0) == pytest.approx(0.35)
        assert cal.calibrate_no(0.5, bracket_width_f=5.0) == pytest.approx(0.35)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Bounds + safety
# ---------------------------------------------------------------------------


def test_calibrate_no_passes_through_out_of_bounds() -> None:
    """NaN or out-of-range values must pass through unchanged. The
    calibrator is on the hot path; we cannot raise on bad input."""
    cal = IsotonicCalibrator(no_json_path="/tmp/_no_iso_missing.json")
    nan = float("nan")
    out = cal.calibrate_no(nan)
    assert math.isnan(out)
    assert cal.calibrate_no(-0.5) == -0.5
    assert cal.calibrate_no(1.5) == 1.5


def test_calibrate_no_independent_of_yes_calibrate() -> None:
    """`calibrate_no()` must not fall back to the YES-side `calibrate()`
    even if the YES file is loaded. The two curves are fit on different
    data (synthetic walk-forward vs real settled NO trades) and must
    not be composed."""
    # Build a YES-side JSON that would massively shift output if applied
    yes_payload = {
        "fit_at_utc": "2026-04-26T00:00:00+00:00",
        "global": {
            "24": {"x": [0.0, 1.0], "y": [0.0, 0.0], "n": 1000},
            "48": {"x": [0.0, 1.0], "y": [0.0, 0.0], "n": 1000},
        },
        "per_city": {},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fy:
        json.dump(yes_payload, fy)
        yes_path = fy.name
    try:
        # No NO-side file exists → calibrate_no returns identity even
        # though calibrate() would map any input to 0.
        cal = IsotonicCalibrator(
            json_path=yes_path,
            no_json_path="/tmp/_no_iso_missing.json",
        )
        assert cal.loaded is True
        assert cal.calibrate(0.85, lead_hours=24) == pytest.approx(0.0)
        # NO-side untouched by YES-side activation:
        assert cal.calibrate_no(0.85) == 0.85
    finally:
        os.unlink(yes_path)


# ---------------------------------------------------------------------------
# Diagnostic accessors
# ---------------------------------------------------------------------------


def test_no_diagnostic_describes_state() -> None:
    """`no_diagnostic` should return a human-readable string indicating
    whether the curve was loaded. Used by deploy verification + dashboard."""
    cal = IsotonicCalibrator(no_json_path="/tmp/_definitely_missing.json")
    assert cal.no_loaded is False
    assert "no NO-side calibration" in cal.no_diagnostic
