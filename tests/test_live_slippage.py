"""Slippage / depth / entry-gate math.

Pins the two contracts the live executor depends on:

1. ``compute_vwap`` returns the correct volume-weighted fill price when the
   book covers the target, and ``None`` when it doesn't.
2. ``check_entry_gates`` fails early on each precondition (empty book, stale
   book, wide spread, price out of band, insufficient depth) and passes
   when every condition holds, with a populated GateResult in every case so
   the executor can log rich diagnostics on reject.
"""
from __future__ import annotations

import time

import pytest

from polymarket_strat.live.orderbook import OrderBook, OrderLevel
from polymarket_strat.live.slippage import (
    REASON_INSUFFICIENT_DEPTH,
    REASON_INVALID_INPUT,
    REASON_NO_ASKS,
    REASON_NO_BIDS,
    REASON_OK,
    REASON_PRICE_OUT_OF_BAND,
    REASON_STALE_BOOK,
    REASON_WIDE_SPREAD,
    check_entry_gates,
    compute_vwap,
    depth_within_tolerance,
)


def _book(
    *,
    asks: list[tuple[float, float]] | None = None,
    bids: list[tuple[float, float]] | None = None,
    timestamp: float | None = None,
) -> OrderBook:
    ts = time.time() if timestamp is None else timestamp
    return OrderBook(
        token_id="t",
        asks=tuple(OrderLevel(price=p, size=s) for p, s in (asks or [])),
        bids=tuple(OrderLevel(price=p, size=s) for p, s in (bids or [])),
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# compute_vwap
# ---------------------------------------------------------------------------


def test_compute_vwap_single_level_covers_target():
    book = _book(asks=[(0.50, 100.0)])
    result = compute_vwap(book, target_shares=20.0)
    assert result is not None
    assert result.vwap == pytest.approx(0.50)
    assert result.cost_usd == pytest.approx(10.0)
    assert result.shares_filled == pytest.approx(20.0)
    assert result.levels_consumed == 1


def test_compute_vwap_walks_multiple_levels():
    # Need 30 shares. First level has 10 at 0.50, second has 30 at 0.55.
    # Fill 10 @ 0.50 = 5.00, then 20 @ 0.55 = 11.00. Total = 16.00 / 30 = 0.5333...
    book = _book(asks=[(0.50, 10.0), (0.55, 30.0)])
    result = compute_vwap(book, target_shares=30.0)
    assert result is not None
    assert result.cost_usd == pytest.approx(16.0)
    assert result.vwap == pytest.approx(16.0 / 30.0)
    assert result.levels_consumed == 2


def test_compute_vwap_returns_none_on_depth_shortfall():
    book = _book(asks=[(0.50, 10.0), (0.55, 5.0)])
    assert compute_vwap(book, target_shares=20.0) is None


def test_compute_vwap_returns_none_on_empty_asks():
    assert compute_vwap(_book(asks=[]), target_shares=10.0) is None


def test_compute_vwap_rejects_nonpositive_target():
    book = _book(asks=[(0.50, 100.0)])
    assert compute_vwap(book, target_shares=0.0) is None
    assert compute_vwap(book, target_shares=-5.0) is None


def test_compute_vwap_stops_exactly_when_target_met():
    # Top level has exactly what we need — stop after one level.
    book = _book(asks=[(0.50, 20.0), (0.51, 100.0)])
    result = compute_vwap(book, target_shares=20.0)
    assert result is not None
    assert result.levels_consumed == 1
    assert result.vwap == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# depth_within_tolerance
# ---------------------------------------------------------------------------


def test_depth_sums_levels_at_or_below_limit():
    book = _book(asks=[(0.50, 10.0), (0.51, 20.0), (0.53, 30.0)])
    usd, shares = depth_within_tolerance(book, limit_price=0.51)
    assert usd == pytest.approx(0.50 * 10 + 0.51 * 20)
    assert shares == pytest.approx(30.0)


def test_depth_excludes_levels_above_limit():
    book = _book(asks=[(0.50, 10.0), (0.55, 999.0)])
    usd, shares = depth_within_tolerance(book, limit_price=0.52)
    assert usd == pytest.approx(5.0)
    assert shares == pytest.approx(10.0)


def test_depth_is_zero_when_limit_below_top_of_book():
    book = _book(asks=[(0.50, 10.0)])
    usd, shares = depth_within_tolerance(book, limit_price=0.49)
    assert usd == 0.0
    assert shares == 0.0


# ---------------------------------------------------------------------------
# check_entry_gates — rejects
# ---------------------------------------------------------------------------


def test_gate_rejects_empty_asks():
    book = _book(asks=[], bids=[(0.49, 50.0)])
    result = check_entry_gates(book=book, notional_target=10.0)
    assert not result.passed
    assert result.reason == REASON_NO_ASKS


def test_gate_rejects_empty_bids():
    book = _book(asks=[(0.50, 50.0)], bids=[])
    result = check_entry_gates(book=book, notional_target=10.0)
    assert not result.passed
    assert result.reason == REASON_NO_BIDS


def test_gate_rejects_stale_book():
    book = _book(
        asks=[(0.50, 100.0)],
        bids=[(0.49, 100.0)],
        timestamp=time.time() - 60,  # 60s old
    )
    result = check_entry_gates(book=book, notional_target=10.0, max_book_age_s=10.0)
    assert not result.passed
    assert result.reason == REASON_STALE_BOOK
    assert result.book_age_s >= 60.0


def test_gate_rejects_wide_spread():
    # ask 0.60, bid 0.40 → spread 0.20, default max 0.03.
    book = _book(asks=[(0.60, 100.0)], bids=[(0.40, 100.0)])
    result = check_entry_gates(book=book, notional_target=10.0)
    assert not result.passed
    assert result.reason == REASON_WIDE_SPREAD
    assert result.spread == pytest.approx(0.20)


def test_gate_rejects_price_above_max_band():
    book = _book(asks=[(0.90, 100.0)], bids=[(0.89, 100.0)])
    result = check_entry_gates(book=book, notional_target=10.0, max_entry_price=0.85)
    assert not result.passed
    assert result.reason == REASON_PRICE_OUT_OF_BAND


def test_gate_rejects_price_below_min_band():
    book = _book(asks=[(0.01, 100.0)], bids=[(0.009, 100.0)])
    result = check_entry_gates(book=book, notional_target=1.0, min_entry_price=0.02)
    assert not result.passed
    assert result.reason == REASON_PRICE_OUT_OF_BAND


def test_gate_rejects_insufficient_depth():
    # Only 5 shares on the book, but we want $10 at 0.50 → 20 shares.
    book = _book(asks=[(0.50, 5.0)], bids=[(0.49, 100.0)])
    result = check_entry_gates(
        book=book, notional_target=10.0, slippage_tol=0.01, max_spread=0.05
    )
    assert not result.passed
    assert result.reason == REASON_INSUFFICIENT_DEPTH
    assert result.shares_target > 0  # it got as far as sizing


def test_gate_rejects_nonpositive_notional():
    book = _book(asks=[(0.50, 100.0)], bids=[(0.49, 100.0)])
    assert check_entry_gates(book=book, notional_target=0.0).reason == REASON_INVALID_INPUT
    assert check_entry_gates(book=book, notional_target=-5.0).reason == REASON_INVALID_INPUT


# ---------------------------------------------------------------------------
# check_entry_gates — happy path
# ---------------------------------------------------------------------------


def test_gate_passes_with_single_deep_level():
    book = _book(asks=[(0.50, 1000.0)], bids=[(0.49, 1000.0)])
    result = check_entry_gates(
        book=book,
        notional_target=10.0,
        slippage_tol=0.01,
        max_spread=0.03,
    )
    assert result.passed
    assert result.reason == REASON_OK
    assert result.limit_price == pytest.approx(0.51)
    # Conservative sizing: $10 / $0.51 ≈ 19.608 shares
    assert result.shares_target == pytest.approx(10.0 / 0.51)
    # VWAP is best_ask (top level covers fill entirely)
    assert result.expected_vwap == pytest.approx(0.50)
    assert result.expected_slippage_per_share == pytest.approx(0.0)


def test_gate_populates_full_diagnostics_on_pass():
    book = _book(asks=[(0.50, 100.0), (0.52, 100.0)], bids=[(0.49, 100.0)])
    result = check_entry_gates(book=book, notional_target=10.0)
    assert result.passed
    assert result.best_ask == pytest.approx(0.50)
    assert result.best_bid == pytest.approx(0.49)
    assert result.spread == pytest.approx(0.01)
    assert result.depth_usd_within_limit > 0
    assert result.depth_shares_within_limit > 0


def test_gate_expected_slippage_when_book_thin_but_sufficient():
    # Need $10 → 10/0.51 ≈ 19.61 shares at limit.
    # Top level has 10 shares at 0.50, next has 100 at 0.51.
    # So 10 fill at 0.50, 9.61 fill at 0.51. VWAP ≈ 0.5049.
    book = _book(asks=[(0.50, 10.0), (0.51, 100.0)], bids=[(0.49, 100.0)])
    result = check_entry_gates(book=book, notional_target=10.0, slippage_tol=0.01)
    assert result.passed
    assert 0.50 < result.expected_vwap < 0.51
    assert result.expected_slippage_per_share > 0


# ---------------------------------------------------------------------------
# GateResult populated on reject (diagnostic check)
# ---------------------------------------------------------------------------


def test_reject_result_populates_best_ask_when_available():
    # Price out of band → still report best_ask/best_bid for logging.
    book = _book(asks=[(0.95, 10.0)], bids=[(0.94, 10.0)])
    result = check_entry_gates(book=book, notional_target=5.0, max_entry_price=0.85)
    assert not result.passed
    assert result.best_ask == pytest.approx(0.95)
    assert result.best_bid == pytest.approx(0.94)


def test_reject_no_asks_still_returns_populated_struct():
    book = _book(asks=[], bids=[(0.49, 10.0)])
    result = check_entry_gates(book=book, notional_target=5.0)
    assert not result.passed
    assert result.best_ask == 0.0  # defaulted
    assert result.best_bid == pytest.approx(0.49)
