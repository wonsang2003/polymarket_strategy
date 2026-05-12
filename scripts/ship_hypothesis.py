"""Mark a strategy hypothesis as shipped / rejected / reverted.

The closed-loop J-curve system needs a one-line action for the trader after
they manually apply Claude's proposed diff and commit. This script is that
action — captures the commit SHA, sets shipped_at, and the evaluator picks
it up automatically 7 days later.

USAGE:
    # Ship after applying R1 and committing locally:
    python scripts/ship_hypothesis.py H_2026_05_05_01

    # Auto-detect HEAD commit SHA (default):
    python scripts/ship_hypothesis.py H_2026_05_05_01 --auto-sha

    # Pass an explicit SHA (e.g., when you cherry-picked):
    python scripts/ship_hypothesis.py H_2026_05_05_01 --sha abc1234

    # Reject without shipping:
    python scripts/ship_hypothesis.py H_2026_05_05_01 --reject \
        --reason "post-mortem'd at desk, R2 contradicts active lesson"

    # Revert a previously-shipped hypothesis (post-deploy regret):
    python scripts/ship_hypothesis.py H_2026_05_05_01 --revert \
        --reason "+EV signal flipped after 3d, see daily_brief 05-08"

    # List proposed (un-actioned) hypotheses:
    python scripts/ship_hypothesis.py --list
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")
ROOT = Path("/home/ubuntu/polymarket")


def conn():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


def auto_sha() -> str:
    """Best-effort current HEAD commit SHA. Empty string if git absent."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()[:12]
    except Exception:
        return ""


def cmd_list() -> int:
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT id, proposed_at, hypothesis, confidence_pct, status
            FROM strategy_hypotheses
            WHERE status IN ('proposed', 'shipped')
            ORDER BY proposed_at DESC
            LIMIT 20
            """
        ).fetchall()
    finally:
        c.close()
    if not rows:
        print("(no proposed or shipped hypotheses)")
        return 0
    print(f"{'id':<24} {'proposed_at':<22} {'status':<10} conf  hypothesis")
    print("─" * 100)
    for r in rows:
        h = (r['hypothesis'] or '')[:55]
        print(f"{r['id']:<24} {r['proposed_at']:<22} {r['status']:<10} "
              f"{r['confidence_pct'] or 0:>3}%   {h}")
    return 0


def cmd_ship(hid: str, sha: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    c = conn()
    try:
        row = c.execute(
            "SELECT status FROM strategy_hypotheses WHERE id = ?", (hid,)
        ).fetchone()
        if not row:
            print(f"hypothesis not found: {hid}", file=sys.stderr)
            return 1
        if row["status"] not in ("proposed",):
            print(f"refusing — current status is '{row['status']}', not 'proposed'.\n"
                  f"  use --revert if you want to undo a prior ship.", file=sys.stderr)
            return 1
        c.execute(
            """
            UPDATE strategy_hypotheses
               SET status          = 'shipped',
                   user_decision   = 'YES',
                   user_decision_at= ?,
                   shipped_at      = ?,
                   ship_commit_sha = ?
             WHERE id = ?
            """,
            (now, now, sha, hid),
        )
        c.commit()
    finally:
        c.close()
    print(f"shipped: {hid}  commit={sha or '(no sha)'}  at {now}")
    print("→ evaluator will compute verdict 7d from now.")
    return 0


def cmd_reject(hid: str, reason: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    c = conn()
    try:
        row = c.execute(
            "SELECT status FROM strategy_hypotheses WHERE id = ?", (hid,)
        ).fetchone()
        if not row:
            print(f"hypothesis not found: {hid}", file=sys.stderr)
            return 1
        if row["status"] != "proposed":
            print(f"refusing — current status is '{row['status']}'", file=sys.stderr)
            return 1
        c.execute(
            """
            UPDATE strategy_hypotheses
               SET status          = 'rejected',
                   user_decision   = 'NO',
                   user_decision_at= ?,
                   actual_effect   = ?
             WHERE id = ?
            """,
            (now, f"REJECTED: {reason}", hid),
        )
        c.commit()
    finally:
        c.close()
    print(f"rejected: {hid}  reason: {reason}")
    return 0


def cmd_revert(hid: str, reason: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    c = conn()
    try:
        row = c.execute(
            "SELECT status FROM strategy_hypotheses WHERE id = ?", (hid,)
        ).fetchone()
        if not row:
            print(f"hypothesis not found: {hid}", file=sys.stderr)
            return 1
        if row["status"] != "shipped":
            print(f"refusing — can only revert shipped hypotheses, not '{row['status']}'",
                  file=sys.stderr)
            return 1
        c.execute(
            """
            UPDATE strategy_hypotheses
               SET status        = 'reverted',
                   reverted_at   = ?,
                   revert_reason = ?
             WHERE id = ?
            """,
            (now, reason, hid),
        )
        c.commit()
    finally:
        c.close()
    print(f"reverted: {hid}  reason: {reason}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("hid", nargs="?", help="hypothesis id (e.g. H_2026_05_05_01)")
    p.add_argument("--list", action="store_true",
                   help="list proposed + shipped hypotheses, then exit")
    p.add_argument("--reject", action="store_true",
                   help="reject this hypothesis instead of shipping")
    p.add_argument("--revert", action="store_true",
                   help="revert a previously-shipped hypothesis")
    p.add_argument("--sha", default=None,
                   help="explicit commit SHA (default: HEAD)")
    p.add_argument("--auto-sha", action="store_true",
                   help="(default behavior) detect HEAD commit SHA")
    p.add_argument("--reason", default="",
                   help="rationale for --reject or --revert")
    args = p.parse_args()

    if args.list:
        return cmd_list()
    if not args.hid:
        p.print_help()
        return 2

    if args.reject:
        if not args.reason:
            print("--reject requires --reason", file=sys.stderr)
            return 2
        return cmd_reject(args.hid, args.reason)

    if args.revert:
        if not args.reason:
            print("--revert requires --reason", file=sys.stderr)
            return 2
        return cmd_revert(args.hid, args.reason)

    sha = args.sha if args.sha else auto_sha()
    return cmd_ship(args.hid, sha)


if __name__ == "__main__":
    sys.exit(main())
