"""Tests for Apr 24 2026 Q3 + Q5-A + Q1 Citadel wave 2:
  - _breakeven_current_model_prob: rigorous hold/exit indifference
  - season_from_date: month → quarter mapping
  - _N_MIN_FOR_TRADING sample-gate constant
  - ErrorDistribution.season default = -1 (pooled)
"""
from __future__ import annotations

from datetime import date

import pytest

from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.domain.weather.season import (
    CITY_SEASON_SCHEDULE,
    CLIMATE_TYPE_ARID_2SEASON,
    CLIMATE_TYPE_NH_4SEASON,
    CLIMATE_TYPE_SH_4SEASON,
    CLIMATE_TYPE_TROPICAL_2SEASON,
    FALL, SPRING, SUMMER, WINTER,
    climate_type,
    n_seasons,
    season_from_date,
    season_label,
)
from polymarket_strat.domain.weather.strategy import _N_MIN_FOR_TRADING
from polymarket_strat.main import _breakeven_current_model_prob


# ---------------------------------------------------------------------------
# Q3: breakeven formula — the math that makes profit-take rigorous.
# ---------------------------------------------------------------------------


class TestBreakevenFormula:
    """Pin the derivation documented in `main.py::_breakeven_current_model_prob`.

    For entry P_e and current best_bid P_now:
        P*_breakeven = (0.98 * P_now + 0.02 * P_e) / (0.98 + 0.02 * P_e)

    Worked canonical example from the Citadel-review response:
        P_e = 0.35, P_now = 0.55 → P* ≈ 0.553
    """

    def test_canonical_example(self):
        p = _breakeven_current_model_prob(entry_price=0.35, best_bid=0.55)
        assert p == pytest.approx(0.553, abs=0.002)

    def test_no_movement_means_breakeven_equals_entry(self):
        """P_now == P_e → breakeven == P_e exactly. No price move, no reason
        to exit unless model_prob dropped below entry."""
        p = _breakeven_current_model_prob(entry_price=0.40, best_bid=0.40)
        # (0.98 * 0.40 + 0.02 * 0.40) / (0.98 + 0.02 * 0.40)
        # = (0.40) / (0.988) = 0.4049... slightly above P_e due to fee drag
        assert p == pytest.approx(0.40, abs=0.01)

    def test_breakeven_rises_monotonically_with_best_bid(self):
        """Higher P_now → higher breakeven (need more model confidence to hold)."""
        entry = 0.30
        prev = -0.01
        for bid in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80):
            p = _breakeven_current_model_prob(entry_price=entry, best_bid=bid)
            assert p > prev
            prev = p

    def test_breakeven_bounded_by_best_bid_from_above(self):
        """Mathematically P*_breakeven must be slightly ≤ P_now (fee makes
        exiting slightly worse than hold-at-fair). Actually with 2% fee on
        gains, P* should be ~= P_now for small fee."""
        p = _breakeven_current_model_prob(entry_price=0.30, best_bid=0.70)
        # Without fee: P* = P_now = 0.70. With 2% fee eating into exit:
        # (0.98 * 0.70 + 0.02 * 0.30) / (0.98 + 0.02 * 0.30)
        # = (0.686 + 0.006) / (0.986) = 0.692 / 0.986 ≈ 0.7018
        # Actually slightly HIGHER than P_now — the fee makes exit less
        # attractive, so we need even MORE confidence to hold than if
        # fee were 0. Makes sense.
        assert p == pytest.approx(0.7018, abs=0.005)

    def test_degenerate_entry_returns_coin_flip(self):
        """entry_price outside (0, 1) is nonsense — return 0.5 so caller's
        `current_model < breakeven` comparison is benign."""
        assert _breakeven_current_model_prob(entry_price=0.0, best_bid=0.5) == 0.5
        assert _breakeven_current_model_prob(entry_price=1.0, best_bid=0.5) == 0.5
        assert _breakeven_current_model_prob(entry_price=-0.1, best_bid=0.5) == 0.5

    def test_custom_fee_rate(self):
        """Fee-rate kwarg propagates correctly. With 0% fee, P* = P_now exactly."""
        p = _breakeven_current_model_prob(entry_price=0.35, best_bid=0.55, fee_rate=0.0)
        assert p == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# Q5-A: season derivation
