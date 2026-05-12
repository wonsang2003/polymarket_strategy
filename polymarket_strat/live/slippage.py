"""Slippage + depth gates for live execution.

Pure functions, no I/O. The executor fetches the orderbook (once) and hands
the snapshot to `check_entry_gates`, which decides whether to submit.

Conservative sizing contract
----------------------------
We submit a FOK limit at ``p_limit = best_ask + slippage_tol``. FOK fills
entirely at or better than the limit, or rejects. To guarantee the trade
never overspends its budget, we size shares at the limit price:

    shares_target = notional_target / p_limit

Worst case: book is so thin that the entire fill lands at ``p_limit``.
Cost in that case = ``shares_target * p_limit = notional_target`` exactly.

Best case: fill averages lower than the limit → same share count, lower cost,
leftover budget stays unspent. Over time that manifests as ``slippage_usd <
0`` in the live.db log, which is a *positive* signal (we got better fills
than we modeled).

If sizes on the top level cover the trade, VWAP = best_ask and slippage = 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymarket_strat.live.orderbook import OrderBook


_SHARE_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class VWAPResult:
    vwap: float
    cost_usd: float
    shares_filled: float
    levels_consumed: int


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reason: str  # "" when passed, else one of the codes below

    # Always populated so the executor can log rich diagnostics even on reject.
    limit_price: float
    shares_target: float
    expected_vwap: float
    expected_cost_usd: float
    expected_slippage_per_share: float  # vwap - best_ask (>=0 by construction)
    best_ask: float
    best_bid: float
    spread: float
    book_age_s: float
    depth_usd_within_limit: float
    depth_shares_within_limit: float


# Reason codes — mirror the gate_rejects histogram in strategy.analyze().
REASON_OK = ""
REASON_NO_ASKS = "no_asks"
REASON_NO_BIDS = "no_bids"
REASON_STALE_BOOK = "stale_book"
REASON_WIDE_SPREAD = "wide_spread"
REASON_PRICE_OUT_OF_BAND = "price_out_of_band"
REASON_INSUFFICIENT_DEPTH = "insufficient_depth"
REASON_INVALID_INPUT = "invalid_input"


def compute_vwap(book: OrderBook, target_shares: float) -> VWAPResult | None:
    """Walk the ask book to fill target_shares. Returns None on depth shortfall.

    target_shares must be > 0. Returns ``None`` if the combined ask sizes
    don't reach target_shares. On success, ``vwap`` is the volume-weighted
    average price across the consumed levels.
    """
    if target_shares <= 0 or not book.asks:
        return None

    shares_filled = 0.0
    cost_usd = 0.0
    levels_consumed = 0
    for level in book.asks:
        if shares_filled + _SHARE_EPS >= target_shares:
            break
        take = min(level.size, target_shares - shares_filled)
        cost_usd += take * level.price
        shares_filled += take
        levels_consumed += 1

    if shares_filled + _SHARE_EPS < target_shares:
        return None  # not enough depth

    # Guard against tiny floating drift rounding shares_filled slightly above target.
    shares_filled = min(shares_filled, target_shares)
    vwap = cost_usd / target_shares if target_shares > 0 else 0.0
    return VWAPResult(
        vwap=vwap,
        cost_usd=cost_usd,
        shares_filled=shares_filled,
        levels_consumed=levels_consumed,
    )


def depth_within_tolerance(book: OrderBook, limit_price: float) -> tuple[float, float]:
    """Sum ask-side liquidity available at or below ``limit_price``.

    Returns ``(usd_available, shares_available)``. Returns ``(0, 0)`` if no
    asks qualify (e.g., limit below top-of-book).
    """
    usd = 0.0
    shares = 0.0
    for level in book.asks:
        if level.price > limit_price + 1e-9:
            break
        usd += level.price * level.size
        shares += level.size
    return usd, shares


def check_entry_gates(
    *,
    book: OrderBook,
    notional_target: float,
    slippage_tol: float = 0.01,
    max_spread: float = 0.03,
    min_entry_price: float = 0.02,
    max_entry_price: float = 0.85,
    max_book_age_s: float = 10.0,
    now_seconds: float | None = None,
) -> GateResult:
    """Run every pre-submit check on the orderbook snapshot.

    Returns a GateResult with ``passed=True`` iff the book is fresh, the
    spread is acceptable, the best ask is in the tradeable price band, and
    there is enough depth at ``best_ask + slippage_tol`` to fill the full
    ``notional_target`` at a computable VWAP.

    Arguments
    ---------
    notional_target : USD budget the executor wants to spend. Share sizing
        is done conservatively at the limit price so the actual fill cost
        is ≤ this value in the worst case.
    slippage_tol : max cents worse than bestAsk we're willing to accept.
        Default 0.01 = 1¢.
    max_spread : abort if best_ask - best_bid exceeds this. Default 0.03.
    min_entry_price / max_entry_price : price band. Default [0.02, 0.85]
        matches TradingConstraints.
    max_book_age_s : orderbook snapshot freshness. 10s default keeps us
        honest — anything older and the book is stale.
    """

    # --- 1. Basic validity --------------------------------------------------
    if notional_target <= 0:
        return _reject(book, REASON_INVALID_INPUT)

    best_ask_level = book.best_ask
    if best_ask_level is None:
        return _reject(book, REASON_NO_ASKS)

    best_bid_level = book.best_bid
    if best_bid_level is None:
        return _reject(book, REASON_NO_BIDS)

    best_ask = best_ask_level.price
    best_bid = best_bid_level.price
    spread = best_ask - best_bid

    # --- 2. Freshness -------------------------------------------------------
    age = book.age_seconds(now=now_seconds)
    if age > max_book_age_s:
        return _reject(book, REASON_STALE_BOOK, best_ask=best_ask, best_bid=best_bid)

    # --- 3. Spread ----------------------------------------------------------
    if spread > max_spread + 1e-9:
        return _reject(book, REASON_WIDE_SPREAD, best_ask=best_ask, best_bid=best_bid)

    # --- 4. Price band ------------------------------------------------------
    if best_ask < min_entry_price or best_ask > max_entry_price:
        return _reject(book, REASON_PRICE_OUT_OF_BAND, best_ask=best_ask, best_bid=best_bid)

    # --- 5. Limit price + conservative share sizing -------------------------
    limit_price = best_ask + slippage_tol
    if limit_price <= 0:
        return _reject(book, REASON_INVALID_INPUT, best_ask=best_ask, best_bid=best_bid)

    shares_target = notional_target / limit_price
    depth_usd, depth_shares = depth_within_tolerance(book, limit_price)

    # --- 6. VWAP + depth gate ----------------------------------------------
    vwap_result = compute_vwap(book, shares_target)
    if vwap_result is None:
        return GateResult(
            passed=False,
            reason=REASON_INSUFFICIENT_DEPTH,
            limit_price=limit_price,
            shares_target=shares_target,
            expected_vwap=0.0,
            expected_cost_usd=0.0,
            expected_slippage_per_share=0.0,
            best_ask=best_ask,
            best_bid=best_bid,
            spread=spread,
            book_age_s=age,
            depth_usd_within_limit=depth_usd,
            depth_shares_within_limit=depth_shares,
        )

    return GateResult(
        passed=True,
        reason=REASON_OK,
        limit_price=limit_price,
        shares_target=shares_target,
        expected_vwap=vwap_result.vwap,
        expected_cost_usd=vwap_result.cost_usd,
        expected_slippage_per_share=max(vwap_result.vwap - best_ask, 0.0),
        best_ask=best_ask,
        best_bid=best_bid,
        spread=spread,
        book_age_s=age,
        depth_usd_within_limit=depth_usd,
        depth_shares_within_limit=depth_shares,
    )


def _reject(
    book: OrderBook,
    reason: str,
    *,
    best_ask: float = 0.0,
    best_bid: float = 0.0,
) -> GateResult:
    """Build a failed GateResult with whatever metadata we have."""
    if best_ask == 0.0 and book.best_ask is not None:
        best_ask = book.best_ask.price
    if best_bid == 0.0 and book.best_bid is not None:
        best_bid = book.best_bid.price
    spread = best_ask - best_bid if best_ask and best_bid else 0.0
    return GateResult(
        passed=False,
        reason=reason,
        limit_price=0.0,
        shares_target=0.0,
        expected_vwap=0.0,
        expected_cost_usd=0.0,
        expected_slippage_per_share=0.0,
        best_ask=best_ask,
        best_bid=best_bid,
        spread=spread,
        book_age_s=book.age_seconds(),
        depth_usd_within_limit=0.0,
        depth_shares_within_limit=0.0,
    )
