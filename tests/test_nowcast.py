"""Pin nowcast bracket-classification logic.

Apr 25 2026 — Layer 2 of late-entry tail-bracket strategy.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polymarket_strat.domain.weather.nowcast import (
    BracketNowcast,
    DEFAULT_NO_MARGIN_F,
    PEAK_LIKELY_PAST_HOUR,
    classify_bracket,
)
from polymarket_strat.domain.weather.models import CityStation, CorrelationGroup


@pytest.fixture
def nyc_station() -> CityStation:
    return CityStation(
        city="nyc", station_id="KLGA",
        lat=40.7772, lon=-73.8726,
        timezone="America/New_York",
        uses_celsius=False,
        correlation_group=CorrelationGroup.US_NORTHEAST,
    )


@pytest.fixture
def post_peak_now_utc() -> datetime:
    """Aug 15, 2026 at 18:00 UTC = 14:00 local NYC (DST). Pre-peak."""
    return datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def evening_now_utc() -> datetime:
    """Aug 15, 2026 at 22:00 UTC = 18:00 local NYC. Post-peak (>16:00)."""
    return datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)


class TestSettledNo:
    def test_post_peak_far_below_lower_settles_no(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        """It's 6pm local, max so far is 70F, bracket is 80-82F. Settled NO."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=70.0, last_obs_utc=evening_now_utc,
            n_readings=12, now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.SETTLED_NO
        assert r.peak_likely_past is True
        assert "gap 10.0F" in r.reason

    def test_pre_peak_below_lower_does_not_settle(
        self, nyc_station: CityStation, post_peak_now_utc: datetime
    ) -> None:
        """It's 2pm local — could still climb. Don't settle yet."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=70.0, last_obs_utc=post_peak_now_utc,
            n_readings=8, now_utc=post_peak_now_utc,
        )
        assert r.classification == BracketNowcast.OPEN
        assert r.peak_likely_past is False

    def test_post_peak_just_below_lower_does_not_settle(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        """Max 79.5F, lower 80F — gap = 0.5F < margin 1F. Don't settle."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=79.5, last_obs_utc=evening_now_utc,
            n_readings=12, now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.OPEN

    def test_post_peak_overshot_upper(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        """Max 90F, bracket 80-82F — overshot. Definitely NO (and yes already settled)."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=90.0, last_obs_utc=evening_now_utc,
            n_readings=14, now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.SETTLED_NO_OVERSHOT
        assert "running_max 90.0F" in r.reason


class TestLikelyYes:
    def test_post_peak_in_bracket_likely_yes(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        """Max so far 81F, bracket 80-82F, post-peak → LIKELY_YES."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=81.0, last_obs_utc=evening_now_utc,
            n_readings=12, now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.LIKELY_YES

    def test_pre_peak_in_bracket_open_not_likely_yes(
        self, nyc_station: CityStation, post_peak_now_utc: datetime
    ) -> None:
        """Pre-peak, in bracket — could still climb out. OPEN."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=81.0, last_obs_utc=post_peak_now_utc,
            n_readings=8, now_utc=post_peak_now_utc,
        )
        assert r.classification == BracketNowcast.OPEN


class TestEdgeCases:
    def test_no_obs_returns_unknown(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=None, last_obs_utc=None, n_readings=0,
            now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.UNKNOWN
        assert "no obs" in r.reason.lower()

    def test_zero_readings_returns_unknown(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=70.0, last_obs_utc=evening_now_utc, n_readings=0,
            now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.UNKNOWN

    def test_bad_timezone_returns_unknown(self) -> None:
        bad_station = CityStation(
            city="nowhere", station_id="XXXX",
            lat=0, lon=0,
            timezone="Not/A/Real_Zone",
            uses_celsius=False,
            correlation_group=CorrelationGroup.US_NORTHEAST,
        )
        r = classify_bracket(
            station=bad_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=70.0, last_obs_utc=datetime.now(timezone.utc),
            n_readings=12,
        )
        assert r.classification == BracketNowcast.UNKNOWN
        assert "timezone" in r.reason.lower()

    def test_margin_override_makes_more_aggressive(
        self, nyc_station: CityStation, evening_now_utc: datetime
    ) -> None:
        """With margin=0.1, even a 0.5F gap settles NO."""
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=79.5, last_obs_utc=evening_now_utc, n_readings=12,
            no_margin_f=0.1, now_utc=evening_now_utc,
        )
        assert r.classification == BracketNowcast.SETTLED_NO

    def test_peak_past_threshold_at_exact_4pm(
        self, nyc_station: CityStation
    ) -> None:
        """Exactly 16:00 local should be classified as peak likely past."""
        # 16:00 local NYC during DST = 20:00 UTC
        now_utc = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=70.0, last_obs_utc=now_utc, n_readings=10,
            now_utc=now_utc,
        )
        assert r.peak_likely_past is True
        assert r.classification == BracketNowcast.SETTLED_NO

    def test_just_before_4pm_not_peak_past(
        self, nyc_station: CityStation
    ) -> None:
        """15:59 local should still be considered pre-peak."""
        now_utc = datetime(2026, 8, 15, 19, 59, tzinfo=timezone.utc)  # 15:59 EDT
        r = classify_bracket(
            station=nyc_station,
            bracket_lower_f=80.0, bracket_upper_f=82.0,
            running_max_f=70.0, last_obs_utc=now_utc, n_readings=8,
            now_utc=now_utc,
        )
        assert r.peak_likely_past is False
        assert r.classification == BracketNowcast.OPEN


class TestRealisticScenario:
    """End-to-end-style: simulate a Polymarket bracket on a real day."""

    def test_seoul_summer_late_evening_bracket_above_30c_settles_no(
        self
    ) -> None:
        seoul = CityStation(
            city="seoul", station_id="RKSI",
            lat=37.4609, lon=126.4407,
            timezone="Asia/Seoul",
            uses_celsius=True,
            correlation_group=CorrelationGroup.EAST_ASIA,
        )
        # Aug 15, 2026 at 14:00 UTC = 23:00 local Seoul. Past peak.
        now_utc = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)
        # Max so far = 28°C = 82.4°F, bracket "above 30°C" = above 86°F
        r = classify_bracket(
            station=seoul,
            bracket_lower_f=86.0, bracket_upper_f=200.0,
            running_max_f=82.4, last_obs_utc=now_utc, n_readings=18,
            now_utc=now_utc,
        )
        assert r.classification == BracketNowcast.SETTLED_NO
        # 3.6F gap, post-peak
        assert "gap 3.6F" in r.reason
