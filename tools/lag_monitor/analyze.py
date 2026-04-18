"""Compute GFS/ECMWF publish → Polymarket reprice lag from monitor.py output.

Input: events.jsonl produced by monitor.py
Output: terminal report + optional CSV of per-match lags.

Algorithm:
1. Parse every event.
2. For each `forecast_change` at time T for (city, model, valid_date), find the
   first `price_change` event AFTER T, in ANY market whose (city + valid_date
   bucket) matches, where the price change is significant (>= --min-price-move,
   default 2 cents) and in the same direction we'd expect.

   "Same direction expected" = forecast moved warmer (delta_f > 0) → prices for
   "X°F or higher" brackets should rise; "Y°F or below" brackets should fall.
   We test both directions and report both.

3. Report: median, p25, p75, p90 lag in minutes, per (city, model) and overall.

Usage:
    python tools/lag_monitor/analyze.py tools/lag_monitor/logs/events.jsonl
    python tools/lag_monitor/analyze.py tools/lag_monitor/logs/events.jsonl --csv out.csv
    python tools/lag_monitor/analyze.py tools/lag_monitor/logs/events.jsonl --min-forecast-move 1.0
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc


def parse_ts(s: str) -> datetime:
    # Accepts "2026-04-18T14:02:31Z"
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def load_events(path: Path) -> list[dict[str, Any]]:
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def extract_bracket_direction(event: dict[str, Any]) -> str:
    """Return 'above', 'below', or 'exact' based on bracket geometry."""
    lo = event.get("lower_f")
    hi = event.get("upper_f")
    if lo is None and hi is None:
        return "unknown"
    if lo is not None and (hi is None or hi > 200):
        return "above"
    if hi is not None and (lo is None or lo < -100):
        return "below"
    return "exact"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("events_file", type=Path)
    parser.add_argument("--csv", type=Path, help="Write per-match lag to CSV")
    parser.add_argument("--min-forecast-move", type=float, default=0.5,
                        help="Ignore forecast_change events with |delta_f| below this (default 0.5)")
    parser.add_argument("--min-price-move", type=float, default=2.0,
                        help="Require price_change of at least this many cents (default 2.0)")
    parser.add_argument("--max-lag-min", type=int, default=360,
                        help="Max lag window to search, in minutes (default 360 = 6h)")
    args = parser.parse_args()

    if not args.events_file.exists():
        print(f"[analyze] file not found: {args.events_file}", file=sys.stderr)
        return 2

    events = load_events(args.events_file)
    if not events:
        print("[analyze] no events", file=sys.stderr)
        return 2

    # Partition events
    forecasts = [e for e in events if e.get("kind") == "forecast_change"
                 and abs(e.get("delta_f", 0)) >= args.min_forecast_move]
    prices = [e for e in events if e.get("kind") == "price_change"
              and e.get("delta_cents", 0) >= args.min_price_move]

    # Index prices by city for fast lookup
    prices_by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in prices:
        prices_by_city[p["city"]].append(p)
    # Sort each city's prices by time ascending
    for k in prices_by_city:
        prices_by_city[k].sort(key=lambda e: e["ts"])

    # For each forecast_change, find first price_change in same city, after ts, within window
    matches: list[dict[str, Any]] = []
    for f in forecasts:
        f_ts = parse_ts(f["ts"])
        f_delta = f["delta_f"]
        city = f["city"]
        city_prices = prices_by_city.get(city, [])

        # Expected direction: warmer forecast → "above" brackets rise, "below" brackets fall
        # Bracket-neutral heuristic: any mid change > threshold in same city, in correct direction
        for p in city_prices:
            p_ts = parse_ts(p["ts"])
            if p_ts <= f_ts:
                continue
            lag_min = (p_ts - f_ts).total_seconds() / 60.0
            if lag_min > args.max_lag_min:
                break  # sorted ascending, everything further is too late

            direction = extract_bracket_direction(p)
            p_delta = p["new_mid"] - p["old_mid"]

            consistent = (
                (direction == "above" and ((f_delta > 0 and p_delta > 0) or (f_delta < 0 and p_delta < 0)))
                or (direction == "below" and ((f_delta > 0 and p_delta < 0) or (f_delta < 0 and p_delta > 0)))
                or (direction in ("exact", "unknown"))
            )
            if not consistent:
                continue

            matches.append({
                "forecast_ts": f["ts"],
                "price_ts": p["ts"],
                "lag_min": round(lag_min, 2),
                "city": city,
                "model": f["model"],
                "lead_hours": f["lead_hours"],
                "forecast_delta_f": f_delta,
                "price_delta_cents": p["delta_cents"],
                "market_question": p.get("question", ""),
                "bracket_direction": direction,
            })
            break  # first match only for this forecast event

    # -------------------- report --------------------
    print(f"\n=== LAG MEASUREMENT REPORT ===")
    print(f"Events file: {args.events_file}")
    print(f"  Total events:            {len(events)}")
    print(f"  Forecast changes (filtered, |Δ| ≥ {args.min_forecast_move}°F): {len(forecasts)}")
    print(f"  Price changes (filtered, ≥{args.min_price_move}¢):            {len(prices)}")
    print(f"  Matched forecast→price pairs:                  {len(matches)}")

    if not matches:
        print("\nNo matches. Either the monitor hasn't been running long enough,")
        print("or (more interesting) Polymarket is NOT responding to forecast shifts in this window.")
        return 0

    lags = [m["lag_min"] for m in matches]
    lags.sort()

    def pct(p: float) -> float:
        idx = int(p * (len(lags) - 1))
        return lags[idx]

    print(f"\n--- Overall lag distribution (minutes) ---")
    print(f"  n:       {len(lags)}")
    print(f"  min:     {min(lags):.1f}")
    print(f"  p10:     {pct(0.10):.1f}")
    print(f"  p25:     {pct(0.25):.1f}")
    print(f"  median:  {statistics.median(lags):.1f}")
    print(f"  mean:    {statistics.mean(lags):.1f}")
    print(f"  p75:     {pct(0.75):.1f}")
    print(f"  p90:     {pct(0.90):.1f}")
    print(f"  max:     {max(lags):.1f}")

    # Per (city, model) breakdown
    by_cm: dict[tuple[str, str], list[float]] = defaultdict(list)
    for m in matches:
        by_cm[(m["city"], m["model"])].append(m["lag_min"])

    print(f"\n--- By city × model (median lag, minutes) ---")
    print(f"  {'city':<12} {'model':<8} {'n':>4} {'median':>8} {'p25':>8} {'p75':>8}")
    for (city, model), vals in sorted(by_cm.items()):
        vals.sort()
        def _p(ps): return vals[int(ps * (len(vals) - 1))]
        print(f"  {city:<12} {model:<8} {len(vals):>4} {statistics.median(vals):>8.1f} {_p(0.25):>8.1f} {_p(0.75):>8.1f}")

    # Decision guidance
    median = statistics.median(lags)
    print(f"\n--- Interpretation ---")
    if median < 15:
        print(f"  Median lag {median:.1f} min → edge is likely already arbitraged by bots.")
        print(f"  Investing in scheduled-reprice infra is LOW VALUE unless you can beat 5 min.")
    elif median < 60:
        print(f"  Median lag {median:.1f} min → modest edge. Lambda + EventBridge would help.")
        print(f"  Worth building if you're fully paper-validated first.")
    else:
        print(f"  Median lag {median:.1f} min → significant edge exists.")
        print(f"  Scheduled-reprice infra (idea 6) is HIGH VALUE. Prioritize it after Phase 2.")

    # Optional CSV
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(matches[0].keys()))
            w.writeheader()
            w.writerows(matches)
        print(f"\n  wrote {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
