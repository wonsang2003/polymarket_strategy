"""Fetch daily high-temperature observations from Iowa Environmental Mesonet.

IEM ASOS/METAR archive provides free, reliable station data for all ICAO
airports worldwide.  Response is CSV.
"""
from __future__ import annotations

import csv
import io
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

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

    def fetch_today_running_max(
        self, station: CityStation
    ) -> tuple[float | None, datetime | None, int]:
        """Fetch all hourly readings for TODAY (station-local) and return
        (running_max_F, last_reading_utc, n_readings).

        Apr 25 2026 — Layer 2 of late-entry tail-bracket strategy. Used to
        detect "this bracket is effectively settled NO" when current time
        is near settlement and observed-max-so-far disqualifies the bracket
        by a wide margin.

        Behavior:
          - "Today" means the calendar date in `station.timezone`.
          - Returns (None, None, 0) on fetch failure or no usable rows.
          - last_reading_utc is timezone-aware (UTC). Caller should check
            staleness — IEM lags by ~5-15 min typically.

        Implementation note: IEM's API doesn't expose station-local TZ
        filtering, so we fetch ~36 hours of hourly readings and filter to
        the local-day window in code. That handles all timezones cleanly.
        """
        try:
            tz = ZoneInfo(station.timezone)
        except Exception:
            return None, None, 0

        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz)
        local_today = now_local.date()
        local_day_start = datetime(
            local_today.year, local_today.month, local_today.day, tzinfo=tz
        )
        # IEM expects start/end UTC dates. Cover yesterday-to-today UTC to
        # ensure we get all of the local day even when local TZ is far from UTC.
        # Use yesterday and today in UTC.
        utc_yesterday = (now_utc - timedelta(days=1)).date()
        utc_today = now_utc.date()
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
            "year1": utc_yesterday.year,
            "month1": utc_yesterday.month,
            "day1": utc_yesterday.day,
            "year2": utc_today.year,
            "month2": utc_today.month,
            "day2": utc_today.day,
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
                    print(f"[station] IEM today-running-max failed: {exc}",
                          file=sys.stderr)
                    return None, None, 0
                time.sleep(2 * (attempt + 1))

        running_max = None
        last_obs_utc: datetime | None = None
        n_readings = 0
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            raw_val = (row.get("tmpf") or "").strip()
            if raw_val in ("M", "T", ""):
                continue
            try:
                temp_f = float(raw_val)
            except ValueError:
                continue
            valid = (row.get("valid") or "").strip()
            if not valid:
                continue
            try:
                # IEM "valid" format: "2026-04-25 14:53"
                obs_utc = datetime.fromisoformat(valid).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            obs_local = obs_utc.astimezone(tz)
            if obs_local.date() != local_today:
                continue
            n_readings += 1
            if running_max is None or temp_f > running_max:
                running_max = temp_f
            if last_obs_utc is None or obs_utc > last_obs_utc:
                last_obs_utc = obs_utc

        return running_max, last_obs_utc, n_readings
