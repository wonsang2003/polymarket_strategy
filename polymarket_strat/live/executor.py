"""Live + shadow executors.

Two modes, one coordinator:

* ``mode="shadow"`` — fetches the orderbook, runs the same gates as live,
  logs what *would* have been submitted, and returns a ``FillResult`` with
  ``filled=False, reason="shadow"``. Safe to run without any wallet
  credentials. Used for the 7–14 day parallel run before any USDC moves.

* ``mode="live"`` — runs every gate, then submits a FOK *limit* order (not
  a market order) at ``p_limit = best_ask + slippage_tol``. FOK semantics:
  entire order fills at or better than the limit, or the whole thing
  rejects. Post-fill, we parse the CLOB response to recover the actual
  fill price (VWAP) and log ``slippage_usd = (actual_vwap - quoted_ask) *
  shares_filled`` — that's the "slippage tax" metric we'll track against
  expected_pnl to detect edge erosion.

Kill switches
-------------
Every call to ``execute_buy`` checks, in order:

1. ``EMERGENCY_STOP=1`` environment variable
2. Existence of ``/tmp/polymarket_halt`` (or ``$POLYMARKET_HALT_FILE``)

Either blocks the submit. Works in live and shadow mode — shadow rejects
too so the log reflects what would happen in prod.

This executor intentionally does NOT share state with
``polymarket_strat.execution.LiveExecutor``. The paper pipeline on EC2
keeps using the old executor; this one runs on the Mac against its own
DB (``data/weather/live.db``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.config import AccountConfig
from polymarket_strat.live.orderbook import OrderBook, parse_orderbook
from polymarket_strat.live.slippage import (
    GateResult,
    REASON_INVALID_INPUT,
    check_entry_gates,
)


# Reason codes unique to the executor (gate reasons live in slippage.py).
REASON_EMERGENCY_STOP = "emergency_stop"
REASON_HALT_FILE = "halt_file_present"
REASON_BOOK_FETCH_FAILED = "book_fetch_failed"
REASON_SHADOW = "shadow"
REASON_SUBMIT_REJECTED = "clob_rejected"
REASON_SUBMIT_ERROR = "clob_error"
REASON_PARSE_ERROR = "response_parse_error"


@dataclass(frozen=True, slots=True)
class FillResult:
    # --- context ---
    mode: str
    token_id: str
    notional_requested: float

    # --- outcome ---
    filled: bool
    reason: str  # "" on happy live fills; see REASON_* for others + slippage reasons
    fill_ts: float

    # --- pre-submit snapshot (populated when book fetched) ---
    quoted_best_ask: float = 0.0
    quoted_best_bid: float = 0.0
    quoted_spread: float = 0.0
    book_age_s: float = 0.0

    # --- planned submit (populated when gates pass) ---
    limit_price: float = 0.0
    shares_target: float = 0.0
    expected_vwap: float = 0.0
    expected_cost_usd: float = 0.0
    expected_slippage_per_share: float = 0.0
    depth_usd_within_limit: float = 0.0
    depth_shares_within_limit: float = 0.0

    # --- actual fill (live only; zeroed otherwise) ---
    order_id: str = ""
    actual_vwap: float = 0.0
    actual_cost_usd: float = 0.0
    actual_shares: float = 0.0
    slippage_usd: float = 0.0

    # --- raw bookkeeping ---
    submit_response: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""

    def to_persist_row(self) -> dict[str, Any]:
        """Flat dict for persistence.save_execution_attempt()."""
        return {
            "mode": self.mode,
            "token_id": self.token_id,
            "notional_requested": self.notional_requested,
            "filled": 1 if self.filled else 0,
            "reason": self.reason,
            "fill_ts": self.fill_ts,
            "quoted_best_ask": self.quoted_best_ask,
            "quoted_best_bid": self.quoted_best_bid,
            "quoted_spread": self.quoted_spread,
            "book_age_s": self.book_age_s,
            "limit_price": self.limit_price,
            "shares_target": self.shares_target,
            "expected_vwap": self.expected_vwap,
            "expected_cost_usd": self.expected_cost_usd,
            "expected_slippage_per_share": self.expected_slippage_per_share,
            "depth_usd_within_limit": self.depth_usd_within_limit,
            "depth_shares_within_limit": self.depth_shares_within_limit,
            "order_id": self.order_id,
            "actual_vwap": self.actual_vwap,
            "actual_cost_usd": self.actual_cost_usd,
            "actual_shares": self.actual_shares,
            "slippage_usd": self.slippage_usd,
            "error_message": self.error_message,
        }


@dataclass(slots=True)
class LiveExecutorConfig:
    mode: str = "shadow"  # "shadow" or "live"
    slippage_tol: float = 0.01
    max_spread: float = 0.03
    min_entry_price: float = 0.02
    max_entry_price: float = 0.85
    max_book_age_s: float = 10.0
    halt_file: Path = Path("/tmp/polymarket_halt")
    emergency_stop_env: str = "EMERGENCY_STOP"
    max_live_notional_per_trade: float = 50.0  # hard ceiling regardless of upstream sizing


class LiveCoordinator:
    """The only class callers should instantiate.

    Usage (shadow)::

        coord = LiveCoordinator(
            http_client=PolymarketPublicClient(),
            config=LiveExecutorConfig(mode="shadow"),
        )
        result = coord.execute_buy(token_id=..., notional=10.0)

    Usage (live)::

        coord = LiveCoordinator(
            http_client=PolymarketPublicClient(),
            account=AccountConfig.from_env(),
            config=LiveExecutorConfig(
                mode="live",
                slippage_tol=0.01,
                max_live_notional_per_trade=10.0,
            ),
        )
        result = coord.execute_buy(token_id=..., notional=10.0)
    """

    def __init__(
        self,
        *,
        http_client: PolymarketPublicClient,
        config: LiveExecutorConfig | None = None,
        account: AccountConfig | None = None,
        clob_client: Any = None,  # for tests: inject a fake
        now_fn: Callable[[], float] = time.time,
    ):
        self.config = config or LiveExecutorConfig()
        self.http = http_client
        self._now = now_fn

        if self.config.mode not in ("shadow", "live"):
            raise ValueError(f"mode must be 'shadow' or 'live', got {self.config.mode!r}")

        self._clob = None
        self._order_args_cls = None
        self._order_type_cls = None
        self._buy_side = None

        if self.config.mode == "live":
            if clob_client is not None:
                # Test injection path.
                self._clob = clob_client
                self._order_args_cls = _FakeOrderArgs
                self._order_type_cls = _FakeOrderType
                self._buy_side = "BUY"
            else:
                if account is None:
                    raise ValueError("mode='live' requires an AccountConfig")
                self._init_real_clob(account)

    # ------------------------------------------------------------------
    # CLOB init — isolated so tests don't need py_clob_client installed.
    # ------------------------------------------------------------------
    def _init_real_clob(self, account: AccountConfig) -> None:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
        except ImportError as exc:
            raise RuntimeError(
                "Live trading requires `py-clob-client`. `pip install py-clob-client`."
            ) from exc

        self._order_args_cls = OrderArgs
        self._order_type_cls = OrderType
        self._buy_side = BUY
        self._clob = ClobClient(
            account.host,
            key=account.private_key,
            chain_id=account.chain_id,
            signature_type=account.signature_type,
            funder=account.funder,
        )
        self._clob.set_api_creds(self._clob.create_or_derive_api_creds())

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------
    def execute_buy(self, *, token_id: str, notional: float) -> FillResult:
        """Run gates and (if live) submit. Returns FillResult either way."""
        # 1. Kill-switch checks ------------------------------------------
        if os.getenv(self.config.emergency_stop_env, "").strip() == "1":
            return self._empty(token_id, notional, REASON_EMERGENCY_STOP)
        if self.config.halt_file.exists():
            return self._empty(token_id, notional, REASON_HALT_FILE)

        # 2. Per-trade notional hard cap ---------------------------------
        if notional <= 0:
            return self._empty(token_id, notional, REASON_INVALID_INPUT)
        capped_notional = min(notional, self.config.max_live_notional_per_trade)

        # 3. Fetch orderbook ---------------------------------------------
        try:
            raw = self.http.get_orderbook(token_id)
            book = parse_orderbook(raw, token_id=token_id)
        except Exception as exc:  # noqa: BLE001 — any fetch failure is fatal for this signal
            return FillResult(
                mode=self.config.mode,
                token_id=token_id,
                notional_requested=capped_notional,
                filled=False,
                reason=REASON_BOOK_FETCH_FAILED,
                fill_ts=self._now(),
                error_message=str(exc),
            )

        # 4. Gate evaluation --------------------------------------------
        gate = check_entry_gates(
            book=book,
            notional_target=capped_notional,
            slippage_tol=self.config.slippage_tol,
            max_spread=self.config.max_spread,
            min_entry_price=self.config.min_entry_price,
            max_entry_price=self.config.max_entry_price,
            max_book_age_s=self.config.max_book_age_s,
            now_seconds=self._now(),
        )

        if not gate.passed:
            return _result_from_gate(
                mode=self.config.mode,
                token_id=token_id,
                notional=capped_notional,
                gate=gate,
                fill_ts=self._now(),
                filled=False,
                reason=gate.reason,
            )

        # 5. Shadow path — short-circuit before submit -------------------
        if self.config.mode == "shadow":
            return _result_from_gate(
                mode="shadow",
                token_id=token_id,
                notional=capped_notional,
                gate=gate,
                fill_ts=self._now(),
                filled=False,
                reason=REASON_SHADOW,
            )

        # 6. Live submit -------------------------------------------------
        return self._submit_fok(
            token_id=token_id,
            notional=capped_notional,
            gate=gate,
        )

    # ------------------------------------------------------------------
    # Live submit — isolated so it's the only thing that actually moves USDC.
    # ------------------------------------------------------------------
    def _submit_fok(
        self,
        *,
        token_id: str,
        notional: float,
        gate: GateResult,
    ) -> FillResult:
        assert self._clob is not None
        assert self._order_args_cls is not None
        assert self._order_type_cls is not None

        try:
            order_args = self._order_args_cls(
                token_id=token_id,
                price=gate.limit_price,
                size=gate.shares_target,
                side=self._buy_side,
            )
            signed = self._clob.create_order(order_args)
            response = self._clob.post_order(signed, self._order_type_cls.FOK)
        except Exception as exc:  # noqa: BLE001
            return _result_from_gate(
                mode="live",
                token_id=token_id,
                notional=notional,
                gate=gate,
                fill_ts=self._now(),
                filled=False,
                reason=REASON_SUBMIT_ERROR,
                error_message=str(exc),
            )

        return _parse_fill_response(
            response=response,
            token_id=token_id,
            notional=notional,
            gate=gate,
            fill_ts=self._now(),
        )

    # ------------------------------------------------------------------
    def _empty(self, token_id: str, notional: float, reason: str) -> FillResult:
        return FillResult(
            mode=self.config.mode,
            token_id=token_id,
            notional_requested=notional,
            filled=False,
            reason=reason,
            fill_ts=self._now(),
        )


# ----------------------------------------------------------------------
# Helpers — pure, testable.
# ----------------------------------------------------------------------


def _result_from_gate(
    *,
    mode: str,
    token_id: str,
    notional: float,
    gate: GateResult,
    fill_ts: float,
    filled: bool,
    reason: str,
    error_message: str = "",
) -> FillResult:
    return FillResult(
        mode=mode,
        token_id=token_id,
        notional_requested=notional,
        filled=filled,
        reason=reason,
        fill_ts=fill_ts,
        quoted_best_ask=gate.best_ask,
        quoted_best_bid=gate.best_bid,
        quoted_spread=gate.spread,
        book_age_s=gate.book_age_s,
        limit_price=gate.limit_price,
        shares_target=gate.shares_target,
        expected_vwap=gate.expected_vwap,
        expected_cost_usd=gate.expected_cost_usd,
        expected_slippage_per_share=gate.expected_slippage_per_share,
        depth_usd_within_limit=gate.depth_usd_within_limit,
        depth_shares_within_limit=gate.depth_shares_within_limit,
        error_message=error_message,
    )


def _parse_fill_response(
    *,
    response: Any,
    token_id: str,
    notional: float,
    gate: GateResult,
    fill_ts: float,
) -> FillResult:
    """Extract actual fill metrics from a CLOB post_order response.

    Polymarket's /order endpoint returns (shape may evolve; be defensive)::

        {
            "success": true,
            "errorMsg": "",
            "orderID": "0x...",
            "transactionsHashes": ["0x..."],
            "status": "matched",        # or "live" / "cancelled" / "unmatched"
            "makingAmount": "5.23",     # USDC spent
            "takingAmount": "10.46",    # shares received
        }
    """
    resp_dict = response if isinstance(response, dict) else {}

    success = bool(resp_dict.get("success", False))
    status = str(resp_dict.get("status") or "").lower()
    error_msg = str(resp_dict.get("errorMsg") or "")
    order_id = str(resp_dict.get("orderID") or resp_dict.get("order_id") or "")

    # FOK must have status "matched" to count as filled. Anything else is a reject.
    if not success or status not in {"matched", "filled"}:
        return _build_fill_result(
            token_id=token_id,
            notional=notional,
            gate=gate,
            fill_ts=fill_ts,
            filled=False,
            reason=REASON_SUBMIT_REJECTED,
            order_id=order_id,
            submit_response=resp_dict,
            error_message=error_msg or f"status={status or 'unknown'}",
        )

    # Success path — extract fill metrics.
    try:
        making = float(resp_dict.get("makingAmount") or 0.0)
        taking = float(resp_dict.get("takingAmount") or 0.0)
    except (TypeError, ValueError):
        return _build_fill_result(
            token_id=token_id,
            notional=notional,
            gate=gate,
            fill_ts=fill_ts,
            filled=False,
            reason=REASON_PARSE_ERROR,
            order_id=order_id,
            submit_response=resp_dict,
            error_message="non-numeric makingAmount/takingAmount",
        )

    if taking <= 0:
        # Response claims matched but reports 0 shares. Treat as reject.
        return _build_fill_result(
            token_id=token_id,
            notional=notional,
            gate=gate,
            fill_ts=fill_ts,
            filled=False,
            reason=REASON_SUBMIT_REJECTED,
            order_id=order_id,
            submit_response=resp_dict,
            error_message="takingAmount=0 despite success/matched",
        )

    actual_vwap = making / taking
    slippage_per_share = actual_vwap - gate.best_ask
    slippage_usd = slippage_per_share * taking

    return _build_fill_result(
        token_id=token_id,
        notional=notional,
        gate=gate,
        fill_ts=fill_ts,
        filled=True,
        reason="",
        order_id=order_id,
        submit_response=resp_dict,
        actual_vwap=actual_vwap,
        actual_cost_usd=making,
        actual_shares=taking,
        slippage_usd=slippage_usd,
    )


def _build_fill_result(
    *,
    token_id: str,
    notional: float,
    gate: GateResult,
    fill_ts: float,
    filled: bool,
    reason: str,
    order_id: str = "",
    submit_response: dict[str, Any] | None = None,
    error_message: str = "",
    actual_vwap: float = 0.0,
    actual_cost_usd: float = 0.0,
    actual_shares: float = 0.0,
    slippage_usd: float = 0.0,
) -> FillResult:
    """Single constructor used by every live branch (rejected + filled)."""
    return FillResult(
        mode="live",
        token_id=token_id,
        notional_requested=notional,
        filled=filled,
        reason=reason,
        fill_ts=fill_ts,
        quoted_best_ask=gate.best_ask,
        quoted_best_bid=gate.best_bid,
        quoted_spread=gate.spread,
        book_age_s=gate.book_age_s,
        limit_price=gate.limit_price,
        shares_target=gate.shares_target,
        expected_vwap=gate.expected_vwap,
        expected_cost_usd=gate.expected_cost_usd,
        expected_slippage_per_share=gate.expected_slippage_per_share,
        depth_usd_within_limit=gate.depth_usd_within_limit,
        depth_shares_within_limit=gate.depth_shares_within_limit,
        order_id=order_id,
        actual_vwap=actual_vwap,
        actual_cost_usd=actual_cost_usd,
        actual_shares=actual_shares,
        slippage_usd=slippage_usd,
        submit_response=submit_response or {},
        error_message=error_message,
    )


# ----------------------------------------------------------------------
# Test-injection fakes so we don't require py_clob_client for unit tests.
# ----------------------------------------------------------------------


class _FakeOrderArgs:
    def __init__(self, *, token_id: str, price: float, size: float, side: str):
        self.token_id = token_id
        self.price = price
        self.size = size
        self.side = side


class _FakeOrderType:
    FOK = "FOK"
    GTC = "GTC"
    GTD = "GTD"
