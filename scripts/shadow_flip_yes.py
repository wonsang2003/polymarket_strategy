"""Shadow-log: what if every NO trade had been flipped to YES at entry?

For each historical NO trade:
  Actual realized P&L = (loaded from trade_history)
  Counterfactual YES P&L = computed by flipping the side at entry,
    then resolving against the same outcome.

Math:
  NO trade entered at no_ask = p_no_market, notional N.
    NO wins iff outcome != bracket_hit:
      payout_no = N/p_no_market × (1 - p_no_market) × 0.98
      pnl_no = +payout_no    (if NO won)
              −N              (if NO lost)

  Flip to YES at the same time:
    yes_ask ≈ 1 − p_no_market  (assume zero spread)
    YES wins iff bracket_hit:
      payout_yes = N/yes_ask × (1 - yes_ask) × 0.98 = N × p_no_market / (1-p_no_market) × 0.98
      pnl_yes = +payout_yes   (if YES won — i.e. NO lost)
              −N              (if YES lost — i.e. NO won)

So flip P&L = swap the win/loss outcome AND change the magnitude per the
implied YES payoff ratio.

Run on EC2."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(t):
    print()
    print("=" * 78)
    print(f"  {t}")
    print("=" * 78)


def fmt(v):
    if v is None:
        return "$0.00"
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def flip_pnl(no_entry: float, notional: float, outcome: int, pnl_real: float) -> float:
    """Counterfactual YES-side P&L if we had flipped at entry.

    Use pnl_real sign (NOT outcome column — outcome semantics turned out
    to be inconsistent between rows). pnl > 0 = NO won, pnl <= 0 = NO lost.

    If NO won (pnl > 0):  YES would have lost full notional → -notional
    If NO lost (pnl <= 0): YES would have won at yes_ask = 1 - no_entry,
                            payout = notional * (1-yes_ask)/yes_ask × 0.98

    outcome=2 means rebal exit → return None (ill-defined for flip)
    """
    if outcome == 2:
        return None
    yes_ask = 1.0 - no_entry  # zero-spread approx
    if yes_ask <= 0 or yes_ask >= 1:
        return None
    if pnl_real <= 0:
        # NO lost (or breakeven). YES would have WON.
        gross = notional * (1 - yes_ask) / yes_ask
        fee = 0.02 * gross
        return gross - fee
    else:
        # NO won. YES would have lost full notional.
        return -notional


def main():
    c = conn()
    print(f"NOW: {(datetime.now(timezone.utc)+timedelta(hours=9)).strftime('%Y-%m-%d %H:%M KST')}\n")

    for window_h in [36, 72, 168]:  # 36h / 3d / 7d
        hr(f"WINDOW: last {window_h}h")
        rows = c.execute("""
            SELECT id, city, target_date,
                   COALESCE(category,'<null>') AS cat,
                   side, token_side,
                   entry_price, notional, outcome, pnl,
                   ROUND(bracket_upper_f - bracket_lower_f, 1) AS w
            FROM trade_history
            WHERE outcome IS NOT NULL
              AND outcome != 2  -- exclude rebal exits (flip ill-defined)
              AND (token_side='NO' OR side LIKE '%NO%')
              AND datetime(settled_at) >= datetime('now', ?)
            ORDER BY id
        """, (f'-{window_h} hours',)).fetchall()

        actual_total = 0.0
        flip_total = 0.0
        actual_wins = 0
        flip_wins = 0
        n_valid = 0
        details = []
        for r in rows:
            entry = float(r['entry_price'] or 0)
            notional = float(r['notional'] or 0)
            outcome = int(r['outcome'])
            actual_pnl = float(r['pnl'] or 0)
            flip = flip_pnl(entry, notional, outcome, actual_pnl)
            if flip is None or entry <= 0 or entry >= 1:
                continue
            n_valid += 1
            actual_total += actual_pnl
            flip_total += flip
            if actual_pnl > 0:
                actual_wins += 1
            if flip > 0:
                flip_wins += 1
            details.append((r, actual_pnl, flip))

        print(f"  Valid NO trades        : {n_valid}")
        print(f"  ACTUAL  (NO realized)  : net={fmt(actual_total)}  "
              f"wins={actual_wins}/{n_valid}  win%={actual_wins/n_valid*100:.1f}" if n_valid else "")
        print(f"  SHADOW  (YES flipped)  : net={fmt(flip_total)}  "
              f"wins={flip_wins}/{n_valid}  win%={flip_wins/n_valid*100:.1f}" if n_valid else "")
        print(f"  Δ (flip − actual)      : {fmt(flip_total - actual_total)}")

        # By category
        print(f"\n  By category:")
        cats = {}
        for r, ap, fp in details:
            cats.setdefault(r['cat'], {'n': 0, 'actual': 0.0, 'flip': 0.0})
            cats[r['cat']]['n'] += 1
            cats[r['cat']]['actual'] += ap
            cats[r['cat']]['flip'] += fp
        for cat, agg in sorted(cats.items()):
            delta = agg['flip'] - agg['actual']
            print(f"    {cat:<22} n={agg['n']:>3}  "
                  f"actual={fmt(agg['actual']):>10}  flip={fmt(agg['flip']):>10}  "
                  f"Δ={fmt(delta):>10}")

        # Itemized — show every trade so we can spot patterns
        if window_h == 72:
            hr("  TRADE-BY-TRADE 72h (NO settled, with flip P&L)")
            print(f"  {'id':>4} {'city':<14} {'tgt':<11} {'cat':<18} "
                  f"{'entry':>5} {'out':>3} {'w':>5} "
                  f"{'actual':>9} {'flip':>9} {'Δ':>9}")
            for r, ap, fp in details:
                d = fp - ap
                print(f"  #{r['id']:>3} {r['city']:<14} {r['target_date']:<11} "
                      f"{r['cat']:<18} {r['entry_price']:>5.3f} "
                      f"{r['outcome']:>3} {(r['w'] or 0):>4}F "
                      f"{fmt(ap):>9} {fmt(fp):>9} {fmt(d):>9}")

    c.close()


if __name__ == "__main__":
    main()
