from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from typing import Any

from polymarket_strat.api import PolymarketPublicClient


def build_sample_whales() -> list[dict[str, object]]:
    now = datetime.now(tz=UTC)
    return [
        {
            "wallet": "0xalpha",
            "closed_positions": [
                {"realizedPnl": 380, "totalBought": 1000, "conditionId": "m1", "closedAt": (now - timedelta(days=4)).isoformat()},
                {"realizedPnl": 220, "totalBought": 800, "conditionId": "m2", "closedAt": (now - timedelta(days=9)).isoformat()},
                {"realizedPnl": -60, "totalBought": 650, "conditionId": "m3", "closedAt": (now - timedelta(days=12)).isoformat()},
                {"realizedPnl": 145, "totalBought": 500, "conditionId": "m4", "closedAt": (now - timedelta(days=20)).isoformat()},
                {"realizedPnl": 120, "totalBought": 450, "conditionId": "m5", "closedAt": (now - timedelta(days=28)).isoformat()},
                {"realizedPnl": 90, "totalBought": 400, "conditionId": "m6", "closedAt": (now - timedelta(days=35)).isoformat()},
            ],
            "trades": [
                {"conditionId": "m10", "outcome": "YES", "side": "BUY", "price": 0.57, "size": 350, "timestamp": (now - timedelta(hours=4)).isoformat(), "title": "Will candidate A win the debate?", "asset": "token-m10-yes"},
                {"conditionId": "m11", "outcome": "NO", "side": "BUY", "price": 0.42, "size": 200, "timestamp": (now - timedelta(hours=15)).isoformat(), "title": "Will rate cuts happen by July?", "asset": "token-m11-no"},
            ],
        },
        {
            "wallet": "0xbeta",
            "closed_positions": [
                {"realizedPnl": 450, "totalBought": 1200, "conditionId": "m7", "closedAt": (now - timedelta(days=2)).isoformat()},
                {"realizedPnl": 180, "totalBought": 700, "conditionId": "m8", "closedAt": (now - timedelta(days=6)).isoformat()},
                {"realizedPnl": 150, "totalBought": 500, "conditionId": "m9", "closedAt": (now - timedelta(days=11)).isoformat()},
                {"realizedPnl": -55, "totalBought": 600, "conditionId": "m3", "closedAt": (now - timedelta(days=18)).isoformat()},
                {"realizedPnl": 100, "totalBought": 350, "conditionId": "m2", "closedAt": (now - timedelta(days=25)).isoformat()},
                {"realizedPnl": 130, "totalBought": 425, "conditionId": "m1", "closedAt": (now - timedelta(days=31)).isoformat()},
            ],
            "trades": [
                {"conditionId": "m10", "outcome": "YES", "side": "BUY", "price": 0.58, "size": 275, "timestamp": (now - timedelta(hours=2)).isoformat(), "title": "Will candidate A win the debate?", "asset": "token-m10-yes"},
                {"conditionId": "m12", "outcome": "YES", "side": "BUY", "price": 0.36, "size": 150, "timestamp": (now - timedelta(hours=20)).isoformat(), "title": "Will BTC settle above 100k this month?", "asset": "token-m12-yes"},
            ],
        },
        {
            "wallet": "0xgamma",
            "closed_positions": [
                {"realizedPnl": 210, "totalBought": 900, "conditionId": "m12", "closedAt": (now - timedelta(days=3)).isoformat()},
                {"realizedPnl": 95, "totalBought": 600, "conditionId": "m13", "closedAt": (now - timedelta(days=8)).isoformat()},
                {"realizedPnl": 80, "totalBought": 500, "conditionId": "m14", "closedAt": (now - timedelta(days=16)).isoformat()},
                {"realizedPnl": -40, "totalBought": 450, "conditionId": "m15", "closedAt": (now - timedelta(days=21)).isoformat()},
                {"realizedPnl": 70, "totalBought": 350, "conditionId": "m16", "closedAt": (now - timedelta(days=27)).isoformat()},
                {"realizedPnl": 60, "totalBought": 300, "conditionId": "m17", "closedAt": (now - timedelta(days=33)).isoformat()},
            ],
            "trades": [
                {"conditionId": "m11", "outcome": "NO", "side": "BUY", "price": 0.43, "size": 125, "timestamp": (now - timedelta(hours=10)).isoformat(), "title": "Will rate cuts happen by July?", "asset": "token-m11-no"},
                {"conditionId": "m10", "outcome": "YES", "side": "SELL", "price": 0.64, "size": 100, "timestamp": (now - timedelta(hours=7)).isoformat(), "title": "Will candidate A win the debate?", "asset": "token-m10-yes"},
            ],
        },
    ]


