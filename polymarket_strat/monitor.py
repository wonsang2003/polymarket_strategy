from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path
from typing import Any

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.config import TelegramConfig, load_env_file
from polymarket_strat.domain.strategies.insider_detection import (
    InsiderDetectionConfig,
    InsiderDetector,
    aggregate_signals,
)
from polymarket_strat.domain.strategies.whale_following import (
    WhaleFollowingStrategy,
    WhaleSelectionConfig,
    WhaleScore,
    _float,
    _parse_timestamp,
    _side_is_buy,
)
from polymarket_strat.notifications.telegram import TelegramNotifier


STATE_DIR = Path("runtime")
STATE_FILE = STATE_DIR / "whale_monitor_state.json"


def _load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str))


class WhaleMonitor:
    def __init__(
        self,
        client: PolymarketPublicClient,
        notifier: TelegramNotifier,
        *,
        min_trade_size: float = 1000.0,
        lookback_minutes: int = 75,
        state_path: Path = STATE_FILE,
        whale_refresh_hours: int = 24,
    ):
        self.client = client
        self.notifier = notifier
        self.min_trade_size = min_trade_size
        self.lookback_minutes = lookback_minutes
        self.state_path = state_path
        self.whale_refresh_hours = whale_refresh_hours

    def poll(self) -> dict[str, Any]:
        state = _load_state(self.state_path)
        now = datetime.now(tz=UTC)

        # Resolve ELITE whale list — cached in state, refreshed periodically
        elite_wallets, whale_profiles = self._resolve_elite_whales(state, now)
        if not elite_wallets:
            print("[monitor] No ELITE whales found. Nothing to monitor.", file=sys.stderr)
            return {"alerts_sent": 0, "reason": "no_elite_whales"}

        profile_lookup = {p["wallet"]: p for p in whale_profiles}
        since = now - timedelta(minutes=self.lookback_minutes)
        last_poll_iso = state.get("last_poll_time")
        if last_poll_iso:
            last_poll = datetime.fromisoformat(last_poll_iso)
            # Use whichever is more recent: last_poll or lookback window
            since = max(since, last_poll)

        # Poll trades for each ELITE whale
        print(f"[monitor] Polling {len(elite_wallets)} ELITE whales for trades since {since.isoformat()}...", file=sys.stderr)
        alerts: list[dict[str, Any]] = []
        seen_trade_ids = set(state.get("seen_trade_ids", []))

        # Aggregate trades by (market, outcome)
        market_buckets: dict[tuple[str, str], dict[str, Any]] = {}

        for wallet in elite_wallets:
            trades = self.client.get_trades(user=wallet, limit=50)
            for trade in trades:
                trade_time = _parse_timestamp(trade.get("timestamp") or trade.get("createdAt"))
                if trade_time is None or trade_time < since:
                    continue
                if not _side_is_buy(trade):
                    continue

                # Dedup by a composite key
                trade_key = f"{wallet}:{trade.get('conditionId','')}:{trade.get('timestamp','')}"
                if trade_key in seen_trade_ids:
                    continue
                seen_trade_ids.add(trade_key)

                market_id = str(trade.get("conditionId") or "")
                outcome = str(trade.get("outcome") or "")
                title = str(trade.get("title") or trade.get("question") or "")
                price = _float(trade.get("price"), 0.5)
                size = _float(trade.get("size") or trade.get("amount"), 0.0)
                notional = price * size

                if not market_id or not outcome:
                    continue

                key = (market_id, outcome)
                if key not in market_buckets:
                    market_buckets[key] = {
                        "title": title,
                        "outcome": outcome,
                        "market_id": market_id,
                        "total_notional": 0.0,
                        "total_size": 0.0,
                        "weighted_price": 0.0,
                        "weight": 0.0,
                        "wallets": set(),
                    }
                bucket = market_buckets[key]
                bucket["total_notional"] += notional
                bucket["total_size"] += size
                bucket["weighted_price"] += price * size
                bucket["weight"] += size
                bucket["wallets"].add(wallet)
                if title and not bucket["title"]:
                    bucket["title"] = title

        # Fire alerts for buckets exceeding min_trade_size
        for (market_id, outcome), bucket in market_buckets.items():
            if bucket["total_notional"] < self.min_trade_size:
                continue
            avg_price = bucket["weighted_price"] / bucket["weight"] if bucket["weight"] > 0 else 0.5
            whale_wallets = sorted(bucket["wallets"])
            whale_summaries = [profile_lookup[w] for w in whale_wallets if w in profile_lookup]

            alert = {
                "title": bucket["title"],
                "outcome": outcome,
                "market_id": market_id,
                "total_notional": round(bucket["total_notional"], 2),
                "total_size": round(bucket["total_size"], 2),
                "avg_price": round(avg_price, 4),
                "whale_count": len(whale_wallets),
                "whale_wallets": whale_wallets,
                "timestamp": now.isoformat(),
            }
            alerts.append(alert)

            try:
                self.notifier.send_whale_alert(
                    title=bucket["title"],
                    outcome=outcome,
                    side="BUY",
                    total_size=bucket["total_notional"],
                    avg_price=avg_price,
                    whale_count=len(whale_wallets),
                    signal_score=bucket["total_notional"] / 100.0,
                    whale_summaries=whale_summaries,
                    market_id=market_id,
                )
            except Exception as exc:
                print(f"[monitor] Failed to send Telegram alert: {exc}", file=sys.stderr)

        # Trim seen_trade_ids to prevent unbounded growth (keep last 5000)
        if len(seen_trade_ids) > 5000:
            seen_trade_ids = set(list(seen_trade_ids)[-3000:])

        # Save state
        state["last_poll_time"] = now.isoformat()
        state["seen_trade_ids"] = list(seen_trade_ids)
        _save_state(state, self.state_path)

        print(f"[monitor] Done. {len(alerts)} alerts sent.", file=sys.stderr)
        return {"alerts_sent": len(alerts), "alerts": alerts}

    def _resolve_elite_whales(
        self, state: dict[str, Any], now: datetime
    ) -> tuple[list[str], list[dict[str, Any]]]:
        cached_wallets = state.get("elite_wallets", [])
        cached_profiles = state.get("elite_profiles", [])
        last_refresh = state.get("elite_refresh_time")

        # Use cache if fresh enough
        if cached_wallets and last_refresh:
            refresh_time = datetime.fromisoformat(last_refresh)
            age_hours = (now - refresh_time).total_seconds() / 3600
            if age_hours < self.whale_refresh_hours:
                print(
                    f"[monitor] Using cached ELITE whale list ({len(cached_wallets)} whales, "
                    f"refreshed {age_hours:.1f}h ago).",
                    file=sys.stderr,
                )
                return cached_wallets, cached_profiles

        # Full discovery + longevity validation
        print("[monitor] Refreshing ELITE whale list...", file=sys.stderr)
        cfg = WhaleSelectionConfig(market_limit=30, holders_per_market=20, max_candidates=60)
        strategy = WhaleFollowingStrategy(self.client, selection=cfg)
        candidates = strategy.discover_candidate_wallets()
        whales = strategy.rank_whales(candidates)
        elite_whales, all_profiles = strategy.validate_whale_longevity(whales, min_tier="ELITE")

        elite_wallets = [w.wallet for w in elite_whales]
        elite_profiles = [p for p in all_profiles if p["wallet"] in set(elite_wallets)]

        # Cache in state
        state["elite_wallets"] = elite_wallets
        state["elite_profiles"] = elite_profiles
        state["elite_refresh_time"] = now.isoformat()
        _save_state(state, self.state_path)

        return elite_wallets, elite_profiles


