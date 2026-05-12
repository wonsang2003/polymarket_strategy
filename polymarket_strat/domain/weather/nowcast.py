"""Live-observation nowcast: classify whether a bracket is effectively
settled given today's-max-so-far.

Apr 25 2026 — Layer 2 of late-entry tail-bracket strategy. Used by the
strategy gate at T-3h on settlement day to detect "this bracket is
mathematically near-impossible given the realized max so far" — typically
"above X°F" brackets when temp has already peaked far below X.

Decision matrix (for bracket [lower_f, upper_f] resolving today):

  if peak likely past AND running_max < lower_f - margin:
      classification = "SETTLED_NO"   # bracket can't hit
  elif peak likely past AND lower_f <= running_max < upper_f:
      classification = "LIKELY_YES"   # already in bracket, drop = unlikely
  elif running_max >= upper_f:
      classification = "SETTLED_NO_OVERSHOT"  # already above bracket upper
  else:
      classification = "OPEN"         # still active, no nowcast signal

"Peak likely past" approximates the diurnal pattern: most stations hit
daily max between 13:00-17:00 local. After 17:00 local on a typical day
the temp is on the down-leg, so "max so far" is likely the actual day
max. We use 16:00 local as the threshold to allow some buffer for late
peaks (sea breezes, frontal passages).

TODO post-Phase-2: tighten the threshold using climatological diurnal
amplitude per (city, day_of_year). For now, fixed 16:00 local is a
defensible heuristic for temperate latitudes; tropical / coastal
stations may peak earlier (Dubai, Singapore, Sydney summer).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from polymarket_strat.domain.weather.models import CityStation


# Margin (°F) below bracket lower for SETTLED_NO classification.
# Lower means more aggressive (more brackets called settled) → more risk
# of late surprises. Higher means more conservative.
DEFAULT_NO_MARGIN_F = 1.0

# Local-time threshold past which we trust "today's max so far" as
# probably-the-day-max. 16:00 local = 4pm. Most temperate stations have
# already peaked by then.
PEAK_LIKELY_PAST_HOUR = 16

# Adaptive margin parameters — climatologically, daily max can climb
# roughly 1-2°F per remaining daylight hour at peak afternoon. We use
# 1.5°F/hour as a defensible mid-estimate for temperate stations.
ADAPTIVE_MARGIN_PER_HOUR_F = 1.5
ADAPTIVE_MARGIN_FLOOR_F = 1.0
ADAPTIVE_MARGIN_CAP_F = 15.0  # don't ever require more than 15°F gap


def adaptive_margin_f(hours_remaining: float) -> float:
    """Compute the SETTLED_NO margin (°F) given remaining hours to settlement.

    Apr 25 2026 — adaptive replacement for the fixed 1°F margin. Climatology
    says max can grow ~1-2°F per remaining daylight hour at peak afternoon.
    Scaling the margin with remaining time makes the strategy:
      - Aggressive when settlement is imminent (T-1h margin = 1.5°F)
      - Conservative when settlement is far (T-5h margin = 7.5°F)
    """
    raw = max(ADAPTIVE_MARGIN_FLOOR_F, hours_remaining * ADAPTIVE_MARGIN_PER_HOUR_F)
    return min(raw, ADAPTIVE_MARGIN_CAP_F)


class BracketNowcast(str, Enum):
    OPEN = "open"
    SETTLED_NO = "settled_no"          # max so far + margin < lower
    SETTLED_NO_OVERSHOT = "settled_no_overshot"  # max so far >= upper
    LIKELY_YES = "likely_yes"          # max so far in [lower, upper)
    UNKNOWN = "unknown"                # no obs available


@dataclass(slots=True)
class NowcastResult:
    classification: BracketNowcast
    running_max_f: Optional[float]
    last_obs_utc: Optional[datetime]
    n_readings: int
    peak_likely_past: bool
    reason: str


def classify_bracket(
    *,
    station: CityStation,
    bracket_lower_f: float,
    bracket_upper_f: float,
    running_max_f: Optional[float],
    last_obs_utc: Optional[datetime],
    n_readings: int,
    no_margin_f: float = DEFAULT_NO_MARGIN_F,
    now_utc: Optional[datetime] = None,
) -> NowcastResult:
    """Classify a bracket given today's-max-so-far observation.

    Args:
        station: CityStation for timezone resolution
        bracket_lower_f, bracket_upper_f: bracket bounds in °F
        running_max_f: result from StationObservationClient.fetch_today_running_max
        last_obs_utc: timestamp of latest reading
        n_readings: how many hourly readings contributed
        no_margin_f: how far below bracket_lower the running max must fall
                     to be SETTLED_NO (default 1.0°F)
        now_utc: defaults to current UTC; param exists for testability

    Returns:
        NowcastResult with classification + diagnostic detail.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(station.timezone)
    except Exception:
        return NowcastResult(
            classification=BracketNowcast.UNKNOWN,
            running_max_f=running_max_f, last_obs_utc=last_obs_utc,
            n_readings=n_readings, peak_likely_past=False,
            reason=f"bad timezone: {station.timezone}",
        )

    now_local = now_utc.astimezone(tz)
    peak_likely_past = now_local.hour >= PEAK_LIKELY_PAST_HOUR

    if running_max_f is None or n_readings == 0:
        return NowcastResult(
            classification=BracketNowcast.UNKNOWN,
            running_max_f=running_max_f, last_obs_utc=last_obs_utc,
            n_readings=n_readings, peak_likely_past=peak_likely_past,
            reason="no obs available",
        )

    # Already exceeded the bracket upper bound → can't hit (high temp passed)
    if running_max_f >= bracket_upper_f:
        return NowcastResult(
            classification=BracketNowcast.SETTLED_NO_OVERSHOT,
            running_max_f=running_max_f, last_obs_utc=last_obs_utc,
            n_readings=n_readings, peak_likely_past=peak_likely_past,
            reason=f"running_max {running_max_f:.1f}F >= bracket_upper {bracket_upper_f:.1f}F",
        )

    # Currently in the bracket → if peak is past, this is the day's max
    if bracket_lower_f <= running_max_f < bracket_upper_f:
        return NowcastResult(
            classification=BracketNowcast.LIKELY_YES if peak_likely_past
                            else BracketNowcast.OPEN,
            running_max_f=running_max_f, last_obs_utc=last_obs_utc,
            n_readings=n_readings, peak_likely_past=peak_likely_past,
            reason=f"running_max {running_max_f:.1f}F in [{bracket_lower_f:.1f}, {bracket_upper_f:.1f})",
        )

    # Currently below bracket lower
    gap = bracket_lower_f - running_max_f
    if peak_likely_past and gap >= no_margin_f:
        return NowcastResult(
            classification=BracketNowcast.SETTLED_NO,
            running_max_f=running_max_f, last_obs_utc=last_obs_utc,
            n_readings=n_readings, peak_likely_past=peak_likely_past,
            reason=f"gap {gap:.1f}F >= margin {no_margin_f:.1f}F, post-peak",
        )

    return NowcastResult(
        classification=BracketNowcast.OPEN,
        running_max_f=running_max_f, last_obs_utc=last_obs_utc,
        n_readings=n_readings, peak_likely_past=peak_likely_past,
        reason=f"gap {gap:.1f}F < margin {no_margin_f:.1f}F or pre-peak",
    )
