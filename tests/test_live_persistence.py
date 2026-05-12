"""LiveDatabase — schema, save/load, settlement, analytics.

These tests instantiate a temp-path DB on every run so there's zero coupling
to the real ``data/weather/live.db`` file. The whole point of the isolated DB
is to let us evolve its schema without risking the paper pipeline, so we lock
in the write→read round-trip plus the analytics helpers the dashboard uses.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from polymarket_strat.live.persistence import LiveDatabase


@pytest.fixture
def db(tmp_path: Path) -> LiveDatabase:
    instance = LiveDatabase(tmp_path / "live.db")
    yield instance
    instance.close()


def _fill_row(
    *,
    mode: str = "live",
    token_id: str = "tok-1",
    filled: int = 1,
    reason: str = "",
    actual_cost_usd: float = 5.0,
    actual_shares: float = 10.0,
    actual_vwap: float = 0.50,
    slippage_usd: float = 0.0,
    notional_requested: float = 5.0,
    order_id: str = "0xabc",
) -> dict[str, Any]:
    return {
        "mode": mode,
        "token_id": token_id,
        "filled": filled,
        "reason": reason,
        "notional_requested": notional_requested,
        "quoted_best_ask": 0.50,
        "quoted_best_bid": 0.49,
        "quoted_spread": 0.01,
        "book_age_s": 2.0,
        "limit_price": 0.51,
        "shares_target": 9.8,
        "expected_vwap": 0.50,
        "expected_cost_usd": 4.9,
        "expected_slippage_per_share": 0.0,
        "depth_usd_within_limit": 50.0,
        "depth_shares_within_limit": 100.0,
        "order_id": order_id,
        "actual_vwap": actual_vwap,
        "actual_cost_usd": actual_cost_usd,
        "actual_shares": actual_shares,
        "slippage_usd": slippage_usd,
        "error_message": "",
    }


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_save_attempt_returns_row_id_and_persists_all_fields(db: LiveDatabase):
    row_id = db.save_attempt(
        city="seoul",
        target_date=date(2026, 4, 22),
        question="Seoul 21°C+?",
        market_id="0xmarket",
        bracket_lower_f=69.8,
        bracket_upper_f=10000.0,
        model_prob=0.72,
        market_prob=0.54,
        edge=0.18,
        regime="STABLE_HIGH",
        expected_pnl=0.85,
        fill_row=_fill_row(),
    )
    assert row_id >= 1
    recent = db.get_recent_attempts(limit=10)
    assert len(recent) == 1
    row = recent[0]
    assert row["city"] == "seoul"
    assert row["target_date"] == "2026-04-22"
    assert row["model_prob"] == pytest.approx(0.72)
    assert row["expected_pnl"] == pytest.approx(0.85)
    assert row["actual_cost_usd"] == pytest.approx(5.0)
    assert row["outcome"] is None
    assert row["filled"] == 1


def test_save_attempt_accepts_iso_string_date(db: LiveDatabase):
    db.save_attempt(
        city="nyc",
        target_date="2026-04-22",
        fill_row=_fill_row(),
    )
    recent = db.get_recent_attempts()
    assert recent[0]["target_date"] == "2026-04-22"


# ---------------------------------------------------------------------------
# Open-position helpers (used by runner to cap exposure + duplicate-guard)
# ---------------------------------------------------------------------------


def test_sum_open_live_notional_counts_only_unsettled_live_fills(db: LiveDatabase):
    # Two live fills — one settled, one still open.
    open_id = db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row=_fill_row(token_id="t-open", actual_cost_usd=10.0),
    )
    settled_id = db.save_attempt(
        city="seoul", target_date=date(2026, 4, 21),
        fill_row=_fill_row(token_id="t-settled", actual_cost_usd=7.0),
    )
    # Rejected attempt — should not count.
    db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row=_fill_row(token_id="t-rej", filled=0, reason="wide_spread", actual_cost_usd=0.0),
    )
    # Shadow fill — should not count toward LIVE notional.
    db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row=_fill_row(mode="shadow", token_id="t-shadow", actual_cost_usd=99.0),
    )

    db.settle_attempt(row_id=settled_id, outcome=1, pnl=3.5)

    assert db.sum_open_live_notional() == pytest.approx(10.0)


def test_list_open_tokens_filters_by_mode(db: LiveDatabase):
    db.save_attempt(city="seoul", target_date=date(2026, 4, 22),
                    fill_row=_fill_row(mode="live", token_id="tok-live"))
    db.save_attempt(city="seoul", target_date=date(2026, 4, 22),
                    fill_row=_fill_row(mode="shadow", token_id="tok-shadow"))
    assert db.list_open_tokens(mode="live") == ["tok-live"]
    assert db.list_open_tokens(mode="shadow") == ["tok-shadow"]
    assert set(db.list_open_tokens()) == {"tok-live", "tok-shadow"}


def test_list_open_tokens_excludes_settled(db: LiveDatabase):
    rid = db.save_attempt(city="seoul", target_date=date(2026, 4, 22),
                          fill_row=_fill_row(token_id="tok-done"))
    db.settle_attempt(row_id=rid, outcome=1, pnl=5.0)
    assert db.list_open_tokens() == []


def test_list_open_tokens_excludes_rejected(db: LiveDatabase):
    db.save_attempt(city="seoul", target_date=date(2026, 4, 22),
                    fill_row=_fill_row(token_id="tok-rej", filled=0, reason="wide_spread"))
    assert db.list_open_tokens() == []


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def test_settle_attempt_writes_outcome_pnl_timestamp(db: LiveDatabase):
    rid = db.save_attempt(city="seoul", target_date=date(2026, 4, 22),
                          fill_row=_fill_row())
    db.settle_attempt(row_id=rid, outcome=1, pnl=2.94)
    rows = db.get_recent_attempts()
    assert rows[0]["outcome"] == 1
    assert rows[0]["pnl"] == pytest.approx(2.94)
    assert rows[0]["settled_at"] is not None


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def test_slippage_summary_averages_filled_rows_only(db: LiveDatabase):
    db.save_attempt(city="a", target_date="2026-04-22",
                    fill_row=_fill_row(slippage_usd=0.10))
    db.save_attempt(city="b", target_date="2026-04-22",
                    fill_row=_fill_row(slippage_usd=0.30))
    # Rejected row with slippage_usd=0 should be ignored by slippage_summary.
    db.save_attempt(city="c", target_date="2026-04-22",
                    fill_row=_fill_row(filled=0, reason="wide_spread", slippage_usd=0.0))
    summary = db.slippage_summary(mode="live")
    assert summary["n"] == 2
    assert summary["avg_slip_usd"] == pytest.approx(0.20)
    assert summary["total_slip_usd"] == pytest.approx(0.40)


def test_fill_rate_breaks_down_by_reason(db: LiveDatabase):
    db.save_attempt(city="a", target_date="2026-04-22",
                    fill_row=_fill_row())  # filled
    db.save_attempt(city="b", target_date="2026-04-22",
                    fill_row=_fill_row(filled=0, reason="wide_spread"))
    db.save_attempt(city="c", target_date="2026-04-22",
                    fill_row=_fill_row(filled=0, reason="wide_spread"))
    db.save_attempt(city="d", target_date="2026-04-22",
                    fill_row=_fill_row(filled=0, reason="insufficient_depth"))
    result = db.fill_rate(mode="live")
    assert result["n"] == 4
    assert result["filled"] == 1
    assert result["fill_rate"] == pytest.approx(0.25)
    assert result["rejections"] == {"wide_spread": 2, "insufficient_depth": 1}


def test_count_today_daily_pnl_sums_settled_today_only(db: LiveDatabase):
    r1 = db.save_attempt(city="a", target_date="2026-04-22", fill_row=_fill_row())
    r2 = db.save_attempt(city="b", target_date="2026-04-22", fill_row=_fill_row())
    db.settle_attempt(row_id=r1, outcome=1, pnl=3.0)
    db.settle_attempt(row_id=r2, outcome=0, pnl=-5.0)
    assert db.count_today_daily_pnl(mode="live") == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# File isolation — the entire reason this module exists
# ---------------------------------------------------------------------------


def test_db_path_is_isolated_from_paper_weather_db(tmp_path: Path):
    """The live DB must live at its own path, not at weather.db."""
    db = LiveDatabase(tmp_path / "live.db")
    try:
        assert db.db_path.name == "live.db"
        assert db.db_path.parent == tmp_path
        # And the file is created eagerly so parallel instances don't race.
        assert db.db_path.exists()
    finally:
        db.close()
