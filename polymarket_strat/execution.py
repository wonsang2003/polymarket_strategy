from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from polymarket_strat.config import AccountConfig


@dataclass(slots=True)
class ExecutedOrder:
    market: str
    outcome: str
    token_id: str
    side: str
    amount: float
    mode: str
    status: str
    metadata: dict[str, Any]


class PaperExecutor:
    def execute_market_buy(self, *, market: str, outcome: str, token_id: str, amount: float, reference_price: float) -> ExecutedOrder:
        return ExecutedOrder(
            market=market,
            outcome=outcome,
            token_id=token_id,
            side="BUY",
            amount=amount,
            mode="paper",
            status="simulated",
            metadata={"reference_price": reference_price},
        )


class LiveExecutor:
    def __init__(self, account: AccountConfig):
        self.account = account
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
        except ImportError as exc:
            raise RuntimeError(
                "Live trading requires the official `py-clob-client` package. Install it before using --mode live."
            ) from exc

        self._market_order_args = MarketOrderArgs
        self._order_type = OrderType
        self._buy = BUY
        self._client = ClobClient(
            self.account.host,
            key=self.account.private_key,
            chain_id=self.account.chain_id,
            signature_type=self.account.signature_type,
            funder=self.account.funder,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

    def execute_market_buy(self, *, market: str, outcome: str, token_id: str, amount: float, reference_price: float) -> ExecutedOrder:
        order_args = self._market_order_args(
            token_id=token_id,
            amount=amount,
            side=self._buy,
            order_type=self._order_type.FOK,
        )
        signed_order = self._client.create_market_order(order_args)
        response = self._client.post_order(signed_order, self._order_type.FOK)
        return ExecutedOrder(
            market=market,
            outcome=outcome,
            token_id=token_id,
            side="BUY",
            amount=amount,
            mode="live",
            status="submitted",
            metadata={"reference_price": reference_price, "exchange_response": response},
        )


def order_to_dict(order: ExecutedOrder) -> dict[str, Any]:
    return asdict(order)
