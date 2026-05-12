"""For each rebalance-exited tail-NO trade, compute the counterfactual P&L
if we had held to natural settlement.

For NO buy at entry_price `p`:
  shares      = notional / p
  WIN  payoff = shares * (1 - p) * 0.98          (fee on winnings only)
  LOSS payoff = -notional

This script compares actual rebalance P&L vs the hold-to-settle P&L. If
hold > rebalance on average, the rebalance was leaking EV. If hold < rebalance,
the rebalance correctly dodged worse losses.

ONLY runs counterfactual on trades whose target_date is in the past (i.e.
the bracket has actually resolved). For Apr 26 trades, Asian/Europe settle
before US.
"""
import sqlite3
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from polymarket_strat.domain.weather.models import CITY_REGISTRY
from polymarket_strat.infrastructure.weather.station_client import (
    StationObservationClient,
)
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    rows = c.execute(
        """SELECT id, city, target_date, side, token_side, market_id,
                  notional, entry_price, pnl, outcome, exit_reason,
                  bracket_lower_f, bracket_upper_f, category
           FROM trade_history
           WHERE category = 'weather_tail_no'
             AND outcome IS NOT NULL
           ORDER BY id"""
    ).fetchall()

    print(f"settled tail-NO trades: {len(rows)}\n")
    if not rows:
        print("nothing to analyze")
        return 0

    station_client = StationObservationClient()
    grib_client = GribDataClient()

    now_utc = datetime.now(timezone.utc)
    fee = 0.02

    # Group trades by (city, target_date) for one observation fetch each.
    by_settlement = {}
    for r in rows:
        key = (r["city"], r["target_date"])
        by_settlement.setdefault(key, []).append(r)

    # Fetch observed high per (city, date) — only if settlement has passed.
    obs_cache = {}
    for (city, tgt) in by_settlement:
        if city not in CITY_REGISTRY:
            continue
        station = CITY_REGISTRY[city]
        try:
            tz = ZoneInfo(station.timezone)
        except Exception:
            continue
        d = datetime.strptime(tgt, "%Y-%m-%d").date()
        settle_local = datetime.combine(d, time(17, 0), tzinfo=tz)
        if settle_local > now_utc:
            obs_cache[(city, tgt)] = ("PENDING", None)
            continue
        # Settlement passed — try IEM first, then ERA5 fallback.
        observed = None
        try:
            obs = station_client.fetch_high_for_date(station, d)
            if obs is not None:
                observed = ("IEM", obs)
        except Exception as e:
            pass
        if observed is None:
            try:
                obs = grib_client.fetch_era5_high_for_date(station, d)
                if obs is not None:
                    observed = ("ERA5", obs)
            except Exception as e:
                pass
        obs_cache[(city, tgt)] = observed if observed else ("NO_DATA", None)

    # Compute counterfactual per trade.
    print(
        f"  {'id':>3} {'city':<13} {'tgt':<11} {'br':<14} "
        f"{'entry':>6} {'notnl':>5} {'rebal_pnl':>10} "
        f"{'observed':>9} {'src':<6} {'bracket':<9} "
        f"{'cf_pnl':>9} {'delta':>9}"
    )

    actual_total = 0.0
    cf_total = 0.0
    cf_known_count = 0
    cf_wins = 0

    for r in rows:
        rid = r["id"]
        city = r["city"]
        tgt = r["target_date"]
        lo = float(r["bracket_lower_f"])
        up = float(r["bracket_upper_f"])
        entry = float(r["entry_price"])
        notional = float(r["notional"])
        rebal_pnl = float(r["pnl"])
        actual_total += rebal_pnl

        src, observed = obs_cache.get((city, tgt), ("NO_DATA", None))

        bracket_str = f"[{lo:.0f},{up:.0f}]F"

        if observed is None:
            # Pending or no data — show but don't include in counterfactual aggregate.
            print(
                f"  #{rid:>3} {city[:13]:<13} {tgt:<11} {bracket_str:<14} "
                f"{entry:>6.3f} ${notional:>4.0f} ${rebal_pnl:>+9.2f} "
                f"{'N/A':>9} {src:<6} {'?':<9} {'—':>9} {'—':>9}"
            )
            continue

        observed_f = float(observed)
        in_bracket = lo <= observed_f <= up
        # NO wins iff observed is NOT in bracket
        no_wins = not in_bracket
        cf_pnl = (
            (notional / entry) * (1 - entry) * (1 - fee) if no_wins
            else -notional
        )
        delta = cf_pnl - rebal_pnl
        cf_total += cf_pnl
        cf_known_count += 1
        if no_wins:
            cf_wins += 1

        bracket_label = "IN " if in_bracket else "OUT"
        print(
            f"  #{rid:>3} {city[:13]:<13} {tgt:<11} {bracket_str:<14} "
            f"{entry:>6.3f} ${notional:>4.0f} ${rebal_pnl:>+9.2f} "
            f"{observed_f:>8.1f}F {src:<6} {bracket_label:<9} "
            f"${cf_pnl:>+8.2f} ${delta:>+8.2f}"
        )

    # Aggregate
    print()
    print("=" * 90)
    print("AGGREGATE")
    print("=" * 90)
    print(f"  Settled tail-NO trades total              : {len(rows)}")
    print(f"  Counterfactual computable (target passed) : {cf_known_count}")
    print(f"  Pending / no obs                          : {len(rows) - cf_known_count}")
    print()
    if cf_known_count > 0:
        # actual_total over the SAME subset
        actual_cf_subset = sum(
            float(r["pnl"]) for r in rows
            if obs_cache.get((r["city"], r["target_date"]), (None, None))[1] is not None
        )
        print(f"  Actual rebalance P&L  (these {cf_known_count})     : ${actual_cf_subset:+.2f}")
        print(f"  Counterfactual hold P&L (these {cf_known_count}): ${cf_total:+.2f}")
        delta = cf_total - actual_cf_subset
        print(f"  Delta (hold − rebal)                      : ${delta:+.2f}")
        wr_cf = cf_wins / cf_known_count * 100
        print(f"  Counterfactual NO-win rate                : {cf_wins}/{cf_known_count} = {wr_cf:.1f}%")
        print()
        if delta > 0:
            print("  → Hold-to-settlement would have been better. Rebalance was leaking EV.")
        elif delta < 0:
            print("  → Rebalance saved us money. Hold-to-settlement would have been worse.")
        else:
            print("  → Wash. No measurable difference.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
