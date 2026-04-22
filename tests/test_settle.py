"""Tests for trade settlement logic."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from polymarket_strat.main import _expected_pnl, _pnl, _resolve_via_polymarket, _settle_from_iem
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


# ---------------------------------------------------------------------------
# _expected_pnl — model-predicted EV at entry time (immutable scoreboard)
# ---------------------------------------------------------------------------

class TestExpectedPnl:
    """Verified against the hand-derived table in the settlement-math spec:
        EV/$1 = P * (1-f)(1-p)/p - (1-P)
    """

    def test_edge_5c_at_low_p(self):
        # p=0.15, P=0.20, notional=$50 → EV/$1 ≈ $0.3107 → EV ≈ $15.53
        result = _expected_pnl(model_prob=0.20, entry_price=0.15, notional=50.0)
        assert result == pytest.approx(15.53, abs=0.05)

    def test_edge_5c_at_mid_p(self):
        # p=0.30, P=0.35, notional=$50 → EV/$1 ≈ $0.1503 → EV ≈ $7.52
        result = _expected_pnl(model_prob=0.35, entry_price=0.30, notional=50.0)
        assert result == pytest.approx(7.52, abs=0.05)

    def test_edge_5c_at_coinflip(self):
        # p=0.50, P=0.55, notional=$50 → EV/$1 = 0.089 → EV ≈ $4.45
        result = _expected_pnl(model_prob=0.55, entry_price=0.50, notional=50.0)
        assert result == pytest.approx(4.45, abs=0.05)

    def test_edge_5c_at_high_p(self):
        # p=0.65, P=0.70, notional=$50 → EV/$1 ≈ 0.0694 → EV ≈ $3.47
        result = _expected_pnl(model_prob=0.70, entry_price=0.65, notional=50.0)
        assert result == pytest.approx(3.47, abs=0.05)

    def test_edge_5c_at_top_band(self):
        # p=0.75, P=0.80, notional=$50 → EV/$1 ≈ 0.0613 → EV ≈ $3.07
        result = _expected_pnl(model_prob=0.80, entry_price=0.75, notional=50.0)
        assert result == pytest.approx(3.07, abs=0.05)

    def test_zero_edge_collapses_to_fee_drag(self):
        # P=p → raw edge is zero. EV should be NEGATIVE (only the fee drag).
        # EV/$1 = p*(1-f)(1-p)/p - (1-p) = (1-f)(1-p) - (1-p) = -f(1-p)
        # At p=0.50, f=0.02: EV/$1 = -0.01 → EV on $50 = -$0.50
        result = _expected_pnl(model_prob=0.50, entry_price=0.50, notional=50.0)
        assert result == pytest.approx(-0.50, abs=0.01)

    def test_zero_entry_price_returns_zero(self):
        # Degenerate input — upstream min_entry=0.02 prevents this in prod.
        assert _expected_pnl(model_prob=0.55, entry_price=0.0, notional=50.0) == 0.0

    def test_zero_notional_returns_zero(self):
        assert _expected_pnl(model_prob=0.55, entry_price=0.50, notional=0.0) == 0.0

    def test_fee_free_matches_edge_over_p(self):
        # Sanity: with fee=0, EV/$1 collapses to (P-p)/p.
        # p=0.40, P=0.50 → EV/$1 = 0.10/0.40 = 0.25 → EV on $100 = $25.00
        result = _expected_pnl(model_prob=0.50, entry_price=0.40, notional=100.0, fee=0.0)
        assert result == pytest.approx(25.0, abs=0.01)


# ---------------------------------------------------------------------------
# _resolve_via_polymarket — hourly resolution check
# ---------------------------------------------------------------------------

class TestResolveViaPolymarket:
    def _pos(self, **over):
        base = {
            "market_id": "0xcondition",
            "token_id": "0xtoken",
            "target_date": "2026-04-22",
            "city": "seoul",
        }
        base.update(over)
        return base

    def _client(self, market: dict | None):
        client = MagicMock()
        client.get_market.return_value = market
        return client

    def test_closed_yes_resolves_to_1(self):
        mkt = {"closed": True, "acceptingOrders": False, "outcomePrices": ["1.0", "0.0"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 1

    def test_closed_no_resolves_to_0(self):
        mkt = {"closed": True, "acceptingOrders": False, "outcomePrices": ["0.0", "1.0"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 0

    def test_still_trading_returns_none(self):
        # Mid-trading market at 0.98 must NOT resolve — not closed.
        mkt = {"closed": False, "acceptingOrders": True, "outcomePrices": ["0.98", "0.02"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) is None

    def test_accepting_false_plus_extreme_resolves(self):
        # Some resolved markets have closed=False but acceptingOrders=False
        # with extreme prices. Accept that as resolved.
        mkt = {"closed": False, "acceptingOrders": False, "outcomePrices": ["0.995", "0.005"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 1

    def test_closed_but_mid_price_returns_none(self):
        # Pathological: Gamma flags closed but price is mid. Be conservative.
        mkt = {"closed": True, "acceptingOrders": False, "outcomePrices": ["0.50", "0.50"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) is None

    def test_outcome_prices_as_json_string(self):
        # Gamma API sometimes returns outcomePrices as a JSON string.
        mkt = {"closed": True, "acceptingOrders": False, "outcomePrices": '["1.0", "0.0"]'}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 1

    def test_missing_market_id_returns_none(self):
        pos = self._pos()
        pos.pop("market_id")
        pos.pop("token_id")
        assert _resolve_via_polymarket(pos, self._client({})) is None

    def test_client_exception_returns_none(self):
        client = MagicMock()
        client.get_market.side_effect = RuntimeError("network")
        assert _resolve_via_polymarket(self._pos(), client) is None

    def test_empty_outcome_prices_returns_none(self):
        mkt = {"closed": True, "acceptingOrders": False, "outcomePrices": []}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) is None

    def test_missing_accepting_field_treated_as_trading(self):
        # If both `closed` is False and `acceptingOrders` is missing → treat as
        # still trading (conservative default).
        mkt = {"closed": False, "outcomePrices": ["0.995", "0.005"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) is None


# ---------------------------------------------------------------------------
# PolymarketPublicClient.get_market — dispatches by ID format
# ---------------------------------------------------------------------------


class TestGammaGetMarketDispatch:
    """Pins the conditionId → Gamma lookup dispatch.

    Gamma's `GET /markets/{id}` path parameter accepts the numeric `id` only.
    `conditionId` (0x-prefixed hex) must route through the list endpoint with
    a `condition_ids` filter. This was the root cause of the "positions never
    settle" bug observed on EC2 — trade_history rows stored conditionIds, and
    every settlement RPC was hitting 404.
    """

    def _client_with_captured_calls(self, list_response=None, path_response=None):
        from polymarket_strat.api import PolymarketPublicClient

        calls: list[tuple[str, str, dict | None]] = []

        class _SpyClient(PolymarketPublicClient):
            # The dataclass uses slots, so _get must be overridden via subclass.
            def _get(self, base_url, path, params=None):
                calls.append((base_url, path, params))
                if path == "/markets":
                    return list_response if list_response is not None else []
                return path_response if path_response is not None else {}

        return _SpyClient(), calls

    def test_numeric_id_hits_path_endpoint(self):
        client, calls = self._client_with_captured_calls(path_response={"id": "12345", "closed": True})
        result = client.get_market("12345")
        assert result == {"id": "12345", "closed": True}
        # Path endpoint was hit, not the list endpoint.
        assert len(calls) == 1
        _, path, params = calls[0]
        assert path == "/markets/12345"
        assert params is None

    def test_condition_id_routes_through_list_endpoint(self):
        cond = "0x" + "a" * 64
        list_response = [{"id": "42", "conditionId": cond, "closed": True, "outcomePrices": ["1", "0"]}]
        client, calls = self._client_with_captured_calls(list_response=list_response)
        result = client.get_market(cond)
        assert result["conditionId"] == cond
        assert result["closed"] is True
        # List endpoint with condition_ids filter, not a path lookup.
        _, path, params = calls[0]
        assert path == "/markets"
        assert params == {"condition_ids": cond, "limit": 1}

    def test_condition_id_returns_empty_dict_when_not_found(self):
        cond = "0xdeadbeef"
        client, calls = self._client_with_captured_calls(list_response=[])
        result = client.get_market(cond)
        assert result == {}
        _, path, _ = calls[0]
        assert path == "/markets"  # still attempted list lookup

    def test_uppercase_0x_still_routes_to_list(self):
        cond = "0XABC123"  # uppercase 0X prefix
        list_response = [{"conditionId": cond, "closed": True}]
        client, calls = self._client_with_captured_calls(list_response=list_response)
        client.get_market(cond)
        _, path, params = calls[0]
        assert path == "/markets"
        assert params["condition_ids"] == cond

    def test_empty_market_id_returns_empty_without_rpc(self):
        client, calls = self._client_with_captured_calls()
        assert client.get_market("") == {}
        # No RPC attempted at all — guards against NULL rows sneaking in.
        assert calls == []

    def test_whitespace_stripped_before_dispatch(self):
        cond = "0xabc"
        list_response = [{"conditionId": cond, "closed": True}]
        client, calls = self._client_with_captured_calls(list_response=list_response)
        client.get_market("   0xabc  ")
        _, path, params = calls[0]
        assert path == "/markets"
        assert params["condition_ids"] == "0xabc"
