"""Fetch daily high-temperature observations from Iowa Environmental Mesonet.

IEM ASOS/METAR archive provides free, reliable station data for all ICAO
airports worldwide.  Response is CSV.
"""
from __future__ import annotations

import csv
import io
import ssl
import time
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_strat.domain.weather.models import CityStation, StationObservation, c_to_f

IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


class StationObservationClient:
    """Fetch verified daily-high temperatures from IEM ASOS archive."""

    def __init__(self, *, timeout: int = 30):
        self.timeout = timeout
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def fetch_daily_highs(
        self,
        station: CityStation,
        *,
        start: date,
        end: date,
    ) -> list[StationObservation]:
        """Fetch daily max temperatures for a station over a date range.

        Downloads hourly ASOS observations from IEM and computes the daily
        maximum for each calendar day (station local timezone).
        Returns temperatures in Fahrenheit.
        """
        params = {
            "station": station.station_id,
            "data": "tmpf",
            "tz": "Etc/UTC",
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "missing": "M",
            "trace": "T",
            "direct": "no",
            "report_type": "3",
            "year1": start.year,
            "month1": start.month,
            "day1": start.day,
            "year2": end.year,
            "month2": end.month,
            "day2": end.day,
        }
        url = f"{IEM_BASE}?{urlencode(params)}"
        req = Request(url, headers={"User-Agent": "polymarket-strat/0.1"})
        text = ""
        for attempt in range(3):
            try:
                with urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                    text = resp.read().decode("utf-8")
                break
            except (HTTPError, URLError) as exc:
                if attempt == 2:
                    import sys
                    print(f"[station] IEM request failed after 3 attempts: {exc}", file=sys.stderr)
                    return []
                time.sleep(5 * (attempt + 1))

        # Accumulate hourly readings keyed by UTC date (good enough for daily max)
        daily_highs: dict[date, float] = {}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            raw_val = (row.get("tmpf") or row.get("max_tmpf") or "").strip()
            if raw_val in ("M", "T", ""):
                continue
            try:
                temp_f = float(raw_val)
            except ValueError:
                continue
            date_str = (row.get("valid") or row.get("day") or "").strip()
            if not date_str:
                continue
            try:
                obs_date = date.fromisoformat(date_str[:10])
            except ValueError:
                continue
            if temp_f > daily_highs.get(obs_date, float("-inf")):
                daily_highs[obs_date] = temp_f

        return [
            StationObservation(
                city=station.city,
                station_id=station.station_id,
                obs_date=obs_date,
                observed_high_f=high_f,
                source="IEM",
            )
            for obs_date, high_f in sorted(daily_highs.items())
        ]
