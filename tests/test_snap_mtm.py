"""Smoke tests for the Apr 27 2026 run_snap_mtm CLI subcommand.

Why this exists:
  run_snap_mtm wraps _snapshot_open_position_prices for use as a
  standalone */5min cron entry. Tests verify:
    - With no open positions, returns 0 cleanly
    - With open positions, calls _snapshot_open_position_prices
    - Returns the expected shape: {snapshot_count, open_positions,
      duration_s}
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
from polymarket_strat.main import run_snap_mtm


@pytest.fixture
def tmp_db(tmp_path: Path) -> WeatherDatabase:
    db_path = tmp_path / "test_snap.db"
    db = WeatherDatabase(str(db_path))
    yield db
    db.close()


class TestRunSnapMtm:
    def test_no_open_positions_returns_zero(self, tmp_path: Path) -> None:
        """Empty DB → 0 snapshots, 0 open. No errors, no API calls."""
        db_path = tmp_path / "empty.db"
        WeatherDatabase(str(db_path)).close()  # init schema then close

        fake_client = MagicMock()
        with patch(
            "polymarket_strat.api.PolymarketPublicClient",
            return_value=fake_client,
        ), patch(
            "polymarket_strat.infrastructure.weather.persistence.WeatherDatabase",
            return_value=WeatherDatabase(str(db_path)),
        ):
            result = run_snap_mtm(env_file="/nonexistent.env")

        assert result["snapshot_count"] == 0
        assert result["open_positions"] == 0
        assert "duration_s" in result
        assert isinstance(result["duration_s"], float)
        # When there are no positions, we should never hit the orderbook API.
        fake_client.get_orderbook.assert_not_called()

    def test_with_open_positions_calls_snapshot(self, tmp_path: Path) -> None:
        """When there are open positions, run_snap_mtm should invoke the
        snapshot helper for each one."""
        from datetime import date
        db_path = tmp_path / "with_pos.db"
        db = WeatherDatabase(str(db_path))
        # Seed two open positions
        for tok in ("token_a", "token_b"):
            db.save_trade(
                city="seoul",
                target_date=date.today(),
                bracket_lower_f=64.0,
                bracket_upper_f=66.0,
                model_prob=0.7,
                market_prob=0.5,
                edge=0.18,
                kelly_fraction=0.02,
                notional=30.0,
                entry_price=0.50,
                side="BUY_NO",
                mode="paper",
                market_id="m1",
                token_id=tok,
                question="q",
                regime="stable_high",
                expected_pnl=5.0,
                entry_edge=0.18,
                forecast_content_hash="h",
                token_side="NO",
            )
        db.close()

        fake_client = MagicMock()
        fake_client.get_orderbook.return_value = {
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        }
        fake_client.get_market.return_value = {"outcomePrices": "[\"0.5\",\"0.5\"]"}

        with patch(
            "polymarket_strat.api.PolymarketPublicClient",
            return_value=fake_client,
        ), patch(
            "polymarket_strat.infrastructure.weather.persistence.WeatherDatabase",
            return_value=WeatherDatabase(str(db_path)),
        ):
            result = run_snap_mtm(env_file="/nonexistent.env")

        assert result["open_positions"] == 2
        assert result["snapshot_count"] == 2
        assert fake_client.get_orderbook.call_count == 2

    def test_orderbook_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """If one orderbook fetch raises, the snapshot should still return
        cleanly with the rows that did succeed. snap-mtm running on a
        */5min cron MUST NOT crash — it'd silently break the dashboard."""
        from datetime import date
        db_path = tmp_path / "fail.db"
        db = WeatherDatabase(str(db_path))
        db.save_trade(
            city="seoul",
            target_date=date.today(),
            bracket_lower_f=64.0,
            bracket_upper_f=66.0,
            model_prob=0.7,
            market_prob=0.5,
            edge=0.18,
            kelly_fraction=0.02,
            notional=30.0,
            entry_price=0.50,
            side="BUY_NO",
            mode="paper",
            market_id="m1",
            token_id="tok_fail",
            question="q",
            regime="stable_high",
            expected_pnl=5.0,
            entry_edge=0.18,
            forecast_content_hash="h",
            token_side="NO",
        )
        db.close()

        fake_client = MagicMock()
        fake_client.get_orderbook.side_effect = RuntimeError("CLOB 500")

        with patch(
            "polymarket_strat.api.PolymarketPublicClient",
            return_value=fake_client,
        ), patch(
            "polymarket_strat.infrastructure.weather.persistence.WeatherDatabase",
            return_value=WeatherDatabase(str(db_path)),
        ):
            result = run_snap_mtm(env_file="/nonexistent.env")

        # The snapshot helper still writes a row even on error (just with
        # NULL prices), so snapshot_count is 1 not 0. Key assertion: no
        # exception propagates.
        assert result["open_positions"] == 1
        assert "duration_s" in result
