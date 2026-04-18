"""Dry-run test script for the insider anomaly detection system.

Scans political/economic Polymarket markets for suspicious trading patterns
and prints results to the terminal. No Telegram, no .env, no state file needed.

Usage:
    python scripts/test_insider.py
    python scripts/test_insider.py --markets 30 --min-score 0.1
    python scripts/test_insider.py --json
    python scripts/test_insider.py --markets 5 --new-wallet-checks 3
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

# Make sure the package root is on the path when running as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.domain.strategies.insider_detection import (
    InsiderDetectionConfig,
    InsiderDetector,
    aggregate_signals,
    _is_political_market,
)


# ---------------------------------------------------------------------------
# Trust-tier labeling for new_wallet signals
# ---------------------------------------------------------------------------

def _trust_tier(notional: float) -> str:
    if notional >= 100_000:
        return "CRITICAL"
    if notional >= 50_000:
        return "HIGH"
    if notional >= 10_000:
        return "MODERATE"
    return "LOW"


def _trust_note(notional: float) -> str:
    if notional >= 100_000:
        return "*** Institutional-level bet from a zero-history wallet. Very credible. ***"
    if notional >= 50_000:
        return "Credible insider pattern — unusual bet size for a brand-new wallet."
    if notional >= 10_000:
        return "Unusual for a first-time wallet, but not conclusive."
    return "Could be a first-time serious gambler — low confidence."


# ---------------------------------------------------------------------------
# Signal tier classification
# ---------------------------------------------------------------------------

_SIGNAL_TIER: dict[str, tuple[int, str]] = {
    "new_wallet":   (1, "STRONG"),
    "large_trade":  (1, "STRONG"),
    "price_impact": (2, "MODERATE"),
    "volume_spike": (3, "WEAK"),
    "coordinated":  (3, "WEAK"),
}

_TIER_NOTE: dict[str, str] = {
    "volume_spike": "Likely a news reaction — verify with external news source before acting.",
    "coordinated":  "May just be many people reacting to the same headline simultaneously.",
    "price_impact": "Price moved during a burst — could be news, but notable if pre-announcement.",
    "new_wallet":   "",
    "large_trade":  "",
}


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _sep(char: str = "=", width: int = 70) -> str:
    return char * width


def _print_section_a(political_titles: list[str], total_scanned: int) -> None:
    print()
    print(_sep())
    print(f"  POLITICAL / ECONOMIC MARKETS SCANNED  ({len(political_titles)}/{total_scanned})")
    print(_sep())
    for i, title in enumerate(political_titles, 1):
        print(f"  {i:2}. {title}")
    if not political_titles:
        print("  (none matched political/economic keywords)")


def _print_section_b(
    aggregated: list[dict],
    min_score: float,
) -> None:
    visible = [b for b in aggregated if b["combined_score"] >= min_score]
    print()
    print(_sep())
    print(
        f"  SIGNALS DETECTED  "
        f"({len(aggregated)} buckets, {sum(b['signal_count'] for b in aggregated)} raw signals)"
        + (f"  |  showing ≥ {min_score} score" if min_score > 0 else "")
    )
    print(_sep())

    if not visible:
        print("  (no buckets met the min-score threshold)")
        return

    for rank, bucket in enumerate(visible, 1):
        score = bucket["combined_score"]
        title = bucket["market_title"]
        outcome = bucket["outcome"]
        market_id = bucket["market_id"]

        tier1_count = sum(1 for s in bucket["signals"] if s["type"] in ("new_wallet", "large_trade"))
        star = "  *** TIER-1 SIGNAL ***" if tier1_count > 0 else ""

        print()
        print(f"[#{rank}]  Score: {score:.3f}{star}")
        print(f"  Market : {title}")
        print(f"  Outcome: {outcome}")
        if market_id:
            print(f"  ID     : {market_id[:28]}...")

        for sig in bucket["signals"]:
            stype = sig["type"]
            sev = sig["severity"]
            details = sig["details"]
            tier_num, tier_name = _SIGNAL_TIER.get(stype, (3, "WEAK"))

            print()
            print(f"    [TIER {tier_num} - {tier_name}]  {stype}  |  severity: {sev:.2f}")

            if stype == "new_wallet":
                notional = details.get("buy_notional_usd", 0.0)
                wallet = details.get("wallet", "?")
                trust = _trust_tier(notional)
                note = _trust_note(notional)
                print(f"      Wallet : {wallet[:18]}...")
                print(f"      Bet    : ${notional:>12,.0f}  |  trust: {trust}")
                print(f"      {note}")

            elif stype == "volume_spike":
                hot = details.get("hot_volume_usd", 0)
                base = details.get("baseline_volume_usd_normalized", 0)
                ratio = details.get("spike_ratio", 0)
                count = details.get("hot_trade_count", 0)
                print(f"      Recent 2h: ${hot:,.0f}  |  Baseline (norm): ${base:,.0f}  |  Ratio: {ratio:.1f}x")
                print(f"      Trade count (hot window): {count}")
                print(f"      ⚠  {_TIER_NOTE['volume_spike']}")

            elif stype == "coordinated":
                wallets = details.get("distinct_wallets", 0)
                window = details.get("window_minutes", 0)
                notional = details.get("total_notional_usd", 0)
                ws = details.get("window_start", "?")
                samples = details.get("wallets_sample", [])
                print(f"      {wallets} wallets in {window}min  |  ${notional:,.0f} notional")
                print(f"      Window start: {ws[:19]}")
                if samples:
                    print(f"      Wallets: {', '.join(w[:14] for w in samples[:3])}...")
                print(f"      ⚠  {_TIER_NOTE['coordinated']}")

            elif stype == "large_trade":
                count = details.get("trade_count", 1)
                total = details.get("total_notional_usd", 0.0)
                biggest = details.get("largest_single_usd", 0.0)
                multiple = details.get("multiple_detected", False)
                trades_list = details.get("trades", [])
                if multiple:
                    print(f"      *** MULTIPLE LARGE TRADES DETECTED ({count} trades in 24h) ***")
                print(f"      Total notional: ${total:>12,.0f}  |  Largest single: ${biggest:>12,.0f}")
                for i, tr in enumerate(trades_list, 1):
                    w = str(tr.get("wallet", "?"))
                    n = tr.get("notional_usd", 0.0)
                    p = tr.get("price", 0.0)
                    ts = str(tr.get("timestamp", "?"))[:19]
                    print(f"      Trade {i}: ${n:>12,.0f} @ {p:.4f}  |  {w}...  |  {ts}")

            elif stype == "price_impact":
                before = details.get("price_before", 0)
                after = details.get("price_after", 0)
                shift = details.get("probability_shift_pts", 0)
                ntrades = details.get("burst_trade_count", 0)
                notional = details.get("burst_notional_usd", 0)
                start = details.get("burst_start", "?")
                print(f"      {before:.2%} → {after:.2%}  (+{shift:.1f} pts over {ntrades} trades)")
                print(f"      Burst: ${notional:,.0f}  starting {start[:19]}")
                print(f"      ⚠  {_TIER_NOTE['price_impact']}")

        print()
        print("  " + "-" * 66)


def _print_section_c(
    aggregated: list[dict],
    political_count: int,
    total_markets: int,
    elapsed: float,
) -> None:
    all_signals = [s for b in aggregated for s in b["signals"]]
    tier1 = sum(1 for s in all_signals if s["type"] in ("new_wallet", "large_trade"))
    tier2 = sum(1 for s in all_signals if s["type"] == "price_impact")
    tier3 = sum(1 for s in all_signals if s["type"] in ("volume_spike", "coordinated"))
    large_trade_sigs = [s for s in all_signals if s["type"] == "large_trade"]
    large_trade_multi = sum(1 for s in large_trade_sigs if s["details"].get("multiple_detected"))

    # Trust breakdown for new_wallet signals
    trust_counts = {"CRITICAL": 0, "HIGH": 0, "MODERATE": 0, "LOW": 0}
    for b in aggregated:
        for sig in b["signals"]:
            if sig["type"] == "new_wallet":
                notional = sig["details"].get("buy_notional_usd", 0)
                trust_counts[_trust_tier(notional)] += 1

    print()
    print(_sep())
    print("  SUMMARY")
    print(_sep())
    print(f"  Scanned {political_count}/{total_markets} political markets in {elapsed:.1f}s")
    print(f"  Raw signals: {len(all_signals)}  |  Aggregated buckets: {len(aggregated)}")
    print(f"  TIER 1 - strong:      {tier1}  (new_wallet + large_trade)")
    if large_trade_sigs:
        print(f"    large_trade: {len(large_trade_sigs)} positions  |  {large_trade_multi} with multiple $100k+ bets")
    print(f"  TIER 2 - price_impact:{tier2}")
    print(f"  TIER 3 - weak:        {tier3}  (volume_spike + coordinated)")
    if tier1 > 0:
        print(
            f"  New wallet trust: "
            f"{trust_counts['CRITICAL']} CRITICAL  "
            f"{trust_counts['HIGH']} HIGH  "
            f"{trust_counts['MODERATE']} MODERATE  "
            f"{trust_counts['LOW']} LOW"
        )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run insider detection scanner — no Telegram, no state file.",
    )
    parser.add_argument("--markets", type=int, default=20, help="Number of markets to scan (default: 20)")
    parser.add_argument("--min-score", type=float, default=0.0, help="Min combined score to display (default: 0.0)")
    parser.add_argument("--new-wallet-checks", type=int, default=5, help="Max wallet-history API calls (default: 5)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Dump raw JSON output")
    args = parser.parse_args()

    client = PolymarketPublicClient()
    cfg = InsiderDetectionConfig(
        market_limit=args.markets,
        new_wallet_check_limit=args.new_wallet_checks,
    )
    detector = InsiderDetector(client, cfg)

    print(f"Fetching top {args.markets} markets by 24h volume...", file=sys.stderr)
    t0 = time.time()

    # Fetch markets separately so we can report which ones are political
    markets = client.get_markets(limit=args.markets, active=True, order="volume24hr", ascending=False)
    political_titles = [
        str(m.get("question") or m.get("title") or "")
        for m in markets
        if _is_political_market(str(m.get("question") or m.get("title") or ""), m.get("tags"))
    ]

    wallet_cache: dict[str, bool] = {}
    signals = detector.scan(markets=markets, wallet_cache=wallet_cache)
    aggregated = aggregate_signals(signals)
    elapsed = time.time() - t0

    if args.json_output:
        print(json.dumps(
            {
                "elapsed_seconds": round(elapsed, 2),
                "markets_scanned": len(markets),
                "political_markets": len(political_titles),
                "political_titles": political_titles,
                "raw_signal_count": len(signals),
                "aggregated_buckets": len(aggregated),
                "buckets": aggregated,
            },
            indent=2,
            default=str,
        ))
        return

    _print_section_a(political_titles, len(markets))
    _print_section_b(aggregated, args.min_score)
    _print_section_c(aggregated, len(political_titles), len(markets), elapsed)


if __name__ == "__main__":
    main()
