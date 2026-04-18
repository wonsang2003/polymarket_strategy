"""Weather domain value objects, enums, and city registry.

All temperature values are in Fahrenheit for consistency with Polymarket's
US-centric bracket labeling.  International cities that use Celsius on
Polymarket are converted at the API boundary (market_scanner.py).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SynopticRegime(enum.Enum):
    """Large-scale weather pattern governing forecast error distribution shape."""
    STABLE_HIGH = "stable_high"           # Clear, tight errors, little edge
    FRONTAL_PASSAGE = "frontal_passage"   # Bimodal errors — timing uncertainty
    CONVECTIVE = "convective"             # Right-skewed — cloud timing
    MARINE_INFLUENCE = "marine_influence"  # Fog/onshore flow — left-skewed
    TRANSITION = "transition"             # Regime change — fat tails


class WeatherModel(enum.Enum):
    """NWP model sources used in the ensemble."""
    GFS = "gfs"       # NOAA Global Forecast System (0.25 deg, 4x/day)
    ECMWF = "ecmwf"   # European Centre (0.1 deg, 2x/day)
    HRRR = "hrrr"     # High-Resolution Rapid Refresh (3km, hourly, US-only)
    NAM = "nam"       # North American Mesoscale (12km, 4x/day)


class DistributionFamily(enum.Enum):
    """Parametric family chosen for forecast-error calibration."""
    NORMAL = "normal"
    SKEW_NORMAL = "skew_normal"
    STUDENT_T = "student_t"


class Season(enum.Enum):
    """Meteorological season for seasonal error stratification."""
    SPRING = "spring"   # Mar-May (NH) / Sep-Nov (SH)
    SUMMER = "summer"   # Jun-Aug (NH) / Dec-Feb (SH)
    AUTUMN = "autumn"   # Sep-Nov (NH) / Mar-May (SH)
    WINTER = "winter"   # Dec-Feb (NH) / Jun-Aug (SH)

    @classmethod
    def from_date(cls, d: date, *, southern_hemisphere: bool = False) -> "Season":
        """Determine meteorological season from date and hemisphere."""
        month = d.month
        if southern_hemisphere:
            month = (month + 6 - 1) % 12 + 1  # flip 6 months
        if month in (3, 4, 5):
            return cls.SPRING
        if month in (6, 7, 8):
            return cls.SUMMER
        if month in (9, 10, 11):
            return cls.AUTUMN
        return cls.WINTER


class CorrelationGroup(enum.Enum):
    """Cities that share the same air-mass on a given day.

    Positions in the same group are correlated and capped together.
    """
    EAST_ASIA = "east_asia"
    WESTERN_EUROPE = "western_europe"
    US_NORTHEAST = "us_northeast"
    US_WEST = "us_west"
    US_SOUTH = "us_south"
    SOUTH_AMERICA = "south_america"
    OCEANIA = "oceania"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class CityStation:
    """Immutable reference to a city + its official observation station."""
    city: str
    station_id: str              # WMO or ICAO ID
    lat: float
    lon: float
    correlation_group: CorrelationGroup
    timezone: str                # IANA tz name (e.g. "America/New_York")
    uses_celsius: bool = False   # True if Polymarket labels brackets in C


@dataclass(slots=True)
class TemperatureForecast:
    """A single NWP model's forecast of daily-high temperature for one city."""
    city: str
    model: WeatherModel
    init_time: datetime          # model initialization time (UTC)
    valid_time: datetime         # forecast target time (UTC)
    lead_hours: int
    forecast_high_f: float       # predicted daily high in Fahrenheit
    ensemble_spread_f: float = 0.0  # max - min across ensemble members (F)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StationObservation:
    """Verified daily-high observation from a weather station."""
    city: str
    station_id: str
    obs_date: date
    observed_high_f: float
    source: str = "IEM"


