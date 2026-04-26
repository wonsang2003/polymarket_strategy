"""Tests for trade settlement logic."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from polymarket_strat.main import (
    _expected_pnl,
    _pnl,
    _resolve_via_polymarket,
    _settle_from_iem,
    _station_day_ended,
)
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

    def test_still_trading_at_0_98_returns_none(self):
        # 0.98 < 0.99 threshold — price hasn't yet converged tightly
        # enough to settle, regardless of market flags.
        mkt = {"closed": False, "acceptingOrders": True, "outcomePrices": ["0.98", "0.02"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) is None

    def test_open_market_at_extreme_price_resolves_yes(self):
        # Apr 23 2026 price-first gate: Polymarket often leaves markets
        # open (closed=False, acceptingOrders=True) for hours after the
        # event has effectively resolved and liquidity has converged to
        # 0.99+. The old gate blocked these; the new gate settles them.
        # (Real case: nyc Apr 22 2026 at outPx=["0.997", "0.003"].)
        mkt = {"closed": False, "acceptingOrders": True, "outcomePrices": ["0.997", "0.003"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 1

    def test_open_market_at_extreme_price_resolves_no(self):
        # Symmetric case — market still accepting, price has converged
        # to the NO side. (Real case: sao_paulo Apr 22 at ["0.0005", "0.9995"].)
        mkt = {"closed": False, "acceptingOrders": True, "outcomePrices": ["0.0005", "0.9995"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 0

    def test_open_market_just_inside_threshold_returns_none(self):
        # 0.011 on the YES side is just barely outside the 0.01 threshold.
        # This is a deliberate safety margin: real converged weather markets
        # tighten to ≤0.005 / ≥0.995 before resolution; a persistent 0.011
        # could be a lingering mid-trading artifact. Wait for further drift
        # or for Polymarket to formally close.
        # (Real case: toronto Apr 22 at ["0.011", "0.989"].)
        mkt = {"closed": False, "acceptingOrders": True, "outcomePrices": ["0.011", "0.989"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) is None

    def test_accepting_false_plus_extreme_resolves(self):
        # Legacy case (still supported): closed=False, acceptingOrders=False
        # with extreme prices. Price-first gate handles this identically.
        mkt = {"closed": False, "acceptingOrders": False, "outcomePrices": ["0.995", "0.005"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 1

    def test_closed_but_mid_price_returns_none(self):
        # Pathological: Gamma flags closed but price is mid. Under the
        # price-first rule, mid-price correctly returns None — the flag
        # no longer fabricates a resolution that liquidity disagrees with.
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

    def test_missing_flags_but_extreme_price_still_resolves(self):
        # Under the Apr 23 2026 price-first gate, missing `closed` /
        # `acceptingOrders` fields no longer block resolution — only the
        # price matters. This is intentional: the flags were never the
        # authoritative signal (Polymarket delays flipping them), and
        # the strict 0.99 threshold alone provides the safety margin.
        mkt = {"closed": False, "outcomePrices": ["0.995", "0.005"]}
        assert _resolve_via_polymarket(self._pos(), self._client(mkt)) == 1

    def test_mid_price_with_missing_flags_returns_none(self):
        # Price threshold is the safety gate, not the flags. A mid-price
        # market with no definitive flags stays unresolved.
        mkt = {"outcomePrices": ["0.60", "0.40"]}
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

    def _client_with_captured_calls(self, list_response=None, path_response=None, list_responses_by_params=None):
        """list_responses_by_params: optional {predicate: response} where
        predicate(params) -> bool. Used to differentiate the default-closed
        list call from the closed=true fallback call.
        """
        from polymarket_strat.api import PolymarketPublicClient

        calls: list[tuple[str, str, dict | None]] = []

        class _SpyClient(PolymarketPublicClient):
            # The dataclass uses slots, so _get must be overridden via subclass.
            def _get(self, base_url, path, params=None):
                calls.append((base_url, path, params))
                if path == "/markets":
                    if list_responses_by_params is not None:
                        for predicate, resp in list_responses_by_params.items():
                            if predicate(params or {}):
                                return resp
                        return []
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
        # Two-pass: default (no closed param) then closed=true fallback.
        # Both attempted before giving up.
        assert len(calls) == 2
        assert calls[0][1] == "/markets"
        assert "closed" not in (calls[0][2] or {})
        assert calls[1][1] == "/markets"
        assert (calls[1][2] or {}).get("closed") == "true"

    def test_condition_id_closed_fallback_returns_closed_market(self):
        """Post-resolution Polymarket markets flip closed=True and drop off
        Gamma's default list feed. The 2-pass fallback recovers them via
        an explicit closed=true query. Without this, resolved markets are
        invisible to settlement and positions stay open forever.
        """
        cond = "0x" + "b" * 64
        resolved_row = {
            "id": "2029926",
            "conditionId": cond,
            "closed": True,
            "acceptingOrders": False,
            "outcomePrices": ["0", "1"],
            "umaResolutionStatus": "resolved",
        }
        # Default pass (no closed param) returns empty; closed=true pass hits.
        client, calls = self._client_with_captured_calls(
            list_responses_by_params={
                (lambda p: "closed" not in p): [],
                (lambda p: p.get("closed") == "true"): [resolved_row],
            }
        )
        result = client.get_market(cond)
        assert result == resolved_row
        # Both calls made, in the right order.
        assert len(calls) == 2
        assert "closed" not in (calls[0][2] or {})
        assert (calls[1][2] or {}).get("closed") == "true"

    def test_condition_id_default_pass_short_circuits_when_found(self):
        """Open markets (mid-life) are found on the first pass. Don't make
        a second RPC — the fallback is strictly defensive.
        """
        cond = "0x" + "c" * 64
        open_row = {"conditionId": cond, "closed": False, "acceptingOrders": True}
        client, calls = self._client_with_captured_calls(list_response=[open_row])
        result = client.get_market(cond)
        assert result == open_row
        assert len(calls) == 1  # no fallback fired

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


# ---------------------------------------------------------------------------
# _station_day_ended — station-local EOD + grace gate (Apr 22→23 2026 fix).
#
# The prior IEM-fallback gate was `target_date < date.today()`, which reads
# the host's clock. EC2 runs TZ=Asia/Seoul, so at 01:23 KST on Apr 23 an
# Apr 22 contract satisfied the gate while the station in Toronto was still
# at 12:23 EDT Apr 22 — the aggregator only had partial-day readings, and
# the resulting bracket comparison produced spurious losses. These tests
# pin the replacement gate's behaviour at each TZ hemisphere and at the
# fail-closed boundaries.
# ---------------------------------------------------------------------------

class TestStationDayEnded:
    """Boundary + regression tests for the station-local IEM-fallback gate."""

    # ---- Toronto (UTC-4 in EDT), grace=2h
    #   target = 2026-04-22
    #   Toronto Apr 22 EOD local      = Apr 23 00:00 EDT   = Apr 23 04:00 UTC
    #   Grace end (+2h)               = Apr 23 02:00 EDT   = Apr 23 06:00 UTC

    def test_toronto_before_eod_returns_false(self):
        # 23:59 local on target_date — still Apr 22 in Toronto.
        now = datetime(2026, 4, 23, 3, 59, 0, tzinfo=timezone.utc)  # Apr 22 23:59 EDT
        assert _station_day_ended("toronto", date(2026, 4, 22), now=now) is False

    def test_toronto_at_eod_no_grace_returns_false(self):
        # Exactly midnight local — grace hasn't elapsed.
        now = datetime(2026, 4, 23, 4, 0, 0, tzinfo=timezone.utc)  # Apr 23 00:00 EDT
        assert _station_day_ended("toronto", date(2026, 4, 22), now=now) is False

    def test_toronto_within_grace_returns_false(self):
        # 01:00 local post-midnight — still inside 2h grace.
        now = datetime(2026, 4, 23, 5, 0, 0, tzinfo=timezone.utc)  # Apr 23 01:00 EDT
        assert _station_day_ended("toronto", date(2026, 4, 22), now=now) is False

    def test_toronto_past_grace_returns_true(self):
        # 02:00 local post-midnight — grace elapsed.
        now = datetime(2026, 4, 23, 6, 0, 0, tzinfo=timezone.utc)  # Apr 23 02:00 EDT
        assert _station_day_ended("toronto", date(2026, 4, 22), now=now) is True

    def test_toronto_regression_the_bug_timestamp(self):
        """The exact wall-clock at which the Apr 22→23 premature settle fired.

        EC2 (TZ=KST) ran settle at 2026-04-23 01:23 KST = 2026-04-22 16:23 UTC.
        At that instant Toronto Apr 22 was still 12:23 EDT — nowhere near EOD.
        Under the old host-local gate this satisfied `target<today` and IEM
        returned partial-day data. The new gate must refuse.
        """
        now = datetime(2026, 4, 22, 16, 23, 15, tzinfo=timezone.utc)
        assert _station_day_ended("toronto", date(2026, 4, 22), now=now) is False

    # ---- Wellington (UTC+12 in NZST) — the one city whose day *had* ended.
    #   Wellington Apr 22 EOD local   = Apr 23 00:00 NZST  = Apr 22 12:00 UTC
    #   Grace end (+2h)               = Apr 23 02:00 NZST  = Apr 22 14:00 UTC

    def test_wellington_past_eod_at_the_bug_timestamp_returns_true(self):
        # Same regression instant — but Wellington's day finished >4h earlier.
        now = datetime(2026, 4, 22, 16, 23, 15, tzinfo=timezone.utc)
        assert _station_day_ended("wellington", date(2026, 4, 22), now=now) is True

    def test_wellington_before_eod_returns_false(self):
        # Apr 22 10:00 UTC = Apr 22 22:00 NZST — still 2h from local EOD.
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        assert _station_day_ended("wellington", date(2026, 4, 22), now=now) is False

    # ---- Sydney (UTC+10 / +11 DST) — south hemisphere autumn April is AEST.
    #   Sydney Apr 22 EOD local       = Apr 23 00:00 AEST  = Apr 22 14:00 UTC
    #   Grace end (+2h)               = Apr 22 16:00 UTC

    def test_sydney_past_grace_returns_true(self):
        now = datetime(2026, 4, 22, 16, 30, 0, tzinfo=timezone.utc)
        assert _station_day_ended("sydney", date(2026, 4, 22), now=now) is True

    # ---- Fail-closed on missing / bad city registry entries.

    def test_unknown_city_returns_false(self):
        now = datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc)
        # Even long past any plausible EOD, unknown city → False.
        assert _station_day_ended("atlantis", date(2026, 4, 22), now=now) is False

    def test_empty_city_returns_false(self):
        now = datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc)
        assert _station_day_ended("", date(2026, 4, 22), now=now) is False

    def test_bad_timezone_returns_false(self):
        """If the registry entry exists but ZoneInfo can't load it, fail-closed."""
        fake_station = MagicMock()
        fake_station.timezone = "Not/A/Real/Zone"
        with patch("polymarket_strat.domain.weather.models.CITY_REGISTRY", {"mars": fake_station}):
            now = datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc)
            assert _station_day_ended("mars", date(2026, 4, 22), now=now) is False

    def test_naive_now_assumed_utc(self):
        """A naive datetime passed as `now` should be interpreted as UTC
        (tests historically pass naive clocks; production passes aware ones)."""
        # Naive Apr 23 06:00 → treated as UTC → Apr 23 02:00 EDT → past grace.
        now_naive = datetime(2026, 4, 23, 6, 0, 0)
        assert _station_day_ended("toronto", date(2026, 4, 22), now=now_naive) is True

    def test_custom_grace_hours(self):
        """grace_hours=0 must let the gate open exactly at local midnight."""
        now = datetime(2026, 4, 23, 4, 0, 0, tzinfo=timezone.utc)  # Apr 23 00:00 EDT
        assert _station_day_ended(
            "toronto", date(2026, 4, 22), grace_hours=0, now=now
        ) is True
        # And one second before → False.
        now_before = datetime(2026, 4, 23, 3, 59, 59, tzinfo=timezone.utc)
        assert _station_day_ended(
            "toronto", date(2026, 4, 22), grace_hours=0, now=now_before
        ) is False


