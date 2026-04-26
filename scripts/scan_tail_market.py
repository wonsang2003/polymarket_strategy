"""Live tail-NO scan v2 — CORRECTED edge-based distance metric.

Run on EC2:
    python -u scripts/scan_tail_market.py

CORRECTION (Apr 26 2026 v2):
  v1 used `dist = fc - (lower+upper)/2` which is meaningless for asymmetric
  open-ended brackets like [-50, 66]°F (a "≤66°F" cap). Center = +8°F is
  not a real distance. Fixed:
    - gap_to_upper = forecast - bracket_upper  (positive if forecast > upper
      → bracket sits BELOW forecast → we bet NO that observed lands above it)
    - gap_to_lower = bracket_lower - forecast  (positive if forecast < lower
      → bracket sits ABOVE forecast → we bet NO that observed stays below it)
    - effective_gap = max(gap_to_upper, gap_to_lower)
        - if positive: forecast is OUTSIDE the bracket → tail-NO candidate
        - if zero or negative: forecast is INSIDE the bracket → not a tail trade

Empirical hit rates derived from forecast_errors directly using edge distance,
not bracket center. NO wins when observed is outside the bracket; the
probability of that depends on how far the nearest bracket edge sits from
forecast (and which direction).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, date
from pathlib import Path

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.infrastructure.weather.market_scanner import (
    WeatherMarketScanner,
)
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
from polymarket_strat.domain.weather.models import CITY_REGISTRY, WeatherModel


def load_empirical_errors() -> tuple[list[float], list[float]]:
    """Return (errors_24h, errors_48h) lists from the forecast_errors table."""
    c = sqlite3.connect("data/weather/weather.db")
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
    c.close()
    return e24, e48


def empirical_p_no_below(errors: list[float], gap_to_upper: float) -> float:
    """For a bracket whose UPPER edge sits `gap_to_upper`°F BELOW forecast,
    NO wins when observed > bracket_upper.
    error = forecast - observed.
    observed > bracket_upper ⟺ error < forecast - bracket_upper = gap_to_upper.

    Note this is the probability that error doesn't reach into the bracket
    territory, regardless of whether it overshoots into the lower tail.
    For wide brackets like [-50, 66] the lower bound is unreachable in
    practice, so NO win = observed > 66 = error < 2 (when forecast=68).
    """
    if not errors:
        return float("nan")
    n_below = sum(1 for e in errors if e < gap_to_upper)
    return n_below / len(errors)


def empirical_p_no_above(errors: list[float], gap_to_lower: float) -> float:
    """For a bracket whose LOWER edge sits `gap_to_lower`°F ABOVE forecast,
    NO wins when observed < bracket_lower.
    observed < bracket_lower ⟺ error > forecast - bracket_lower = -gap_to_lower.
    """
    if not errors:
        return float("nan")
    n_above = sum(1 for e in errors if e > -gap_to_lower)
    return n_above / len(errors)


def main() -> int:
    print("[scan_tail v2] step 1/4: pulling active brackets...", flush=True)
    scanner = WeatherMarketScanner(PolymarketPublicClient())
    contracts = scanner.find_weather_bracket_markets()
    print(f"  total_active_brackets : {len(contracts)}", flush=True)

    city_dates: dict[str, set[date]] = defaultdict(set)
    for c_ in contracts:
        city_dates[c_.city].add(c_.target_date)
    print(f"  unique cities         : {len(city_dates)}", flush=True)

    print("\n[scan_tail v2] step 2/4: fetching forecasts...", flush=True)
    client = GribDataClient()
    fc_cache: dict[tuple[str, date], dict[str, float]] = {}
    today = datetime.now(timezone.utc).date()

    for city, dates in city_dates.items():
        if city not in CITY_REGISTRY:
            continue
        station = CITY_REGISTRY[city]
        for tgt in sorted(dates):
            days_out = (tgt - today).days
            if days_out < 0 or days_out > 7:
                continue
            lead_h = max(24, days_out * 24)
            try:
                forecasts = client.fetch_all_models(
                    station=station, lead_hours=lead_h
                )
                forecasts = [
                    f for f in forecasts
                    if f.model in (WeatherModel.GFS, WeatherModel.ECMWF)
                ]
                highs = [f.forecast_high_f for f in forecasts if f.forecast_high_f]
                if highs:
                    fc_cache[(city, tgt)] = {
                        "forecast_high_f": sum(highs) / len(highs),
                        "n_models": len(highs),
                    }
            except Exception as exc:
                print(f"    {city} d+{days_out} FAIL: {exc!r}",
                      file=sys.stderr, flush=True)
    print(f"  forecasts_fetched     : {len(fc_cache)}", flush=True)

    print("\n[scan_tail v2] step 3/4: loading empirical errors...", flush=True)
    e24, e48 = load_empirical_errors()
    print(f"  errors_24h             : {len(e24)}")
    print(f"  errors_48h             : {len(e48)}")

    print("\n[scan_tail v2] step 4/4: classifying brackets with EDGE distance...",
          flush=True)
    rows: list[dict] = []
    for ct in contracts:
        fc = fc_cache.get((ct.city, ct.target_date))
        if fc is None:
            continue
        bracket_lower = float(ct.lower_f)
        bracket_upper = float(ct.upper_f)
        width_f = bracket_upper - bracket_lower
        fc_high = fc["forecast_high_f"]

        gap_to_upper = fc_high - bracket_upper      # positive: bracket BELOW forecast
        gap_to_lower = bracket_lower - fc_high      # positive: bracket ABOVE forecast

        if gap_to_upper > 0:
            edge_distance = gap_to_upper
            direction = "below"
        elif gap_to_lower > 0:
            edge_distance = gap_to_lower
            direction = "above"
        else:
            edge_distance = 0.0
            direction = "inside"

        best_ask_yes = float(ct.best_ask_yes or 0)
        best_bid_yes = float(ct.best_bid_yes or 0)
        if best_ask_yes <= 0 and best_bid_yes <= 0:
            continue
        no_ask = 1.0 - best_bid_yes if best_bid_yes > 0 else None
        spread = (
            best_ask_yes - best_bid_yes
            if best_ask_yes > 0 and best_bid_yes > 0 else None
        )
        days_out = (ct.target_date - today).days

        # Empirical hit rate based on which empirical pool to draw from.
        # 24h pool for d+0/d+1 (similar lead), 48h for d+2+.
        errors_pool = e24 if days_out <= 1 else e48
        if direction == "below":
            hit_rate = empirical_p_no_below(errors_pool, gap_to_upper)
        elif direction == "above":
            hit_rate = empirical_p_no_above(errors_pool, gap_to_lower)
        else:
            hit_rate = float("nan")  # forecast inside bracket — tail strategy doesn't apply

        rows.append({
            "city": ct.city,
            "days_out": days_out,
            "fc_high": fc_high,
            "lower_f": bracket_lower,
            "upper_f": bracket_upper,
            "width_f": width_f,
            "gap_to_upper": gap_to_upper,
            "gap_to_lower": gap_to_lower,
            "edge_distance": edge_distance,
            "direction": direction,
            "best_ask_yes": best_ask_yes,
            "best_bid_yes": best_bid_yes,
            "no_ask": no_ask,
            "spread": spread,
            "liquidity": float(ct.liquidity or 0),
            "hit_rate": hit_rate,
            "question": (ct.question or "")[:60],
        })

    out_path = Path("data/weather/tail_scan_snapshot.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump({
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "metric": "edge_distance_v2",
            "n_brackets": len(rows),
            "rows": rows,
        }, f, indent=2, default=str)
    print(f"  brackets_classified    : {len(rows)}")

    # Distance bands using EDGE distance
    bands_below = [
        ("> 10°F BELOW", 10, 999),
        ("5-10°F BELOW", 5, 10),
        ("3-5°F BELOW", 3, 5),
        ("1-3°F BELOW", 1, 3),
        ("0-1°F BELOW", 0, 1),
    ]
    bands_above = [
        ("0-1°F ABOVE", 0, 1),
        ("1-3°F ABOVE", 1, 3),
        ("3-5°F ABOVE", 3, 5),
        ("5-10°F ABOVE", 5, 10),
        ("> 10°F ABOVE", 10, 999),
    ]

    print()
    print("=" * 105)
    print("=== ACTUAL Polymarket NO ASK prices by EDGE distance band ===")
    print(f"  {'band':<15} {'n':>4} {'avg_no':>9} {'med_no':>9} "
          f"{'avg_hit':>9} {'avg_break':>10} {'avg_EV/$1':>11} {'avg_liq':>9}")

    def report(label: str, items: list[dict]) -> None:
        if not items:
            return
        items_with_no = [r for r in items if r["no_ask"] is not None]
        if not items_with_no:
            return
        n = len(items_with_no)
        no_asks = [r["no_ask"] for r in items_with_no]
        hits = [r["hit_rate"] for r in items_with_no
                if r["hit_rate"] == r["hit_rate"]]  # filter NaN
        liqs = [r["liquidity"] for r in items_with_no]
        sorted_no = sorted(no_asks)
        med_no = sorted_no[len(sorted_no) // 2]
        avg_hit = sum(hits) / len(hits) if hits else 0
        avg_no = sum(no_asks) / n
        breakeven = avg_hit / (avg_hit + 0.02 * (1 - avg_hit)) if avg_hit > 0 else 0
        # Avg EV: per-bracket EV at its own NO ask + own hit, then averaged.
        evs = []
        for r in items_with_no:
            if r["hit_rate"] != r["hit_rate"]:
                continue
            na = r["no_ask"]
            hit = r["hit_rate"]
            if na <= 0 or na >= 1:
                continue
            win_pl = (1 - na) / na * 0.98
            ev = hit * win_pl - (1 - hit) * 1
            evs.append(ev)
        avg_ev = sum(evs) / len(evs) if evs else 0
        flag = "[+]" if avg_ev > 0 else "[-]"
        print(f"  {flag} {label:<11} {n:>4} {avg_no*100:>8.2f}% "
              f"{med_no*100:>8.2f}% {avg_hit*100:>8.2f}% "
              f"{breakeven*100:>9.2f}% ${avg_ev:>+9.4f} "
              f"${sum(liqs)/n:>8.0f}")

    print(f"  -- BELOW (forecast above bracket; bet NO observed > upper) --")
    for name, lo, hi in bands_below:
        items = [r for r in rows
                 if r["direction"] == "below"
                 and lo <= r["edge_distance"] < hi]
        report(name, items)
    print(f"  -- ABOVE (forecast below bracket; bet NO observed < lower) --")
    for name, lo, hi in bands_above:
        items = [r for r in rows
                 if r["direction"] == "above"
                 and lo <= r["edge_distance"] < hi]
        report(name, items)

    print()
    print("=" * 105)
    print("=== Top-20 highest-EV TAIL NO opportunities (CORRECTED) ===")
    print(f"  {'city':<12} {'d':>2} {'fc':>5} {'bracket':>13} {'dir':>5} "
          f"{'edge_d':>7} {'no_ask':>7} {'hit':>7} {'EV/$':>9} {'EV$50':>8} "
          f"{'liq':>7} {'q':<30}")
    opps = []
    for r in rows:
        if r["no_ask"] is None or r["direction"] == "inside":
            continue
        if r["edge_distance"] < 1.0:  # need ≥1°F outside the bracket
            continue
        if r["hit_rate"] != r["hit_rate"]:
            continue
        if r["no_ask"] >= 0.99:
            continue
        if r["liquidity"] < 100:
            continue
        na = r["no_ask"]
        hit = r["hit_rate"]
        if na <= 0 or na >= 1:
            continue
        win_pl = (1 - na) / na * 0.98
        ev = hit * win_pl - (1 - hit) * 1
        if ev <= 0:
            continue
        opps.append((ev, r))
    opps.sort(reverse=True, key=lambda x: x[0])

    for ev, r in opps[:20]:
        bracket = f"[{r['lower_f']:.0f},{r['upper_f']:.0f}]"
        print(f"  {r['city'][:12]:<12} {r['days_out']:>2} "
              f"{r['fc_high']:>4.0f}F {bracket:>13} {r['direction']:>5} "
              f"{r['edge_distance']:>+6.1f}F {r['no_ask']*100:>6.2f}% "
              f"{r['hit_rate']*100:>6.2f}% ${ev:>+8.4f} ${ev*50:>+7.2f} "
              f"${r['liquidity']:>6.0f} {r['question'][:30]}")

    print()
    print("=== SUMMARY ===")
    in_tail = [r for r in rows
               if r["direction"] != "inside" and r["edge_distance"] >= 1.0
               and r["no_ask"] is not None]
    sum_ev_50 = sum(ev for ev, _ in opps) * 50
    sum_ev_200 = sum(ev for ev, _ in opps) * 200
    print(f"  bracket count: total={len(rows)}  outside_forecast={len(in_tail)}")
    print(f"  positive_EV_opps: {len(opps)}")
    print(f"  potential EV @ $50/trade  summed: ${sum_ev_50:+.2f}")
    print(f"  potential EV @ $200/trade summed: ${sum_ev_200:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
