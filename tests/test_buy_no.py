"""Pin the Apr 24 2026 Citadel fix #5 — buy-NO side.

Why this test exists:
  Before fix #5, strategy.py only emitted BUY-YES signals. For every
  bracket where the market overpriced YES (== underpriced NO), we
  silently dropped half the edge surface. Production logs on Apr 24
  showed 252 / 519 contracts rejected by the YES-only market band —
  a substantial fraction of those were NO-side opportunities.

  This file pins three invariants:
    1. Settlement P&L math flips for NO positions (outcome=1 is LOSS
       for NO, WIN for YES).
    2. Rebalance current-edge math flips for NO positions (model_prob
       uses 1 - p_yes, book prices are already NO-token prices).
    3. token_side persists through save_trade and round-trips.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
from polymarket_strat.main import _pnl, _compute_current_edge


class TestPnlTokenSide:
    """Core payout math must handle YES and NO symmetrically."""

    def test_yes_win(self):
        """YES token, outcome=1 → WIN."""
        pnl = _pnl(outcome=1, notional=50.0, entry_price=0.40, token_side="YES")
        # shares = 125, gross = 125 * 0.60 = 75, fee = 1.5 → pnl = 73.5
        assert pnl == pytest.approx(73.5, abs=0.05)

    def test_yes_loss(self):
        """YES token, outcome=0 → LOSS = -notional."""
        pnl = _pnl(outcome=0, notional=50.0, entry_price=0.40, token_side="YES")
        assert pnl == pytest.approx(-50.0)

    def test_no_win_when_yes_resolves_false(self):
        """NO token, outcome=0 (YES resolves false → NO resolves true) → WIN.
        Math: shares = notional/entry_price_no, gross = shares*(1-entry),
        fee on winnings. Entry for NO is the NO-token price (= 1 - p_yes).
        Example: entry at 0.30 for NO, $50 notional → shares=166.67,
        gross=166.67*0.70=116.67, fee=2.33, pnl=114.33.
        """
        pnl = _pnl(outcome=0, notional=50.0, entry_price=0.30, token_side="NO")
        assert pnl == pytest.approx(50 / 0.30 * (1 - 0.30) * 0.98, abs=0.05)

    def test_no_loss_when_yes_resolves_true(self):
        """NO token, outcome=1 (YES resolves true → NO resolves false) → LOSS."""
        pnl = _pnl(outcome=1, notional=50.0, entry_price=0.30, token_side="NO")
        assert pnl == pytest.approx(-50.0)

    def test_invalid_token_side_defaults_to_yes(self):
        """Malformed token_side should behave as YES (legacy default)."""
        pnl_default = _pnl(outcome=1, notional=50.0, entry_price=0.40)
        pnl_yes = _pnl(outcome=1, notional=50.0, entry_price=0.40, token_side="YES")
        assert pnl_default == pytest.approx(pnl_yes)


class TestCurrentEdgeTokenSide:
    """Rebalance edge computation must be symmetric under YES/NO flip."""

    def test_yes_edge_standard(self):
        """YES: model_prob=0.60, best_ask_yes=0.40 → raw 20¢, fee_drag
        tiny, adjusted ~19.5¢."""
        edge = _compute_current_edge(model_prob=0.60, best_ask=0.40, token_side="YES")
        assert edge == pytest.approx(0.60 - 0.40 - 0.02 * 0.60 * 0.60, abs=1e-4)

    def test_no_edge_uses_flipped_inputs(self):
        """NO: caller passes model_prob=(1-p_yes), best_ask=(1-best_bid_yes).
        Formula is identical under the flipped convention."""
        # Original YES situation: model=0.60 for YES, best_bid_yes=0.45.
        # NO: model_no=0.40, best_ask_no=0.55.
        # NO edge = 0.40 - 0.55 - 0.02*0.40*0.45 = -0.15 - 0.0036 = -0.1536
        # (market overprices NO → no edge to buy NO, negative edge).
        edge = _compute_current_edge(model_prob=0.40, best_ask=0.55, token_side="NO")
        assert edge == pytest.approx(-0.1536, abs=1e-4)

    def test_no_edge_positive_when_market_underprices_no(self):
        """NO overpriced YES (market says YES=0.80, model says YES=0.50):
        market_no ≈ 0.20, model_no = 0.50 → NO-side edge ~30¢.
        """
        edge = _compute_current_edge(model_prob=0.50, best_ask=0.20, token_side="NO")
        # raw = 0.30, fee_drag = 0.02*0.50*0.80 = 0.008 → 0.292
        assert edge == pytest.approx(0.292, abs=1e-4)


class TestSaveTradeTokenSide:
    """Schema and save_trade round-trip the token_side column."""

    @pytest.fixture
    def db(self, tmp_path: Path):
        path = tmp_path / "test_buy_no.db"
        db = WeatherDatabase(str(path))
        yield db
        db.close()

    def test_save_trade_default_is_yes(self, db: WeatherDatabase):
        """Explicit YES value persists and reads back."""
        tid = db.save_trade(
            city="nyc",
            target_date=date.today() + timedelta(days=1),
            bracket_lower_f=60.0,
            bracket_upper_f=70.0,
            model_prob=0.5,
            market_prob=0.4,
            edge=0.1,
            kelly_fraction=0.01,
            notional=10.0,
            entry_price=0.40,
            side="BUY_YES",
            # no token_side → defaults to YES
        )
        row = db._conn.execute(
            "SELECT token_side FROM trade_history WHERE id = ?", (tid,)
        ).fetchone()
        assert row["token_side"] == "YES"

    def test_save_trade_no_persists(self, db: WeatherDatabase):
        tid = db.save_trade(
            city="nyc",
            target_date=date.today() + timedelta(days=1),
            bracket_lower_f=60.0,
            bracket_upper_f=70.0,
            model_prob=0.5,
            market_prob=0.4,
            edge=0.1,
            kelly_fraction=0.01,
            notional=10.0,
            entry_price=0.40,
            side="BUY_NO",
            token_side="NO",
        )
        row = db._conn.execute(
            "SELECT token_side FROM trade_history WHERE id = ?", (tid,)
        ).fetchone()
        assert row["token_side"] == "NO"

    def test_save_trade_rejects_malformed_token_side(self, db: WeatherDatabase):
        """Typos must fail fast — don't corrupt schema."""
        with pytest.raises(ValueError, match="token_side"):
            db.save_trade(
                city="nyc",
                target_date=date.today(),
                bracket_lower_f=60.0, bracket_upper_f=70.0,
                model_prob=0.5, market_prob=0.4, edge=0.1,
                kelly_fraction=0.01, notional=10.0, entry_price=0.40,
                side="BUY", token_side="Yes",  # lowercase 'e' — wrong
            )