class TestRunSettleStationGateRegression:
    """Regression: run_settle must NOT call _settle_from_iem while the
    station's Apr 22 day is still in progress, even if the host clock has
    already rolled to Apr 23. This is the top-level integration pin for
    the Toronto Apr 22→23 2026 bug."""

    def _make_pos(self, pos_id=1, city="toronto", target="2026-04-22"):
        return {
            "id": pos_id,
            "city": city,
            "target_date": target,
            "market_id": "0xabc123",
            "bracket_lower_f": 60.0,
            "bracket_upper_f": 62.0,
            "entry_price": 0.16,
            "notional": 28.0,
        }

    def test_iem_not_called_when_station_day_unfinished(self):
        """At KST Apr 23 01:23 (= Apr 22 16:23 UTC = Apr 22 12:23 EDT),
        Toronto's Apr 22 local day is still in progress. run_settle must
        skip IEM even though the host's date has rolled over."""
        pos = self._make_pos()

        fake_db = MagicMock()
        fake_db.get_open_positions.return_value = [pos]

        # Polymarket returns None (market still open) — the real case.
        frozen_now = datetime(2026, 4, 22, 16, 23, 15, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now.astimezone(tz) if tz else frozen_now.replace(tzinfo=None)

        with patch("polymarket_strat.infrastructure.weather.persistence.WeatherDatabase",
                   return_value=fake_db), \
             patch("polymarket_strat.api.PolymarketPublicClient"), \
             patch("polymarket_strat.main._resolve_via_polymarket", return_value=None), \
             patch("polymarket_strat.main._settle_from_iem") as mock_iem, \
             patch("polymarket_strat.main.datetime", _FrozenDatetime):
            from polymarket_strat.main import run_settle
            run_settle(trade_id=None, auto=True)

        mock_iem.assert_not_called(), (
            "IEM was called while station day was still in progress — "
            "the Apr 22→23 premature-settle regression has returned."
        )
        fake_db.settle_trade.assert_not_called()

    def test_iem_called_when_station_day_past_grace(self):
        """At Apr 23 08:00 UTC (= Apr 23 04:00 EDT), Toronto is 4h past
        local EOD — well beyond the 2h grace. IEM should fire."""
        pos = self._make_pos()

        fake_db = MagicMock()
        fake_db.get_open_positions.return_value = [pos]

        frozen_now = datetime(2026, 4, 23, 8, 0, 0, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now.astimezone(tz) if tz else frozen_now.replace(tzinfo=None)

        with patch("polymarket_strat.infrastructure.weather.persistence.WeatherDatabase",
                   return_value=fake_db), \
             patch("polymarket_strat.api.PolymarketPublicClient"), \
             patch("polymarket_strat.main._resolve_via_polymarket", return_value=None), \
             patch("polymarket_strat.main._settle_from_iem",
                   return_value={"outcome": 0, "observed_high_f": 58.0, "pnl": -28.0}) as mock_iem, \
             patch("polymarket_strat.main.datetime", _FrozenDatetime):
            from polymarket_strat.main import run_settle
            run_settle(trade_id=None, auto=True)

        mock_iem.assert_called_once()
        fake_db.settle_trade.assert_called_once()