@dataclass(slots=True)
class ForecastError:
    """forecast - observed, in Fahrenheit."""
    city: str
    model: WeatherModel
    regime: SynopticRegime
    lead_hours: int
    error_f: float               # positive = model ran hot
    obs_date: date


@dataclass(slots=True)
class ErrorDistribution:
    """Fitted parametric distribution for a (city, model, regime, lead) slice."""
    city: str
    model: WeatherModel
    regime: SynopticRegime
    lead_hours: int
    family: DistributionFamily
    mu: float                    # location
    sigma: float                 # scale (> 0)
    shape: float = 0.0           # skew-normal alpha, or 0 for symmetric
    nu: float = 30.0             # Student-t degrees of freedom
    n_samples: int = 0           # how many errors were used to fit


@dataclass(slots=True)
class BracketContract:
    """A single temperature-bracket contract on Polymarket."""
    market_id: str
    question: str
    city: str
    target_date: date
    lower_f: float               # bracket lower bound (inclusive)
    upper_f: float               # bracket upper bound (exclusive)
    token_id_yes: str
    token_id_no: str = ""
    market_price_yes: float = 0.5
    best_ask_yes: float = 1.0
    best_bid_yes: float = 0.0
    spread: float = 1.0
    liquidity: float = 0.0
    # Multi-day contract support: end_date > target_date means multi-day
    end_date: date | None = None  # None = single-day contract

    @property
    def is_multi_day(self) -> bool:
        return self.end_date is not None and self.end_date > self.target_date

    @property
    def span_days(self) -> int:
        if self.end_date is None:
            return 1
        return (self.end_date - self.target_date).days + 1


@dataclass(slots=True)
class BracketProbability:
    """Model's probability estimate for a single bracket, with edge info."""
    city: str
    target_date: date
    lower_f: float
    upper_f: float
    model_prob: float
    prob_std: float              # posterior uncertainty on model_prob
    market_prob: float
    raw_edge: float
    edge_after_fees: float
    kelly_fraction: float
    uncertainty_shrinkage: float
    regime: SynopticRegime
    contributing_models: list[WeatherModel] = field(default_factory=list)


# ---------------------------------------------------------------------------
# City registry — 22 cities across all Polymarket weather markets
# ---------------------------------------------------------------------------

