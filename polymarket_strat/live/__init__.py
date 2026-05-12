"""Isolated live-money execution module.

Completely decoupled from the paper pipeline that runs on EC2 cron.
Has its own database file (data/weather/live.db), its own CLI entrypoint
(`python -m polymarket_strat.live.runner`), and its own lock file
(/tmp/polymarket_live.lock) so the two paths can run simultaneously
on different machines without stepping on each other.

Nothing in `polymarket_strat.main` imports from this package.
`polymarket_strat.live.runner` imports *read-only* from
`polymarket_strat.domain.weather.strategy` (for signal generation),
`polymarket_strat.api` (for Gamma + CLOB HTTP), and
`polymarket_strat.config` (for TradingConstraints / AccountConfig).
"""

from polymarket_strat.live.orderbook import OrderBook, OrderLevel, parse_orderbook
from polymarket_strat.live.slippage import (
    GateResult,
    check_entry_gates,
    compute_vwap,
    depth_within_tolerance,
)

__all__ = [
    "OrderBook",
    "OrderLevel",
    "parse_orderbook",
    "GateResult",
    "check_entry_gates",
    "compute_vwap",
    "depth_within_tolerance",
]
