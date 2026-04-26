"""SQLite persistence for weather forecasts, observations, and calibration data."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from polymarket_strat.domain.weather.models import (
    DistributionFamily,
    ErrorDistribution,
    ForecastError,
    StationObservation,
    SynopticRegime,
    TemperatureForecast,
    WeatherModel,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    model TEXT NOT NULL,
    init_time TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    lead_hours INTEGER NOT NULL,
    forecast_high_f REAL NOT NULL,
    ensemble_spread_f REAL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(city, model, init_time, valid_time)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    station_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    observed_high_f REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'IEM',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(city, obs_date)
);

CREATE TABLE IF NOT EXISTS forecast_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    model TEXT NOT NULL,
    regime TEXT NOT NULL,
    lead_hours INTEGER NOT NULL,
    error_f REAL NOT NULL,
    obs_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(city, model, obs_date, lead_hours)
);

CREATE TABLE IF NOT EXISTS error_distributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    model TEXT NOT NULL,
    regime TEXT NOT NULL,
    lead_hours INTEGER NOT NULL,
    family TEXT NOT NULL,
    mu REAL NOT NULL,
    sigma REAL NOT NULL,
    shape REAL DEFAULT 0.0,
    nu REAL DEFAULT 30.0,
    n_samples INTEGER NOT NULL,
    fitted_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(city, model, regime, lead_hours)
);

CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    bracket_lower_f REAL NOT NULL,
    bracket_upper_f REAL NOT NULL,
    model_prob REAL NOT NULL,
    market_prob REAL NOT NULL,
    edge REAL NOT NULL,
    kelly_fraction REAL NOT NULL,
    notional REAL NOT NULL,
    entry_price REAL NOT NULL DEFAULT 0.0,
    side TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    market_id TEXT,
    token_id TEXT,
    question TEXT,
    regime TEXT,
    outcome INTEGER,
    pnl REAL,
    expected_pnl REAL,
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_forecasts_city_date ON forecasts(city, valid_time);
CREATE INDEX IF NOT EXISTS idx_observations_city_date ON observations(city, obs_date);
CREATE INDEX IF NOT EXISTS idx_errors_lookup ON forecast_errors(city, model, regime, lead_hours);
CREATE INDEX IF NOT EXISTS idx_distributions_lookup ON error_distributions(city, model, regime, lead_hours);
CREATE INDEX IF NOT EXISTS idx_trades_city_date ON trade_history(city, target_date);

-- market_prices: per-tick MTM snapshot for each open position token.
-- Written by _snapshot_open_position_prices at the top of run_autotrade
-- (after settlement + rebalance), consumed read-only by the dashboard.
-- PK is (token_id, fetched_at_utc) so multiple snapshots per day coexist;
-- dashboard joins the latest row per token via ROW_NUMBER window.
CREATE TABLE IF NOT EXISTS market_prices (
    token_id            TEXT NOT NULL,
    market_id           TEXT,
    best_bid            REAL,
    best_ask            REAL,
    mid_price           REAL,
    bid_size            REAL,
    ask_size            REAL,
    outcome_prices_json TEXT,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (token_id, fetched_at_utc)
);
CREATE INDEX IF NOT EXISTS idx_market_prices_latest
    ON market_prices (token_id, fetched_at_utc DESC);

-- token_cooldown: post-exit re-entry blocklist. When run_rebalance closes
-- a position, it inserts (token_id, closed_at_utc, cooldown_until_utc).
-- analyze()'s duplicate-token guard unions this table's active rows into
-- already_open_token_ids so the same token can't be re-entered during the
-- window (default 6h, aligned with GFS 00/06/12/18Z refresh cadence).
CREATE TABLE IF NOT EXISTS token_cooldown (
    token_id       TEXT PRIMARY KEY,
    closed_at      TEXT NOT NULL,
    cooldown_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cooldown_until
    ON token_cooldown (cooldown_until);
"""


