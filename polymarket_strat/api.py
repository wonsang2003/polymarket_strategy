from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_API_BASE_URL = "https://data-api.polymarket.com"


def _normalize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}

    normalized: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        normalized[key] = value
    return normalized


def _build_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    normalized = _normalize_params(params)
    if not normalized:
        return f"{base_url}{path}"
    return f"{base_url}{path}?{urlencode(normalized, doseq=True)}"


@dataclass(slots=True)
class PolymarketPublicClient:
    timeout_seconds: int = 20
    user_agent: str = "polymarket-strat/0.1"

    def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = _build_url(base_url, path, params)
        request = Request(url, headers={"User-Agent": self.user_agent})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urlopen(request, timeout=self.timeout_seconds, context=ctx) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_markets(
        self,
        *,
        limit: int = 50,
        active: bool = True,
        closed: bool | None = None,
        order: str = "volume24hr",
        ascending: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = {"limit": limit, "active": active, "closed": closed, "order": order, "ascending": str(ascending).lower(), "offset": offset}
        return list(self._get(GAMMA_BASE_URL, "/markets", params))

    def get_market(self, market_id: str) -> dict[str, Any]:
        """Fetch a single market by numeric id OR 0x-prefixed conditionId.

        Gamma's `GET /markets/{id}` path parameter accepts only the numeric
        `id` field. Passing a conditionId (0x-prefixed 32-byte hex) returns
        404 and breaks downstream settlement. Forwards-compat: detect the
        0x prefix and route to the list endpoint with a `condition_ids`
        filter, which *does* accept the hash form and returns a 1-element
        array. Empty id → empty dict (guard against legacy NULL rows).
        """
        if not market_id:
            return {}
        ident = str(market_id).strip()
        if ident.lower().startswith("0x"):
            rows = list(
                self._get(GAMMA_BASE_URL, "/markets", {"condition_ids": ident, "limit": 1})
            )
            return dict(rows[0]) if rows else {}
        return dict(self._get(GAMMA_BASE_URL, f"/markets/{ident}"))

    def get_events(
        self,
        *,
        limit: int = 500,
        active: bool = True,
        closed: bool | None = False,
        order: str = "volume24hr",
        ascending: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Paginate Gamma `/events`. Each event groups child bracket markets,
        so event-based scanning captures every child regardless of individual
        market's 24h volume rank — avoids the pagination-depth filter that
        dropped low-volume middle brackets (e.g. Tokyo 21/22°C for April 20)
        when scanning the flat `/markets` feed."""
        params = {
            "limit": limit, "active": active, "closed": closed,
            "order": order, "ascending": str(ascending).lower(), "offset": offset,
        }
        return list(self._get(GAMMA_BASE_URL, "/events", params))

    def get_event(self, event_id: str | int) -> dict[str, Any]:
        """Fetch a single event with its full child markets array embedded."""
        return dict(self._get(GAMMA_BASE_URL, f"/events/{event_id}"))

    def get_market_holders(self, market: str, *, limit: int = 25) -> list[dict[str, Any]]:
        return list(
            self._get(
                DATA_API_BASE_URL,
                "/holders",
                {"market": market, "limit": limit},
            )
        )

    def get_trades(
        self,
        *,
        user: str | None = None,
        market: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = {"user": user, "market": market, "limit": limit, "offset": offset}
        return list(self._get(DATA_API_BASE_URL, "/trades", params))

    def get_closed_positions(self, user: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return list(
            self._get(
                DATA_API_BASE_URL,
                "/closed-positions",
                {"user": user, "limit": limit},
            )
        )

    def get_positions(self, user: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return list(
            self._get(
                DATA_API_BASE_URL,
                "/positions",
                {"user": user, "limit": limit},
            )
        )

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        return dict(self._get("https://clob.polymarket.com", "/book", {"token_id": token_id}))

    def get_price(self, token_id: str, side: str) -> dict[str, Any]:
        return dict(self._get("https://clob.polymarket.com", "/price", {"token_id": token_id, "side": side}))

    def get_spread(self, token_id: str) -> dict[str, Any]:
        return dict(self._get("https://clob.polymarket.com", "/spread", {"token_id": token_id}))
