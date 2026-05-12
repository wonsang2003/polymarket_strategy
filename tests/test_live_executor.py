"""LiveCoordinator — gate enforcement, shadow vs live branching, fill parsing.

These tests run without touching the real Polymarket CLOB or requiring
py-clob-client to be installed. The HTTP client and the CLOB client are both
dependency-injected, so the test can set up any orderbook shape and any
submit response and assert the coordinator behaves correctly.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from polymarket_strat.live.executor import (
    REASON_BOOK_FETCH_FAILED,
    REASON_EMERGENCY_STOP,
    REASON_HALT_FILE,
    REASON_PARSE_ERROR,
    REASON_SHADOW,
    REASON_SUBMIT_ERROR,
    REASON_SUBMIT_REJECTED,
    FillResult,
    LiveCoordinator,
    LiveExecutorConfig,
)
from polymarket_strat.live.slippage import (
    REASON_INSUFFICIENT_DEPTH,
    REASON_STALE_BOOK,
    REASON_WIDE_SPREAD,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeHttpClient:
    """Stand-in for PolymarketPublicClient with only get_orderbook exercised."""

    def __init__(self, response: dict | Exception):
        self._response = response
        self.calls: list[str] = []

    def get_orderbook(self, token_id: str) -> dict:
        self.calls.append(token_id)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _book_payload(
    *,
    asks: list[tuple[str, str]] | None = None,
    bids: list[tuple[str, str]] | None = None,
    fresh: bool = True,
    now: float | None = None,
) -> dict:
    """Build a /book JSON payload. Defaults to the wall-clock now so the gate's
    freshness check (max_book_age_s=10 by default) won't reject it."""
    if now is None:
        now = time.time()
    return {
        "asset_id": "t-1",
        "hash": "h",
        "timestamp": now if fresh else now - 120.0,
        "bids": [{"price": p, "size": s} for p, s in (bids or [])],
        "asks": [{"price": p, "size": s} for p, s in (asks or [])],
    }


def _fixed_now(stamp: float | None = None):
    if stamp is None:
        stamp = time.time()
    return lambda: stamp


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def test_emergency_stop_env_blocks_submit(monkeypatch):
    monkeypatch.setenv("EMERGENCY_STOP", "1")
    coord = LiveCoordinator(
        http_client=_FakeHttpClient(_book_payload(asks=[("0.5", "100")], bids=[("0.49", "100")])),
        config=LiveExecutorConfig(mode="shadow"),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_EMERGENCY_STOP


def test_halt_file_blocks_submit(tmp_path: Path):
    halt = tmp_path / "halt"
    halt.write_text("stop")
    coord = LiveCoordinator(
        http_client=_FakeHttpClient(_book_payload(asks=[("0.5", "100")], bids=[("0.49", "100")])),
        config=LiveExecutorConfig(mode="shadow", halt_file=halt),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_HALT_FILE


# ---------------------------------------------------------------------------
# Book fetch errors
# ---------------------------------------------------------------------------


def test_orderbook_fetch_exception_is_caught():
    coord = LiveCoordinator(
        http_client=_FakeHttpClient(RuntimeError("network down")),
        config=LiveExecutorConfig(mode="shadow"),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_BOOK_FETCH_FAILED
    assert "network down" in result.error_message


# ---------------------------------------------------------------------------
# Shadow mode — runs every gate but never submits
# ---------------------------------------------------------------------------


def test_shadow_mode_passes_gates_but_does_not_submit():
    now = time.time()
    http = _FakeHttpClient(_book_payload(
        asks=[("0.50", "1000")],
        bids=[("0.49", "1000")],
        now=now,
    ))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="shadow"),
        now_fn=_fixed_now(now),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert result.mode == "shadow"
    assert not result.filled
    assert result.reason == REASON_SHADOW
    # Diagnostics populated even on shadow:
    assert result.quoted_best_ask == pytest.approx(0.50)
    assert result.limit_price == pytest.approx(0.51)
    assert result.shares_target > 0
    # And no CLOB client was ever needed.
    assert http.calls == ["t"]


def test_shadow_reports_gate_rejections():
    http = _FakeHttpClient(_book_payload(asks=[], bids=[("0.49", "100")]))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="shadow"),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == "no_asks"


def test_shadow_reports_stale_book():
    now = time.time()
    http = _FakeHttpClient(_book_payload(
        asks=[("0.50", "100")], bids=[("0.49", "100")], fresh=False, now=now,
    ))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="shadow", max_book_age_s=10.0),
        now_fn=_fixed_now(now),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_STALE_BOOK


def test_shadow_reports_wide_spread():
    http = _FakeHttpClient(_book_payload(
        asks=[("0.60", "100")], bids=[("0.40", "100")],
    ))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="shadow", max_spread=0.03),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_WIDE_SPREAD


def test_shadow_reports_depth_shortfall():
    http = _FakeHttpClient(_book_payload(
        asks=[("0.50", "5")], bids=[("0.49", "100")],
    ))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="shadow"),
    )
    # Need ~$10 at ~0.51 → ~19.6 shares, only 5 on book.
    result = coord.execute_buy(token_id="t", notional=10.0)
    assert not result.filled
    assert result.reason == REASON_INSUFFICIENT_DEPTH


# ---------------------------------------------------------------------------
# Live mode — with injected fake CLOB client
# ---------------------------------------------------------------------------


