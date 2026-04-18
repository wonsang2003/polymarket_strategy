"""Show current open positions held by ELITE whales.

Usage:
    python -m polymarket_strat.whale_positions
    python -m polymarket_strat.whale_positions --top 10
    python -m polymarket_strat.whale_positions --wallet 0xafbacaee...
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone

UTC = timezone.utc
from typing import Any

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.domain.strategies.whale_following import (
    WhaleFollowingStrategy,
    WhaleSelectionConfig,
    WhaleScore,
    _float,
)


def _fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:,.1f}K"
    return f"${v:,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v:.0%}" if abs(v) < 10 else f"{v:.1f}x"


def _truncate(s: str, n: int = 55) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def fetch_elite_whales(client: PolymarketPublicClient, top: int) -> tuple[list[WhaleScore], list[dict[str, Any]]]:
    cfg = WhaleSelectionConfig(market_limit=30, holders_per_market=20, max_candidates=60)
    strategy = WhaleFollowingStrategy(client, selection=cfg)
    print("[discovery] Finding candidate wallets...", file=sys.stderr)
    candidates = strategy.discover_candidate_wallets()
    print(f"[discovery] Scoring {len(candidates)} wallets...", file=sys.stderr)
    whales = strategy.rank_whales(candidates)
    print(f"[discovery] Validating longevity for {len(whales)} whales...", file=sys.stderr)
    elite, profiles = strategy.validate_whale_longevity(whales, min_tier="ELITE")
    elite = elite[:top]
    elite_wallets = {w.wallet for w in elite}
    elite_profiles = [p for p in profiles if p["wallet"] in elite_wallets]
    return elite, elite_profiles


def show_positions_for_wallet(
    client: PolymarketPublicClient,
    wallet: str,
    profile: dict[str, Any] | None = None,
) -> None:
    positions = client.get_positions(wallet, limit=200)
    if not positions:
        print(f"  (no open positions)")
        return

    # Sort by current value descending
    def _sort_key(p: dict[str, Any]) -> float:
        size = _float(p.get("size") or p.get("amount"), 0)
        price = _float(p.get("curPrice") or p.get("currentPrice"), 0)
        return -(size * price)

    positions.sort(key=_sort_key)

    total_value = 0.0
    total_cost = 0.0
    for pos in positions:
        title = str(pos.get("title") or pos.get("question") or pos.get("marketSlug") or pos.get("conditionId", "")[:20])
        outcome = str(pos.get("outcome") or "?")
        size = _float(pos.get("size") or pos.get("amount"), 0)
        avg_price = _float(pos.get("avgPrice") or pos.get("averagePrice"), 0)
        cur_price = _float(pos.get("curPrice") or pos.get("currentPrice"), 0)
        cost = _float(pos.get("totalBought") or pos.get("costBasis"), size * avg_price)

        value = size * cur_price
        pnl = value - cost if cost > 0 else 0
        roi = pnl / cost if cost > 0 else 0
        total_value += value
        total_cost += cost

        pnl_str = f"+{_fmt_usd(pnl)}" if pnl >= 0 else f"-{_fmt_usd(abs(pnl))}"
        roi_color = "+" if roi >= 0 else ""

        print(f"  {_truncate(title, 50):50s}  {outcome:12s}  "
              f"avg={avg_price:.3f}  cur={cur_price:.3f}  "
              f"size={_fmt_usd(size):>8s}  value={_fmt_usd(value):>8s}  "
              f"PnL={pnl_str:>9s} ({roi_color}{roi:.0%})")

    total_pnl = total_value - total_cost
    print(f"  {'─' * 120}")
    print(f"  TOTAL: {len(positions)} positions | "
          f"value={_fmt_usd(total_value)} | cost={_fmt_usd(total_cost)} | "
          f"unrealized PnL={'+' if total_pnl >= 0 else ''}{_fmt_usd(total_pnl)}")


def show_consensus(client: PolymarketPublicClient, elite: list[WhaleScore]) -> None:
    """Show markets where multiple ELITE whales hold the same position."""
    print("\n" + "=" * 80)
    print("CONSENSUS POSITIONS — markets where multiple ELITE whales are in")
    print("=" * 80)

    market_holders: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for whale in elite:
        positions = client.get_positions(whale.wallet, limit=200)
        for pos in positions:
            title = str(pos.get("title") or pos.get("conditionId", "")[:20])
            outcome = str(pos.get("outcome") or "?")
            key = (title, outcome)
            size = _float(pos.get("size") or pos.get("amount"), 0)
            cur_price = _float(pos.get("curPrice") or pos.get("currentPrice"), 0)
            market_holders[key].append({
                "wallet": whale.wallet[:12],
                "size": size,
                "value": size * cur_price,
                "score": whale.score,
            })

    # Filter to markets with 2+ whales, sort by total value
    consensus = [
        (key, holders)
        for key, holders in market_holders.items()
        if len(holders) >= 2
    ]
    consensus.sort(key=lambda x: -sum(h["value"] for h in x[1]))

    if not consensus:
        print("  (no consensus positions found)")
        return

    for (title, outcome), holders in consensus[:20]:
        total_value = sum(h["value"] for h in holders)
        print(f"\n  {_truncate(title, 60)}")
        print(f"  Outcome: {outcome} | {len(holders)} whales | combined value: {_fmt_usd(total_value)}")
        for h in sorted(holders, key=lambda x: -x["value"]):
            print(f"    {h['wallet']}...  value={_fmt_usd(h['value']):>8s}  score={h['score']:.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="View open positions held by ELITE whales.")
    parser.add_argument("--top", type=int, default=20, help="Number of top ELITE whales to show (default: 20)")
    parser.add_argument("--wallet", type=str, default=None, help="Show positions for a specific wallet address only")
    parser.add_argument("--consensus", action="store_true", help="Show markets where multiple ELITE whales overlap")
    args = parser.parse_args()

    client = PolymarketPublicClient()

    if args.wallet:
        print(f"\nPositions for {args.wallet}:")
        show_positions_for_wallet(client, args.wallet)
        return

    elite, profiles = fetch_elite_whales(client, args.top)
    profile_lookup = {p["wallet"]: p for p in profiles}

    print(f"\n{'=' * 80}")
    print(f"ELITE WHALE OPEN POSITIONS — top {len(elite)} whales")
    print(f"{'=' * 80}")

    for i, whale in enumerate(elite, 1):
        profile = profile_lookup.get(whale.wallet, {})
        tier = profile.get("longevity_tier", "?")
        wr = profile.get("win_rate", 0)
        sharpe = profile.get("sharpe", 0)
        months = profile.get("months_active", 0)
        total_pnl = profile.get("total_pnl", 0)
        trend = profile.get("trend", "?")

        print(f"\n{'─' * 80}")
        print(f"#{i} {whale.wallet}  [{tier}]")
        print(f"   WR={wr:.0%}  Sharpe={sharpe:.1f}  {months}mo active  "
              f"realized PnL={_fmt_usd(total_pnl)}  trend={trend}  score={whale.score:.3f}")
        print()

        show_positions_for_wallet(client, whale.wallet, profile)

    if args.consensus:
        show_consensus(client, elite)

    print()


if __name__ == "__main__":
    main()