class WeatherDatabase:
    def __init__(self, db_path: Path | str = "data/weather/weather.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # WAL mode: allows concurrent readers while a writer is active,
        # reduces `database is locked` errors, persistent across connections.
        # synchronous=NORMAL pairs safely with WAL (OS-level crash-safe, not torn-write safe).
        # busy_timeout prevents immediate errors if another process briefly holds a lock.
        #
        # WAL is unsupported on some filesystems (network drives, certain Docker
        # overlay FS, tmpfs in sandboxes). Fall back to DELETE journal silently
        # so the app still boots — we keep busy_timeout either way.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass  # filesystem doesn't support WAL; default journal is fine
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def backup(self, dest_path: Path | str) -> None:
        """Online backup to dest_path. Safe while writers are active (uses SQLite backup API)."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(dest_path)) as dest:
            self._conn.backup(dest)

    def _migrate(self) -> None:
        """Add columns introduced after initial schema creation."""
        new_cols = [
            ("trade_history", "entry_price",  "REAL NOT NULL DEFAULT 0.0"),
            ("trade_history", "mode",         "TEXT NOT NULL DEFAULT 'paper'"),
            ("trade_history", "market_id",    "TEXT"),
            ("trade_history", "token_id",     "TEXT"),
            ("trade_history", "question",     "TEXT"),
            ("trade_history", "settled_at",   "TEXT"),
            # expected_pnl: model-predicted EV at entry, immutable after save.
            # Nullable so legacy pre-Apr-22-2026 rows remain valid without backfill.
            ("trade_history", "expected_pnl", "REAL"),
            # entry_edge: fee-adjusted edge at the moment the trade was
            # placed. run_rebalance compares current_edge to this to decide
            # whether to exit. Nullable for legacy rows (rebalance treats
            # those rows as "hold" by default — can't drop from an unknown
            # baseline).
            ("trade_history", "entry_edge",   "REAL"),
            # forecast_content_hash: fingerprint of the (model, forecast_f)
            # tuples that produced the entry edge. run_rebalance hashes the
            # current forecast the same way and applies the dual threshold:
            #   - hash unchanged → market-only move, tolerate -15%
            #   - hash changed   → fresh p_model, cut harder at -10%
            ("trade_history", "forecast_content_hash", "TEXT"),
            # Apr 24 2026 (Citadel fix #5) — token_side for buy-NO support.
            # Values: "YES" (we hold the YES token) or "NO" (we hold the NO
            # token). Default "YES" for backward compat — every pre-Apr-24
            # trade was YES-only. Settlement pnl math flips on this: for NO
            # positions, outcome=1 (YES resolves true) is a LOSS, outcome=0
            # (NO resolves true) is a WIN.
            ("trade_history", "token_side",  "TEXT NOT NULL DEFAULT 'YES'"),
            # Apr 24 2026 — comprehensive data recording expansion. Every
            # field below is nullable so legacy rows remain valid without
            # backfill. All are populated at entry time by run_execute,
            # except `observed_high_f`, `exit_reason`, and `forecast_error_f`
            # which are populated at settlement / rebalance-exit time.
            #
            # Purpose: enable full posthoc analysis of every trading decision.
            # For ANY past trade we should be able to answer:
            #   - What was the weather forecast at entry? (per-model)
            #   - What did the actual observation turn out to be?
            #   - Was our probability calibrated? (raw vs isotonic-adjusted)
            #   - Which model run (GFS / ECMWF init_time) did we trade on?
            #   - Why did we exit if we did? (profit_take / edge_drop / etc.)
            #   - How much reliability shrinkage applied at sizing?
            # Without these fields, bleed-source attribution requires
            # cross-joining multiple tables and inferring causality.
            ("trade_history", "observed_high_f",       "REAL"),  # settlement
            ("trade_history", "forecast_high_f_gfs",   "REAL"),  # entry
            ("trade_history", "forecast_high_f_ecmwf", "REAL"),  # entry
            ("trade_history", "ensemble_spread_f",     "REAL"),  # entry
            ("trade_history", "model_prob_raw",        "REAL"),  # pre-isotonic
            ("trade_history", "reliability_multiplier", "REAL"), # sizing shrink
            ("trade_history", "init_time_gfs",         "TEXT"),  # ISO8601
            ("trade_history", "init_time_ecmwf",       "TEXT"),  # ISO8601
            ("trade_history", "season",                "INTEGER"), # 0-3
            ("trade_history", "exit_reason",           "TEXT"),  # rebalance exits
            ("trade_history", "forecast_error_f",      "REAL"),  # settle: fcst - obs
            # Apr 26 2026 — strategy provenance. Without this, downstream
            # analytics + the rebalance skip-list cannot tell tail-NO
            # (buy-and-hold) trades apart from mainstream rebalanceable
            # trades. NULL on legacy rows = "mainstream/unknown" — handled
            # in the rebalance loop as the default rebalanceable case.
            ("trade_history", "category",              "TEXT"),  # weather_tail_no, etc.
            ("trade_history", "strategy_name",         "TEXT"),  # weather_tail_no, etc.
            # Apr 24 2026 — season key for error distributions. -1 = pooled
            # (legacy rows pre-migration), 0-3 = meteorological quarter.
            # Both tables get the column so the fitter can group and the
            # backfill can populate from obs_date.
            ("forecast_errors",     "season", "INTEGER NOT NULL DEFAULT -1"),
            ("error_distributions", "season", "INTEGER NOT NULL DEFAULT -1"),
        ]
        for table, col, typedef in new_cols:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

        # Apr 24 2026 — fix UNIQUE constraint on error_distributions to
        # include season. Pre-fix schema was
        #   UNIQUE(city, model, regime, lead_hours)
        # which means per-season INSERT OR REPLACE fits overwrite each
        # other + the pooled fit — we only keep whichever side got saved
        # last. Need
        #   UNIQUE(city, model, regime, lead_hours, season)
        # so pooled (-1) and per-season (0..3) coexist.
        #
        # SQLite doesn't support ALTER TABLE ... DROP CONSTRAINT. The
        # canonical fix: CREATE new table with correct schema, INSERT
        # old rows (keeping only the pooled version for duplicate leads,
        # since those are the only "safe" fits pre-migration), DROP old,
        # RENAME. Idempotent via sqlite_master introspection.
        existing_idx = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='sqlite_autoindex_error_distributions_1'"
        ).fetchone()
        # The old UNIQUE auto-creates an autoindex. If the auto-index SQL
        # mentions `season`, we're already migrated; otherwise migrate.
        needs_migrate = True
        if existing_idx and existing_idx[0] and "season" in (existing_idx[0] or ""):
            needs_migrate = False

        if needs_migrate:
            try:
                # Check the CREATE TABLE sql itself — more reliable than
                # auto-index which may be null in some SQLite versions.
                table_sql = self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='error_distributions'"
                ).fetchone()
                if table_sql and "UNIQUE(city, model, regime, lead_hours, season)" in (table_sql[0] or ""):
                    needs_migrate = False
            except Exception:
                pass

        if needs_migrate:
            try:
                self._conn.executescript(
                    """
                    BEGIN;
                    CREATE TABLE error_distributions_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        city TEXT NOT NULL,
                        model TEXT NOT NULL,
                        regime TEXT NOT NULL,
                        lead_hours INTEGER NOT NULL,
                        family TEXT NOT NULL,
                        mu REAL NOT NULL,
                        sigma REAL NOT NULL,
                        shape REAL DEFAULT 0.0,
                        nu REAL DEFAULT 30.0,
                        n_samples INTEGER NOT NULL,
                        fitted_at TEXT NOT NULL DEFAULT (datetime('now')),
                        season INTEGER NOT NULL DEFAULT -1,
                        UNIQUE(city, model, regime, lead_hours, season)
                    );
                    INSERT INTO error_distributions_new
                      (id, city, model, regime, lead_hours, family, mu, sigma,
                       shape, nu, n_samples, fitted_at, season)
                    SELECT id, city, model, regime, lead_hours, family, mu, sigma,
                           shape, nu, n_samples, fitted_at, season
                    FROM error_distributions;
                    DROP TABLE error_distributions;
                    ALTER TABLE error_distributions_new RENAME TO error_distributions;
                    CREATE INDEX idx_distributions_lookup
                      ON error_distributions(city, model, regime, lead_hours);
                    CREATE INDEX idx_distributions_season_lookup
                      ON error_distributions(city, model, regime, lead_hours, season);
                    COMMIT;
                    """
                )
            except sqlite3.Error as exc:
                # If migration fails, don't leave the DB in a half-state.
                # Rolled back by the BEGIN/COMMIT semantics; re-raise so
                # the caller knows something's off.
                self._conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"Failed to migrate error_distributions UNIQUE constraint: {exc}"
                )

    def close(self) -> None:
        self._conn.close()

    # -- forecasts ----------------------------------------------------------

    def save_forecast(self, fc: TemperatureForecast) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO forecasts
               (city, model, init_time, valid_time, lead_hours, forecast_high_f, ensemble_spread_f)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fc.city, fc.model.value, fc.init_time.isoformat(), fc.valid_time.isoformat(),
             fc.lead_hours, fc.forecast_high_f, fc.ensemble_spread_f),
        )
        self._conn.commit()

    def get_forecasts(self, city: str, *, model: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if model:
            rows = self._conn.execute(
                "SELECT * FROM forecasts WHERE city = ? AND model = ? ORDER BY valid_time DESC LIMIT ?",
                (city, model, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM forecasts WHERE city = ? ORDER BY valid_time DESC LIMIT ?",
                (city, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- observations -------------------------------------------------------

    def save_observation(self, obs: StationObservation) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO observations
               (city, station_id, obs_date, observed_high_f, source)
               VALUES (?, ?, ?, ?, ?)""",
            (obs.city, obs.station_id, obs.obs_date.isoformat(), obs.observed_high_f, obs.source),
        )
        self._conn.commit()

    def get_observation(self, city: str, obs_date: date) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM observations WHERE city = ? AND obs_date = ?",
            (city, obs_date.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    def get_observations(self, city: str, *, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM observations WHERE city = ?"
        params: list[Any] = [city]
        if start:
            query += " AND obs_date >= ?"
            params.append(start.isoformat())
        if end:
            query += " AND obs_date <= ?"
            params.append(end.isoformat())
        query += " ORDER BY obs_date"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    # -- forecast errors ----------------------------------------------------

    def save_forecast_error(self, err: ForecastError) -> None:
        # Derive season from err.season (if >=0) or from (obs_date, city)
        # via the city-aware schedule. Tropical/arid cities return 0 or 1
        # only; temperate return 0-3. Keeps the interface compatible with
        # legacy callers that don't set `season` on ForecastError.
        from polymarket_strat.domain.weather.season import season_from_date
        season = err.season if err.season in (0, 1, 2, 3) else season_from_date(err.obs_date, err.city)
        self._conn.execute(
            """INSERT OR REPLACE INTO forecast_errors
               (city, model, regime, lead_hours, error_f, obs_date, season)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (err.city, err.model.value, err.regime.value, err.lead_hours,
             err.error_f, err.obs_date.isoformat(), season),
        )
        self._conn.commit()

    def get_forecast_errors(
        self,
        city: str,
        model: WeatherModel,
        regime: SynopticRegime,
        lead_hours: int,
        season: int | None = None,
    ) -> list[float]:
        """Return historical (forecast - observed) errors matching the key.

        Apr 24 2026 — when `season` is provided (0-3), returns ONLY errors
        observed during that meteorological quarter. When None (default),
        returns all errors regardless of season (pooled / legacy behavior).
        """
        if season is None:
            rows = self._conn.execute(
                "SELECT error_f FROM forecast_errors "
                "WHERE city = ? AND model = ? AND regime = ? AND lead_hours = ?",
                (city, model.value, regime.value, lead_hours),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT error_f FROM forecast_errors "
                "WHERE city = ? AND model = ? AND regime = ? AND lead_hours = ? "
                "AND season = ?",
                (city, model.value, regime.value, lead_hours, season),
            ).fetchall()
        return [r["error_f"] for r in rows]

    # -- error distributions ------------------------------------------------

    def delete_distributions_for_models(self, city: str, models: list[WeatherModel]) -> None:
        """Remove error distributions for the given city/model combos.

        Used to purge HRRR/NAM entries that were generated from duplicate
        gfs_seamless archive data in earlier calibration runs.
        """
        for model in models:
            self._conn.execute(
                "DELETE FROM error_distributions WHERE city = ? AND model = ?",
                (city, model.value),
            )
        self._conn.commit()

    def save_error_distribution(self, dist: ErrorDistribution) -> None:
        # season=-1 means "pooled" (legacy behavior). season ∈ {0..3} means
        # bucket per meteorological quarter. INSERT OR REPLACE keys on the
        # full composite (city, model, regime, lead_hours, season) so
        # pooled and per-season fits coexist rather than overwriting.
        season = dist.season if dist.season in (0, 1, 2, 3) else -1
        self._conn.execute(
            """INSERT OR REPLACE INTO error_distributions
               (city, model, regime, lead_hours, family, mu, sigma, shape, nu, n_samples, season)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dist.city, dist.model.value, dist.regime.value, dist.lead_hours,
             dist.family.value, dist.mu, dist.sigma, dist.shape, dist.nu,
             dist.n_samples, season),
        )
        self._conn.commit()

    def get_error_distribution(
        self,
        city: str,
        model: WeatherModel,
        regime: SynopticRegime,
        lead_hours: int,
        season: int | None = None,
    ) -> ErrorDistribution | None:
        """Fetch the fitted distribution matching the key.

        Apr 24 2026 — `season` optional. When provided (0-3), looks up the
        season-specific fit. When None (default), fetches the pooled
        (season=-1) fit — preserves legacy behavior for callers that
        haven't been updated yet.

        Inference at the strategy layer should now ALWAYS pass season
        explicitly (derived from target_date.month), with a fallback to
        the pooled fit when the per-season bucket lacks enough samples.
        """
        target_season = season if season is not None else -1
        row = self._conn.execute(
            """SELECT * FROM error_distributions
               WHERE city = ? AND model = ? AND regime = ? AND lead_hours = ?
               AND season = ?""",
            (city, model.value, regime.value, lead_hours, target_season),
        ).fetchone()
        if not row:
            return None
        return ErrorDistribution(
            city=row["city"],
            model=WeatherModel(row["model"]),
            regime=SynopticRegime(row["regime"]),
            lead_hours=row["lead_hours"],
            family=DistributionFamily(row["family"]),
            mu=row["mu"],
            sigma=row["sigma"],
            shape=row["shape"],
            nu=row["nu"],
            n_samples=row["n_samples"],
            season=row["season"] if "season" in row.keys() else -1,
        )

    # -- trade history ------------------------------------------------------

    def save_trade(
        self,
        *,
        city: str,
        target_date: date,
        bracket_lower_f: float,
        bracket_upper_f: float,
        model_prob: float,
        market_prob: float,
        edge: float,
        kelly_fraction: float,
        notional: float,
        entry_price: float = 0.0,
        side: str,
        mode: str = "paper",
        market_id: str | None = None,
        token_id: str | None = None,
        question: str | None = None,
        regime: str | None = None,
        expected_pnl: float | None = None,
        entry_edge: float | None = None,
        forecast_content_hash: str | None = None,
        token_side: str = "YES",
        # Apr 24 2026 — comprehensive entry-time diagnostics. All optional
        # so callers without the full context (tests, legacy paths) still
        # work. Values are stored for posthoc analysis only — they don't
        # affect trading behavior.
        forecast_high_f_gfs: float | None = None,
        forecast_high_f_ecmwf: float | None = None,
        ensemble_spread_f: float | None = None,
        model_prob_raw: float | None = None,
        reliability_multiplier: float | None = None,
        init_time_gfs: str | None = None,
        init_time_ecmwf: str | None = None,
        season: int | None = None,
        # Apr 26 2026 — strategy provenance for rebalance skip-list and
        # downstream analytics. Both default to None so legacy callers
        # remain working; rebalance treats NULL as "rebalanceable".
        category: str | None = None,
        strategy_name: str | None = None,
    ) -> int:
        # Token-side must be one of the two valid values — reject malformed
        # input early so we don't corrupt the schema with typos like "Yes"
        # or "NOs".
        if token_side not in ("YES", "NO"):
            raise ValueError(
                f"token_side must be 'YES' or 'NO', got {token_side!r}"
            )
        # Season validation — if provided, must be 0-3 (winter=0, spring=1,
        # summer=2, fall=3). Consistent with domain/weather/season.py.
        if season is not None and season not in (0, 1, 2, 3):
            raise ValueError(
                f"season must be 0-3 or None, got {season!r}"
            )
        cur = self._conn.execute(
            """INSERT INTO trade_history
               (city, target_date, bracket_lower_f, bracket_upper_f,
                model_prob, market_prob, edge, kelly_fraction, notional,
                entry_price, side, mode, market_id, token_id, question, regime,
                expected_pnl, entry_edge, forecast_content_hash, token_side,
                forecast_high_f_gfs, forecast_high_f_ecmwf, ensemble_spread_f,
                model_prob_raw, reliability_multiplier,
                init_time_gfs, init_time_ecmwf, season,
                category, strategy_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?)""",
            (city, target_date.isoformat(), bracket_lower_f, bracket_upper_f,
             model_prob, market_prob, edge, kelly_fraction, notional,
             entry_price, side, mode, market_id, token_id, question, regime,
             expected_pnl, entry_edge, forecast_content_hash, token_side,
             forecast_high_f_gfs, forecast_high_f_ecmwf, ensemble_spread_f,
             model_prob_raw, reliability_multiplier,
             init_time_gfs, init_time_ecmwf, season,
             category, strategy_name),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Return all paper/live trades that have not yet been settled."""
        rows = self._conn.execute(
            "SELECT * FROM trade_history WHERE outcome IS NULL ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def settle_trade(
        self,
        trade_id: int,
        *,
        outcome: int,
        pnl: float,
        observed_high_f: float | None = None,
        forecast_error_f: float | None = None,
    ) -> None:
        """Mark a trade as settled with its binary outcome (1=YES, 0=NO) and P&L.

        Apr 24 2026 — also records the actual observed high (from IEM or
        polymarket) and the forecast error (our ensemble prediction minus
        observed). Both nullable to keep pre-fix callers working.
        Enables posthoc "which forecast was off by how much" analysis.
        """
        self._conn.execute(
            """UPDATE trade_history
               SET outcome = ?, pnl = ?, settled_at = datetime('now'),
                   observed_high_f = COALESCE(?, observed_high_f),
                   forecast_error_f = COALESCE(?, forecast_error_f)
               WHERE id = ?""",
            (outcome, pnl, observed_high_f, forecast_error_f, trade_id),
        )
        self._conn.commit()

    def close_position_as_exit(
        self,
        trade_id: int,
        *,
        pnl: float,
        exit_price: float,
        settled_at: str | None = None,
        exit_reason: str | None = None,
    ) -> None:
        """Close a position via rebalance-driven exit (not settlement).

        Uses a sentinel `outcome = 2` to distinguish rebalance exits from
        Polymarket settlements (YES=1 / NO=0). The existing `outcome IS NULL`
        filter on open_positions still excludes these; downstream consumers
        (dashboard Recent Settlements, PnL aggregators) treat non-null
        outcome uniformly via the `pnl` column.

        Stores the effective exit price (best_bid at close) in
        `market_prob` would conflict with the entry value, so we repurpose
        `settled_at` for the timestamp and encode the exit price path
        via a dedicated log row in market_prices (see _snapshot logic).
        The `pnl` delta flows through the same column as a regular settle.
        """
        ts = settled_at or datetime.utcnow().isoformat() + "Z"
        # outcome=2 is the "exit" sentinel. Legacy readers that check
        # `outcome == 1` for WIN and `outcome == 0` for LOSS see this as
        # neither; any residual `outcome IS NOT NULL` filter (e.g. the
        # dashboard's settled view) picks it up as a closed row.
        #
        # Apr 24 2026 — `exit_reason` records which rebalance rule fired:
        # "profit_take", "edge_drop_stale_model", "edge_drop_fresh_model",
        # "breakeven_triggered". Nullable so pre-fix callers still work.
        self._conn.execute(
            """UPDATE trade_history
               SET outcome = 2, pnl = ?, settled_at = ?,
                   exit_reason = COALESCE(?, exit_reason)
               WHERE id = ?""",
            (pnl, ts, exit_reason, trade_id),
        )
        self._conn.commit()

    def get_trades(self, *, city: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if city:
            rows = self._conn.execute(
                "SELECT * FROM trade_history WHERE city = ? ORDER BY created_at DESC LIMIT ?",
                (city, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trade_history ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- market prices (per-tick MTM snapshots) -----------------------------

    def insert_market_price(
        self,
        *,
        token_id: str,
        market_id: str | None,
        best_bid: float | None,
        best_ask: float | None,
        mid_price: float | None,
        bid_size: float | None = None,
        ask_size: float | None = None,
        outcome_prices_json: str | None = None,
        fetched_at_utc: str | None = None,
    ) -> None:
        """Persist one per-token price snapshot.

        Called by `_snapshot_open_position_prices` at the top of
        run_autotrade (after settle + rebalance). Row is keyed on
        (token_id, fetched_at_utc) so multiple snapshots per token per day
        coexist — the dashboard joins the latest row per token via the
        ROW_NUMBER window, and analysts can reconstruct intraday paths if
        they want later.
        """
        ts = fetched_at_utc or (datetime.utcnow().isoformat() + "Z")
        self._conn.execute(
            """INSERT OR REPLACE INTO market_prices
               (token_id, market_id, best_bid, best_ask, mid_price,
                bid_size, ask_size, outcome_prices_json, fetched_at_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token_id, market_id, best_bid, best_ask, mid_price,
             bid_size, ask_size, outcome_prices_json, ts),
        )
        self._conn.commit()

    def get_latest_market_price(self, token_id: str) -> dict[str, Any] | None:
        """Return most recent snapshot for a single token, or None."""
        row = self._conn.execute(
            """SELECT * FROM market_prices
               WHERE token_id = ?
               ORDER BY fetched_at_utc DESC
               LIMIT 1""",
            (token_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- token cooldown (post-exit re-entry blocklist) ----------------------

    def insert_cooldown(
        self,
        *,
        token_id: str,
        closed_at: str,
        cooldown_until: str,
    ) -> None:
        """Register a post-exit cooldown on `token_id`.

        `INSERT OR REPLACE` so repeated exits on the same token extend the
        cooldown to the latest close's window rather than failing the
        PRIMARY KEY constraint.
        """
        self._conn.execute(
            """INSERT OR REPLACE INTO token_cooldown
               (token_id, closed_at, cooldown_until)
               VALUES (?, ?, ?)""",
            (token_id, closed_at, cooldown_until),
        )
        self._conn.commit()

    def get_cooldown_tokens(self, now_utc: str | None = None) -> set[str]:
        """Return the set of token_ids currently in active cooldown.

        `analyze()`'s duplicate-token guard unions this into
        `already_open_token_ids` so the scanner can't re-book a just-closed
        token while `now < cooldown_until`.
        """
        now = now_utc or (datetime.utcnow().isoformat() + "Z")
        rows = self._conn.execute(
            "SELECT token_id FROM token_cooldown WHERE cooldown_until > ?",
            (now,),
        ).fetchall()
        return {r["token_id"] for r in rows if r["token_id"]}
