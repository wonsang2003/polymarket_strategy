"""GFS/ECMWF publish → Polymarket reprice lag monitor.

PURPOSE
-------
The only genuine edge in CLAUDE.md §12 is temporal arbitrage: the window between
an NWP model cycle publishing and Polymarket bracket prices updating. This
script measures that lag empirically so you can decide whether to build the
scheduled-reprice infrastructure (idea 6 in the roadmap).

HOW IT WORKS
------------
Two independent polling loops, both writing timestamped events to a single
JSONL log:

1. FORECAST LOOP (every --forecast-interval seconds, default 180)
   For each tracked (city, model, lead_hours) tuple, polls Open-Meteo.
   Logs a `forecast_change` event whenever the forecast value moves by at
   least --forecast-threshold °F (default 0.3) vs the last seen value.

   This is a PROXY for model-cycle publish. Open-Meteo's ingestion introduces
   its own lag (~30-60 min after NOAA publishes); for a more authoritative
   signal, uncomment the NOAA NOMADS check at the bottom of poll_forecast().

2. PRICE LOOP (every --price-interval seconds, default 60)
   For each tracked weather market (discovered via market scanner), polls the
   Polymarket Gamma API. Logs a `price_change` event whenever the YES-side
   midpoint moves by at least --price-threshold ¢ (default 1.0) vs last seen.

LOG FORMAT (JSONL, one event per line)
--------------------------------------
{"ts": "2026-04-18T14:02:31Z", "kind": "forecast_change", "city": "nyc",
 "model": "gfs", "lead_hours": 24, "valid_date": "2026-04-19",
 "old_f": 62.1, "new_f": 63.8, "delta_f": 1.7}

{"ts": "2026-04-18T14:14:02Z", "kind": "price_change", "city": "nyc",
 "market_id": "0x...", "token_id": "...", "question": "Will NYC be 65F+ on Apr 19?",
 "old_mid": 0.42, "new_mid": 0.48, "delta_cents": 6.0, "best_bid": 0.47,
 "best_ask": 0.49, "liquidity": 48320.0}

RUNNING
-------
In background, two weeks:
    nohup python tools/lag_monitor/monitor.py \
        --cities nyc,london,seoul,tokyo \
        --output-dir tools/lag_monitor/logs \
        > tools/lag_monitor/logs/monitor.stdout 2>&1 &

Then after two weeks:
    python tools/lag_monitor/analyze.py tools/lag_monitor/logs/events.jsonl
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

# Add project root so we can import the package
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_strat.api import PolymarketPublicClient  # noqa: E402
from polymarket_strat.domain.weather.models import CITY_REGISTRY, WeatherModel  # noqa: E402
from polymarket_strat.infrastructure.weather.grib_client import GribDataClient  # noqa: E402
from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner  # noqa: E402


# ----------------------------------------------------------------------
# Event emission
# ----------------------------------------------------------------------

@dataclass
class Event:
    ts: str
    kind: str
    payload: dict[str, Any]

    def to_line(self) -> str:
        d = {"ts": self.ts, "kind": self.kind, **self.payload}
        return json.dumps(d, ensure_ascii=False)


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Open in line-buffered append mode so writes survive SIGKILL
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")

    def write(self, event: Event) -> None:
        self._fh.write(event.to_line() + "\n")

    def close(self) -> None:
        self._fh.close()


def now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Forecast polling
# ----------------------------------------------------------------------

MODELS_TO_WATCH = [WeatherModel.GFS, WeatherModel.ECMWF]
LEAD_HOURS = [24, 48]  # D+1 and D+2 forecasts — most relevant to Polymarket markets


def poll_forecasts(
    client: GribDataClient,
    cities: list[str],
    last_seen: dict[tuple[str, str, int], float],
    log: EventLog,
    threshold_f: float,
) -> None:
    """Poll each (city, model, lead_hours) tuple; emit forecast_change if value moved."""
    for city_key in cities:
        station = CITY_REGISTRY.get(city_key)
        if not station:
            continue
        for model in MODELS_TO_WATCH:
            for lead_hours in LEAD_HOURS:
                try:
                    fc = client._fetch_open_meteo(
                        station=station,
                        model=model,
                        init_time=None,
                        lead_hours=lead_hours,
                    )
                except Exception as exc:
                    log.write(Event(now_utc_iso(), "poll_error", {
                        "scope": "forecast",
                        "city": city_key,
                        "model": model.value,
                        "lead_hours": lead_hours,
                        "error": str(exc),
                    }))
                    continue

                if fc is None:
                    continue

                key = (city_key, model.value, lead_hours)
                new_f = fc.forecast_high_f
                old_f = last_seen.get(key)

                if old_f is None:
                    # First observation — record baseline as 'seed' event
                    log.write(Event(now_utc_iso(), "forecast_seed", {
                        "city": city_key,
                        "model": model.value,
                        "lead_hours": lead_hours,
                        "valid_date": fc.valid_time.date().isoformat(),
                        "value_f": new_f,
                    }))
                    last_seen[key] = new_f
                    continue

                delta = new_f - old_f
                if abs(delta) >= threshold_f:
                    log.write(Event(now_utc_iso(), "forecast_change", {
                        "city": city_key,
                        "model": model.value,
                        "lead_hours": lead_hours,
                        "valid_date": fc.valid_time.date().isoformat(),
                        "old_f": round(old_f, 2),
                        "new_f": round(new_f, 2),
                        "delta_f": round(delta, 2),
                    }))
                    last_seen[key] = new_f

    # FUTURE: add NOAA NOMADS cycle-publish detection here. Example pattern:
    #
    #     import urllib.request
    #     cycle_url = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
    #                 f"gfs.{yyyymmdd}/{hh:02d}/atmos/gfs.t{hh:02d}z.pgrb2.0p25.f024"
    #     req = urllib.request.Request(cycle_url, method="HEAD")
    #     # 200 → cycle published. Track first-seen timestamp per cycle.
    #
    # The reason to skip it for v1: Open-Meteo changes are a sufficient proxy for
    # "new model data is being priced by consumers", since most Polymarket traders
    # are likely also on Open-Meteo or similar consumer APIs, not raw NOMADS.


# ----------------------------------------------------------------------
# Price polling
# ----------------------------------------------------------------------

def poll_prices(
    scanner: WeatherMarketScanner,
    last_seen: dict[str, float],
    log: EventLog,
    threshold_cents: float,
    cities_filter: set[str] | None,
) -> None:
    """Poll all active weather bracket markets; emit price_change when mid moves."""
    try:
        contracts = scanner.find_weather_bracket_markets()
    except Exception as exc:
        log.write(Event(now_utc_iso(), "poll_error", {
            "scope": "price", "error": str(exc),
        }))
        return

    for c in contracts:
        if cities_filter and c.city not in cities_filter:
            continue
        if not c.token_id_yes:
            continue

        # Mid-price: prefer (bid+ask)/2 when both sides quote; fallback to market_price_yes
        if c.best_bid_yes > 0 and c.best_ask_yes < 1.0 and c.best_ask_yes > c.best_bid_yes:
            mid = (c.best_bid_yes + c.best_ask_yes) / 2.0
        else:
            mid = c.market_price_yes

        key = c.token_id_yes
        old_mid = last_seen.get(key)

        if old_mid is None:
            log.write(Event(now_utc_iso(), "price_seed", {
                "city": c.city,
                "market_id": c.market_id,
                "token_id": c.token_id_yes,
                "question": c.question,
                "lower_f": c.lower_f,
                "upper_f": c.upper_f,
                "mid": round(mid, 4),
                "best_bid": c.best_bid_yes,
                "best_ask": c.best_ask_yes,
                "liquidity": c.liquidity,
            }))
            last_seen[key] = mid
            continue

        delta_cents = abs(mid - old_mid) * 100
        if delta_cents >= threshold_cents:
            log.write(Event(now_utc_iso(), "price_change", {
                "city": c.city,
                "market_id": c.market_id,
                "token_id": c.token_id_yes,
                "question": c.question,
                "lower_f": c.lower_f,
                "upper_f": c.upper_f,
                "old_mid": round(old_mid, 4),
                "new_mid": round(mid, 4),
                "delta_cents": round(delta_cents, 2),
                "best_bid": c.best_bid_yes,
                "best_ask": c.best_ask_yes,
                "liquidity": c.liquidity,
            }))
            last_seen[key] = mid


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

_RUNNING = True


def _handle_signal(signum, frame):
    global _RUNNING
    _RUNNING = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cities", type=lambda s: s.split(","), default=["nyc", "london", "seoul", "tokyo"],
                        help="Comma-sep city keys to monitor (default nyc,london,seoul,tokyo)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tools" / "lag_monitor" / "logs")
    parser.add_argument("--forecast-interval", type=int, default=180, help="Seconds between forecast polls (default 180)")
    parser.add_argument("--price-interval", type=int, default=60, help="Seconds between price polls (default 60)")
    parser.add_argument("--forecast-threshold", type=float, default=0.3, help="°F change required to log (default 0.3)")
    parser.add_argument("--price-threshold", type=float, default=1.0, help="cents change required to log (default 1.0)")
    parser.add_argument("--all-cities", action="store_true", help="Monitor prices for every weather market, not just --cities")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log_path = args.output_dir / "events.jsonl"
    log = EventLog(log_path)
    log.write(Event(now_utc_iso(), "monitor_start", {
        "cities": args.cities,
        "forecast_interval_s": args.forecast_interval,
        "price_interval_s": args.price_interval,
        "forecast_threshold_f": args.forecast_threshold,
        "price_threshold_cents": args.price_threshold,
        "all_cities_prices": args.all_cities,
    }))

    grib = GribDataClient()
    polymarket = PolymarketPublicClient()
    scanner = WeatherMarketScanner(client=polymarket)

    last_forecast: dict[tuple[str, str, int], float] = {}
    last_price: dict[str, float] = {}
    cities_filter = None if args.all_cities else set(args.cities)

    last_forecast_poll = 0.0
    last_price_poll = 0.0

    print(f"[lag-monitor] writing to {log_path}")
    print(f"[lag-monitor] tracking cities: {args.cities}")
    print(f"[lag-monitor] forecast every {args.forecast_interval}s, price every {args.price_interval}s")
    print("[lag-monitor] SIGINT/SIGTERM to stop")

    while _RUNNING:
        now = time.time()
        if now - last_forecast_poll >= args.forecast_interval:
            poll_forecasts(grib, args.cities, last_forecast, log, args.forecast_threshold)
            last_forecast_poll = now
        if now - last_price_poll >= args.price_interval:
            poll_prices(scanner, last_price, log, args.price_threshold, cities_filter)
            last_price_poll = now
        time.sleep(1.0)

    log.write(Event(now_utc_iso(), "monitor_stop", {}))
    log.close()
    print("[lag-monitor] stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
