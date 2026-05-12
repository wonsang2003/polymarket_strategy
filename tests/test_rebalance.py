"""Rebalance + MTM snapshot tests.

Covers:
  - `_compute_current_edge` math parity with BracketProbabilityCalculator.edge
  - `_station_local_lead_hours` timezone boundary conditions
  - `_best_price_from_book` CLOB book parsing
  - Dual-threshold rule: exit when drop ≥ 0.15 stale / 0.10 fresh, else hold
  - `run_rebalance` end-to-end with mocked forecast + orderbook
  - Cooldown insertion on paper-mode exit
  - `--dry-run` produces no DB writes
  - Legacy rows (entry_edge=None) skipped
  - `_snapshot_open_position_prices` writes one row per open token
  - Snapshot failure on one token doesn't poison the cycle
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from polymarket_strat.domain.weather.forecast import forecast_content_hash
from polymarket_strat.domain.weather.models import (
    CITY_REGISTRY,
    DistributionFamily,
    ErrorDistribution,
    SynopticRegime,
    TemperatureForecast,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
from polymarket_strat.main import (
    _best_price_from_book,
    _compute_current_edge,
    _snapshot_open_position_prices,
    _station_local_lead_hours,
    run_rebalance,
)


# ---------------------------------------------------------------------------
# _compute_current_edge — pure math
# ---------------------------------------------------------------------------

class TestComputeCurrentEdge:
    def test_positive_edge(self):
        # p_model=0.65, ask=0.50, fee=0.02
        # raw = 0.15, fee_drag = 0.02 * 0.65 * 0.50 = 0.0065
        # edge = 0.15 - 0.0065 = 0.1435
        result = _compute_current_edge(model_prob=0.65, best_ask=0.50)
        assert result == pytest.approx(0.1435, abs=1e-4)

    def test_zero_edge(self):
        # p_model == ask → raw=0, fee_drag > 0 → slightly negative
        result = _compute_current_edge(model_prob=0.50, best_ask=0.50)
        assert result == pytest.approx(-0.005, abs=1e-4)

    def test_negative_edge(self):
        # p_model < ask
        result = _compute_current_edge(model_prob=0.40, best_ask=0.60)
        assert result < 0

    def test_no_fee_override(self):
        # fee_rate=0 reproduces raw edge
        result = _compute_current_edge(model_prob=0.60, best_ask=0.50, fee_rate=0.0)
        assert result == pytest.approx(0.10, abs=1e-6)


# ---------------------------------------------------------------------------
# _station_local_lead_hours — timezone math
# ---------------------------------------------------------------------------

class TestStationLocalLead:
    def test_pre_lockin_same_day(self):
        # Seoul @ KST. Ask at 09:00 KST = 00:00 UTC same day.
        # Lockin at 17:00 KST → 8h lead.
        seoul = CITY_REGISTRY["seoul"]
        now_utc = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)  # 09:00 KST
        target = date(2026, 4, 24)
        result = _station_local_lead_hours(seoul, target, now_utc=now_utc)
        assert result == pytest.approx(8.0, abs=0.01)

    def test_post_lockin_negative(self):
        # 20:00 KST = 11:00 UTC, lockin was at 17:00 KST → -3h lead
        seoul = CITY_REGISTRY["seoul"]
        now_utc = datetime(2026, 4, 24, 11, 0, tzinfo=timezone.utc)
        target = date(2026, 4, 24)
        result = _station_local_lead_hours(seoul, target, now_utc=now_utc)
        assert result < 0

    def test_tomorrow_d_plus_one(self):
        # Seoul at 23:00 KST = 14:00 UTC → to 17:00 KST tomorrow = 18h
        seoul = CITY_REGISTRY["seoul"]
        now_utc = datetime(2026, 4, 24, 14, 0, tzinfo=timezone.utc)
        target = date(2026, 4, 25)
        result = _station_local_lead_hours(seoul, target, now_utc=now_utc)
        assert result == pytest.approx(18.0, abs=0.01)

    def test_bad_timezone_returns_negative(self):
        station = SimpleNamespace(timezone="Not/A/Zone")
        now_utc = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
        result = _station_local_lead_hours(station, date(2026, 4, 24), now_utc=now_utc)
        assert result < 0


# ---------------------------------------------------------------------------
# _best_price_from_book — CLOB book parsing
# ---------------------------------------------------------------------------

class TestBestPriceFromBook:
    def test_best_bid_picks_max(self):
        book = {"bids": [{"price": "0.50", "size": "100"}, {"price": "0.48", "size": "50"}]}
        assert _best_price_from_book(book, side="bids", take_min=False) == 0.50

    def test_best_ask_picks_min(self):
        book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.52", "size": "20"}]}
        assert _best_price_from_book(book, side="asks", take_min=True) == 0.52

    def test_empty_side_returns_none(self):
        assert _best_price_from_book({"bids": []}, side="bids", take_min=False) is None

    def test_missing_side_returns_none(self):
        assert _best_price_from_book({}, side="asks", take_min=True) is None

    def test_malformed_prices_skipped(self):
        book = {"bids": [{"price": "bad"}, {"price": "0.40"}]}
        assert _best_price_from_book(book, side="bids", take_min=False) == 0.40

    def test_zero_prices_filtered(self):
        book = {"bids": [{"price": "0.00"}, {"price": "0.30"}]}
        assert _best_price_from_book(book, side="bids", take_min=False) == 0.30


# ---------------------------------------------------------------------------
# run_rebalance — end-to-end with mocked I/O
# ---------------------------------------------------------------------------

def _seed_trade(
    db: WeatherDatabase,
    *,
    entry_edge: float | None = 0.20,
    forecast_hash: str = "abc123deadbeef",
    target_date: date | None = None,
    city: str = "seoul",
) -> int:
    """Insert a single open paper-mode trade with rebalance baseline."""
    tdate = target_date or (date.today() + timedelta(days=1))
    return db.save_trade(
        city=city,
        target_date=tdate,
        bracket_lower_f=64.4,
        bracket_upper_f=200.0,
        model_prob=0.80,
        market_prob=0.55,
        edge=entry_edge or 0.0,
        kelly_fraction=0.015,
        notional=50.0,
        entry_price=0.55,
        side="BUY",
        mode="paper",
        market_id="0xdeadbeef0000000000000000000000000000000000000000000000000000dead",
        token_id="token_seoul_18c_or_higher",
        question="Seoul temp 18°C or higher April 25?",
        regime="stable_high",
        expected_pnl=5.0,
        entry_edge=entry_edge,
        forecast_content_hash=forecast_hash,
    )


def _stub_forecasts(city: str = "seoul", forecast_f: float = 68.0) -> list[TemperatureForecast]:
    """Two-model ensemble (GFS + ECMWF) at 24h lead — matches minimum set
    that _load_dists_for_rebalance can pair with calibrated dists."""
    init = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    valid = init + timedelta(hours=24)
    return [
        TemperatureForecast(
            city=city, model=WeatherModel.GFS, init_time=init, valid_time=valid,
            lead_hours=24, forecast_high_f=forecast_f, ensemble_spread_f=1.0,
        ),
        TemperatureForecast(
            city=city, model=WeatherModel.ECMWF, init_time=init, valid_time=valid,
            lead_hours=24, forecast_high_f=forecast_f + 0.5, ensemble_spread_f=1.0,
        ),
    ]


def _seed_distributions(db: WeatherDatabase, city: str = "seoul") -> None:
    """Minimal calibrated dist set so _load_dists_for_rebalance finds matches."""
    for model in (WeatherModel.GFS, WeatherModel.ECMWF):
        dist = ErrorDistribution(
            city=city,
            model=model,
            regime=SynopticRegime.STABLE_HIGH,
            lead_hours=24,
            family=DistributionFamily.NORMAL,
            mu=0.0,
            sigma=2.5,
            shape=0.0,
            nu=30.0,
            n_samples=90,
        )
        db.save_error_distribution(dist)


class TestRunRebalance:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.db = WeatherDatabase(self.db_path)
        _seed_distributions(self.db)

    def teardown_method(self):
        self.db.close()
        Path(self.db_path).unlink(missing_ok=True)

    def _run_with_mocks(
        self,
        *,
        forecast_f: float = 68.0,
        best_bid: float = 0.30,
        best_ask: float = 0.35,
        ensemble_stats: dict | None = None,
        dry_run: bool = False,
        orderbook_raises: bool = False,
    ):
        """Patch grib + client I/O so run_rebalance is fully deterministic."""
        forecasts = _stub_forecasts(forecast_f=forecast_f)
        book = {
            "bids": [{"price": str(best_bid), "size": "100"}],
            "asks": [{"price": str(best_ask), "size": "100"}],
        }
        # Force the fallback regime classification branch so we don't need
        # to exercise fetch_ensemble_spread_stats — just returning {} here
        # triggers the except path in run_rebalance which uses the
        # heuristic spread-based classifier.
        ens_stats = ensemble_stats or {"n_members": 0}

        fake_client = MagicMock()
        if orderbook_raises:
            fake_client.get_orderbook.side_effect = RuntimeError("clob 500")
        else:
            fake_client.get_orderbook.return_value = book

        fake_grib = MagicMock()
        fake_grib.fetch_all_models.return_value = forecasts
        fake_grib.fetch_ensemble_spread_stats.return_value = ens_stats

        # Route through the imports inside run_rebalance's function body —
        # it does `from polymarket_strat.api import PolymarketPublicClient`
        # and `from ...grib_client import GribDataClient`, so we patch the
        # source symbols rather than the import.
        with patch(
            "polymarket_strat.api.PolymarketPublicClient",
            return_value=fake_client,
        ), patch(
            "polymarket_strat.infrastructure.weather.grib_client.GribDataClient",
            return_value=fake_grib,
        ), patch(
            "polymarket_strat.infrastructure.weather.persistence.WeatherDatabase",
            return_value=WeatherDatabase(self.db_path),
        ):
            # env_file won't exist in tmp, load_env_file no-ops in that case
            return run_rebalance(
                mode="paper", env_file="/nonexistent.env", dry_run=dry_run
            )

    def _stable_hash_for_stub(self) -> str:
        """Deterministic hash of _stub_forecasts() output — matches what
        run_rebalance computes internally."""
        return forecast_content_hash(_stub_forecasts())

    def test_hold_when_drop_below_stale_threshold(self):
        """Hash unchanged, small edge drop → hold."""
        _seed_trade(self.db, entry_edge=0.20, forecast_hash=self._stable_hash_for_stub())
        # Same forecast → same hash. With forecast 68°F, σ=2.5°F, bracket
        # 64.4→∞: P(bracket) ≈ 0.926. raw_edge = 0.926 - 0.35 = 0.576.
        # edge_drop ≈ 0.20 - 0.55 = -0.35  (negative, current > entry).
        # Negative drop → HOLD.
        result = self._run_with_mocks(best_ask=0.35, best_bid=0.30)
        assert result["exit_count"] == 0
        assert result["hold_count"] + result["skip_count"] == 1

    def test_profit_take_fires_at_big_price_gain(self):
        """Apr 24 2026 (Citadel fix #2) — profit-take preempts edge-drop.

        Entry at 0.55, current best_bid 0.90 → +35¢ price gain, which is
        well above the 20¢ profit-take threshold. Regardless of whether
        edge_drop would also have fired (it does — 35¢ gain means the
        market has caught up to our model → edge shrinks), the exit
        reason should be `profit_take` because it's the earlier trigger
        and it's the structural reason the exit should happen: market
        agreed with us, book the win, don't gamble on settlement.
        """
        tid = _seed_trade(self.db, entry_edge=0.50, forecast_hash=self._stable_hash_for_stub())
        result = self._run_with_mocks(best_ask=0.92, best_bid=0.90)
        assert result["exit_count"] == 1
        ex = result["exits"][0]
        assert ex["id"] == tid
        assert ex["reason"] == "profit_take"
        assert ex["price_gain"] == pytest.approx(0.35, abs=0.01)
        # Paper exit P&L: shares * (best_bid - entry) with 2% fee on gains.
        # shares = 50/0.55 = 90.909, gross = 90.909 * (0.90 - 0.55) = 31.82
        # fee = 0.02 * 31.82 = 0.636; pnl ≈ 31.18
        assert ex["exit_pnl"] == pytest.approx(31.18, abs=0.05)

    def test_profit_take_does_not_fire_below_20c_gain(self):
        """Apr 24 2026 (Citadel fix #2) — boundary test for profit-take.

        Entry at 0.55, best_bid=0.74 → +19¢ gain (just below 20¢ threshold).
        Profit-take should NOT fire. Edge-drop also shouldn't fire because
        forecast matches entry (same hash, high p_model). Result: HOLD.
        """
        _seed_trade(self.db, entry_edge=0.20, forecast_hash=self._stable_hash_for_stub())
        result = self._run_with_mocks(best_ask=0.76, best_bid=0.74)
        assert result["exit_count"] == 0
        # Either hold or skip — never exit on gain < 20¢ if edge holds.
        assert result["hold_count"] + result["skip_count"] == 1

    def test_exit_on_stale_hash_big_drop_without_profit_take(self):
        """Hash unchanged, edge_drop ≥ 15%, but price moved AGAINST us (no
        profit-take trigger). Original pre-fix #2 behavior — edge-drop
        remains the sole exit trigger in loss scenarios.
        """
        # best_bid=0.52 vs entry 0.55 → price_gain = -0.03 (below 20¢ threshold)
        # best_ask=0.80 vs p_bracket ≈ 0.926 → current_edge = 0.926 - 0.80 - 0.02*0.926*0.20 = 0.122
        # edge_drop = 0.50 - 0.122 = 0.378 → above 0.15 stale threshold → exit
        tid = _seed_trade(self.db, entry_edge=0.50, forecast_hash=self._stable_hash_for_stub())
        result = self._run_with_mocks(best_ask=0.80, best_bid=0.52)
        assert result["exit_count"] == 1
        ex = result["exits"][0]
        assert ex["id"] == tid
        assert ex["reason"] == "edge_drop_stale_model"
        assert ex["price_gain"] < 0.20  # didn't trigger profit-take
        assert ex["edge_drop"] >= 0.15  # did trigger edge-drop

    def test_exit_on_fresh_hash_smaller_drop(self):
        """Hash CHANGED, drop between 10 and 15% → exit at tighter threshold."""
        _seed_trade(self.db, entry_edge=0.50, forecast_hash="stored_old_hash")
        # Current hash will differ (we seed a new forecast with different
        # temps implicitly — but more robustly we just use a stored_hash
        # that can't match any real hash output).
        # Pick a best_ask that produces current_edge ≈ 0.38 → drop ≈ 0.12.
        # P_bracket ≈ 0.926. 0.926 - ask - 0.02*0.926*(1-ask) = 0.38
        #   solve: 0.926 - ask - 0.01852 + 0.01852*ask = 0.38
        #   0.9075 + ask*(0.01852 - 1) = 0.38
        #   ask * 0.98148 = 0.5275  → ask ≈ 0.537
        result = self._run_with_mocks(best_ask=0.537, best_bid=0.52)
        assert result["exit_count"] == 1
        ex = result["exits"][0]
        assert ex["reason"] == "edge_drop_fresh_model"
        assert ex["hash_changed"] is True
        assert ex["threshold"] == pytest.approx(0.10)
        # Drop would be ~0.12, below 0.15 stale threshold → fresh gate is
        # what made it exit.
        assert 0.10 <= ex["edge_drop"] < 0.20

    def test_dry_run_no_db_writes(self):
        tid = _seed_trade(self.db, entry_edge=0.50, forecast_hash=self._stable_hash_for_stub())
        result = self._run_with_mocks(best_ask=0.92, best_bid=0.90, dry_run=True)
        assert result["exit_count"] == 1
        # Trade row still open + no cooldown inserted.
        row = self.db._conn.execute(
            "SELECT outcome FROM trade_history WHERE id = ?", (tid,)
        ).fetchone()
        assert row["outcome"] is None
        assert len(self.db.get_cooldown_tokens()) == 0

    def test_cooldown_inserted_on_real_exit(self):
        _seed_trade(self.db, entry_edge=0.50, forecast_hash=self._stable_hash_for_stub())
        self._run_with_mocks(best_ask=0.92, best_bid=0.90)
        cd = self.db.get_cooldown_tokens()
        assert "token_seoul_18c_or_higher" in cd

    def test_legacy_row_skipped(self):
        """entry_edge IS NULL → can't compute drop → skip, don't crash."""
        _seed_trade(self.db, entry_edge=None, forecast_hash="")
        result = self._run_with_mocks()
        assert result["exit_count"] == 0
        skip_reasons = [s["reason"] for s in result["skipped"]]
        assert "legacy_no_entry_edge" in skip_reasons

    def test_past_settlement_skipped(self):
        # target date already past — the station-local lead is negative.
        past = date.today() - timedelta(days=2)
        _seed_trade(self.db, entry_edge=0.50, target_date=past)
        result = self._run_with_mocks(best_ask=0.92, best_bid=0.90)
        assert result["exit_count"] == 0
        skip_reasons = [s["reason"] for s in result["skipped"]]
        assert "past_settlement" in skip_reasons

    def test_orderbook_failure_skips_position(self):
        _seed_trade(self.db, entry_edge=0.50, forecast_hash="abc123deadbeef")
        result = self._run_with_mocks(orderbook_raises=True)
        assert result["exit_count"] == 0
        skip_reasons = [s["reason"] for s in result["skipped"]]
        assert "orderbook_fetch_failed" in skip_reasons


# ---------------------------------------------------------------------------
# _snapshot_open_position_prices — MTM writer
# ---------------------------------------------------------------------------

class TestSnapshotOpenPositions:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.db = WeatherDatabase(self.db_path)

    def teardown_method(self):
        self.db.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_writes_one_row_per_open_position(self):
        _seed_trade(self.db, entry_edge=0.20)
        client = MagicMock()
        client.get_orderbook.return_value = {
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        }
        client.get_market.return_value = {"outcomePrices": ["0.42", "0.58"]}

        written = _snapshot_open_position_prices(self.db, client)
        assert written == 1

        latest = self.db.get_latest_market_price("token_seoul_18c_or_higher")
        assert latest is not None
        assert latest["best_bid"] == pytest.approx(0.40)
        assert latest["best_ask"] == pytest.approx(0.45)
        assert latest["mid_price"] == pytest.approx(0.425)

    def test_orderbook_failure_does_not_crash(self):
        _seed_trade(self.db, entry_edge=0.20)
        client = MagicMock()
        client.get_orderbook.side_effect = RuntimeError("clob down")
        client.get_market.return_value = {"outcomePrices": ["0.5", "0.5"]}

        # Should still write a row with None prices, not raise.
        written = _snapshot_open_position_prices(self.db, client)
        assert written == 1
        latest = self.db.get_latest_market_price("token_seoul_18c_or_higher")
        assert latest["best_bid"] is None

    def test_no_open_positions_writes_nothing(self):
        client = MagicMock()
        written = _snapshot_open_position_prices(self.db, client)
        assert written == 0
        client.get_orderbook.assert_not_called()
