"""NWP model forecast fetching via Open-Meteo.

Open-Meteo (https://open-meteo.com) is a free API that sources data from the
same NWP models described in the original GRIB docstrings (GFS, ECMWF, HRRR)
and returns JSON — no cfgrib / eccodes installation required.

If you later want raw GRIB parsing (e.g. to extract ensemble spread directly
from model members), the URL templates and xarray patterns from the original
stubs are preserved in each method's docstring.
"""
from __future__ import annotations

import json
import ssl
import sys
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_strat.domain.weather.models import (
    CityStation,
    TemperatureForecast,
    WeatherModel,
    c_to_f,
)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_OPEN_METEO_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Open-Meteo model identifiers for each WeatherModel.
# HRRR is only available as hourly data (gfs_hrrr); daily max is computed locally.
_OM_MODEL_DAILY = {
    WeatherModel.GFS:  "gfs_seamless",
    WeatherModel.ECMWF: "ecmwf_ifs025",
    WeatherModel.NAM:  "gfs_seamless",
}
_OM_MODEL_HOURLY = {
    WeatherModel.HRRR: "gfs_hrrr",
}
# Ensemble models on Open-Meteo (31 GFS + 51 ECMWF = 82 members)
_OM_ENSEMBLE_MODELS = {
    WeatherModel.GFS: "gfs_seamless",
    WeatherModel.ECMWF: "ecmwf_ifs025",
}


