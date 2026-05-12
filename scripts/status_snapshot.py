"""Polymarket Strategy — single-shot status snapshot for remote monitoring.

Designed to be invokable from any Claude session via SSH:
    ssh -i ~/.ssh/polymarket-seoul.pem ubuntu@<host> \
        'python3 /home/ubuntu/polymarket/scripts/status_snapshot.py'

Outputs a compact monospace block — phone-readable, paste-able — covering:
  • cron health (MTM snapshot freshness, last autotrade event age)
  • P&L (today / yesterday / 7d / lifetime)
  • open book (count, notional, unrealized, walking-dead, cruising)
  • recent rebal exits + settlements (last 5 each)
  • pending hypotheses awaiting user decision
  • recent evaluator verdicts
  • cumulative confidence-bucket calibration

No arguments. Read-only against weather.db (mode=ro). Failure-tolerant —
if any section errors, prints '[err: ...]' and the rest still ships.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


# ────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────

def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def fmt_money(v) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):,.2f}"


def fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds/60)}min ago"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h ago"
    return f"{seconds/86400:.1f}d ago"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_kst() -> str:
    return (now_utc() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")


def parse_iso(stamp: str) -> datetime | None:
    """Robust parse — handles plain UTC strings, '+00:00', and 'Z'."""
    if not stamp:
        return None
    s = stamp.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ────────────────────────────────────────────────────────────────────
# section renderers — each catches its own errors so partial output ships
# ────────────────────────────────────────────────────────────────────

def section_header() -> None:
    print(f"📊 POLYMARKET STATUS · {now_kst()}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def section_health(c) -> None:
    """Cron freshness — MTM snapshot every 5min is the heartbeat."""
    now = now_utc()
    try:
        row = c.execute(
            "SELECT MAX(fetched_at_utc) AS t FROM market_prices"
        ).fetchone()
        if row and row["t"]:
            last = parse_iso(row["t"])
            if last:
                age = (now - last).total_seconds()
                mark = "✓" if age < 600 else ("⚠" if age < 1800 else "🔴")
                print(f"⏱ MTM snapshot   : {fmt_age(age):<14} {mark}")
            else:
                print(f"⏱ MTM snapshot   : (unparseable: {row['t'][:30]})")
        else:
            print("⏱ MTM snapshot   : (none yet)")
    except sqlite3.OperationalError as e:
        print(f"⏱ MTM snapshot   : [err: {e}]")

    # Most recent trade event (entry or settle) — shows hourly cron alive
    try:
        row = c.execute(
            "SELECT MAX(settled_at) AS s, "
            "       (SELECT settled_at FROM trade_history "
            "        ORDER BY id DESC LIMIT 1) AS last_id_ts "
            "FROM trade_history WHERE settled_at IS NOT NULL"
        ).fetchone()
        if row:
            stamp = row["s"] or row["last_id_ts"]
            if stamp:
                last = parse_iso(stamp)
                if last:
                    age = (now - last).total_seconds()
                    mark = "✓" if age < 7200 else "⚠"
                    print(f"⏱ Last trade evt : {fmt_age(age):<14} {mark}")
                else:
                    print(f"⏱ Last trade evt : ({stamp[:19]})")
    except sqlite3.OperationalError as e:
        print(f"⏱ Last trade evt : [err: {e}]")
    print()


def section_pnl(c) -> None:
    now = now_utc()
    today_kst = (now + timedelta(hours=9)).strftime("%Y-%m-%d")
    yest_kst = (now + timedelta(hours=9, days=-1)).strftime("%Y-%m-%d")

    try:
        today = c.execute(
            "SELECT SUM(pnl) AS net, COUNT(*) AS n FROM trade_history "
            "WHERE settled_at LIKE ? || '%'", (today_kst,)
        ).fetchone()
        yest = c.execute(
            "SELECT SUM(pnl) AS net, COUNT(*) AS n FROM trade_history "
            "WHERE settled_at LIKE ? || '%'", (yest_kst,)
        ).fetchone()
        week = c.execute(
            "SELECT SUM(pnl) AS net, COUNT(*) AS n FROM trade_history "
            "WHERE outcome IS NOT NULL "
            "AND datetime(settled_at) >= datetime('now', '-7 days')"
        ).fetchone()
        life = c.execute(
            "SELECT SUM(pnl) AS net, COUNT(*) AS n FROM trade_history "
            "WHERE outcome IS NOT NULL"
        ).fetchone()

        print("💰 P&L")
        print(f"   Today (KST)   : {fmt_money(today['net']):<14}  (n={today['n']})")
        print(f"   Yesterday     : {fmt_money(yest['net']):<14}  (n={yest['n']})")
        print(f"   7d            : {fmt_money(week['net']):<14}  (n={week['n']})")
        print(f"   Lifetime      : {fmt_money(life['net']):<14}  (n={life['n']})")
    except sqlite3.OperationalError as e:
        print(f"💰 P&L: [err: {e}]")
    print()


def section_open_book(c) -> None:
    try:
        rows = c.execute("""
            SELECT t.entry_price, t.notional, mp.best_bid AS bid
            FROM trade_history t
            LEFT JOIN (
                SELECT mp1.* FROM market_prices mp1
                JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx
                      FROM market_prices GROUP BY token_id) lt
                  ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
            ) mp ON mp.token_id = t.token_id
            WHERE t.outcome IS NULL
        """).fetchall()
    except sqlite3.OperationalError as e:
        print(f"📦 Open Book: [err: {e}]")
        print()
        return

    if not rows:
        print("📦 Open Book: (none open)")
        print()
        return

    open_count = len(rows)
    open_notl = sum(r["notional"] or 0 for r in rows)
    open_upnl = 0.0
    cruising = 0
    walking_dead = 0
    priced = 0
    for r in rows:
        e = r["entry_price"] or 0
        b = r["bid"]
        n = r["notional"] or 0
        if e > 0 and b is not None:
            priced += 1
            shares = n / e
            gross = shares * (b - e)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            open_upnl += upnl
            if upnl > 1:
                cruising += 1
            if b < 0.10:
                walking_dead += 1

    print("📦 Open Book")
    print(f"   {open_count} positions · ${open_notl:,.0f} notional · "
          f"upnl {fmt_money(open_upnl)}")
    print(f"   ✓ Cruising (upnl > $1)   : {cruising}")
    if walking_dead > 0:
        print(f"   ⚠ Walking-dead (bid<0.10): {walking_dead}")
    if priced < open_count:
        print(f"   📉 Unpriced (no MTM yet)  : {open_count - priced}")
    print()


def section_recent_rebals(c) -> None:
    try:
        rows = c.execute("""
            SELECT id, city, COALESCE(exit_reason, '?') AS reason,
                   ROUND(pnl, 2) AS pnl, settled_at
            FROM trade_history
            WHERE outcome = 2
            ORDER BY datetime(settled_at) DESC
            LIMIT 5
        """).fetchall()
    except sqlite3.OperationalError as e:
        print(f"🚨 Recent rebals: [err: {e}]")
        return

    if not rows:
        print("🚨 Recent rebals: (none)")
        print()
        return

    print("🚨 Recent rebals (last 5)")
    for r in rows:
        print(f"   #{r['id']}  {r['city']:<13} {r['reason']:<22} "
              f"{fmt_money(r['pnl'])}")
    print()


def section_recent_settles(c) -> None:
    try:
        rows = c.execute("""
            SELECT id, city, ROUND(pnl, 2) AS pnl, settled_at
            FROM trade_history
            WHERE outcome IN (0, 1)
            ORDER BY datetime(settled_at) DESC
            LIMIT 5
        """).fetchall()
    except sqlite3.OperationalError as e:
        print(f"⚖ Recent settles: [err: {e}]")
        return

    if not rows:
        print("⚖ Recent settles: (none)")
        print()
        return

    print("⚖ Last 5 settled")
    for r in rows:
        verdict = "WIN" if (r["pnl"] or 0) > 0 else "LOSS"
        emoji = "🟢" if verdict == "WIN" else "🔴"
        print(f"   {emoji} #{r['id']}  {r['city']:<13} "
              f"{verdict:<5}  {fmt_money(r['pnl'])}")
    print()


def section_pending_hypotheses(c) -> None:
    try:
        rows = c.execute("""
            SELECT id, hypothesis, confidence_pct, proposed_at
            FROM strategy_hypotheses
            WHERE status = 'proposed'
            ORDER BY datetime(proposed_at) DESC
            LIMIT 10
        """).fetchall()
    except sqlite3.OperationalError:
        print("🔬 Pending hypotheses: (table missing — run init_hypothesis_db.py)")
        print()
        return

    if not rows:
        print("🔬 Pending hypotheses: (none — no decision needed)")
        print()
        return

    print(f"🔬 Pending hypotheses ({len(rows)} awaiting your decision)")
    for r in rows:
        h = (r["hypothesis"] or "")[:55]
        conf = r["confidence_pct"]
        conf_str = f"{conf:>3}%" if conf is not None else "  ?%"
        print(f"   {r['id']:<22}  conf {conf_str}  {h}")
    print('   ⮡ ssh in → python scripts/ship_hypothesis.py <id>  to ship')
    print('   ⮡           --reject --reason "<why>"            to reject')
    print()


def section_recent_verdicts(c) -> None:
    try:
        rows = c.execute("""
            SELECT id, verdict, ROUND(measured_pnl_delta, 2) AS d, evaluation_at
            FROM strategy_hypotheses
            WHERE evaluation_at IS NOT NULL
            ORDER BY datetime(evaluation_at) DESC
            LIMIT 5
        """).fetchall()
    except sqlite3.OperationalError:
        return

    if not rows:
        print("📈 Verdicts: (none yet — first verdicts arrive 7d post-ship)")
        print()
        return

    print("📈 Recent verdicts (last 5)")
    for r in rows:
        v = r["verdict"] or "?"
        emoji = {"win": "🟢", "partial": "🟡", "lose": "🔴",
                 "inconclusive": "⚪"}.get(v, "•")
        print(f"   {emoji} {r['id']:<22}  {v:<13}  {fmt_money(r['d'])}")
    print()


def section_calibration(c) -> None:
    try:
        rows = c.execute("""
            SELECT
              CASE
                WHEN claude_confidence_pct >= 80 THEN '80-100%'
                WHEN claude_confidence_pct >= 60 THEN '60-79% '
                WHEN claude_confidence_pct >= 40 THEN '40-59% '
                ELSE '<40%   '
              END AS bucket,
              COUNT(*) AS n,
              SUM(CASE WHEN was_correct = 1 THEN 1 ELSE 0 END) AS hits
            FROM claude_calibration
            WHERE actual_outcome IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket DESC
        """).fetchall()
    except sqlite3.OperationalError:
        return

    if not rows:
        print("🎲 Calibration: (no completed hypotheses yet)")
        print()
        return

    print("🎲 Calibration so far (Claude's confidence vs reality)")
    for r in rows:
        rate = (r["hits"] / r["n"] * 100) if r["n"] else 0
        bar_full = int(rate / 10)
        bar = "█" * bar_full + "░" * (10 - bar_full)
        print(f"   {r['bucket']}  {r['hits']}/{r['n']}  {bar}  {rate:.0f}%")
    print()


# ────────────────────────────────────────────────────────────────────
# entry
# ────────────────────────────────────────────────────────────────────

def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1
    try:
        c = conn()
    except sqlite3.OperationalError as e:
        print(f"DB open failed: {e}", file=sys.stderr)
        return 1

    try:
        section_header()
        section_health(c)
        section_pnl(c)
        section_open_book(c)
        section_recent_rebals(c)
        section_recent_settles(c)
        section_pending_hypotheses(c)
        section_recent_verdicts(c)
        section_calibration(c)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