class _FakeClobSuccess:
    """post_order returns a 'matched' response with specific fill metrics."""

    def __init__(self, making: str, taking: str, order_id: str = "0xdeadbeef"):
        self._making = making
        self._taking = taking
        self._order_id = order_id
        self.created: list[Any] = []
        self.posted: list[tuple[Any, Any]] = []

    def create_order(self, args: Any) -> Any:
        self.created.append(args)
        return {"signed": True, "args": args}

    def post_order(self, signed: Any, order_type: Any) -> dict:
        self.posted.append((signed, order_type))
        return {
            "success": True,
            "status": "matched",
            "errorMsg": "",
            "orderID": self._order_id,
            "makingAmount": self._making,
            "takingAmount": self._taking,
        }


class _FakeClobRejected:
    def create_order(self, args: Any) -> Any:
        return args

    def post_order(self, *_: Any) -> dict:
        return {
            "success": False,
            "status": "unmatched",
            "errorMsg": "price moved",
            "orderID": "",
        }


class _FakeClobRaises:
    def create_order(self, args: Any) -> Any:
        return args

    def post_order(self, *_: Any) -> dict:
        raise RuntimeError("clob offline")


class _FakeClobMalformed:
    def create_order(self, args: Any) -> Any:
        return args

    def post_order(self, *_: Any) -> dict:
        return {
            "success": True,
            "status": "matched",
            "makingAmount": "not-a-number",
            "takingAmount": "5",
            "orderID": "0x1",
        }


def test_live_mode_happy_path_records_actual_fill():
    book = _book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")])
    http = _FakeHttpClient(book)
    fake_clob = _FakeClobSuccess(making="5.00", taking="10.0")
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="live"),
        clob_client=fake_clob,
        now_fn=_fixed_now(1_000_000.0),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert result.filled
    assert result.reason == ""
    assert result.actual_vwap == pytest.approx(0.50)  # 5.00 / 10.0
    assert result.actual_shares == pytest.approx(10.0)
    assert result.actual_cost_usd == pytest.approx(5.00)
    assert result.slippage_usd == pytest.approx(0.0)
    assert result.order_id == "0xdeadbeef"
    assert len(fake_clob.posted) == 1


def test_live_mode_computes_positive_slippage_when_vwap_above_quote():
    book = _book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")])
    http = _FakeHttpClient(book)
    # Fill at 0.505 average (5.05/10) when quoted ask was 0.50 → +0.5¢ slippage.
    fake_clob = _FakeClobSuccess(making="5.05", taking="10.0")
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="live"),
        clob_client=fake_clob,
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert result.filled
    assert result.actual_vwap == pytest.approx(0.505)
    assert result.slippage_usd == pytest.approx((0.505 - 0.50) * 10.0)


def test_live_mode_records_negative_slippage_on_better_fill():
    # Price improvement — fill 0.49 average when quoted 0.50.
    book = _book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")])
    fake_clob = _FakeClobSuccess(making="4.90", taking="10.0")
    coord = LiveCoordinator(
        http_client=_FakeHttpClient(book),
        config=LiveExecutorConfig(mode="live"),
        clob_client=fake_clob,
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert result.filled
    assert result.slippage_usd < 0


def test_live_mode_clob_rejection_reported():
    http = _FakeHttpClient(_book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")]))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="live"),
        clob_client=_FakeClobRejected(),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_SUBMIT_REJECTED
    assert "price moved" in result.error_message


def test_live_mode_clob_exception_reported():
    http = _FakeHttpClient(_book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")]))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="live"),
        clob_client=_FakeClobRaises(),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_SUBMIT_ERROR
    assert "clob offline" in result.error_message


def test_live_mode_malformed_response_reported():
    http = _FakeHttpClient(_book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")]))
    coord = LiveCoordinator(
        http_client=http,
        config=LiveExecutorConfig(mode="live"),
        clob_client=_FakeClobMalformed(),
    )
    result = coord.execute_buy(token_id="t", notional=5.0)
    assert not result.filled
    assert result.reason == REASON_PARSE_ERROR


# ---------------------------------------------------------------------------
# Per-trade notional cap
# ---------------------------------------------------------------------------


def test_per_trade_cap_is_applied_before_gate_check():
    book = _book_payload(asks=[("0.50", "100")], bids=[("0.49", "100")])
    fake_clob = _FakeClobSuccess(making="10.00", taking="20.0")
    coord = LiveCoordinator(
        http_client=_FakeHttpClient(book),
        config=LiveExecutorConfig(mode="live", max_live_notional_per_trade=10.0),
        clob_client=fake_clob,
    )
    # Caller asks for $50, cap should clip to $10.
    result = coord.execute_buy(token_id="t", notional=50.0)
    assert result.filled
    assert result.notional_requested == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Configuration guards
# ---------------------------------------------------------------------------


def test_mode_must_be_shadow_or_live():
    with pytest.raises(ValueError, match="shadow"):
        LiveCoordinator(
            http_client=_FakeHttpClient({}),
            config=LiveExecutorConfig(mode="bogus"),
        )


def test_live_mode_without_account_or_fake_client_raises():
    with pytest.raises(ValueError, match="AccountConfig"):
        LiveCoordinator(
            http_client=_FakeHttpClient({}),
            config=LiveExecutorConfig(mode="live"),
        )