def build_sample_mispricing_markets() -> list[dict[str, Any]]:
    return [
        {
            "market_id": "kp-1",
            "question": "Will candidate Lee win the election?",
            "category": "politics",
            "poll_probability": 0.58,
            "poll_momentum_7d": 0.10,
            "poll_momentum_14d": 0.08,
            "pollster_bias_adjustment": 0.01,
            "regional_pattern_signal": 0.05,
            "turnout_signal": 0.04,
            "naver_trend_delta": 0.25,
            "youtube_velocity": 0.22,
            "community_sentiment": 0.12,
            "twitter_volume_spike": 0.18,
            "market_probability": 0.49,
            "historical_market_probability": 0.47,
            "historical_exit_probability": 0.58,
            "liquidity_depth": 4500.0,
            "spread": 0.02,
            "top_ask_liquidity": 700.0,
            "top_bid_liquidity": 650.0,
            "best_ask_yes": 0.50,
            "best_bid_yes": 0.48,
            "best_ask_no": 0.52,
            "best_bid_no": 0.50,
            "token_yes": "kp-1-yes",
            "token_no": "kp-1-no",
        },
        {
            "market_id": "kp-2",
            "question": "Will candidate Kim win the election?",
            "category": "politics",
            "poll_probability": 0.42,
            "poll_momentum_7d": -0.08,
            "poll_momentum_14d": -0.05,
            "pollster_bias_adjustment": -0.01,
            "regional_pattern_signal": -0.03,
            "turnout_signal": -0.02,
            "naver_trend_delta": -0.18,
            "youtube_velocity": -0.15,
            "community_sentiment": -0.10,
            "twitter_volume_spike": -0.12,
            "market_probability": 0.54,
            "historical_market_probability": 0.56,
            "historical_exit_probability": 0.45,
            "liquidity_depth": 4300.0,
            "spread": 0.03,
            "top_ask_liquidity": 620.0,
            "top_bid_liquidity": 600.0,
            "best_ask_yes": 0.55,
            "best_bid_yes": 0.52,
            "best_ask_no": 0.47,
            "best_bid_no": 0.45,
            "token_yes": "kp-2-yes",
            "token_no": "kp-2-no",
        },
        {
            "market_id": "kp-3",
            "question": "Will turnout exceed 70%?",
            "category": "macro",
            "poll_probability": 0.63,
            "poll_momentum_7d": 0.03,
            "poll_momentum_14d": 0.02,
            "pollster_bias_adjustment": 0.00,
            "regional_pattern_signal": 0.01,
            "turnout_signal": 0.09,
            "naver_trend_delta": 0.08,
            "youtube_velocity": 0.05,
            "community_sentiment": 0.03,
            "twitter_volume_spike": 0.07,
            "market_probability": 0.60,
            "historical_market_probability": 0.61,
            "historical_exit_probability": 0.64,
            "liquidity_depth": 2100.0,
            "spread": 0.02,
            "top_ask_liquidity": 350.0,
            "top_bid_liquidity": 320.0,
            "best_ask_yes": 0.61,
            "best_bid_yes": 0.59,
            "best_ask_no": 0.41,
            "best_bid_no": 0.39,
            "token_yes": "kp-3-yes",
            "token_no": "kp-3-no",
        },
    ]


def build_sample_mispricing_calibration_history() -> list[dict[str, float]]:
    return [
        {"raw_probability": 0.35, "outcome": 0.0},
        {"raw_probability": 0.41, "outcome": 0.0},
        {"raw_probability": 0.46, "outcome": 0.0},
        {"raw_probability": 0.52, "outcome": 1.0},
        {"raw_probability": 0.57, "outcome": 1.0},
        {"raw_probability": 0.61, "outcome": 1.0},
        {"raw_probability": 0.66, "outcome": 1.0},
        {"raw_probability": 0.72, "outcome": 1.0},
    ]


class SamplePolymarketClient(PolymarketPublicClient):
    def __init__(self):
        super().__init__()
        self.dataset = build_sample_whales()

    def get_markets(self, **_: Any) -> list[dict[str, Any]]:
        return [{"conditionId": f"market-{index}"} for index, _ in enumerate(self.dataset, start=1)]

    def get_market_holders(self, market: str, *, limit: int = 25) -> list[dict[str, Any]]:
        return [{"user": whale["wallet"]} for whale in self.dataset[:limit]]

    def get_trades(self, *, user: str | None = None, **_: Any) -> list[dict[str, Any]]:
        for whale in self.dataset:
            if whale["wallet"] == user:
                return list(whale["trades"])
        return []

    def get_closed_positions(self, user: str, *, limit: int = 200) -> list[dict[str, Any]]:
        for whale in self.dataset:
            if whale["wallet"] == user:
                return list(whale["closed_positions"][:limit])
        return []

    def get_positions(self, user: str, *, limit: int = 200) -> list[dict[str, Any]]:
        # Sample whales have small open positions with modest unrealized PnL
        for whale in self.dataset:
            if whale["wallet"] == user:
                return [
                    {"totalBought": 200, "size": 250, "curPrice": 0.6, "conditionId": "m10", "outcome": "YES"},
                    {"totalBought": 150, "size": 100, "curPrice": 0.3, "conditionId": "m11", "outcome": "NO"},
                ]
        return []

    def get_market(self, market_id: str) -> dict[str, Any]:
        market_map = {
            "m10": {"id": "m10", "question": "Will candidate A win the debate?", "outcomes": json.dumps(["YES", "NO"]), "clobTokenIds": json.dumps(["token-m10-yes", "token-m10-no"])},
            "m11": {"id": "m11", "question": "Will rate cuts happen by July?", "outcomes": json.dumps(["YES", "NO"]), "clobTokenIds": json.dumps(["token-m11-yes", "token-m11-no"])},
            "m12": {"id": "m12", "question": "Will BTC settle above 100k this month?", "outcomes": json.dumps(["YES", "NO"]), "clobTokenIds": json.dumps(["token-m12-yes", "token-m12-no"])},
        }
        return market_map[market_id]

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        orderbooks = {
            "token-m10-yes": {"bids": [{"price": "0.56", "size": "220"}], "asks": [{"price": "0.59", "size": "340"}]},
            "token-m11-no": {"bids": [{"price": "0.41", "size": "140"}], "asks": [{"price": "0.44", "size": "180"}]},
            "token-m12-yes": {"bids": [{"price": "0.34", "size": "45"}], "asks": [{"price": "0.40", "size": "60"}]},
        }
        return orderbooks[token_id]
