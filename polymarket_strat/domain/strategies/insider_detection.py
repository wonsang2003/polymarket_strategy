"""Insider-trading anomaly detection for Polymarket.

Strategy: scan political/economic markets at the market level (not wallet level).
Four independent signals are computed per market/outcome:

  1. volume_spike   — recent 2-hour volume vs 2-26h baseline ratio ≥ threshold
  2. new_wallet     — wallet with no prior history makes a large bet (≥ min_bet)
  3. coordinated    — N+ distinct wallets buy the same outcome in a short window
  4. price_impact   — a single burst of trades shifts the implied probability ≥ X pts

Each signal produces an InsiderSignal with a severity score (0–1+).  The caller
(InsiderMonitor) decides which signals to alert on.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from typing import Any

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.domain.strategies.whale_following import (
    _float,
    _parse_timestamp,
    _side_is_buy,
)


# ---------------------------------------------------------------------------
# Keywords used to identify political / economic markets
# ---------------------------------------------------------------------------
_POLITICAL_KEYWORDS: frozenset[str] = frozenset(
    [
        "fed", "federal reserve", "fomc", "rate cut", "rate hike", "interest rate",
        "inflation", "cpi", "gdp", "unemployment", "recession",
        "election", "president", "senate", "congress", "vote", "ballot", "impeach",
        "tariff", "trade war", "sanction", "embargo",
        "sec", "fda", "cfpb", "regulatory", "regulation",
        "war", "ceasefire", "treaty", "invasion", "missile",
        "arrest", "indictment", "verdict", "conviction", "trial",
        "merger", "acquisition", "takeover", "ipo",
        "iran", "russia", "china", "ukraine", "north korea", "taiwan",
        "trump", "biden", "xi", "putin",
        "nato", "g7", "g20", "imf", "world bank",
        "budget", "debt ceiling", "default", "shutdown",
    ]
)


def _is_political_market(title: str, tags: list[str] | None = None) -> bool:
    """Return True if the market title or tags contain political/economic keywords."""
    lower_title = title.lower()
    for kw in _POLITICAL_KEYWORDS:
        if kw in lower_title:
            return True
    if tags:
        for tag in tags:
            if tag.lower() in _POLITICAL_KEYWORDS:
                return True
    return False


def _wallet_from_trade(trade: dict[str, Any]) -> str:
    """Extract the trader wallet address from a trade record."""
    for key in ("maker", "owner", "user", "wallet", "trader", "proxyWallet", "transactorAddress"):
        val = trade.get(key)
        if val and isinstance(val, str) and val.startswith("0x"):
            return val
    return ""


# ---------------------------------------------------------------------------
# Config & signal dataclasses
# ---------------------------------------------------------------------------

@dataclass
class InsiderDetectionConfig:
    # Market scanning
    market_limit: int = 30                 # how many markets to fetch per poll
    trades_per_market: int = 200           # max trades to fetch per market
    lookback_hours: int = 26              # how far back to look for baseline

    # Volume spike
    hot_window_hours: float = 2.0         # "recent" window for spike detection
    volume_spike_threshold: float = 4.0  # recent_vol / baseline_vol to flag

    # New wallet
    new_wallet_min_bet: float = 5_000.0  # minimum USD notional to check wallet age (filters casual gamblers)
    new_wallet_check_limit: int = 8      # max wallets to check per scan (API cost)

    # Coordinated buy
    coordinated_window_minutes: int = 20  # time window to group buys
    coordinated_min_wallets: int = 3     # distinct wallets needed to flag

    # Price impact
    price_impact_threshold: float = 0.04  # 4 percentage-point probability shift

    # Large single trade
    large_trade_min_usd: float = 100_000.0  # minimum single-trade notional to flag
    large_trade_lookback_hours: float = 24.0  # "recent" window for large-trade detection

    # Severity weights (used in combined score)
    weight_volume: float = 0.35
    weight_new_wallet: float = 0.25
    weight_coordinated: float = 0.25
    weight_price_impact: float = 0.15
    weight_large_trade: float = 0.40


@dataclass
class InsiderSignal:
    signal_type: str          # "volume_spike" | "new_wallet" | "coordinated" | "price_impact" | "large_trade"
    market_id: str
    market_title: str
    outcome: str
    severity: float           # 0.0–2.0+ (can exceed 1.0 for extreme anomalies)
    details: dict[str, Any] = field(default_factory=dict)
    detected_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


# ---------------------------------------------------------------------------
# Core detector
# ---------------------------------------------------------------------------

class InsiderDetector:
    def __init__(
        self,
        client: PolymarketPublicClient,
        config: InsiderDetectionConfig | None = None,
    ) -> None:
        self.client = client
        self.cfg = config or InsiderDetectionConfig()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scan(
        self,
        *,
        markets: list[dict[str, Any]] | None = None,
        wallet_cache: dict[str, bool] | None = None,
    ) -> list[InsiderSignal]:
        """Scan political/economic markets and return all detected signals.

        Args:
            markets: pre-fetched list of market dicts (skips API call if provided).
            wallet_cache: dict mapping wallet→is_new_wallet (avoids re-checking).
        """
        if wallet_cache is None:
            wallet_cache = {}

        if markets is None:
            markets = self.client.get_markets(
                limit=self.cfg.market_limit,
                active=True,
                order="volume24hr",
                ascending=False,
            )

        # Filter to political/economic markets that are still active / bettable
        political = [
            m for m in markets
            if _is_political_market(
                str(m.get("question") or m.get("title") or ""),
                m.get("tags"),
            )
            and not m.get("closed", False)
            and m.get("active", True)
        ]
        print(
            f"[insider] Scanning {len(political)}/{len(markets)} political/economic markets.",
            file=sys.stderr,
        )

        signals: list[InsiderSignal] = []
        new_wallet_checks = 0

        for market in political:
            market_id = str(market.get("conditionId") or market.get("id") or "")
            title = str(market.get("question") or market.get("title") or "")
            if not market_id:
                continue

            try:
                trades = self.client.get_trades(market=market_id, limit=self.cfg.trades_per_market)
            except Exception as exc:
                print(f"[insider] Failed to fetch trades for {market_id}: {exc}", file=sys.stderr)
                continue

            if not trades:
                continue

            now = datetime.now(tz=UTC)
            cutoff = now - timedelta(hours=self.cfg.lookback_hours)

            # Parse and filter trades within lookback window
            parsed: list[dict[str, Any]] = []
            for t in trades:
                ts = _parse_timestamp(t.get("timestamp") or t.get("createdAt"))
                if ts is None or ts < cutoff:
                    continue
                wallet = _wallet_from_trade(t)
                outcome = str(t.get("outcome") or "")
                price = _float(t.get("price"), 0.5)
                size = _float(t.get("size") or t.get("amount"), 0.0)
                parsed.append({
                    "ts": ts,
                    "wallet": wallet,
                    "outcome": outcome,
                    "price": price,
                    "size": size,
                    "notional": price * size,
                    "is_buy": _side_is_buy(t),
                    "raw": t,
                })

            if not parsed:
                continue

            # --- Signal 1: Volume spike ---
            vol_signal = self._check_volume_spike(market_id, title, parsed, now)
            if vol_signal:
                signals.append(vol_signal)

            # --- Signal 2: New wallet large bets ---
            if new_wallet_checks < self.cfg.new_wallet_check_limit:
                nw_signals, checked = self._check_new_wallets(
                    market_id,
                    title,
                    parsed,
                    wallet_cache,
                    max_checks=self.cfg.new_wallet_check_limit - new_wallet_checks,
                )
                signals.extend(nw_signals)
                new_wallet_checks += checked

            # --- Signal 3: Coordinated buys ---
            coord_signals = self._check_coordinated_buys(market_id, title, parsed, now)
            signals.extend(coord_signals)

            # --- Signal 4: Price impact ---
            impact_signals = self._check_price_impact(market_id, title, parsed)
            signals.extend(impact_signals)

            # --- Signal 5: Large single trade ($100k+ within 24h) ---
            large_signals = self._check_large_trades(market_id, title, parsed, now)
            signals.extend(large_signals)

        return signals

    # ------------------------------------------------------------------
    # Signal 1: Volume spike
    # ------------------------------------------------------------------

    def _check_volume_spike(
        self,
        market_id: str,
        title: str,
        parsed: list[dict[str, Any]],
        now: datetime,
    ) -> InsiderSignal | None:
        hot_cutoff = now - timedelta(hours=self.cfg.hot_window_hours)
        baseline_cutoff = now - timedelta(hours=self.cfg.lookback_hours)

        hot_trades = [t for t in parsed if t["ts"] >= hot_cutoff and t["is_buy"]]
        baseline_trades = [t for t in parsed if baseline_cutoff <= t["ts"] < hot_cutoff and t["is_buy"]]

        hot_vol = sum(t["notional"] for t in hot_trades)
        if hot_vol < 200:  # too small to care about
            return None

        # Normalize baseline to same time span as hot window for fair comparison
        baseline_hours = self.cfg.lookback_hours - self.cfg.hot_window_hours
        baseline_vol = sum(t["notional"] for t in baseline_trades)

        # Annualize baseline to hot-window scale
        if baseline_hours > 0 and baseline_vol > 0:
            baseline_vol_normalized = baseline_vol * (self.cfg.hot_window_hours / baseline_hours)
        else:
            baseline_vol_normalized = 1.0  # no baseline → treat as spike

        ratio = hot_vol / max(baseline_vol_normalized, 1.0)
        if ratio < self.cfg.volume_spike_threshold:
            return None

        # Find the dominant outcome being bought in the hot window
        outcome_vols: dict[str, float] = {}
        for t in hot_trades:
            outcome_vols[t["outcome"]] = outcome_vols.get(t["outcome"], 0.0) + t["notional"]
        top_outcome = max(outcome_vols, key=outcome_vols.__getitem__) if outcome_vols else ""

        severity = min(ratio / self.cfg.volume_spike_threshold, 3.0)
        return InsiderSignal(
            signal_type="volume_spike",
            market_id=market_id,
            market_title=title,
            outcome=top_outcome,
            severity=severity,
            details={
                "hot_volume_usd": round(hot_vol, 2),
                "baseline_volume_usd_normalized": round(baseline_vol_normalized, 2),
                "spike_ratio": round(ratio, 2),
                "hot_trade_count": len(hot_trades),
            },
        )

    # ------------------------------------------------------------------
    # Signal 2: New wallet large bet
    # ------------------------------------------------------------------

    def _check_new_wallets(
        self,
        market_id: str,
        title: str,
        parsed: list[dict[str, Any]],
        wallet_cache: dict[str, bool],
        *,
        max_checks: int,
    ) -> tuple[list[InsiderSignal], int]:
        """Check if large buys came from wallets with no prior history."""
        signals: list[InsiderSignal] = []
        checks_done = 0

        # Aggregate notional per wallet for buy trades
        wallet_notional: dict[str, float] = {}
        wallet_outcome: dict[str, str] = {}
        for t in parsed:
            if not t["is_buy"] or not t["wallet"]:
                continue
            w = t["wallet"]
            wallet_notional[w] = wallet_notional.get(w, 0.0) + t["notional"]
            if w not in wallet_outcome:
                wallet_outcome[w] = t["outcome"]

        # Check wallets above threshold, most to least notional
        for wallet, notional in sorted(wallet_notional.items(), key=lambda x: -x[1]):
            if notional < self.cfg.new_wallet_min_bet:
                break
            if checks_done >= max_checks:
                break

            # Use cache to avoid redundant API calls
            if wallet in wallet_cache:
                is_new = wallet_cache[wallet]
            else:
                is_new = self._is_new_wallet(wallet)
                wallet_cache[wallet] = is_new
                checks_done += 1

            if is_new:
                severity = self._new_wallet_severity(notional)
                signals.append(
                    InsiderSignal(
                        signal_type="new_wallet",
                        market_id=market_id,
                        market_title=title,
                        outcome=wallet_outcome.get(wallet, ""),
                        severity=severity,
                        details={
                            "wallet": wallet,
                            "buy_notional_usd": round(notional, 2),
                            "note": "Wallet has no closed-position history — first-ever large bet",
                        },
                    )
                )

        return signals, checks_done

    @staticmethod
    def _new_wallet_severity(notional: float) -> float:
        """Graduated severity based on trade size.

        Small bets from new wallets are likely casual gamblers.
        Large bets from new wallets are credible insider signals.

          $5k–$10k  → 0.3  LOW      (serious gambler range)
          $10k–$50k → 1.0  MODERATE (unusual for a first-time wallet)
          $50k–$100k→ 2.0  HIGH     (credible insider pattern)
          $100k+    → 2–3  CRITICAL (institutional-level, very suspicious)
        """
        if notional < 10_000:
            return 0.3
        if notional < 50_000:
            return 1.0
        if notional < 100_000:
            return 2.0
        return min(2.0 + math.log10(notional / 100_000), 3.0)

    def _is_new_wallet(self, wallet: str) -> bool:
        """Return True if wallet has no closed positions (no trading history)."""
        try:
            closed = self.client.get_closed_positions(wallet, limit=5)
            return len(closed) == 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Signal 3: Coordinated buys
    # ------------------------------------------------------------------

    def _check_coordinated_buys(
        self,
        market_id: str,
        title: str,
        parsed: list[dict[str, Any]],
        now: datetime,
    ) -> list[InsiderSignal]:
        """Detect multiple distinct wallets buying the same outcome in a short window."""
        signals: list[InsiderSignal] = []
        window = timedelta(minutes=self.cfg.coordinated_window_minutes)

        # Only care about trades in the last 6 hours (recent coordination)
        recent_cutoff = now - timedelta(hours=6)
        buys = [t for t in parsed if t["is_buy"] and t["ts"] >= recent_cutoff and t["wallet"]]
        if len(buys) < self.cfg.coordinated_min_wallets:
            return []

        # Sort by timestamp, then slide a window
        buys.sort(key=lambda t: t["ts"])

        # Find the single worst-case window per outcome (most wallets, then most notional)
        best_per_outcome: dict[str, dict[str, Any]] = {}

        for i, anchor in enumerate(buys):
            window_end = anchor["ts"] + window
            outcome = anchor["outcome"]

            wallets_in_window: set[str] = set()
            total_notional = 0.0
            for j in range(i, len(buys)):
                t = buys[j]
                if t["ts"] > window_end:
                    break
                if t["outcome"] != outcome:
                    continue
                wallets_in_window.add(t["wallet"])
                total_notional += t["notional"]

            wallet_count = len(wallets_in_window)
            if wallet_count < self.cfg.coordinated_min_wallets:
                continue

            prev = best_per_outcome.get(outcome)
            if prev is None or wallet_count > prev["wallet_count"] or (
                wallet_count == prev["wallet_count"] and total_notional > prev["total_notional"]
            ):
                best_per_outcome[outcome] = {
                    "wallet_count": wallet_count,
                    "total_notional": total_notional,
                    "window_start": anchor["ts"],
                    "wallets_in_window": wallets_in_window,
                }

        for outcome, best in best_per_outcome.items():
            wallet_count = best["wallet_count"]
            severity = min(wallet_count / self.cfg.coordinated_min_wallets, 3.0)
            signals.append(
                InsiderSignal(
                    signal_type="coordinated",
                    market_id=market_id,
                    market_title=title,
                    outcome=outcome,
                    severity=severity,
                    details={
                        "distinct_wallets": wallet_count,
                        "window_minutes": self.cfg.coordinated_window_minutes,
                        "total_notional_usd": round(best["total_notional"], 2),
                        "window_start": best["window_start"].isoformat(),
                        "wallets_sample": sorted(best["wallets_in_window"])[:5],
                    },
                )
            )

        return signals

    # ------------------------------------------------------------------
    # Signal 4: Price impact
    # ------------------------------------------------------------------

    def _check_price_impact(
        self,
        market_id: str,
        title: str,
        parsed: list[dict[str, Any]],
    ) -> list[InsiderSignal]:
        """Detect a burst of trades that shifted the implied probability significantly."""
        signals: list[InsiderSignal] = []

        # Group by outcome, sort by time, compute rolling price change
        outcomes: dict[str, list[dict[str, Any]]] = {}
        for t in parsed:
            if not t["is_buy"] or t["price"] <= 0:
                continue
            outcomes.setdefault(t["outcome"], []).append(t)

        for outcome, trades in outcomes.items():
            if len(trades) < 2:
                continue
            trades.sort(key=lambda t: t["ts"])

            # Look for a 30-minute burst that shifted price by threshold
            window = timedelta(minutes=30)
            for i in range(len(trades) - 1):
                anchor_price = trades[i]["price"]
                window_end = trades[i]["ts"] + window
                burst_trades = [t for t in trades[i:] if t["ts"] <= window_end]
                if len(burst_trades) < 2:
                    continue
                end_price = burst_trades[-1]["price"]
                shift = abs(end_price - anchor_price)
                if shift < self.cfg.price_impact_threshold:
                    continue

                total_notional = sum(t["notional"] for t in burst_trades)
                severity = min(shift / self.cfg.price_impact_threshold, 3.0)
                signals.append(
                    InsiderSignal(
                        signal_type="price_impact",
                        market_id=market_id,
                        market_title=title,
                        outcome=outcome,
                        severity=severity,
                        details={
                            "price_before": round(anchor_price, 4),
                            "price_after": round(end_price, 4),
                            "probability_shift_pts": round(shift * 100, 2),
                            "burst_trade_count": len(burst_trades),
                            "burst_notional_usd": round(total_notional, 2),
                            "burst_start": burst_trades[0]["ts"].isoformat(),
                        },
                    )
                )
                break  # one signal per outcome is enough

        return signals

    # ------------------------------------------------------------------
    # Signal 5: Large single trade ($100k+ within 24h)
    # ------------------------------------------------------------------

    def _check_large_trades(
        self,
        market_id: str,
        title: str,
        parsed: list[dict[str, Any]],
        now: datetime,
    ) -> list[InsiderSignal]:
        """Detect buy trades of $100k+ in the last 24 hours on this market.

        A single massive bet on an active political market is notable on its own.
        Multiple large trades on the same outcome within 24h is even more suspicious —
        it suggests coordinated institutional money or a single actor splitting a position.
        """
        cutoff_24h = now - timedelta(hours=self.cfg.large_trade_lookback_hours)

        # Collect large buy trades per outcome
        outcome_large: dict[str, list[dict[str, Any]]] = {}
        for t in parsed:
            if not t["is_buy"]:
                continue
            if t["ts"] < cutoff_24h:
                continue
            if t["notional"] < self.cfg.large_trade_min_usd:
                continue
            outcome_large.setdefault(t["outcome"], []).append(t)

        signals: list[InsiderSignal] = []
        for outcome, large_trades in outcome_large.items():
            large_trades.sort(key=lambda t: -t["notional"])
            total_notional = sum(t["notional"] for t in large_trades)
            max_single = large_trades[0]["notional"]
            count = len(large_trades)

            # Base severity by total notional
            if total_notional >= 1_000_000:
                base_sev = 3.0
            elif total_notional >= 500_000:
                base_sev = 2.5
            elif total_notional >= 200_000:
                base_sev = 2.0
            else:
                base_sev = 1.5

            # Boost when multiple separate large trades hit the same position
            if count >= 3:
                severity = min(base_sev * 1.3, 3.0)
            elif count >= 2:
                severity = min(base_sev * 1.15, 3.0)
            else:
                severity = base_sev

            signals.append(
                InsiderSignal(
                    signal_type="large_trade",
                    market_id=market_id,
                    market_title=title,
                    outcome=outcome,
                    severity=severity,
                    details={
                        "trade_count": count,
                        "total_notional_usd": round(total_notional, 2),
                        "largest_single_usd": round(max_single, 2),
                        "multiple_detected": count > 1,
                        "trades": [
                            {
                                "wallet": t["wallet"][:18] if t["wallet"] else "unknown",
                                "notional_usd": round(t["notional"], 2),
                                "price": round(t["price"], 4),
                                "timestamp": t["ts"].isoformat(),
                            }
                            for t in large_trades[:5]
                        ],
                    },
                )
            )

        return signals


# ---------------------------------------------------------------------------
# Utility: combine multiple signals on the same market/outcome
# ---------------------------------------------------------------------------

def aggregate_signals(signals: list[InsiderSignal]) -> list[dict[str, Any]]:
    """Group signals by (market_id, outcome) and compute a combined suspicion score."""
    buckets: dict[tuple[str, str], list[InsiderSignal]] = {}
    for sig in signals:
        key = (sig.market_id, sig.outcome)
        buckets.setdefault(key, []).append(sig)

    results = []
    cfg = InsiderDetectionConfig()
    weight_map = {
        "volume_spike": cfg.weight_volume,
        "new_wallet": cfg.weight_new_wallet,
        "coordinated": cfg.weight_coordinated,
        "price_impact": cfg.weight_price_impact,
        "large_trade": cfg.weight_large_trade,
    }
    for (market_id, outcome), sigs in buckets.items():
        combined_score = sum(
            s.severity * weight_map.get(s.signal_type, 0.1) for s in sigs
        )
        results.append(
            {
                "market_id": market_id,
                "market_title": sigs[0].market_title,
                "outcome": outcome,
                "combined_score": round(combined_score, 4),
                "signal_count": len(sigs),
                "signal_types": [s.signal_type for s in sigs],
                "signals": [
                    {
                        "type": s.signal_type,
                        "severity": round(s.severity, 3),
                        "details": s.details,
                        "detected_at": s.detected_at,
                    }
                    for s in sigs
                ],
            }
        )

    results.sort(key=lambda x: -x["combined_score"])
    return results
