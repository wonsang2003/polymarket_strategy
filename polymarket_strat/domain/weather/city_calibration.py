"""Per-city walk-forward calibration corrections (Apr 29 2026).

Corrects systematic per-city bias in the tail-NO model output. Measured
via strategy-aligned walk-forward (n=96-172 per city, narrow pm_1F
brackets in tail-NO probability band [0.55, 0.95]).

For each city we compute:
    gap = realized_p_no − predicted_p_no   (over many synthetic narrow brackets)

If gap is negative, the model OVER-predicts NO win rate — calibrated
probability should be lower. If gap is positive, model under-predicts —
calibrated probability should be higher.

Math at entry time:
    p_no_calibrated = p_no_emp + CITY_NO_BIAS[city]
    edge_pp = p_no_calibrated − no_ask

Cap rules (statistical conservatism):
    - |gap| < 0.05         → no correction (within noise)
    - |gap| > 0.20         → cap at ±0.20 (London/Amsterdam capped)
    - n < 100              → omit city (insufficient sample)

Source: scripts/analyze_wf_strategy_aligned.py output (last_run_24h.csv,
2026-01-15 to 2026-04-10 walk-forward, 22 cities × ~150 narrow-bracket
predictions in tail-NO band each).

To DISABLE entire correction layer: set ENABLED = False below.
To recompute: re-run analyze_wf_strategy_aligned.py and update the dict.
"""
from __future__ import annotations

# Master toggle. Set to False to bypass all city corrections.
ENABLED: bool = True

CITY_NO_BIAS: dict[str, float] = {
    # Strong negative (capped at -0.20 due to large measured gap):
    "london":      -0.20,  # WF gap −0.306, n=96  (capped from -0.31)
    "amsterdam":   -0.20,  # WF gap −0.237, n=124 (capped from -0.24)

    # Moderate negative (model over-predicts NO by 10-15pp):
    "shanghai":    -0.14,  # WF gap −0.141, n=172
    "munich":      -0.14,  # WF gap −0.139, n=172
    "toronto":     -0.14,  # WF gap −0.135, n=172
    "tokyo":       -0.12,  # WF gap −0.119, n=172
    "mexico_city": -0.12,  # WF gap −0.119, n=172
    "seattle":     -0.11,  # WF gap −0.114, n=172
    "chicago":     -0.10,  # WF gap −0.099, n=172

    # Mild negative (5-10pp):
    "buenos_aires": -0.09,  # WF gap −0.091, n=171
    "sf":           -0.07,  # WF gap −0.069, n=172
    "wellington":   -0.07,  # WF gap −0.068, n=172
    "atlanta":      -0.07,  # WF gap −0.068, n=172
    "miami":        -0.07,  # WF gap −0.065, n=172
    "sydney":       -0.05,  # WF gap −0.054, n=172

    # 2026-05-10 trade-history-evidence overrides (H_2026_05_10_01).
    # WF originally omitted these 4 cities as "within noise" (|gap| < 0.05),
    # but 7d realized trade history (n≥20 each) shows them as the worst
    # bleeders WITHOUT bias protection:
    #   milan      −$295/7d (n=23)
    #   seoul      −$170/7d (n=20)
    #   hong_kong  −$149/7d (n=22)
    #   sao_paulo   −$82/7d (n=26)
    # Conservative magnitude (smaller than WF-cap'd cities) because we're
    # going below the WF |gap| < 0.05 threshold based on shorter-window
    # evidence. Re-evaluate at next strategy_review (Sun 09:00 KST) —
    # evaluator will compute 7d post-ship verdict automatically.
    "milan":       -0.10,
    "seoul":       -0.10,
    "hong_kong":   -0.10,
    "sao_paulo":   -0.05,

    # Within noise — no correction (|gap| < 0.05, omitted):
    # nyc (-0.042)

    # Mild positive (model under-predicts — boost):
    "la":           +0.06,  # WF gap +0.063, n=129
    "dubai":        +0.06,  # WF gap +0.063, n=172
}


def get_no_bias(city: str) -> float:
    """Return per-city bias correction for tail-NO model_p_no.

    Returns:
        bias to add to raw model_p_no. Positive = boost p_no.
        Negative = deflate p_no. 0.0 if city not in correction set or
        if ENABLED is False.
    """
    if not ENABLED:
        return 0.0
    return CITY_NO_BIAS.get(city, 0.0)