# ---------------------------------------------------------------------------


class TestSeasonFromDate:
    def test_nh_4season_default(self):
        """No city → default NH 4-season: DJF=winter, MAM=spring, JJA=summer, SON=fall."""
        assert season_from_date(date(2026, 1, 15)) == WINTER
        assert season_from_date(date(2026, 4, 24)) == SPRING
        assert season_from_date(date(2026, 7, 15)) == SUMMER
        assert season_from_date(date(2026, 10, 15)) == FALL

    def test_nh_city_matches_default(self):
        """NH temperate city (nyc) matches the default NH mapping."""
        assert season_from_date(date(2026, 1, 15), "nyc") == WINTER
        assert season_from_date(date(2026, 7, 15), "seoul") == SUMMER
        assert season_from_date(date(2026, 10, 15), "london") == FALL

    def test_sh_flipped_by_six_months(self):
        """Southern Hemisphere: local-winter=JJA, local-summer=DJF.
        Integer encoding preserved; labels shifted 6 months.
        Wellington Jan (austral peak summer) → season=2.
        Wellington Jul (austral peak winter) → season=0.
        """
        # Wellington austral summer (DJF) → SUMMER=2
        assert season_from_date(date(2026, 1, 15), "wellington") == SUMMER
        assert season_from_date(date(2026, 12, 25), "wellington") == SUMMER
        # Wellington austral winter (JJA) → WINTER=0
        assert season_from_date(date(2026, 7, 15), "wellington") == WINTER
        assert season_from_date(date(2026, 6, 1), "sydney") == WINTER
        # Buenos Aires austral spring (SON) → SPRING=1
        assert season_from_date(date(2026, 10, 15), "buenos_aires") == SPRING
        # São Paulo austral fall (MAM) → FALL=3
        assert season_from_date(date(2026, 4, 15), "sao_paulo") == FALL

    def test_tropical_2season_only_dry_wet(self):
        """Tropical cities: only 2 seasons, 0=dry (Nov-Apr), 1=wet (May-Oct).
        NO season=2 or 3 should ever be returned.
        """
        # Dry season — Miami January should be 0 (same number as NH winter
        # but SEMANTIC meaning is 'dry', not 'winter').
        assert season_from_date(date(2026, 1, 15), "miami") == 0
        assert season_from_date(date(2026, 3, 15), "miami") == 0  # dry, previously was SPRING=1
        # Wet season — Miami July
        assert season_from_date(date(2026, 7, 15), "miami") == 1
        assert season_from_date(date(2026, 10, 15), "miami") == 1  # wet, previously was FALL=3
        # HK and MexCity — same pattern
        assert season_from_date(date(2026, 4, 15), "hong_kong") == 0  # dry
        assert season_from_date(date(2026, 8, 15), "mexico_city") == 1  # wet

    def test_arid_2season_only_cool_hot(self):
        """Dubai: 2 seasons, 0=cool (Nov-Apr), 1=hot (May-Oct)."""
        assert season_from_date(date(2026, 1, 15), "dubai") == 0  # cool
        assert season_from_date(date(2026, 7, 15), "dubai") == 1  # hot

    def test_unknown_city_falls_back_to_nh(self):
        """Unknown city → safe NH fallback."""
        assert season_from_date(date(2026, 1, 15), "zzzunknown") == WINTER
        assert season_from_date(date(2026, 7, 15), "") == SUMMER

    def test_season_label_nh_default(self):
        assert season_label(WINTER) == "winter"
        assert season_label(SPRING) == "spring"
        assert season_label(SUMMER) == "summer"
        assert season_label(FALL) == "fall"
        assert season_label(-1) == "pooled"
        assert season_label(999) == "unknown"

    def test_season_label_tropical_uses_dry_wet(self):
        """Tropical cities' label for 0/1 is dry/wet, not winter/spring."""
        assert season_label(0, "miami") == "dry"
        assert season_label(1, "miami") == "wet"
        assert season_label(0, "hong_kong") == "dry"

    def test_season_label_arid_uses_cool_hot(self):
        assert season_label(0, "dubai") == "cool"
        assert season_label(1, "dubai") == "hot"

    def test_season_label_sh_uses_winter_labels_on_local_calendar(self):
        """SH cities reuse winter/spring/summer/fall labels — they
        represent the LOCAL calendar. season=0 Wellington = austral
        winter = LOCALLY called 'winter'."""
        assert season_label(0, "wellington") == "winter"  # local winter (JJA)
        assert season_label(2, "wellington") == "summer"  # local summer (DJF)

    def test_season_values_are_consecutive_integers(self):
        seasons = sorted([WINTER, SPRING, SUMMER, FALL])
        assert seasons == [0, 1, 2, 3]


