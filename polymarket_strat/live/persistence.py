"""Isolated SQLite store for live + shadow execution.

Separate DB file from ``data/weather/weather.db`` — the paper pipeline on EC2
never touches this file, and vice versa. This keeps the schema free to evolve
(slippage fields, post-fill verification columns) without ALTER TABLE risk on
the production DB that's actively being written to by cron.

One table: ``live_attempts``. Every call to ``LiveCoordinator.execute_buy``
persists exactly one row — filled or rejected, shadow or live. Rejected rows
are equally valuable for analytics (how often depth fails, how often spreads
blow out, etc.), so they get a full row too with ``filled=0``.

Settlement writes back ``outcome / pnl / settled_at`` onto the same row.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Mode + timing
    mode TEXT NOT NULL,                   -- 'shadow' or 'live'
    submitted_at TEXT NOT NULL,           -- ISO timestamp (UTC)

    -- Trading context (from strategy signal)
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    bracket_lower_f REAL,
    bracket_upper_f REAL,
    model_prob REAL,
    market_prob REAL,
    edge REAL,
    regime TEXT,
    question TEXT,
    market_id TEXT,
    token_id TEXT,
    side TEXT DEFAULT 'YES',

    -- Execution outcome
    filled INTEGER NOT NULL DEFAULT 0,    -- 0 or 1
    reason TEXT NOT NULL DEFAULT '',
    notional_requested REAL NOT NULL,

    -- Pre-submit orderbook snapshot
    quoted_best_ask REAL,
    quoted_best_bid REAL,
    quoted_spread REAL,
    book_age_s REAL,

    -- Planned submit
    limit_price REAL,
    shares_target REAL,
    expected_vwap REAL,
    expected_cost_usd REAL,
    expected_slippage_per_share REAL,
    depth_usd_within_limit REAL,
    depth_shares_within_limit REAL,

    -- Actual fill (NULL if not filled)
    order_id TEXT,
    actual_vwap REAL,
    actual_cost_usd REAL,
    actual_shares REAL,
    slippage_usd REAL,
    error_message TEXT,

    -- Settlement (written post-hoc)
    outcome INTEGER,
    pnl REAL,
    expected_pnl REAL,
    settled_at TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_live_attempts_token_open
    ON live_attempts(token_id, outcome);
CREATE INDEX IF NOT EXISTS idx_live_attempts_city_date
    ON live_attempts(city, target_date);
CREATE INDEX IF NOT EXISTS idx_live_attempts_filled_mode
    ON live_attempts(filled, mode);
CREATE INDEX IF NOT EXISTS idx_live_attempts_submitted
    ON live_attempts(submitted_at);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LiveDatabase:
    """Owns the live.db file. Safe to instantiate multiple times (WAL)."""

    def __init__(self, db_path: Path | str = "data/weather/live.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # Match the paper DB's pragmas for consistency.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        """Additive-only column migrations.

        Same pattern as infrastructure/weather/persistence.py::_migrate: list
        every column added after initial schema release, attempt ALTER TABLE,
        swallow OperationalError (column already exists).
        """
        new_cols: list[tuple[str, str, str]] = [
            # Add future columns here as the schema evolves.
        ]
        for table, col, typedef in new_cols:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    def backup(self, dest_path: Path | str) -> None:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(dest_path)) as dest:
            self._conn.backup(dest)

    # ------------------------------------------------------------------
    # Write: persist an execution attempt.
    # ------------------------------------------------------------------
    def save_attempt(
        self,
        *,
        # Signal context
        city: str,
        target_date: date | str,
        question: str | None = None,
        market_id: str | None = None,
        bracket_lower_f: float | None = None,
        bracket_upper_f: float | None = None,
        model_prob: float | None = None,
        market_prob: float | None = None,
        edge: float | None = None,
        regime: str | None = None,
        side: str = "YES",
        expected_pnl: float | None = None,
        # Execution result (flat dict from FillResult.to_persist_row())
        fill_row: dict[str, Any],
    ) -> int:
        """Insert a live_attempts row. Returns the row id."""
        target_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)

        self._conn.execute(
            """
            INSERT INTO live_attempts (
                mode, submitted_at, city, target_date,
                bracket_lower_f, bracket_upper_f, model_prob, market_prob, edge,
                regime, question, market_id, token_id, side,
                filled, reason, notional_requested,
                quoted_best_ask, quoted_best_bid, quoted_spread, book_age_s,
                limit_price, shares_target, expected_vwap, expected_cost_usd,
                expected_slippage_per_share, depth_usd_within_limit, depth_shares_within_limit,
                order_id, actual_vwap, actual_cost_usd, actual_shares, slippage_usd,
                error_message, expected_pnl
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?
            )
            """,
            (
                fill_row["mode"], _utcnow_iso(), city, target_str,
                bracket_lower_f, bracket_upper_f, model_prob, market_prob, edge,
                regime, question, market_id, fill_row["token_id"], side,
                fill_row["filled"], fill_row["reason"], fill_row["notional_requested"],
                fill_row["quoted_best_ask"], fill_row["quoted_best_bid"],
                fill_row["quoted_spread"], fill_row["book_age_s"],
                fill_row["limit_price"], fill_row["shares_target"],
                fill_row["expected_vwap"], fill_row["expected_cost_usd"],
                fill_row["expected_slippage_per_share"],
                fill_row["depth_usd_within_limit"], fill_row["depth_shares_within_limit"],
                fill_row["order_id"], fill_row["actual_vwap"],
                fill_row["actual_cost_usd"], fill_row["actual_shares"], fill_row["slippage_usd"],
                fill_row["error_message"], expected_pnl,
            ),
        )
        self._conn.commit()
        cursor = self._conn.execute("SELECT last_insert_rowid() AS id")
        return int(cursor.fetchone()["id"])

    # ------------------------------------------------------------------
    # Read helpers for the runner + dashboard.
    # ------------------------------------------------------------------
    def sum_open_live_notional(self) -> float:
        """Total USDC currently deployed in unsettled live fills.

        Used by the per-cycle cap check: if sum ≥ MAX_LIVE_OPEN_NOTIONAL_USD
        the runner refuses to open any more positions.
        """
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(actual_cost_usd), 0.0) AS total
            FROM live_attempts
            WHERE mode = 'live' AND filled = 1 AND outcome IS NULL
            """
        ).fetchone()
        return float(row["total"]) if row else 0.0

    def list_open_tokens(self, *, mode: str | None = None) -> list[str]:
        """Token IDs for filled positions that haven't been settled yet.

        The runner uses this to skip re-opens within the same day, matching
        the duplicate guard in the paper pipeline.
        """
        if mode is None:
            rows = self._conn.execute(
                "SELECT DISTINCT token_id FROM live_attempts WHERE filled = 1 AND outcome IS NULL"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT token_id FROM live_attempts "
                "WHERE mode = ? AND filled = 1 AND outcome IS NULL",
                (mode,),
            ).fetchall()
        return [r["token_id"] for r in rows if r["token_id"]]

    def get_open_positions(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        """Full row data for every filled, unsettled position."""
        query = "SELECT * FROM live_attempts WHERE filled = 1 AND outcome IS NULL"
        params: list[Any] = []
        if mode is not None:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY submitted_at"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def get_recent_attempts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM live_attempts ORDER BY submitted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_today_daily_pnl(self, *, mode: str = "live") -> float:
        """Sum of pnl for positions settled today (UTC).

        Used for the daily drawdown brake. Mirrors main.py::run_autotrade
        semantics.
        """
        today = date.today().isoformat()
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0.0) AS total
            FROM live_attempts
            WHERE mode = ? AND outcome IS NOT NULL
              AND date(settled_at) = ?
            """,
            (mode, today),
        ).fetchone()
        return float(row["total"]) if row else 0.0

    # ------------------------------------------------------------------
    # Settlement: update outcome + pnl on an open position.
    # ------------------------------------------------------------------
    def settle_attempt(
        self,
        *,
        row_id: int,
        outcome: int,
        pnl: float,
    ) -> None:
        self._conn.execute(
            """
            UPDATE live_attempts
            SET outcome = ?, pnl = ?, settled_at = ?
            WHERE id = ?
            """,
            (outcome, pnl, _utcnow_iso(), row_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Slippage analytics — used by the dashboard + weekly reports.
    # ------------------------------------------------------------------
    def slippage_summary(self, *, mode: str = "live") -> dict[str, float]:
        """Aggregate slippage stats for filled rows."""
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                COALESCE(AVG(slippage_usd), 0.0) AS avg_slip,
                COALESCE(SUM(slippage_usd), 0.0) AS total_slip,
                COALESCE(AVG(expected_slippage_per_share), 0.0) AS avg_expected_slip
            FROM live_attempts
            WHERE mode = ? AND filled = 1
            """,
            (mode,),
        ).fetchone()
        if not row:
            return {"n": 0, "avg_slip_usd": 0.0, "total_slip_usd": 0.0, "avg_expected_slip": 0.0}
        return {
            "n": int(row["n"]),
            "avg_slip_usd": float(row["avg_slip"]),
            "total_slip_usd": float(row["total_slip"]),
            "avg_expected_slip": float(row["avg_expected_slip"]),
        }

    def fill_rate(self, *, mode: str = "live") -> dict[str, float]:
        """Ratio of filled attempts to total attempts, with rejection breakdown."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(filled), 0) AS filled "
            "FROM live_attempts WHERE mode = ?",
            (mode,),
        ).fetchone()
        n = int(row["n"]) if row else 0
        filled = int(row["filled"]) if row else 0
        rate = filled / n if n > 0 else 0.0

        reason_rows = self._conn.execute(
            "SELECT reason, COUNT(*) AS c FROM live_attempts "
            "WHERE mode = ? AND filled = 0 GROUP BY reason ORDER BY c DESC",
            (mode,),
        ).fetchall()
        rejections = {str(r["reason"]): int(r["c"]) for r in reason_rows}
        return {"n": n, "filled": filled, "fill_rate": rate, "rejections": rejections}
