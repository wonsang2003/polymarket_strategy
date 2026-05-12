"""Climate-aware season derivation from (city, date).

Apr 24 2026 — refactored for per-city climate. The previous implementation
applied Northern-hemisphere meteorological quarters (DJF=winter, MAM=spring,
JJA=summer, SON=fall) uniformly to all 22 cities. For 11 cities that was
fine (standard NH temperate), for the other 11 it was either:
  - Mislabeled but still functional: Southern Hemisphere cities
    (Wellington, Sydney, Buenos Aires, São Paulo) where the "winter"
    label actually meant peak austral summer. Within-city fits were
    correct (Jan/Feb/Dec are all self-consistently austral summer), but
    the label was semantically inverted.
  - Sample-inefficient: Tropical and arid cities (Miami, Hong Kong,
    Mexico City, Dubai) where climate has 2 distinct regimes (wet/dry
    or hot/cool), not 4. Splitting into quartiles cut samples by 2x vs
    a proper 2-season partition, yielding noisier fits.

The schedule below treats each city's climate on its own terms:

    NH_4SEASON:       winter=DJF, spring=MAM, summer=JJA, fall=SON
                      -> season ∈ {0, 1, 2, 3}
    SH_4SEASON:       local-winter=JJA, local-spring=SON,
                      local-summer=DJF, local-fall=MAM
                      -> season ∈ {0, 1, 2, 3} (same integer encoding,
                         but labels shifted by 6 months relative to NH)
    TROPICAL_2SEASON: dry=Nov-Apr, wet=May-Oct
                      -> season ∈ {0, 1} (only)
    ARID_2SEASON:     cool=Nov-Apr, hot=May-Oct
                      -> season ∈ {0, 1} (only)

Integer encoding is city-local: `season=0 for NYC` means NH winter
(DJF), `season=0 for Wellington` means austral winter (JJA),
`season=0 for Miami` means dry season (Nov-Apr). Within-city fits are
self-consistent; cross-city aggregation requires joining on climate_type
to compare like-for-like.

Sample-efficiency consequence: tropical/arid cities with 2 seasons have
~2x more samples per season bucket than 4-season cities with the same
total sample count. Fits should be correspondingly tighter.
"""
from __future__ import annotations

from datetime import date


# --- Climate classification constants (semantic metadata for docs)
CLIMATE_TYPE_NH_4SEASON = "NH_4SEASON"
CLIMATE_TYPE_SH_4SEASON = "SH_4SEASON"
CLIMATE_TYPE_TROPICAL_2SEASON = "TROPICAL_2SEASON"
CLIMATE_TYPE_ARID_2SEASON = "ARID_2SEASON"

# --- Season integer constants (unchanged — per-city meaning varies)
WINTER = 0
SPRING = 1
SUMMER = 2
FALL = 3

# --- Month → season maps per climate type.
# Frozen at module init for speed and immutability.
_NH_4SEASON_MAP: dict[int, int] = {
    12: 0, 1: 0, 2: 0,       # winter: DJF
    3: 1, 4: 1, 5: 1,        # spring: MAM
    6: 2, 7: 2, 8: 2,        # summer: JJA
    9: 3, 10: 3, 11: 3,      # fall: SON
}

_SH_4SEASON_MAP: dict[int, int] = {
    # Austral calendar — integers 0-3 preserved for storage compatibility,
    # but the mapping is flipped by 6 months from NH.
    6: 0, 7: 0, 8: 0,        # austral winter: JJA
    9: 1, 10: 1, 11: 1,      # austral spring: SON
    12: 2, 1: 2, 2: 2,       # austral summer: DJF
    3: 3, 4: 3, 5: 3,        # austral fall: MAM
}

_TROPICAL_2SEASON_MAP: dict[int, int] = {
    # Wet/dry tropical — only 2 seasons exist climatologically. Integer
    # values 2 and 3 are deliberately unused for these cities so that
    # attempts to write/read them during the fit loop fall through.
    11: 0, 12: 0, 1: 0, 2: 0, 3: 0, 4: 0,      # dry: Nov-Apr
    5: 1, 6: 1, 7: 1, 8: 1, 9: 1, 10: 1,        # wet: May-Oct
}

_ARID_2SEASON_MAP: dict[int, int] = {
    # Desert — 2 seasons: mild cool and scorching hot. Same 2-bucket
    # encoding as tropical; names differ semantically only.
    11: 0, 12: 0, 1: 0, 2: 0, 3: 0, 4: 0,      # cool: Nov-Apr
    5: 1, 6: 1, 7: 1, 8: 1, 9: 1, 10: 1,        # hot: May-Oct
}


