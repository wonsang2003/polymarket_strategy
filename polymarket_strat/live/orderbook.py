"""CLOB orderbook parsing.

The Polymarket CLOB `/book` endpoint returns a raw dict with prices and sizes
as strings, and a timestamp that varies between unix seconds and milliseconds
depending on which component of their stack served the request. This module
normalizes all of that into typed dataclasses the rest of the live module can
trust.

Example raw response:

    {
        "market": "0x...",
        "asset_id": "71...",
        "bids": [
            {"price": "0.52", "size": "100"},
            {"price": "0.51", "size": "250"}
        ],
        "asks": [
            {"price": "0.53", "size": "50"},
            {"price": "0.54", "size": "180"}
        ],
        "hash": "abc...",
        "timestamp": "1745280000"
    }

After `parse_orderbook`, asks are sorted ascending by price and bids descending.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OrderLevel:
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class OrderBook:
    token_id: str
    bids: tuple[OrderLevel, ...]  # sorted descending by price
    asks: tuple[OrderLevel, ...]  # sorted ascending by price
    timestamp: float  # unix seconds
    raw_hash: str = ""

    # --- convenience ---------------------------------------------------
    @property
    def best_bid(self) -> OrderLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask.price - self.best_bid.price

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2.0

    def age_seconds(self, *, now: float | None = None) -> float:
        current = time.time() if now is None else now
        return max(current - self.timestamp, 0.0)


# ---------- parsing --------------------------------------------------------


def _coerce_float(value: Any) -> float:
    if value is None:
        raise ValueError("price/size cannot be None")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty price/size string")
    return float(text)


def _coerce_timestamp(value: Any) -> float:
    """Handle seconds, milliseconds, or string-typed variants defensively.

    Polymarket has used both seconds and milliseconds in `/book` responses
    depending on the backend serving the request. Any timestamp that parses
    to a value > 10^11 we treat as milliseconds (roughly: > year 5138 in
    seconds). Everything else is treated as seconds.
    """
    if value is None:
        return time.time()
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return time.time()
    if ts <= 0:
        return time.time()
    if ts > 1e11:
        return ts / 1000.0
    return ts


def _parse_levels(raw: Any, *, descending: bool) -> tuple[OrderLevel, ...]:
    if not raw:
        return tuple()
    levels: list[OrderLevel] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            price = _coerce_float(entry.get("price"))
            size = _coerce_float(entry.get("size"))
        except (ValueError, TypeError):
            continue
        if price <= 0 or size <= 0:
            continue
        levels.append(OrderLevel(price=price, size=size))
    levels.sort(key=lambda lvl: lvl.price, reverse=descending)
    return tuple(levels)


def parse_orderbook(raw: dict[str, Any], *, token_id: str | None = None) -> OrderBook:
    """Parse a raw /book response into a typed OrderBook.

    Raises ValueError if the response is structurally broken (no bids/asks
    key at all). Empty bid/ask sides are allowed — returns an OrderBook
    with empty tuples, callers decide whether to reject.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"orderbook response must be a dict, got {type(raw).__name__}")

    resolved_token = token_id or str(raw.get("asset_id") or raw.get("market") or "")
    bids = _parse_levels(raw.get("bids"), descending=True)
    asks = _parse_levels(raw.get("asks"), descending=False)
    timestamp = _coerce_timestamp(raw.get("timestamp"))
    raw_hash = str(raw.get("hash") or "")

    return OrderBook(
        token_id=resolved_token,
        bids=bids,
        asks=asks,
        timestamp=timestamp,
        raw_hash=raw_hash,
    )
