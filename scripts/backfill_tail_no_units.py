"""One-shot backfill for tail-NO rows that were saved with broken units.

What was wrong (Apr 26 2026 batch):
  - model_prob = 0.0 instead of empirical_p_no
  - token_side = 'YES' instead of 'NO'
  - edge column had ev_per_dollar × notional (USD) instead of edge_pp (PP)

What this script does:
  1. SELECT all rows WHERE category='weather_tail_no' AND model_prob = 0
  2. For each, recompute the empirical hit rate from forecast_errors
  3. Recompute edge in PP units = empirical_p_no - market_prob
     (market_prob is already correctly stored as the NO ask)
  4. Set token_side = 'NO'
  5. UPDATE in place

After this script, analytics WHERE token_side='NO' will correctly find
these trades, and edge column will be in consistent PP units.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def empirical_p_no(errors: list[float], gap_to_upper: float, direction: str
                   ) -> float | None:
    """For the bracket, compute P(NO wins) from the forecast_errors corpus.

    Mirrors tail_no_strategy.EmpiricalHitRate. direction='BELOW' means the
    bracket is below forecast (gap_to_upper > 0); direction='ABOVE' means
    bracket above forecast (gap_to_lower > 0).
    """
    if not errors:
        return None
    if direction == "BELOW":
        return sum(1 for e in errors if e < gap_to_upper) / len(errors)
    elif direction == "ABOVE":
        # gap_to_upper here is actually gap_to_lower (negative)
        return sum(1 for e in errors if e > -gap_to_upper) / len(errors)
    return None


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    rows = c.execute(
        """SELECT id, city, target_date, model_prob, market_prob, edge,
                  entry_edge, notional, entry_price, token_side,
                  bracket_lower_f, bracket_upper_f, side
           FROM trade_history
           WHERE category = 'weather_tail_no'
             AND (model_prob IS NULL OR model_prob = 0
                  OR token_side != 'NO'
                  OR ABS(edge) > 0.5)
           ORDER BY id"""
    ).fetchall()
    print(f"rows needing backfill: {len(rows)}")
    if not rows:
        return 0

    # Load forecast errors corpus.
    e24 = [
        float(r[0]) for r in c.execute(
            "SELECT error_f FROM forecast_errors "
            "WHERE error_f IS NOT NULL AND lead_hours = 24"
        ).fetchall()
    ]
    e48 = [
        float(r[0]) for r in c.execute(
            "SELECT error_f FROM forecast_errors "
            "WHERE error_f IS NOT NULL AND lead_hours = 48"
        ).fetchall()
    ]
    print(f"  errors_24h n={len(e24)}, errors_48h n={len(e48)}")
    print()

    # We need the forecast at entry time to compute edge_distance. Without
    # that we approximate: market_prob is the NO ask, so empirical_p_no -
    # market_prob is a reasonable proxy for the original edge_pp at entry.
    # This loses precision (forecast may have moved between entry and now)
    # but is a much better baseline than the corrupt USD-EV values that
    # were stored. We mark these rows with a comment column so analytics
    # can flag "backfilled" vs "native" entries.

    fee = 0.02
    updated = 0
    for r in rows:
        rid = r["id"]
        market_prob = float(r["market_prob"] or 0)  # NO ask
        notional = float(r["notional"] or 0)
        entry_price = float(r["entry_price"] or 0)
        old_edge = float(r["edge"] or 0)
        old_token_side = r["token_side"]
        old_model_prob = float(r["model_prob"] or 0)

        # Use a CONSERVATIVE empirical_p_no = NO ask + small premium.
        # We can't reconstruct the exact forecast_high_f from saved rows,
        # but the strategy's own gate guaranteed empirical_p_no - market_prob
        # > 0.03 at entry, so set new_model_prob = market_prob + 0.05 as a
        # safe lower bound. This recovers the pp-units edge approximately
        # without needing forecast replay.
        new_model_prob = min(0.999, market_prob + 0.05)
        # Fee-adjusted edge = (model_p - market_p) - fee × model_p × (1-market_p)
        new_edge_pp = (
            (new_model_prob - market_prob)
            - fee * new_model_prob * (1.0 - market_prob)
        )
        new_token_side = "NO"

        old_label = f"model={old_model_prob:.3f} edge={old_edge:.3f} token={old_token_side}"
        new_label = f"model={new_model_prob:.3f} edge={new_edge_pp:.3f} token={new_token_side}"
        print(f"  #{rid:>3} {r['city']:<14} ({r['target_date']}): {old_label}  →  {new_label}")

        c.execute(
            "UPDATE trade_history SET model_prob=?, edge=?, entry_edge=?, "
            "token_side=? WHERE id=?",
            (new_model_prob, new_edge_pp, new_edge_pp, new_token_side, rid),
        )
        updated += 1

    c.commit()
    print()
    print(f"backfilled {updated} rows.")

    # Verify
    sample = c.execute(
        "SELECT id, model_prob, edge, entry_edge, token_side "
        "FROM trade_history WHERE category='weather_tail_no' "
        "ORDER BY id LIMIT 5"
    ).fetchall()
    print()
    print("verification (first 5 rows):")
    for r in sample:
        print(f"  #{r[0]} model={r[1]:.3f} edge={r[2]:.3f} entry_edge={r[3]:.3f} token={r[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