# --- Per-city schedule. (climate_type, month_to_season map).
# Organized by climate group for readability.
CITY_SEASON_SCHEDULE: dict[str, tuple[str, dict[int, int]]] = {
    # ===== NH temperate 4-season =====
    # Standard DJF/MAM/JJA/SON schedule. Applies to continental NH cities
    # that have four distinct temperature-driven seasons.
    "nyc":       (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "chicago":   (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "toronto":   (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "atlanta":   (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "la":        (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),   # blocked but keep schedule for consistency
    "sf":        (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),   # blocked
    "seattle":   (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),   # blocked
    "london":    (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "amsterdam": (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "munich":    (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "milan":     (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "seoul":     (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "tokyo":     (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),
    "shanghai":  (CLIMATE_TYPE_NH_4SEASON, _NH_4SEASON_MAP),

    # ===== Southern Hemisphere 4-season =====
    # JJA is local winter; DJF is local summer. Same integer encoding
    # (0-3) as NH — just shifted 6 months. Within-city fits work; cross-
    # city comparisons should use climate_type to normalize.
    "wellington":   (CLIMATE_TYPE_SH_4SEASON, _SH_4SEASON_MAP),
    "sydney":       (CLIMATE_TYPE_SH_4SEASON, _SH_4SEASON_MAP),
    "buenos_aires": (CLIMATE_TYPE_SH_4SEASON, _SH_4SEASON_MAP),
    "sao_paulo":    (CLIMATE_TYPE_SH_4SEASON, _SH_4SEASON_MAP),

    # ===== Tropical 2-season (wet/dry) =====
    # Coastal/subtropical cities where the dominant climate axis is
    # precipitation regime, not temperature. 4-season quartiling splits
    # climatologically-similar months into different buckets — worse
    # sample efficiency than a proper 2-season split.
    "miami":       (CLIMATE_TYPE_TROPICAL_2SEASON, _TROPICAL_2SEASON_MAP),
    "hong_kong":   (CLIMATE_TYPE_TROPICAL_2SEASON, _TROPICAL_2SEASON_MAP),
    "mexico_city": (CLIMATE_TYPE_TROPICAL_2SEASON, _TROPICAL_2SEASON_MAP),

    # ===== Arid 2-season (cool/hot) =====
    "dubai":       (CLIMATE_TYPE_ARID_2SEASON, _ARID_2SEASON_MAP),
}

# Default fallback for unknown cities — NH 4-season is the safest
# assumption for a generic city outside the curated list.
_DEFAULT_CLIMATE_TYPE = CLIMATE_TYPE_NH_4SEASON
_DEFAULT_MONTH_MAP = _NH_4SEASON_MAP


def season_from_date(d: date, city: str = "") -> int:
    """Map (city, date) → city-local season integer.

    Apr 24 2026 — `city` parameter added. Previously uniform-NH; now
    consults CITY_SEASON_SCHEDULE. Backward compatible: calling without
    city (or with unknown city) falls back to NH 4-season, which matches
    the pre-refactor behavior.

    Returns:
        0, 1, 2, or 3 for 4-season cities.
        0 or 1 for 2-season tropical/arid cities.
        -1 only if city is unknown AND month is outside [1, 12] (shouldn't
        happen — month is always 1-12 for valid date objects).
    """
    schedule = CITY_SEASON_SCHEDULE.get(city)
    month_map = schedule[1] if schedule is not None else _DEFAULT_MONTH_MAP
    return month_map.get(d.month, -1)


def climate_type(city: str) -> str:
    """Return the climate classification label for a city.

    Used in the refit loop to decide n_seasons(), and in the dashboard
    to group cities by climate regime.
    """
    schedule = CITY_SEASON_SCHEDULE.get(city)
    return schedule[0] if schedule is not None else _DEFAULT_CLIMATE_TYPE


def n_seasons(city: str) -> int:
    """Number of distinct season buckets for this city's climate type.

    4 for NH_4SEASON / SH_4SEASON.
    2 for TROPICAL_2SEASON / ARID_2SEASON.

    The refit loop iterates range(n_seasons(city)) rather than a fixed
    (0, 1, 2, 3) tuple so tropical cities don't waste effort trying to
    fit seasons 2/3 that have zero samples.
    """
    ct = climate_type(city)
    if ct in (CLIMATE_TYPE_TROPICAL_2SEASON, CLIMATE_TYPE_ARID_2SEASON):
        return 2
    return 4


def season_label(s: int, city: str = "") -> str:
    """Human-readable label for logs and dashboards.

    When city is provided, uses climate-type-specific labels:
      NH_4SEASON: winter/spring/summer/fall
      SH_4SEASON: winter/spring/summer/fall (local — 0=JJA=austral winter)
      TROPICAL_2SEASON: dry/wet (only 0 and 1)
      ARID_2SEASON: cool/hot (only 0 and 1)
    When city is omitted, default NH labels.
    """
    if s == -1:
        return "pooled"
    ct = climate_type(city) if city else CLIMATE_TYPE_NH_4SEASON

    if ct == CLIMATE_TYPE_TROPICAL_2SEASON:
        return {0: "dry", 1: "wet"}.get(s, "unknown")
    if ct == CLIMATE_TYPE_ARID_2SEASON:
        return {0: "cool", 1: "hot"}.get(s, "unknown")
    # 4-season variants — labels apply to the city's local calendar.
    return {0: "winter", 1: "spring", 2: "summer", 3: "fall"}.get(s, "unknown")
