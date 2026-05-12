"""For every currently open position, compute the actual settlement time
in UTC + station-local + KST, plus hours-from-now. Use this to plan when
we'll have natural-settlement data points to evaluate tail-NO.

Also project: at the current rate of tail-NO entries per cron tick, when
do we hit n=30 natural settlements (the threshold for statistical
inference)?
"""
import sqlite3
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Match strategy.py — settlement at 17:00 station-local
SETTLEMENT_LOCAL_HOUR = 17

# Same registry as polymarket_strat/domain/weather/models.py
CITY_TZ = {
    "nyc": "America/New_York",
    "chicago": "America/Chicago",
    "toronto": "America/Toronto",
    "miami": "America/New_York",
    "atlanta": "America/New_York",
    "la": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "london": "Europe/London",
    "amsterdam": "Europe/Amsterdam",
    "munich": "Europe/Berlin",
    "milan": "Europe/Rome",
    "seoul": "Asia/Seoul",
    "tokyo": "Asia/Tokyo",
    "hong_kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai",
    "buenos_aires": "America/Argentina/Buenos_Aires",
    "sao_paulo": "America/Sao_Paulo",
    "mexico_city": "America/Mexico_City",
    "wellington": "Pacific/Auckland",
    "sydney": "Australia/Sydney",
    "dubai": "Asia/Dubai",
}

KNOWN_TAIL_NO_MARKETS = {
    "2064941", "2064826", "2074435", "2074503", "2065058", "2074468",
    "2064881", "2074477", "2064827", "2064860", "2074311", "2064848",
    "2074469", "2082313",
}


def settlement_dt(city: str, target_date: str) -> datetime | None:
    tz_name = CITY_TZ.get(city)
    if tz_name is None:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return None
    d = datetime.strptime(target_date, "%Y-%m-%d").date()
    local = datetime.combine(d, time(SETTLEMENT_LOCAL_HOUR, 0), tzinfo=tz)
    return local.astimezone(timezone.utc)


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    now_utc = datetime.now(timezone.utc)
    kst = ZoneInfo("Asia/Seoul")
    print(f"now_utc = {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"now_kst = {now_utc.astimezone(kst).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    rows = c.execute(
        """SELECT id, city, side, target_date, notional, market_id, category,
                  created_at, entry_price, model_prob
           FROM trade_history
           WHERE outcome IS NULL
           ORDER BY target_date, created_at"""
    ).fetchall()
    print(f"open positions: {len(rows)}")
    print()
    print(f"  {'id':>3} {'city':<14} {'tgt_date':<11} {'side':<6} "
          f"{'category':<18} {'settles_utc':<19} {'settles_kst':<19} "
          f"{'h_from_now':>10}")

    settle_times: list[tuple[int, str, str, datetime, bool]] = []
    for r in rows:
        sdt = settlement_dt(r["city"], r["target_date"])
        is_tn = (
            r["market_id"] in KNOWN_TAIL_NO_MARKETS
            or (r["category"] or "").startswith("weather_tail")
        )
        if sdt is None:
            print(f"  #{r['id']:>3} {(r['city'] or 'NA'):<14} "
                  f"{r['target_date']} unknown_tz")
            continue
        kst_dt = sdt.astimezone(kst)
        h = (sdt - now_utc).total_seconds() / 3600
        h_str = f"{h:+.1f}h"
        cat = (r["category"] or ("[old]" if not is_tn else "tail?"))[:18]
        marker = "[TN]" if is_tn else "    "
        print(f"  {marker} #{r['id']:>3} {(r['city'] or 'NA'):<14} "
              f"{r['target_date']} {(r['side'] or '')[:6]:<6} "
              f"{cat:<18} {sdt.strftime('%Y-%m-%d %H:%M'):<19} "
              f"{kst_dt.strftime('%Y-%m-%d %H:%M'):<19} {h_str:>10}")
        settle_times.append((r["id"], r["city"], r["category"] or "", sdt, is_tn))

    # When does the first / last position settle?
    if settle_times:
        first = min(settle_times, key=lambda x: x[3])
        last = max(settle_times, key=lambda x: x[3])
        h_first = (first[3] - now_utc).total_seconds() / 3600
        h_last = (last[3] - now_utc).total_seconds() / 3600
        print()
        print(f"first settle: #{first[0]} {first[1]} at "
              f"{first[3].astimezone(kst).strftime('%Y-%m-%d %H:%M KST')} "
              f"(in {h_first:+.1f}h)")
        print(f"last  settle: #{last[0]} {last[1]} at "
              f"{last[3].astimezone(kst).strftime('%Y-%m-%d %H:%M KST')} "
              f"(in {h_last:+.1f}h)")

    # Tail-NO subset
    tn_open = [s for s in settle_times if s[4]]
    print(f"\ntail-NO open: {len(tn_open)}")
    if tn_open:
        first_tn = min(tn_open, key=lambda x: x[3])
        last_tn = max(tn_open, key=lambda x: x[3])
        print(f"  first tail-NO settle: #{first_tn[0]} {first_tn[1]} at "
              f"{first_tn[3].astimezone(kst).strftime('%Y-%m-%d %H:%M KST')}")
        print(f"  last  tail-NO settle: #{last_tn[0]} {last_tn[1]} at "
              f"{last_tn[3].astimezone(kst).strftime('%Y-%m-%d %H:%M KST')}")

    # Projection: at current placement rate, when do we have n=30 natural
    # settlements? Look at the last 24h placement rate.
    print()
    rate_rows = c.execute(
        """SELECT COUNT(*) FROM trade_history
           WHERE category = 'weather_tail_no'
             AND created_at >= datetime('now', '-24 hours')"""
    ).fetchone()
    rate_24h = rate_rows[0] if rate_rows else 0
    print(f"tail-NO placements in last 24h (post-backfill): {rate_24h}")
    if rate_24h > 0:
        # Most settle within 4-72h of placement (LEAD_HOURS_MAX=72).
        # Optimistic: assume average 24h to settlement.
        n_target = 30
        days_to_n30 = n_target / max(rate_24h, 1)
        print(f"  estimated days to n={n_target}: {days_to_n30:.1f} days "
              f"(assuming current rate continues)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
