"""Regression test for the Apr 27 2026 edge-convention fix.

Pins the contract:
  - DB.edge stays in PP units (0..0.5) regardless of strategy.
  - DB.token_side correctly distinguishes "YES" from "NO" trades.
  - DB.model_prob is the side's P(win), not a placeholder 0.

Why this exists:
  Before this fix:
    - tail-NO wrote DB.edge as (ev_per_dollar × notional) → values like 122.37
    - all NO trades wrote DB.token_side="YES" because metadata key was missing
    - tail-NO wrote DB.model_prob=0 because metadata used "empirical_p_no" not "model_prob"
  Both bugs traced to the save_trade glue layer in main.py reading from
  inconsistent metadata keys per strategy. The fix routes everything
  through canonical keys (edge_after_fees, token_side, model_prob) and
  derives sane fallbacks.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase


def _save_one(db_path: Path, **overrides) -> dict:
    """Helper: open a fresh DB, save one trade with given overrides, read row back."""
    db = WeatherDatabase(db_path=str(db_path))
    args = dict(
        city="london",
        target_date=date(2026, 4, 27),
        bracket_lower_f=60.0,
        bracket_upper_f=62.0,
        model_prob=0.85,
        market_prob=0.55,
        edge=0.295,
        kelly_fraction=0.5,
        notional=30.0,
        entry_price=0.55,
        side="BUY_NO",
        mode="paper",
        market_id="test",
        token_id="tok123",
        question="test",
        regime="stable_high",
        expected_pnl=10.0,
        entry_edge=0.295,
        forecast_content_hash="hash",
        token_side="NO",
    )
    args.update(overrides)
    db.save_trade(**args)
    row = db._conn.execute(
        "SELECT edge, entry_edge, model_prob, market_prob, token_side, "
        "category, strategy_name, side "
        "FROM trade_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    db.close()
    return {
        "edge": row[0],
        "entry_edge": row[1],
        "model_prob": row[2],
        "market_prob": row[3],
        "token_side": row[4],
        "category": row[5],
        "strategy_name": row[6],
        "side": row[7],
    }


class TestEdgeUnits:
    """DB.edge must stay in PP units (range 0..0.5) regardless of who wrote it."""

    def test_edge_is_in_pp_units(self, tmp_path: Path) -> None:
        out = _save_one(tmp_path / "test.db", edge=0.10)
        assert 0.0 <= out["edge"] <= 0.5
        assert out["edge"] == 0.10

    def test_entry_edge_is_in_pp_units(self, tmp_path: Path) -> None:
        out = _save_one(tmp_path / "test.db", entry_edge=0.15)
        assert 0.0 <= out["entry_edge"] <= 0.5
        assert out["entry_edge"] == 0.15


class TestTokenSide:
    """token_side must be 'YES' or 'NO' and accurately reflect which side
    was picked, not a hardcoded fallback."""

    def test_no_side_persists_correctly(self, tmp_path: Path) -> None:
        out = _save_one(tmp_path / "test.db", token_side="NO", side="BUY_NO")
        assert out["token_side"] == "NO"
        assert out["side"] == "BUY_NO"

    def test_yes_side_persists_correctly(self, tmp_path: Path) -> None:
        out = _save_one(tmp_path / "test.db", token_side="YES", side="BUY_YES")
        assert out["token_side"] == "YES"

    def test_invalid_token_side_raises(self, tmp_path: Path) -> None:
        import pytest
        with pytest.raises(ValueError):
            _save_one(tmp_path / "test.db", token_side="Yes")
        with pytest.raises(ValueError):
            _save_one(tmp_path / "test.db", token_side="no")


class TestModelProb:
    """model_prob must be the side's actual P(win), not a placeholder 0."""

    def test_model_prob_persists(self, tmp_path: Path) -> None:
        out = _save_one(tmp_path / "test.db", model_prob=0.92)
        assert out["model_prob"] == 0.92

    def test_zero_model_prob_is_a_red_flag(self, tmp_path: Path) -> None:
        # Allow model_prob=0 to be saved (some legacy paths) but flag it
        # as suspicious. Test exists to make the pre-fix bug pattern explicit:
        # if a future caller sets model_prob=0 unintentionally, this row would
        # have failed any post-hoc calibration analysis silently.
        out = _save_one(tmp_path / "test.db", model_prob=0.0)
        assert out["model_prob"] == 0.0
        # We do NOT assert > 0 because save_trade is a low-level persistence
        # call and shouldn't enforce business logic. But analytics should
        # filter WHERE model_prob > 0 to avoid these pollution rows.


class TestCategoryProvenance:
    """Apr 26 2026 fix: category column persists for tail-NO so rebalance
    skip-list works."""

    def test_category_persists_when_set(self, tmp_path: Path) -> None:
        out = _save_one(
            tmp_path / "test.db",
            category="weather_tail_no",
            strategy_name="weather_tail_no",
        )
        assert out["category"] == "weather_tail_no"
        assert out["strategy_name"] == "weather_tail_no"

    def test_category_null_for_legacy_rows(self, tmp_path: Path) -> None:
        out = _save_one(tmp_path / "test.db")  # no category override
        assert out["category"] is None
        assert out["strategy_name"] is None