CITY_REGISTRY: dict[str, CityStation] = {
    # --- US Northeast ---
    "nyc": CityStation(
        city="nyc", station_id="KLGA", lat=40.7772, lon=-73.8726,
        correlation_group=CorrelationGroup.US_NORTHEAST, timezone="America/New_York",
    ),
    "chicago": CityStation(
        city="chicago", station_id="KORD", lat=41.98, lon=-87.90,
        correlation_group=CorrelationGroup.US_NORTHEAST, timezone="America/Chicago",
    ),
    "toronto": CityStation(
        city="toronto", station_id="CYYZ", lat=43.68, lon=-79.63,
        correlation_group=CorrelationGroup.US_NORTHEAST, timezone="America/Toronto",
    ),
    # --- US South ---
    "miami": CityStation(
        city="miami", station_id="KMIA", lat=25.79, lon=-80.29,
        correlation_group=CorrelationGroup.US_SOUTH, timezone="America/New_York",
    ),
    "atlanta": CityStation(
        city="atlanta", station_id="KATL", lat=33.64, lon=-84.43,
        correlation_group=CorrelationGroup.US_SOUTH, timezone="America/New_York",
    ),
    # --- US West ---
    "la": CityStation(
        city="la", station_id="KLAX", lat=33.94, lon=-118.41,
        correlation_group=CorrelationGroup.US_WEST, timezone="America/Los_Angeles",
    ),
    "sf": CityStation(
        city="sf", station_id="KSFO", lat=37.62, lon=-122.37,
        correlation_group=CorrelationGroup.US_WEST, timezone="America/Los_Angeles",
    ),
    "seattle": CityStation(
        city="seattle", station_id="KSEA", lat=47.45, lon=-122.31,
        correlation_group=CorrelationGroup.US_WEST, timezone="America/Los_Angeles",
    ),
    # --- Western Europe ---
    "london": CityStation(
        city="london", station_id="EGLC", lat=51.5048, lon=0.0498,
        correlation_group=CorrelationGroup.WESTERN_EUROPE, timezone="Europe/London",
        uses_celsius=True,
    ),
    "amsterdam": CityStation(
        city="amsterdam", station_id="EHAM", lat=52.31, lon=4.76,
        correlation_group=CorrelationGroup.WESTERN_EUROPE, timezone="Europe/Amsterdam",
        uses_celsius=True,
    ),
    "munich": CityStation(
        city="munich", station_id="EDDM", lat=48.35, lon=11.79,
        correlation_group=CorrelationGroup.WESTERN_EUROPE, timezone="Europe/Berlin",
        uses_celsius=True,
    ),
    "milan": CityStation(
        city="milan", station_id="LIMC", lat=45.63, lon=8.72,
        correlation_group=CorrelationGroup.WESTERN_EUROPE, timezone="Europe/Rome",
        uses_celsius=True,
    ),
    # --- East Asia ---
    "seoul": CityStation(
        city="seoul", station_id="RKSI", lat=37.4609, lon=126.4407,
        correlation_group=CorrelationGroup.EAST_ASIA, timezone="Asia/Seoul",
        uses_celsius=True,
    ),
    "tokyo": CityStation(
        city="tokyo", station_id="RJTT", lat=35.55, lon=139.78,
        correlation_group=CorrelationGroup.EAST_ASIA, timezone="Asia/Tokyo",
        uses_celsius=True,
    ),
    "hong_kong": CityStation(
        city="hong_kong", station_id="HKO", lat=22.3023, lon=114.1747,
        correlation_group=CorrelationGroup.EAST_ASIA, timezone="Asia/Hong_Kong",
        uses_celsius=True,
    ),
    "shanghai": CityStation(
        city="shanghai", station_id="ZSPD", lat=31.1434, lon=121.8083,
        correlation_group=CorrelationGroup.EAST_ASIA, timezone="Asia/Shanghai",
        uses_celsius=True,
    ),
    # --- South America ---
    "buenos_aires": CityStation(
        city="buenos_aires", station_id="SAEZ", lat=-34.82, lon=-58.54,
        correlation_group=CorrelationGroup.SOUTH_AMERICA, timezone="America/Argentina/Buenos_Aires",
        uses_celsius=True,
    ),
    "sao_paulo": CityStation(
        city="sao_paulo", station_id="SBGR", lat=-23.43, lon=-46.47,
        correlation_group=CorrelationGroup.SOUTH_AMERICA, timezone="America/Sao_Paulo",
        uses_celsius=True,
    ),
    # --- Latin America (grouped with US South — similar tropical air mass) ---
    "mexico_city": CityStation(
        city="mexico_city", station_id="MMMX", lat=19.44, lon=-99.07,
        correlation_group=CorrelationGroup.US_SOUTH, timezone="America/Mexico_City",
        uses_celsius=True,
    ),
    # --- Oceania ---
    "wellington": CityStation(
        city="wellington", station_id="NZWN", lat=-41.33, lon=174.81,
        correlation_group=CorrelationGroup.OCEANIA, timezone="Pacific/Auckland",
        uses_celsius=True,
    ),
    "sydney": CityStation(
        city="sydney", station_id="YSSY", lat=-33.95, lon=151.18,
        correlation_group=CorrelationGroup.OCEANIA, timezone="Australia/Sydney",
        uses_celsius=True,
    ),
    # --- Middle East (grouped into its own — uncorrelated with others) ---
    "dubai": CityStation(
        city="dubai", station_id="OMDB", lat=25.25, lon=55.36,
        correlation_group=CorrelationGroup.US_SOUTH,  # reuse — low correlation with US South
        timezone="Asia/Dubai",
        uses_celsius=True,
    ),
}


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32.0) * 5.0 / 9.0
