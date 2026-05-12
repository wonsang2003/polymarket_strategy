"""Live open-positions snapshot — what the dashboard shows right now."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


def main():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
    print(f"NOW: {now_kst}")
    print()

    # Latest MTM freshness
    r = c.execute("SELECT MAX(fetched_at_utc) AS last FROM market_prices").fetchone()
    if r["last"]:
        last_dt = datetime.fromisoformat(str(r["last"]).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
        print(f"Last snap-mtm: {age_s:.0f}s ago "
              f"({last_dt.astimezone(timezone(timedelta(hours=9))).strftime('%H:%M:%S KST')})")

    # All open positions with latest bid
    rows = c.execute("""
        SELECT t.id, t.city, t.target_date,
               COALESCE(t.category,'<null>') AS cat,
               t.token_side,
               ROUND(t.bracket_upper_f - t.bracket_lower_f, 1) AS w,
               ROUND(t.entry_price, 3) AS px,
               ROUND(t.edge, 3) AS edge,
               t.notional,
               t.created_at,
               mp.best_bid AS bid,
               mp.best_ask AS ask,
               mp.fetched_at_utc AS fetched
        FROM trade_history t
        LEFT JOIN (
            SELECT mp1.* FROM market_prices mp1
            JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx
                  FROM market_prices GROUP BY token_id) lt
              ON lt.token_id = mp1.token_id
             AND lt.mx = mp1.fetched_at_utc
        ) mp ON mp.token_id = t.token_id
        WHERE t.outcome IS NULL
        ORDER BY t.target_date, t.city
    """).fetchall()

    print(f"\nOPEN POSITIONS: {len(rows)}")
    print(f"{'id':>4} {'city':<14} {'target':<11} {'cat':<18} {'tk':>3} "
          f"{'w':>6} {'entry':>5} {'bid':>5} {'ask':>5} {'notl':>5} "
          f"{'upnl':>8} {'roi%':>7} {'fetched':<10}")

    danger, cruising, neutral, no_data = [], [], [], []
    total_notional = 0.0
    total_upnl = 0.0
    by_target = {}
    by_city_upnl = {}

    for r in rows:
        entry = r["px"] or 0
        bid = r["bid"]
        notional = r["notional"] or 0
        total_notional += notional

        if entry > 0 and bid is not None:
            shares = notional / entry
            gross = shares * (bid - entry)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            roi = upnl / notional if notional > 0 else 0
            total_upnl += upnl
        else:
            shares = upnl = roi = None

        # Fetch age
        try:
            fdt = datetime.fromisoformat(str(r["fetched"]).replace("Z", "+00:00"))
            if fdt.tzinfo is None:
                fdt = fdt.replace(tzinfo=timezone.utc)
            fhh = fdt.astimezone(timezone(timedelta(hours=9))).strftime("%H:%M:%S")
        except Exception:
            fhh = "—"

        bid_s = f"{bid:.3f}" if bid is not None else "—"
        ask_s = f"{r['ask']:.3f}" if r["ask"] is not None else "—"
        upnl_s = f"{upnl:+7.2f}" if upnl is not None else "    —  "
        roi_s = f"{roi*100:+6.1f}%" if roi is not None else "    —"

        print(f"  {r['id']:>3} {r['city']:<14} {r['target_date']:<11} "
              f"{r['cat']:<18} {(r['token_side'] or '—'):>3} "
              f"{r['w']:>5}F {entry:>5.3f} {bid_s:>5} {ask_s:>5} "
              f"{notional:>5.0f} {upnl_s} {roi_s} {fhh:<10}")

        # Bucket
        if upnl is None:
            no_data.append((r, upnl, roi, bid))
        elif bid is not None and bid < 0.10:
            danger.append((r, upnl, roi, bid))
        elif bid is not None and bid > entry:
            cruising.append((r, upnl, roi, bid))
        else:
            neutral.append((r, upnl, roi, bid))

        by_target.setdefault(r["target_date"], {"n": 0, "notl": 0.0, "upnl": 0.0})
        by_target[r["target_date"]]["n"] += 1
        by_target[r["target_date"]]["notl"] += notional
        if upnl is not None:
            by_target[r["target_date"]]["upnl"] += upnl

        by_city_upnl.setdefault(r["city"], 0.0)
        if upnl is not None:
            by_city_upnl[r["city"]] += upnl

    print(f"\n{'='*78}")
    print(f"TOTAL notional         : ${total_notional:,.2f}")
    print(f"TOTAL unrealized P&L   : ${total_upnl:+,.2f}")
    print(f"  Cruising (bid>entry) : {len(cruising)}  combined upnl=${sum(x[1] for x in cruising):+.2f}")
    print(f"  Neutral              : {len(neutral)}   combined upnl=${sum(x[1] for x in neutral):+.2f}")
    print(f"  Danger (bid<0.10)    : {len(danger)}   combined upnl=${sum(x[1] for x in danger):+.2f}")
    print(f"  No data              : {len(no_data)}")

    print(f"\nBY TARGET DATE:")
    for d, agg in sorted(by_target.items()):
        print(f"  {d}: n={agg['n']:>2}  notional=${agg['notl']:>6.2f}  "
              f"upnl=${agg['upnl']:+7.2f}")

    print(f"\nDANGER (bid < 0.10) — likely full-notional losses:")
    for r, upnl, roi, bid in sorted(danger, key=lambda x: x[1]):
        print(f"  #{r[0]['id']:>3} {r[0]['city']:<12} {r[0]['target_date']} "
              f"{r[0]['cat']:<18} entry={r[0]['px']:.3f} bid={bid:.3f} "
              f"upnl=${upnl:+.2f}")

    print(f"\nCRUISING (bid > entry) — top winners:")
    for r, upnl, roi, bid in sorted(cruising, key=lambda x: -x[1])[:10]:
        print(f"  #{r[0]['id']:>3} {r[0]['city']:<12} {r[0]['target_date']} "
              f"{r[0]['cat']:<18} entry={r[0]['px']:.3f} bid={bid:.3f} "
              f"upnl=${upnl:+.2f} roi={roi*100:+.1f}%")

    print(f"\nNEUTRAL (bid <= entry but >= 0.10) — at risk if forecast moves:")
    for r, upnl, roi, bid in sorted(neutral, key=lambda x: x[1])[:10]:
        print(f"  #{r[0]['id']:>3} {r[0]['city']:<12} {r[0]['target_date']} "
              f"{r[0]['cat']:<18} entry={r[0]['px']:.3f} bid={bid:.3f} "
              f"upnl=${upnl:+.2f} roi={roi*100:+.1f}%")

    # Today's KST realized P&L for context
    today_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
    r = c.execute("SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
                  "FROM trade_history WHERE settled_at LIKE ?", (today_kst + "%",)).fetchone()
    print(f"\nP&L TODAY (KST {today_kst}): ${r['net'] or 0:+.2f} over {r['n']} events")

    # Lifetime
    r = c.execute("SELECT ROUND(SUM(pnl),2) AS net, COUNT(*) AS n "
                  "FROM trade_history WHERE outcome IS NOT NULL").fetchone()
    print(f"LIFETIME cumulative   : ${r['net']:+.2f} over {r['n']} settles")
    print(f"TRUE total (lifetime + open unrealized): ${(r['net'] or 0) + total_upnl:+.2f}")

    c.close()


if __name__ == "__main__":
    main()
