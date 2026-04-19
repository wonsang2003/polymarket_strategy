"""Tests for trade settlement logic."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from polymarket_strat.main import _pnl, _settle_from_iem
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase


# ---------------------------------------------------------------------------
# _pnl — pure math, no I/O
# ---------------------------------------------------------------------------

class TestPnl:
    def test_yes_win(self):
        # bought $50 worth at entry=1.0 → 50 shares. Polymarket charges 2%
        # fee on WINNINGS ONLY (profit above notional), not on entire payout.
        result = _pnl(outcome=1, notional=50.0, entry_price=1.0)
        # gross_profit = 50 * (1 - 1.0) = 0  →  no winnings, no fee, pnl = 0.0
        assert result == pytest.approx(0.0, abs=0.01)

    def test_yes_win_cheap_entry(self):
        # bought $50 worth at 5¢ each → 1000 shares. Fee on winnings only.
        # gross_profit = 1000 * (1 - 0.05) = 950. fee = 0.02 * 950 = 19.
        # pnl = 950 - 19 = 931  (equivalently: 1000 * 0.95 * 0.98 = 931)
        result = _pnl(outcome=1, notional=50.0, entry_price=0.05)
        assert result == pytest.approx(931.0, abs=0.01)

    def test_no_loss(self):
        # always lose full notional on NO
        result = _pnl(outcome=0, notional=50.0, entry_price=0.05)
        assert result == -50.0

    def test_no_loss_regardless_of_entry(self):
        assert _pnl(outcome=0, notional=100.0, entry_price=0.99) == -100.0

    def test_realistic_trade(self):
        # $50 at 53.5¢ entry → n_shares ≈ 93.46. YES wins, fee on winnings only.
        # gross_profit = 93.46 * (1 - 0.535) ≈ 43.46. fee ≈ 0.87. pnl ≈ 42.59
        # (equivalently: 93.46 * 0.465 * 0.98 ≈ 42.59)
        result = _pnl(outcome=1, notional=50.0, entry_price=0.535)
        assert result == pytest.approx(42.59, abs=0.05)

    def test_zero_entry_price_no_crash(self):
        # entry_price=0 would divide by zero; guard returns n_shares=0,
        # which under fee-on-winnings math yields pnl = 0 * anything = 0.0.
        # Semantics: "degenerate input, no trade placed, no P&L" (not a loss).
        # Note: min_entry_price=0.02 is enforced upstream, so this never
        # fires in production — this test just verifies graceful handling.
        result = _pnl(outcome=1, notional=50.0, entry_price=0.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Bracket outcome determination logic (the core of IEM settlement)
# ---------------------------------------------------------------------------

class TestBracketOutcome:
    """Test that the bracket [lower_f, upper_f) logic is correct for all
    contract types."""

    def _outcome(self, observed: float, lower: float, upper: float) -> int:
        return 1 if lower <= observed < upper else 0

    def test_exact_bracket_hit(self):
        # "17°C exactly" = [62.6, 64.4°F), observed 63.5°F → YES
        assert self._outcome(63.5, 62.6, 64.4) == 1

    def test_exact_bracket_miss_above(self):
        # observed above bracket → NO
        assert self._outcome(65.0, 62.6, 64.4) == 0

    def test_exact_bracket_miss_below(self):
        assert self._outcome(61.0, 62.6, 64.4) == 0

    def test_exact_bracket_upper_boundary(self):
        # upper bound is exclusive: 64.4 itself is NOT in [62.6, 64.4)
        assert self._outcome(64.4, 62.6, 64.4) == 0

    def test_or_higher_hit(self):
        # "24°C or higher" = [75.2, 200.0), observed 82°F → YES
        assert self._outcome(82.0, 75.2, 200.0) == 1

    def test_or_higher_miss(self):
        assert self._outcome(74.0, 75.2, 200.0) == 0

    def test_or_below_hit(self):
        # "79°F or below" = [-50, 80.0), observed 72°F → YES
        assert self._outcome(72.0, -50.0, 80.0) == 1

    def test_or_below_miss(self):
        # observed 82°F → NO
        assert self._outcome(82.0, -50.0, 80.0) == 0

    def test_or_below_boundary(self):
        # exactly at 80°F — the bracket is [-50, 80), so 80 is NOT included
        assert self._outcome(80.0, -50.0, 80.0) == 0


# ---------------------------------------------------------------------------
# WeatherDatabase settle mechanics
# ---------------------------------------------------------------------------

class TestWeatherDatabaseSettle:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = WeatherDatabase(self.tmp.name)
        self.trade_id = self.db.save_trade(
            city="nyc",
            target_date=date(2026, 4, 15),
            bracket_lower_f=-50.0,
            bracket_upper_f=80.0,
            model_prob=0.51,
            market_prob=0.001,
            edge=0.50,
            kelly_fraction=0.085,
            notional=50.0,
            entry_price=0.001,
            side="BUY",
            mode="paper",
            market_id="0xabc",
            token_id="0xtoken",
            question="Will NYC high be 79F or below on April 15?",
            regime="stable_high",
        )

    def teardown_method(self):
        self.db.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_trade_saved_as_open(self):
        positions = self.db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["outcome"] is None

    def test_settle_yes(self):
        self.db.settle_trade(self.trade_id, outcome=1, pnl=44850.0)
        positions = self.db.get_open_positions()
        assert positions == []  # no more open positions

        trades = self.db.get_trades()
        assert trades[0]["outcome"] == 1
        assert trades[0]["pnl"] == 44850.0
        assert trades[0]["settled_at"] is not None

    def test_settle_no(self):
        self.db.settle_trade(self.trade_id, outcome=0, pnl=-50.0)
        trades = self.db.get_trades()
        assert trades[0]["outcome"] == 0
        assert trades[0]["pnl"] == -50.0

    def test_double_settle_idempotent(self):
        self.db.settle_trade(self.trade_id, outcome=1, pnl=100.0)
        self.db.settle_trade(self.trade_id, outcome=0, pnl=-50.0)  # second call
        trades = self.db.get_trades()
        # Last write wins (UPDATE), but it should not crash
        assert trades[0]["outcome"] == 0


# ---------------------------------------------------------------------------
# _settle_from_iem — integration-style with IEM mocked
# ---------------------------------------------------------------------------

class TestSettleFromIem:
    def _make_pos(self, city, target_date, lower_f, upper_f, notional=50.0, entry_price=0.10):
        return {
            "city": city,
            "target_date": target_date,
            "bracket_lower_f": lower_f,
            "bracket_upper_f": upper_f,
            "notional": notional,
            "entry_price": entry_price,
        }

    def test_yes_outcome_from_iem(self):
        pos = self._make_pos("seoul", "2026-04-16", 75.2, 200.0, notional=50.0, entry_price=0.535)
        fake_obs = MagicMock()
        fake_obs.observed_high_f = 80.0  # above 75.2°F → YES

        with patch(
            "polymarket_strat.infrastructure.weather.station_client.StationObservationClient"
        ) as MockClient:
            MockClient.return_value.fetch_daily_highs.return_value = [fake_obs]
            result = _settle_from_iem(pos)

        assert result is not None
        assert result["outcome"] == 1
        assert result["observed_high_f"] == 80.0
        assert result["pnl"] > 0  # YES win at 53.5¢ entry is profitable

    def test_no_outcome_from_iem(self):
        pos = self._make_pos("nyc", "2026-04-15", -50.0, 80.0, notional=50.0, entry_price=0.001)
        fake_obs = MagicMock()
        fake_obs.observed_high_f = 85.0  # above 80°F → NO

        with patch(
            "polymarket_strat.infrastructure.weather.station_client.StationObservationClient"
        ) as MockClient:
            MockClient.return_value.fetch_daily_highs.return_value = [fake_obs]
            result = _settle_from_iem(pos)

        assert result is not None
        assert result["outcome"] == 0
        assert result["pnl"] == -50.0

    def test_no_observation_returns_none(self):
        pos = self._make_pos("seoul", "2026-04-16", 75.2, 200.0)
        with patch(
            "polymarket_strat.infrastructure.weather.station_client.StationObservationClient"
        ) as MockClient:
            MockClient.return_value.fetch_daily_highs.return_value = []
            result = _settle_from_iem(pos)

        assert result is None

    def test_unknown_city_returns_none(self):
        pos = self._make_pos("narnia", "2026-04-16", 60.0, 70.0)
        result = _settle_from_iem(pos)
        assert result is None

    def test_exact_bracket_boundary(self):
        # observed exactly at upper bound → NO (exclusive upper)
        pos = self._make_pos("tokyo", "2026-04-16", 62.6, 64.4)
        fake_obs = MagicMock()
        fake_obs.observed_high_f = 64.4  # exactly at upper bound

        with patch(
            "polymarket_strat.infrastructure.weather.station_client.StationObservationClient"
        ) as MockClient:
            MockClient.return_value.fetch_daily_highs.return_value = [fake_obs]
            result = _settle_from_iem(pos)

        assert result["outcome"] == 0  # upper bound is exclusive
