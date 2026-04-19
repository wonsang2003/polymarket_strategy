"""End-to-end verification: dry-run of the live weather pipeline with full diagnostics.

Runs the same steps as `polymarket-strat weather-analyze` but prints granular trace
of each change landed on April 19, 2026:

  1. Market scanner (`market_scanner.py` endDate fix)       — which contracts kept/dropped, why
  2. Forecast horizon (`strategy.py` lead_hours threading)  — per-contract lead_days, forecast cache keys
  3. Multi-lead calibration (`_load_dists` 24h/48h lookup)  — distribution hit/miss per (city, model, regime, lead)
  4. Strategy gates (`forecast.py:edge()`)                  — pass/fail with reasoning per bracket
  5. Payout formula (`main.py::_pnl` fix)                   — shows n_shares × (1 - entry) × 0.98 for each signal

Read-only — no DB writes, no trades. Safe to re-run any time.

Usage:
    python scripts/e2e_verify.py
    python scripts/e2e_verify.py --max-markets 20     # truncate output
    python scripts/e2e_verify.py --only-tomorrow      # skip today contracts
    python scripts/e2e_verify.py --city seoul         # filter one city
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Make the package importable regardless of which conda/venv is active.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polymarket_strat.api import PolymarketPublicClient  # noqa: E402
from polymarket_strat.domain.weather.forecast import BracketProbabilityCalculator  # noqa: E402
from polymarket_strat.domain.weather.models import (  # noqa: E402
    CITY_REGISTRY,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient  # noqa: E402
from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner  # noqa: E402
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase  # noqa: E402

UTC = timezone.utc
_BUCKETS = (6, 12, 24, 48, 72)


def _bucket_lead(hours: int) -> int:
    return min(_BUCKETS, key=lambda b: abs(b - hours))


def bar(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=50)
    ap.add_argument("--only-tomorrow", action="store_true")
    ap.add_argument("--city", default=None, help="Filter to a single city key")
    ap.add_argument("--db", default="data/weather/weather.db")
    args = ap.parse_args()

    today_utc = datetime.now(UTC).date()
    now_utc = datetime.now(UTC)

    print(f"E2E verification — run at {now_utc.isoformat(timespec='seconds')}")
    print(f"DB: {args.db}")
    print(f"Today (UTC): {today_utc}")

    # ---------------------------------------------------------------------
    # 1. MARKET SCANNER — endDate fix
    # ---------------------------------------------------------------------
    bar("1. Market scanner  (endDate-based cutoff)")
    public = PolymarketPublicClient()
    scanner = WeatherMarketScanner(public)
    contracts = scanner.find_weather_bracket_markets()

    section(f"Contracts retained by scanner: {len(contracts)}")
    # Bucket by (city, target_date) so tomorrow-vs-today is obvious at a glance
    by_city_date: dict[tuple[str, date], list] = defaultdict(list)
    for c in contracts:
        by_city_date[(c.city, c.target_date)].append(c)

    cutoff_dates = Counter()
    for (city, td), cs in sorted(by_city_date.items()):
        if args.city and city != args.city:
            continue
        days_ahead = (td - today_utc).days
        label = {0: "TODAY", 1: "TOMORROW", 2: "D+2"}.get(days_ahead, f"D+{days_ahead}")
        if args.only_tomorrow and days_ahead != 1:
            continue
        cutoff_dates[label] += 1
        station = CITY_REGISTRY.get(city)
        tz = ZoneInfo(station.timezone) if station else None
        local_date = datetime.now(tz).date() if tz else None
        print(
            f"  {city:14s} target={td}  local={local_date}  "
            f"{label:8s}  n_brackets={len(cs)}"
        )

    section("target_date distribution")
    for label, n in sorted(cutoff_dates.items()):
        print(f"  {label:10s}: {n} city-date groups")

    if not contracts:
        print("\n(No contracts returned — scanner filters or Polymarket outage.)")
        return 1

    # Show a sample market's raw metadata to verify endDate fix
    section("Sample market metadata (first 3 tomorrow contracts)")
    tomorrow_cs = [c for c in contracts if (c.target_date - today_utc).days == 1]
    if args.city:
        tomorrow_cs = [c for c in tomorrow_cs if c.city == args.city]
    for c in tomorrow_cs[:3]:
        print(f"  city={c.city}  target_date={c.target_date}")
        print(f"    q: {c.question}")
        print(f"    bracket: [{c.lower_f:.1f}°F, {c.upper_f:.1f}°F)")
        print(f"    market_price_yes: {c.market_price_yes:.3f}  "
              f"bid={c.best_bid_yes:.3f}  ask={c.best_ask_yes:.3f}  "
              f"liquidity=${c.liquidity:,.0f}")
        print(f"    market_id: {c.market_id}")

    # ---------------------------------------------------------------------
    # 2. FORECAST HORIZON — lead_hours threading
    # ---------------------------------------------------------------------
    bar("2. Forecast horizon  (lead_hours = 24 today / 48 tomorrow)")
    grib = GribDataClient()
    forecast_cache: dict[tuple[str, int], list] = {}

    unique_keys: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for (city, td), _cs in by_city_date.items():
        if args.city and city != args.city:
            continue
        days_ahead = max(0, (td - today_utc).days)
        raw_lead = 24 * (days_ahead + 1)
        lead_hours = _bucket_lead(raw_lead)
        if (city, lead_hours) not in seen:
            seen.add((city, lead_hours))
            unique_keys.append((city, lead_hours))

    section(f"Unique (city, lead_hours) combinations to fetch: {len(unique_keys)}")
    for city, lh in unique_keys[: args.max_markets]:
        station = CITY_REGISTRY.get(city)
        if not station:
            continue
        try:
            fcs = grib.fetch_all_models(station, lead_hours=lh)
        except Exception as exc:
            print(f"  {city}/{lh}h: FETCH FAILED → {exc}")
            forecast_cache[(city, lh)] = []
            continue
        forecast_cache[(city, lh)] = fcs
        models_seen = sorted({fc.model.value for fc in fcs})
        values = {fc.model.value: f"{fc.forecast_high_f:.1f}°F" for fc in fcs}
        print(f"  {city:14s} lead={lh}h  models={models_seen}  values={values}")

    # ---------------------------------------------------------------------
    # 3. MULTI-LEAD CALIBRATION LOOKUP
    # ---------------------------------------------------------------------
    bar("3. Multi-lead calibration  (24h + 48h error_distributions)")
    db = WeatherDatabase(args.db)
    section("DB error_distributions breakdown")
    # Query the DB directly (read-only)
    import sqlite3
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT city, model, regime, lead_hours, mu, sigma, n_samples "
            "FROM error_distributions ORDER BY city, model, lead_hours, regime"
        ).fetchall()
    total_by_lead = Counter(r[3] for r in rows)
    print(f"  total rows: {len(rows)}  by lead_hours: {dict(total_by_lead)}")

    section("Lookup hits/misses per unique (city, lead, regime=stable_high)")
    hit = miss = 0
    miss_examples: list[str] = []
    for city, lh in unique_keys:
        fcs = forecast_cache.get((city, lh), [])
        for fc in fcs:
            fc_bucket = _bucket_lead(fc.lead_hours)
            dist = db.get_error_distribution(
                city, fc.model, SynopticRegime.STABLE_HIGH, fc_bucket
            )
            # HRRR/NAM borrow GFS
            if dist is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                dist = db.get_error_distribution(
                    city, WeatherModel.GFS, SynopticRegime.STABLE_HIGH, fc_bucket
                )
            if dist is None:
                miss += 1
                if len(miss_examples) < 5:
                    miss_examples.append(f"{city}/{fc.model.value}/{fc_bucket}h")
            else:
                hit += 1
    print(f"  hits: {hit}   misses: {miss}")
    if miss_examples:
        print(f"  miss examples: {miss_examples}")

    # ---------------------------------------------------------------------
    # 4. STRATEGY GATES — pass/fail per bracket
    # ---------------------------------------------------------------------
    bar("4. Strategy gates  (edge, Sharpe, min_price, tiered edge)")
    calc = BracketProbabilityCalculator()
    signals_passed: list[dict] = []
    gate_rejects = Counter()
    shown = 0
    show_limit = args.max_markets

    for (city, td), cs in sorted(by_city_date.items()):
        if args.city and city != args.city:
            continue
        days_ahead = max(0, (td - today_utc).days)
        if args.only_tomorrow and days_ahead != 1:
            continue
        raw_lead = 24 * (days_ahead + 1)
        lead_hours = _bucket_lead(raw_lead)
        fcs = forecast_cache.get((city, lead_hours), [])
        if not fcs:
            gate_rejects["no_forecast"] += len(cs)
            continue

        # Mirror strategy.py HRRR/NAM long-lead inference gate
        if lead_hours > 36:
            fcs = [fc for fc in fcs if fc.model not in {WeatherModel.HRRR, WeatherModel.NAM}]
            if not fcs:
                gate_rejects["no_forecast_after_hrrr_gate"] += len(cs)
                continue

        # Load distributions (mirror strategy.py:_load_dists)
        error_dists = []
        matching_forecasts = []
        for fc in fcs:
            fc_bucket = _bucket_lead(fc.lead_hours)
            dist = db.get_error_distribution(
                city, fc.model, SynopticRegime.STABLE_HIGH, fc_bucket
            )
            if dist is None and fc.model in {WeatherModel.HRRR, WeatherModel.NAM}:
                dist = db.get_error_distribution(
                    city, WeatherModel.GFS, SynopticRegime.STABLE_HIGH, fc_bucket
                )
            if dist is None:
                continue
            if abs(dist.mu) > 5.0 or dist.sigma > 5.0:
                continue
            error_dists.append(dist)
            matching_forecasts.append(fc)

        if not error_dists:
            gate_rejects["no_dist"] += len(cs)
            continue

        for c in cs:
            # model probability (ensemble)
            p_model, p_std = calc.ensemble_bracket_probability(
                forecasts=matching_forecasts,
                error_dists=error_dists,
                lower_f=c.lower_f,
                upper_f=c.upper_f,
            )
            market_price = c.market_price_yes
            # min entry price gate
            if market_price < 0.02:
                gate_rejects["min_entry_price"] += 1
                continue
            # Market range gate
            if not (0.15 <= market_price <= 0.75):
                gate_rejects["market_out_of_range"] += 1
                continue
            # Model probability gate
            if p_model < 0.55:
                gate_rejects["p_model<0.55"] += 1
                continue
            # Tiered edge
            min_edge = 0.05 + max(0, (p_model - 0.50) * 0.40)
            raw_edge = p_model - market_price
            if raw_edge < min_edge:
                gate_rejects["edge<tiered_min"] += 1
                continue
            # Sharpe per trade
            import math
            sharpe = raw_edge / max(1e-6, math.sqrt(market_price * (1 - market_price)))
            if sharpe < 0.15:
                gate_rejects["sharpe<0.15"] += 1
                continue

            # PASS — compute payout
            notional = 50.0  # display only
            n_shares = notional / market_price
            pnl_win = n_shares * (1 - market_price) * 0.98
            pnl_loss = -notional

            signals_passed.append(
                dict(
                    city=city,
                    target=td.isoformat(),
                    lead=lead_hours,
                    q=c.question[:60],
                    p_model=p_model,
                    market=market_price,
                    edge=raw_edge,
                    sharpe=sharpe,
                    pnl_win=pnl_win,
                    pnl_loss=pnl_loss,
                )
            )
            if shown < show_limit:
                shown += 1
                print(
                    f"  PASS  {city:12s} {td}  lead={lead_hours}h  "
                    f"P={p_model:.3f}±{p_std:.3f}  mkt={market_price:.3f}  "
                    f"edge={raw_edge:+.3f}  sharpe={sharpe:.2f}  "
                    f"pnl_win=${pnl_win:+.2f}  pnl_loss=${pnl_loss:+.2f}"
                )
                print(f"        q: {c.question[:80]}")

    section("Gate rejection histogram")
    for gate, n in sorted(gate_rejects.items(), key=lambda kv: -kv[1]):
        print(f"  {gate:24s} {n}")

    # ---------------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------------
    bar("SUMMARY")
    n_today = sum(1 for (c, td) in by_city_date if (td - today_utc).days == 0)
    n_tomorrow = sum(1 for (c, td) in by_city_date if (td - today_utc).days == 1)
    n_future = sum(1 for (c, td) in by_city_date if (td - today_utc).days >= 2)
    print(f"  Markets scanned:    {len(contracts)} brackets across "
          f"{len(by_city_date)} (city, target_date) groups")
    print(f"    TODAY:    {n_today} groups")
    print(f"    TOMORROW: {n_tomorrow} groups")
    print(f"    D+2+:     {n_future} groups")
    print(f"  Forecast cache:     {len(forecast_cache)} unique (city, lead) keys fetched")
    print(f"  Distribution hits:  {hit} / {hit + miss}  ({miss} misses)")
    print(f"  Signals passing all gates: {len(signals_passed)}")
    if signals_passed:
        tomorrow_signals = [s for s in signals_passed if s["lead"] == 48]
        today_signals = [s for s in signals_passed if s["lead"] == 24]
        print(f"    24h (today) signals:    {len(today_signals)}")
        print(f"    48h (tomorrow) signals: {len(tomorrow_signals)}")
        best = max(signals_passed, key=lambda s: s["edge"])
        print(f"  Best-edge signal: {best['city']} {best['target']} "
              f"lead={best['lead']}h  edge={best['edge']:+.3f}  P={best['p_model']:.3f}")

    # Verdict
    ok = True
    if len(contracts) == 0:
        print("\n  FAIL: scanner returned 0 contracts"); ok = False
    if n_tomorrow == 0:
        print("\n  WARN: no TOMORROW contracts — may be off-hours for Polymarket markets")
    if hit == 0 and (hit + miss) > 0:
        print("\n  FAIL: zero distribution hits — calibration DB not populated"); ok = False

    print()
    print("  END-TO-END: PASS" if ok else "  END-TO-END: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