def _http_get_json(url: str, params: dict) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    req = Request(full_url, headers={"User-Agent": "polymarket-strat/0.1"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read().decode())


class GribDataClient:
    """Fetch NWP temperature forecasts for city stations.

    Uses Open-Meteo for simplicity.  To switch to raw GRIB files, install
    cfgrib + eccodes and replace _fetch_open_meteo with the URL-template
    approach documented in each method's docstring.
    """

    def __init__(self, *, cache_dir: Path = Path("data/weather/grib")):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_open_meteo(
        self,
        station: CityStation,
        model: WeatherModel,
        *,
        init_time: datetime | None,
        lead_hours: int,
    ) -> TemperatureForecast | None:
        """Call Open-Meteo and return the daily high for the target date.

        GFS and ECMWF use the daily endpoint (temperature_2m_max).
        HRRR uses the hourly endpoint (gfs_hrrr model) and takes the max.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        base = init_time or now
        target_dt = base + timedelta(hours=lead_hours)
        target_date = target_dt.date().isoformat()

        if model in _OM_MODEL_HOURLY:
            return self._fetch_hourly_max(station, model, base, target_dt, target_date)

        om_model = _OM_MODEL_DAILY[model]
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max",
            "models": om_model,
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": target_date,
            "end_date": target_date,
        }
        try:
            data = _http_get_json(_OPEN_METEO_URL, params)
        except Exception as exc:
            print(f"[grib] Open-Meteo error ({om_model}, {station.city}): {exc}", file=sys.stderr)
            return None

        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if not temps or temps[0] is None:
            return None

        return TemperatureForecast(
            city=station.city,
            model=model,
            init_time=base,
            valid_time=target_dt,
            lead_hours=lead_hours,
            forecast_high_f=float(temps[0]),
            ensemble_spread_f=0.0,
        )

    def _fetch_hourly_max(
        self,
        station: CityStation,
        model: WeatherModel,
        base: datetime,
        target_dt: datetime,
        target_date: str,
    ) -> TemperatureForecast | None:
        """Fetch hourly temperature and return the daily maximum."""
        om_model = _OM_MODEL_HOURLY[model]
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "temperature_2m",
            "models": om_model,
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": target_date,
            "end_date": target_date,
        }
        try:
            data = _http_get_json(_OPEN_METEO_URL, params)
        except Exception as exc:
            print(f"[grib] Open-Meteo error ({om_model}, {station.city}): {exc}", file=sys.stderr)
            return None

        hourly_temps = [t for t in data.get("hourly", {}).get("temperature_2m", []) if t is not None]
        if not hourly_temps:
            return None

        return TemperatureForecast(
            city=station.city,
            model=model,
            init_time=base,
            valid_time=target_dt,
            lead_hours=int((target_dt - base).total_seconds() / 3600),
            forecast_high_f=float(max(hourly_temps)),
            ensemble_spread_f=0.0,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_gfs_forecast(
        self,
        station: CityStation,
        *,
        init_time: datetime | None = None,
        lead_hours: int = 24,
    ) -> TemperatureForecast | None:
        """Fetch GFS 2m temperature forecast for a city.

        Currently implemented via Open-Meteo (model: gfs_seamless).

        Raw GRIB URL template (requires cfgrib + eccodes):
            https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/
            gfs.{YYYYMMDD}/{HH}/atmos/gfs.t{HH}z.pgrb2.0p25.f{FFF:03d}

        Parsing with xarray + cfgrib:
            ds = xr.open_dataset(path, engine='cfgrib',
                                 filter_by_keys={'typeOfLevel': 'heightAboveGround',
                                                 'level': 2, 'shortName': '2t'})
            t_k = float(ds['t2m'].sel(latitude=station.lat,
                                      longitude=station.lon % 360,
                                      method='nearest').values)
            forecast_high_f = (t_k - 273.15) * 9/5 + 32
        """
        return self._fetch_open_meteo(
            station, WeatherModel.GFS, init_time=init_time, lead_hours=lead_hours
        )

    def fetch_ecmwf_forecast(
        self,
        station: CityStation,
        *,
        init_time: datetime | None = None,
        lead_hours: int = 24,
    ) -> TemperatureForecast | None:
        """Fetch ECMWF IFS 2m temperature forecast for a city.

        Currently implemented via Open-Meteo (model: ecmwf_ifs04).

        Raw GRIB URL template (requires cfgrib + eccodes):
            https://data.ecmwf.int/forecasts/{YYYYMMDD}/{HH}z/ifs/0p25/oper/
            {YYYYMMDD}{HH}0000-{LEAD}h-oper-fc.grib2

        Parsing: same xarray pattern as GFS, variable '2t' (Kelvin).
        """
        return self._fetch_open_meteo(
            station, WeatherModel.ECMWF, init_time=init_time, lead_hours=lead_hours
        )

    def fetch_hrrr_forecast(
        self,
        station: CityStation,
        *,
        init_time: datetime | None = None,
        lead_hours: int = 6,
    ) -> TemperatureForecast | None:
        """Fetch HRRR 2m temperature forecast (US cities only).

        Currently implemented via Open-Meteo (model: hrrr_conus).

        Raw GRIB URL template (requires cfgrib + eccodes):
            https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/
            hrrr.{YYYYMMDD}/conus/hrrr.t{HH}z.wrfsfcf{FF:02d}.grib2

        Note: HRRR is CONUS-only. Returns None for non-US cities.
        """
        us_groups = {"us_northeast", "us_west", "us_south"}
        # mexico_city is tagged US_SOUTH for correlation-group/risk purposes
        # but sits at 19.44°N — outside HRRR's CONUS grid (~21°N–50°N).
        # Open-Meteo returns HTTP 400 for its coords. Skip.
        if station.correlation_group.value not in us_groups or station.city == "mexico_city":
            return None
        return self._fetch_open_meteo(
            station, WeatherModel.HRRR, init_time=init_time, lead_hours=min(lead_hours, 48)
        )

    def fetch_all_models(
        self,
        station: CityStation,
        *,
        init_time: datetime | None = None,
        lead_hours: int = 24,
    ) -> list[TemperatureForecast]:
        """Fetch forecasts from all available models and annotate ensemble spread.

        The inter-model spread (max forecast - min forecast across models) is
        written back onto each result's ensemble_spread_f so the RegimeClassifier
        can distinguish stable vs. active weather regimes.
        """
        fetchers = [
            (self.fetch_gfs_forecast,  {"lead_hours": lead_hours}),
            (self.fetch_ecmwf_forecast, {"lead_hours": lead_hours}),
            (self.fetch_hrrr_forecast,  {"lead_hours": min(lead_hours, 48)}),
        ]
        results: list[TemperatureForecast] = []
        for fetcher, kwargs in fetchers:
            fc = fetcher(station, init_time=init_time, **kwargs)
            if fc is not None:
                results.append(fc)

        # Annotate each forecast with the inter-model spread so the regime
        # classifier has something to work with even without native ensemble data.
        if len(results) >= 2:
            highs = [fc.forecast_high_f for fc in results]
            spread = max(highs) - min(highs)
            for fc in results:
                fc.ensemble_spread_f = spread

        return results

    # ------------------------------------------------------------------
    # Ensemble forecasts (82 members: 31 GFS + 51 ECMWF)
    # ------------------------------------------------------------------

    def fetch_ensemble_members(
        self,
        station: CityStation,
        *,
        target_date: date | None = None,
    ) -> dict[str, list[float]]:
        """Fetch ensemble member forecasts from Open-Meteo ensemble API.

        Returns dict keyed by model name with lists of daily-high forecasts
        (one per ensemble member, in Fahrenheit).

        Example return:
            {"gfs_seamless": [66.1, 65.8, 67.2, ...],   # 31 members
             "ecmwf_ifs025": [65.5, 66.0, 64.9, ...]}   # 51 members
        """
        td = (target_date or (datetime.now(UTC).date() + timedelta(days=1))).isoformat()
        result: dict[str, list[float]] = {}

        for model, om_name in _OM_ENSEMBLE_MODELS.items():
            params = {
                "latitude": station.lat,
                "longitude": station.lon,
                "daily": "temperature_2m_max",
                "models": om_name,
                "temperature_unit": "fahrenheit",
                "timezone": station.timezone,
                "start_date": td,
                "end_date": td,
            }
            try:
                data = _http_get_json(_OPEN_METEO_ENSEMBLE_URL, params)
            except Exception as exc:
                print(f"[grib] Ensemble fetch error ({om_name}, {station.city}): {exc}", file=sys.stderr)
                continue

            # Open-Meteo ensemble returns daily data with member arrays
            daily = data.get("daily", {})
            members: list[float] = []
            for key, values in daily.items():
                if key.startswith("temperature_2m_max") and isinstance(values, list):
                    for v in values:
                        if v is not None:
                            members.append(float(v))
            if members:
                result[om_name] = members

        return result

    def fetch_ensemble_spread_stats(
        self,
        station: CityStation,
        *,
        target_date: date | None = None,
    ) -> dict[str, float]:
        """Compute ensemble spread statistics for regime classification.

        Returns dict with keys: spread, std, skewness, mean, n_members, cape_max.
        All temperatures in Fahrenheit.
        """
        import statistics as _stats

        members_by_model = self.fetch_ensemble_members(station, target_date=target_date)
        all_members: list[float] = []
        for m_list in members_by_model.values():
            all_members.extend(m_list)

        if len(all_members) < 3:
            return {"spread": 0.0, "std": 0.0, "skewness": 0.0,
                    "mean": 0.0, "n_members": 0, "cape_max": 0.0}

        mean_val = _stats.fmean(all_members)
        std_val = _stats.stdev(all_members)
        spread = max(all_members) - min(all_members)

        # Skewness (Fisher)
        n = len(all_members)
        if std_val > 0 and n >= 3:
            skew = sum(((x - mean_val) / std_val) ** 3 for x in all_members) * n / ((n - 1) * (n - 2))
        else:
            skew = 0.0

        # Fetch CAPE from standard forecast API
        cape_max = self._fetch_cape_max(station, target_date=target_date)

        return {
            "spread": spread,
            "std": std_val,
            "skewness": skew,
            "mean": mean_val,
            "n_members": n,
            "cape_max": cape_max,
        }

    def _fetch_cape_max(
        self,
        station: CityStation,
        *,
        target_date: date | None = None,
    ) -> float:
        """Fetch max CAPE for a day from Open-Meteo hourly forecast."""
        td = (target_date or (datetime.now(UTC).date() + timedelta(days=1))).isoformat()
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "cape",
            "timezone": station.timezone,
            "start_date": td,
            "end_date": td,
        }
        try:
            data = _http_get_json(_OPEN_METEO_URL, params)
        except Exception:
            return 0.0

        cape_vals = [v for v in data.get("hourly", {}).get("cape", []) if v is not None]
        return max(cape_vals) if cape_vals else 0.0

    # ------------------------------------------------------------------
    # Historical forecast archive (for calibration backfill)
    # ------------------------------------------------------------------

    def fetch_archived_forecasts(
        self,
        station: CityStation,
        model: WeatherModel,
        *,
        start: date,
        end: date,
        lead_days: int = 1,
    ) -> dict[date, float]:
        """Fetch archived model forecasts from Open-Meteo previous-runs API.

        Unlike fetch_historical_highs (which uses ERA5/reanalysis archive),
        this fetches what the model ACTUALLY PREDICTED — enabling true
        forecast-vs-observation error calibration over long periods.

        `lead_days` selects which previous run is returned:
          - 1 → forecast issued ~24h before each valid date (24h lead)
          - 2 → forecast issued ~48h before each valid date (48h lead)
          - 3 → forecast issued ~72h before each valid date (72h lead)

        Open-Meteo's previous-runs API only exposes the `_previous_dayN` suffix
        on **hourly** variables (not daily aggregates), so for lead_days >= 2
        we fetch hourly `temperature_2m_previous_day{N-1}`, group by station-
        local date (the API already returns timestamps in station TZ because
        we pass `timezone=station.timezone`), and take the max per local day.
        For lead_days == 1 we use the cheaper daily aggregate endpoint.

        Returns {valid_date: forecast_high_f} in Fahrenheit.
        """
        om_model = _OM_MODEL_DAILY.get(model, "gfs_seamless")
        use_hourly = lead_days >= 2
        if use_hourly:
            hourly_var = f"temperature_2m_previous_day{lead_days - 1}"
        else:
            hourly_var = ""  # unused

        # Previous-runs API may limit date ranges; chunk into 90-day windows
        result: dict[date, float] = {}
        chunk_start = start

        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=89), end)
            params: dict[str, Any] = {
                "latitude": station.lat,
                "longitude": station.lon,
                "models": om_model,
                "temperature_unit": "fahrenheit",
                "timezone": station.timezone,
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
            }
            if use_hourly:
                params["hourly"] = hourly_var
            else:
                params["daily"] = "temperature_2m_max"

            try:
                data = _http_get_json(_OPEN_METEO_PREVIOUS_RUNS_URL, params)
            except Exception as exc:
                print(
                    f"[grib] Previous-runs fetch error ({om_model}, {station.city}, "
                    f"lead_days={lead_days}, {chunk_start}-{chunk_end}): {exc}",
                    file=sys.stderr,
                )
                chunk_start = chunk_end + timedelta(days=1)
                continue

            if use_hourly:
                # Parse hourly → group by local date → take max per day.
                hourly = data.get("hourly", {})
                times_list = hourly.get("time", [])
                temps_list: list = []
                for key, values in hourly.items():
                    if key.startswith("temperature_2m") and isinstance(values, list):
                        temps_list = values
                        break
                per_day_max: dict[date, float] = {}
                for ts, t in zip(times_list, temps_list):
                    if t is None:
                        continue
                    # Open-Meteo returns ISO timestamps in the requested timezone;
                    # the date prefix is the station-local calendar day.
                    try:
                        d = date.fromisoformat(ts[:10])
                    except ValueError:
                        continue
                    val = float(t)
                    if d not in per_day_max or val > per_day_max[d]:
                        per_day_max[d] = val
                result.update(per_day_max)
            else:
                daily = data.get("daily", {})
                dates_list = daily.get("time", [])
                temps_daily: list = []
                for key, values in daily.items():
                    if key.startswith("temperature_2m_max") and isinstance(values, list):
                        temps_daily = values
                        break
                for d_str, t in zip(dates_list, temps_daily):
                    if t is None:
                        continue
                    try:
                        result[date.fromisoformat(d_str)] = float(t)
                    except ValueError:
                        pass

            chunk_start = chunk_end + timedelta(days=1)

        return result

    # ------------------------------------------------------------------
    # Historical highs (archive API — existing)
    # ------------------------------------------------------------------

    def fetch_historical_highs(
        self,
        station: CityStation,
        model: WeatherModel,
        *,
        start: date,
        end: date,
    ) -> dict[date, float]:
        """Fetch historical daily-high temperatures from Open-Meteo archive.

        Returns a mapping of {obs_date: forecast_high_f} for the date range.
        Uses GFS archive for GFS/NAM, ECMWF reanalysis for ECMWF,
        and GFS archive as a proxy for HRRR (HRRR archive not on Open-Meteo).
        """

        archive_model_map = {
            WeatherModel.GFS:  "gfs_seamless",
            WeatherModel.ECMWF: "ecmwf_ifs025",
            WeatherModel.HRRR: "gfs_seamless",   # HRRR not in archive; GFS proxy
            WeatherModel.NAM:  "gfs_seamless",
        }
        om_model = archive_model_map[model]
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max",
            "models": om_model,
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        try:
            data = _http_get_json(_OPEN_METEO_ARCHIVE_URL, params)
        except Exception as exc:
            print(
                f"[grib] Archive fetch error ({om_model}, {station.city}): {exc}",
                file=sys.stderr,
            )
            return {}

        dates = data.get("daily", {}).get("time", [])
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        result: dict[date, float] = {}
        for d_str, t in zip(dates, temps):
            if t is None:
                continue
            try:
                result[date.fromisoformat(d_str)] = float(t)
            except ValueError:
                pass
        return result

    def fetch_era5_observations(
        self,
        station: CityStation,
        *,
        start: date,
        end: date,
    ) -> dict[date, float]:
        """ERA5 reanalysis daily highs as observation proxy.

        Fallback for stations not in the IEM ASOS network (most Asian and some
        European airports). ERA5 RMSE vs actual station obs is ~0.3-0.5°C for
        daily max temperature — good enough for error-distribution calibration.

        Returns {obs_date: observed_high_f} in Fahrenheit.
        """
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max",
            "models": "era5",
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        try:
            data = _http_get_json(_OPEN_METEO_ARCHIVE_URL, params)
        except Exception as exc:
            print(f"[grib] ERA5 obs fetch error ({station.city}): {exc}", file=sys.stderr)
            return {}

        dates = data.get("daily", {}).get("time", [])
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        result: dict[date, float] = {}
        for d_str, t in zip(dates, temps):
            if t is None:
                continue
            try:
                result[date.fromisoformat(d_str)] = float(t)
            except ValueError:
                pass
        return result
