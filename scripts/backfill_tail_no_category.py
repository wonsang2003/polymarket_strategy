"""One-shot backfill: stamp `category` + `strategy_name` on the 14 tail-NO
trades that shipped before the schema migration.

Identification (from the cycle JSON we captured):
  trades with these market_ids placed at 2026-04-26 16:32 KST were tail-NO.
"""
import sqlite3
import sys

KNOWN_TAIL_NO_MARKETS = {
    "2064941",  # chicago $50
    "2064826",  # london $40
    "2074435",  # munich $40
    "2074503",  # milan $40
    "2065058",  # milan $40
    "2074468",  # hong_kong $40
    "2064881",  # toronto $40
    "2074477",  # shanghai $40
    "2064827",  # london $30
    "2064860",  # buenos_aires $30
    "2074311",  # seoul $30
    "2064848",  # sao_paulo $30
    "2074469",  # hong_kong $30
    "2082313",  # sao_paulo $30
}


def main() -> int:
    c = sqlite3.connect("data/weather/weather.db")
    cur = c.cursor()

    # Sanity: ensure the columns exist (will be no-ops if migration ran).
    for col, typ in [("category", "TEXT"), ("strategy_name", "TEXT")]:
        try:
            cur.execute(f"ALTER TABLE trade_history ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    c.commit()

    # Find the open trades matching these market_ids, created post 2026-04-26 16:30 KST
    # NB: trade_history.created_at is UTC. The 14 trades placed at 16:32 KST
    # (= 07:32 UTC) and 20:07 KST (= 11:07 UTC). Match on market_id alone
    # (these market_ids are unique to that batch) and only post the deploy
    # cutoff in UTC. We DO NOT filter outcome IS NULL because most have
    # already been forced-exited by rebalance (outcome=2).
    rows = cur.execute(
        """
        SELECT id, market_id, city, side, notional, created_at, outcome,
               category, strategy_name
        FROM trade_history
        WHERE market_id IN ({})
          AND created_at >= '2026-04-26 07:30:00'
        """.format(",".join(["?"] * len(KNOWN_TAIL_NO_MARKETS))),
        list(KNOWN_TAIL_NO_MARKETS),
    ).fetchall()
    print(f"matching trades (settled or open): {len(rows)}")
    for r in rows:
        rid, mid, city, side, notional, ts, outcome, cat, strat = r
        out_label = "OPEN" if outcome is None else f"out={outcome}"
        print(f"  #{rid:>3} {ts[:16]} {city:<14} {side:<8} ${notional:>5.0f} "
              f"{out_label:<8} market={mid} (category={cat}, strategy={strat})")

    if not rows:
        print("No matching rows. Aborting backfill.")
        return 0

    # Backfill
    ids = [r[0] for r in rows]
    cur.executemany(
        "UPDATE trade_history SET category=?, strategy_name=? WHERE id=?",
        [("weather_tail_no", "weather_tail_no", rid) for rid in ids],
    )
    c.commit()
    print(f"\nbackfilled {cur.rowcount if cur.rowcount > 0 else len(ids)} rows.")

    # Verify
    cur2 = c.execute(
        "SELECT id, category, strategy_name FROM trade_history "
        "WHERE id IN ({})".format(",".join(["?"] * len(ids))),
        ids,
    ).fetchall()
    for rid, cat, strat in cur2:
        print(f"  verified #{rid}: category={cat}, strategy={strat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
