"""Lock-in tests for the CLOB orderbook parser.

The Polymarket /book response is a semi-typed mess — prices and sizes are
strings, the timestamp flips between seconds and milliseconds across
endpoints, and either side can come back empty on illiquid markets. These
tests pin the parsing contract so a regression can't silently corrupt the
gate math downstream.
"""
from __future__ import annotations

import time

import pytest

from polymarket_strat.live.orderbook import OrderLevel, parse_orderbook


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _raw_book(
    *,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    timestamp: str | float = "1745280000",
    token_id: str = "t-1",
    include_market: bool = True,
) -> dict:
    payload: dict = {
        "asset_id": token_id,
        "hash": "abc",
        "timestamp": timestamp,
        "bids": [{"price": p, "size": s} for p, s in (bids or [])],
        "asks": [{"price": p, "size": s} for p, s in (asks or [])],
    }
    if include_market:
        payload["market"] = "0xmarket"
    return payload


# ---------------------------------------------------------------------------
# Level parsing + sorting
# ---------------------------------------------------------------------------


def test_parses_string_prices_and_sizes():
    raw = _raw_book(asks=[("0.53", "10")], bids=[("0.51", "20")])
    book = parse_orderbook(raw)
    assert book.asks == (OrderLevel(price=0.53, size=10.0),)
    assert book.bids == (OrderLevel(price=0.51, size=20.0),)


def test_asks_sorted_ascending_bids_descending():
    raw = _raw_book(
        asks=[("0.56", "1"), ("0.53", "2"), ("0.54", "3")],
        bids=[("0.48", "4"), ("0.50", "5"), ("0.49", "6")],
    )
    book = parse_orderbook(raw)
    assert [lvl.price for lvl in book.asks] == [0.53, 0.54, 0.56]
    assert [lvl.price for lvl in book.bids] == [0.50, 0.49, 0.48]


def test_drops_zero_and_negative_levels():
    raw = _raw_book(
        asks=[("0.53", "10"), ("0.54", "0"), ("-0.01", "5"), ("0.55", "7")],
    )
    book = parse_orderbook(raw)
    assert [lvl.price for lvl in book.asks] == [0.53, 0.55]


def test_drops_non_dict_entries_defensively():
    raw = _raw_book(asks=[("0.53", "10")])
    raw["asks"].append("not-a-dict")  # type: ignore[arg-type]
    raw["asks"].append({"price": None, "size": "5"})
    book = parse_orderbook(raw)
    assert book.asks == (OrderLevel(price=0.53, size=10.0),)


# ---------------------------------------------------------------------------
# Timestamp normalization
# ---------------------------------------------------------------------------


def test_timestamp_in_seconds_passed_through():
    book = parse_orderbook(_raw_book(timestamp="1745280000"))
    assert book.timestamp == pytest.approx(1745280000.0)


def test_timestamp_in_milliseconds_normalized_to_seconds():
    # Anything > 1e11 is treated as ms.
    book = parse_orderbook(_raw_book(timestamp="1745280000000"))
    assert book.timestamp == pytest.approx(1745280000.0)


def test_missing_timestamp_falls_back_to_now():
    raw = _raw_book()
    del raw["timestamp"]
    before = time.time()
    book = parse_orderbook(raw)
    after = time.time()
    assert before - 1 <= book.timestamp <= after + 1


def test_non_numeric_timestamp_falls_back_to_now():
    book = parse_orderbook(_raw_book(timestamp="not-a-number"))
    assert book.timestamp > 0


# ---------------------------------------------------------------------------
# Empty / malformed books
# ---------------------------------------------------------------------------


def test_empty_asks_returns_empty_tuple():
    book = parse_orderbook(_raw_book(asks=[], bids=[("0.48", "1")]))
    assert book.asks == tuple()
    assert book.best_ask is None


def test_empty_bids_returns_empty_tuple():
    book = parse_orderbook(_raw_book(asks=[("0.53", "1")], bids=[]))
    assert book.bids == tuple()
    assert book.best_bid is None


def test_raises_on_non_dict_input():
    with pytest.raises(ValueError):
        parse_orderbook("not a dict")  # type: ignore[arg-type]


def test_missing_asks_key_yields_empty_tuple():
    raw = _raw_book()
    del raw["asks"]
    book = parse_orderbook(raw)
    assert book.asks == tuple()


# ---------------------------------------------------------------------------
# Derived properties
# ---------------------------------------------------------------------------


def test_best_ask_and_best_bid_return_top_levels():
    raw = _raw_book(
        asks=[("0.53", "1"), ("0.54", "2")],
        bids=[("0.50", "3"), ("0.49", "4")],
    )
    book = parse_orderbook(raw)
    assert book.best_ask == OrderLevel(0.53, 1.0)
    assert book.best_bid == OrderLevel(0.50, 3.0)


def test_spread_is_best_ask_minus_best_bid():
    raw = _raw_book(asks=[("0.53", "1")], bids=[("0.50", "1")])
    book = parse_orderbook(raw)
    assert book.spread == pytest.approx(0.03)


def test_spread_is_none_when_either_side_empty():
    raw = _raw_book(asks=[], bids=[("0.50", "1")])
    book = parse_orderbook(raw)
    assert book.spread is None


def test_midpoint_averages_top_of_book():
    raw = _raw_book(asks=[("0.54", "1")], bids=[("0.50", "1")])
    book = parse_orderbook(raw)
    assert book.midpoint == pytest.approx(0.52)


def test_age_seconds_uses_injected_now():
    book = parse_orderbook(_raw_book(timestamp="1000"))
    assert book.age_seconds(now=1010) == pytest.approx(10.0)


def test_age_seconds_clamped_to_zero_for_future_timestamps():
    book = parse_orderbook(_raw_book(timestamp="2000"))
    assert book.age_seconds(now=1000) == 0.0


# ---------------------------------------------------------------------------
# Token ID resolution
# ---------------------------------------------------------------------------


def test_token_id_prefers_explicit_override():
    book = parse_orderbook(_raw_book(token_id="from-body"), token_id="from-caller")
    assert book.token_id == "from-caller"


def test_token_id_falls_back_to_asset_id_then_market():
    raw = _raw_book()
    del raw["asset_id"]
    book = parse_orderbook(raw)
    assert book.token_id == "0xmarket"
