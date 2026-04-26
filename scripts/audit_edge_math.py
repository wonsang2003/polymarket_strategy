"""Edge math sanity check on recent trades.

For every recent trade, compare what's stored in DB columns
  - `edge`
  - `entry_edge`
against what the math SHOULD be:
  raw_edge       = model_prob - market_prob               # pp gap
  fee_adj_edge   = raw_edge - 0.02 * model_prob * (1 - market_prob)
  ev_dollars     = fee_adj_edge * notional                # USD EV

Also reports per-strategy what the columns represent. Surfaces
inconsistencies between mainstream and tail-NO storage conventions.
"""
import sqlite3
import sys
from collections import defaultdict


FEE = 0.02


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    c.row_factory = sqlite3.Row

    rows = c.execute(
        """SELECT id, city, side, token_side, model_prob, market_prob,
                  edge, entry_edge, notional, entry_price, pnl, outcome,
                  created_at, exit_reason, category, strategy_name,
                  bracket_lower_f, bracket_upper_f
           FROM trade_history
           ORDER BY id DESC
           LIMIT 30"""
    ).fetchall()

    print(f"{'─'*120}")
    print("EDGE MATH AUDIT — last 30 trades")
    print(f"{'─'*120}")
    print(
        f"  {'id':>3} {'cat':<8} {'city':<13} {'side':<7} "
        f"{'p_mod':>6} {'p_mkt':>6} {'entry':>6} {'notnl':>5} "
        f"{'raw_pp':>7} {'fee_adj_pp':>10} {'ev_$':>7} "
        f"{'DB_edge':>9} {'DB_entry_edge':>14}  flag"
    )

    deltas_edge = []
    deltas_entry_edge = []
    for r in rows:
        cat = (r["category"] or "—")[:8]
        p_mod = float(r["model_prob"] or 0)
        p_mkt = float(r["market_prob"] or 0)
        notional = float(r["notional"] or 0)
        entry = float(r["entry_price"] or 0)
        raw_pp = p_mod - p_mkt
        fee_adj_pp = raw_pp - FEE * p_mod * (1 - p_mkt)
        ev_dollars = fee_adj_pp * notional

        db_edge = float(r["edge"] or 0)
        db_entry_edge = (
            float(r["entry_edge"]) if r["entry_edge"] is not None else None
        )

        # What units does DB_edge match?
        flag_parts = []
        if abs(db_edge - fee_adj_pp) < 0.005:
            flag_parts.append("edge=PP")
        elif abs(db_edge - ev_dollars) < 0.01:
            flag_parts.append("edge=USD")
        elif abs(db_edge - raw_pp) < 0.005:
            flag_parts.append("edge=raw_pp")
        else:
            flag_parts.append(f"edge=?({db_edge:.2f})")
        if db_entry_edge is not None:
            if abs(db_entry_edge - fee_adj_pp) < 0.005:
                flag_parts.append("ee=PP")
            elif abs(db_entry_edge - ev_dollars) < 0.05:
                flag_parts.append("ee=USD")
            elif abs(db_entry_edge - raw_pp) < 0.005:
                flag_parts.append("ee=raw_pp")
            else:
                flag_parts.append(f"ee=?({db_entry_edge:.2f})")

        print(
            f"  #{r['id']:>3} {cat:<8} {r['city'][:13]:<13} "
            f"{(r['side'] or '')[:7]:<7} "
            f"{p_mod:>6.3f} {p_mkt:>6.3f} {entry:>6.3f} ${notional:>4.0f} "
            f"{raw_pp:>+7.3f} {fee_adj_pp:>+10.3f} ${ev_dollars:>+5.2f}  "
            f"{db_edge:>+9.3f} "
            f"{db_entry_edge if db_entry_edge is not None else 'NULL':>14}  "
            f"{','.join(flag_parts)}"
        )

    # Aggregate convention check by category
    print()
    print(f"{'─'*120}")
    print("CONVENTION SUMMARY by category")
    print(f"{'─'*120}")
    by_cat = defaultdict(lambda: {"PP": 0, "USD": 0, "raw": 0, "?": 0})
    for r in rows:
        cat = r["category"] or "mainstream"
        p_mod = float(r["model_prob"] or 0)
        p_mkt = float(r["market_prob"] or 0)
        notional = float(r["notional"] or 0)
        raw_pp = p_mod - p_mkt
        fee_adj_pp = raw_pp - FEE * p_mod * (1 - p_mkt)
        ev_dollars = fee_adj_pp * notional
        db_edge = float(r["edge"] or 0)
        if abs(db_edge - fee_adj_pp) < 0.005:
            by_cat[cat]["PP"] += 1
        elif abs(db_edge - ev_dollars) < 0.01:
            by_cat[cat]["USD"] += 1
        elif abs(db_edge - raw_pp) < 0.005:
            by_cat[cat]["raw"] += 1
        else:
            by_cat[cat]["?"] += 1
    print(f"  {'category':<25} {'PP units':>10} {'USD units':>10} {'raw pp':>10} {'unknown':>10}")
    for cat, stats in sorted(by_cat.items()):
        print(
            f"  {cat:<25} {stats['PP']:>10} {stats['USD']:>10} "
            f"{stats['raw']:>10} {stats['?']:>10}"
        )

    # Also: market_prob sanity. For a NO trade, market_prob should be near
    # entry_price (= 1 - YES_bid). Check if model_prob also references the NO
    # side or the YES side.
    print()
    print(f"{'─'*120}")
    print("NO-side trades: model_prob represents NO side or YES side?")
    print(f"{'─'*120}")
    no_rows = [
        r for r in rows
        if r["token_side"] == "NO" or (r["side"] and "NO" in r["side"])
    ]
    print(
        f"  {'id':>3} {'cat':<8} {'side':<7} {'tk':<3} "
        f"{'p_mod':>6} {'p_mkt':>6} {'entry':>6} {'1-p_mkt':>8} "
        f"{'matches':<20}"
    )
    for r in no_rows[:15]:
        cat = (r["category"] or "—")[:8]
        p_mod = float(r["model_prob"] or 0)
        p_mkt = float(r["market_prob"] or 0)
        entry = float(r["entry_price"] or 0)
        # NO-side market_prob should match NO ask = 1 - YES bid ≈ entry_price
        # If market_prob ≈ entry, market_prob is the NO-side price
        # If market_prob ≈ 1-entry, it's stored as YES-side
        match_no = "yes" if abs(p_mkt - entry) < 0.05 else "no"
        match_yes = "yes" if abs(p_mkt - (1 - entry)) < 0.05 else "no"
        match_label = (
            f"NO ✓" if match_no == "yes" else
            f"YES ✓" if match_yes == "yes" else "neither"
        )
        print(
            f"  #{r['id']:>3} {cat:<8} {(r['side'] or '')[:7]:<7} "
            f"{(r['token_side'] or '')[:3]:<3} "
            f"{p_mod:>6.3f} {p_mkt:>6.3f} {entry:>6.3f} {1-p_mkt:>8.3f}  "
            f"{match_label}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