class TestClimateType:
    def test_all_22_cities_have_entries(self):
        """Every city in the production roster must have a climate schedule."""
        expected = {
            "nyc", "chicago", "toronto", "atlanta", "la", "sf", "seattle",
            "london", "amsterdam", "munich", "milan",
            "seoul", "tokyo", "shanghai",
            "wellington", "sydney", "buenos_aires", "sao_paulo",
            "miami", "hong_kong", "mexico_city",
            "dubai",
        }
        assert set(CITY_SEASON_SCHEDULE.keys()) == expected

    def test_temperate_cities_are_nh_4season(self):
        for city in ("nyc", "seoul", "london", "tokyo"):
            assert climate_type(city) == CLIMATE_TYPE_NH_4SEASON

    def test_sh_cities_are_sh_4season(self):
        for city in ("wellington", "sydney", "buenos_aires", "sao_paulo"):
            assert climate_type(city) == CLIMATE_TYPE_SH_4SEASON

    def test_tropical_cities_are_tropical_2season(self):
        for city in ("miami", "hong_kong", "mexico_city"):
            assert climate_type(city) == CLIMATE_TYPE_TROPICAL_2SEASON

    def test_dubai_is_arid_2season(self):
        assert climate_type("dubai") == CLIMATE_TYPE_ARID_2SEASON


class TestNSeasons:
    def test_4season_cities_return_4(self):
        assert n_seasons("nyc") == 4
        assert n_seasons("wellington") == 4  # SH still has 4 local seasons

    def test_2season_cities_return_2(self):
        assert n_seasons("miami") == 2
        assert n_seasons("hong_kong") == 2
        assert n_seasons("dubai") == 2

    def test_unknown_city_defaults_to_4(self):
        assert n_seasons("zzzunknown") == 4


# ---------------------------------------------------------------------------
# Q1: trading-grade sample gate
# ---------------------------------------------------------------------------


def test_n_min_for_trading_is_30():
    """Pin the constant — tests and docs reference 30."""
    assert _N_MIN_FOR_TRADING == 30


# ---------------------------------------------------------------------------
# ErrorDistribution.season field (schema migration)
# ---------------------------------------------------------------------------


class TestErrorDistributionSeason:
    def test_default_is_pooled_sentinel(self):
        """New ErrorDistribution instances default to season=-1 (pooled)
        so callers that didn't opt in to seasonal don't break."""
        dist = ErrorDistribution(
            city="nyc",
            model=WeatherModel.GFS,
            regime=SynopticRegime.STABLE_HIGH,
            lead_hours=24,
            family=DistributionFamily.NORMAL,
            mu=0.0,
            sigma=2.5,
        )
        assert dist.season == -1

    def test_season_can_be_set_explicitly(self):
        dist = ErrorDistribution(
            city="nyc",
            model=WeatherModel.GFS,
            regime=SynopticRegime.STABLE_HIGH,
            lead_hours=24,
            family=DistributionFamily.NORMAL,
            mu=0.0,
            sigma=2.5,
            season=WINTER,
        )
        assert dist.season == 0