INSIDER_STATE_FILE = STATE_DIR / "insider_monitor_state.json"


class InsiderMonitor:
    """Poll political/economic Polymarket markets for insider-anomaly signals."""

    def __init__(
        self,
        client: PolymarketPublicClient,
        notifier: TelegramNotifier,
        *,
        min_score: float = 0.15,
        config: InsiderDetectionConfig | None = None,
        state_path: Path = INSIDER_STATE_FILE,
    ):
        self.client = client
        self.notifier = notifier
        self.min_score = min_score
        self.detector = InsiderDetector(client, config)
        self.state_path = state_path

    def poll(self) -> dict[str, Any]:
        state = _load_state(self.state_path)
        now = datetime.now(tz=UTC)
        seen_ids: set[str] = set(state.get("seen_signal_ids", []))
        wallet_cache: dict[str, bool] = state.get("wallet_cache", {})

        print("[insider] Scanning markets for anomalies...", file=sys.stderr)
        signals = self.detector.scan(wallet_cache=wallet_cache)
        aggregated = aggregate_signals(signals)

        alerts_sent = 0
        for bucket in aggregated:
            if bucket["combined_score"] < self.min_score:
                continue
            # Dedup by market + outcome (one alert per poll per market/outcome)
            sig_id = f"{bucket['market_id']}:{bucket['outcome']}"
            if sig_id in seen_ids:
                continue
            seen_ids.add(sig_id)

            try:
                self.notifier.send_insider_alert(aggregated=bucket)
                alerts_sent += 1
            except Exception as exc:
                print(f"[insider] Failed to send alert: {exc}", file=sys.stderr)

        # Trim seen IDs to prevent unbounded growth
        seen_list = list(seen_ids)
        if len(seen_list) > 2000:
            seen_list = seen_list[-1000:]

        state["last_poll_time"] = now.isoformat()
        state["seen_signal_ids"] = seen_list
        state["wallet_cache"] = wallet_cache  # persist so we don't re-check wallets
        _save_state(state, self.state_path)

        print(f"[insider] Done. {alerts_sent} alerts sent, {len(aggregated)} buckets found.", file=sys.stderr)
        return {
            "alerts_sent": alerts_sent,
            "total_signals": len(signals),
            "aggregated_buckets": len(aggregated),
            "top_buckets": aggregated[:5],
        }


def run_insider_monitor(
    *,
    min_score: float = 0.15,
    state_path: str = str(INSIDER_STATE_FILE),
    env_file: str = ".env",
) -> dict[str, Any]:
    load_env_file(env_file)
    telegram_config = TelegramConfig.from_env()
    client = PolymarketPublicClient()
    notifier = TelegramNotifier(telegram_config)

    monitor = InsiderMonitor(
        client,
        notifier,
        min_score=min_score,
        state_path=Path(state_path),
    )
    return monitor.poll()


def run_monitor(
    *,
    min_size: float = 1000.0,
    state_path: str = str(STATE_FILE),
    env_file: str = ".env",
) -> dict[str, Any]:
    load_env_file(env_file)
    telegram_config = TelegramConfig.from_env()
    client = PolymarketPublicClient()
    notifier = TelegramNotifier(telegram_config)

    monitor = WhaleMonitor(
        client,
        notifier,
        min_trade_size=min_size,
        state_path=Path(state_path),
    )
    return monitor.poll()
