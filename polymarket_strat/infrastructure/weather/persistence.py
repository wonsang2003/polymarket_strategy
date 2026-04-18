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
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_forecasts_city_date ON forecasts(city, valid_time);
CREATE INDEX IF NOT EXISTS idx_observations_city_date ON observations(city, obs_date);
CREATE INDEX IF NOT EXISTS idx_errors_lookup ON forecast_errors(city, model, regime, lead_hours);
CREATE INDEX IF NOT EXISTS idx_distributions_lookup ON error_distributions(city, model, regime, lead_hours);
CREATE INDEX IF NOT EXISTS idx_trades_city_date ON trade_history(city, target_date);
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
            ("trade_history", "entry_price", "REAL NOT NULL DEFAULT 0.0"),
            ("trade_history", "mode",        "TEXT NOT NULL DEFAULT 'paper'"),
            ("trade_history", "market_id",   "TEXT"),
            ("trade_history", "token_id",    "TEXT"),
            ("trade_history", "question",    "TEXT"),
            ("trade_history", "settled_at",  "TEXT"),
        ]
        for table, col, typedef in new_cols:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

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
        self._conn.execute(
            """INSERT OR REPLACE INTO forecast_errors
               (city, model, regime, lead_hours, error_f, obs_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (err.city, err.model.value, err.regime.value, err.lead_hours,
             err.error_f, err.obs_date.isoformat()),
        )
        self._conn.commit()

    def get_forecast_errors(
        self,
        city: str,
        model: WeatherModel,
        regime: SynopticRegime,
        lead_hours: int,
    ) -> list[float]:
        rows = self._conn.execute(
            "SELECT error_f FROM forecast_errors WHERE city = ? AND model = ? AND regime = ? AND lead_hours = ?",
            (city, model.value, regime.value, lead_hours),
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
        self._conn.execute(
            """INSERT OR REPLACE INTO error_distributions
               (city, model, regime, lead_hours, family, mu, sigma, shape, nu, n_samples)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dist.city, dist.model.value, dist.regime.value, dist.lead_hours,
             dist.family.value, dist.mu, dist.sigma, dist.shape, dist.nu, dist.n_samples),
        )
        self._conn.commit()

    def get_error_distribution(
        self,
        city: str,
        model: WeatherModel,
        regime: SynopticRegime,
        lead_hours: int,
    ) -> ErrorDistribution | None:
        row = self._conn.execute(
            """SELECT * FROM error_distributions
               WHERE city = ? AND model = ? AND regime = ? AND lead_hours = ?""",
            (city, model.value, regime.value, lead_hours),
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
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO trade_history
               (city, target_date, bracket_lower_f, bracket_upper_f,
                model_prob, market_prob, edge, kelly_fraction, notional,
                entry_price, side, mode, market_id, token_id, question, regime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (city, target_date.isoformat(), bracket_lower_f, bracket_upper_f,
             model_prob, market_prob, edge, kelly_fraction, notional,
             entry_price, side, mode, market_id, token_id, question, regime),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Return all paper/live trades that have not yet been settled."""
        rows = self._conn.execute(
            "SELECT * FROM trade_history WHERE outcome IS NULL ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def settle_trade(self, trade_id: int, *, outcome: int, pnl: float) -> None:
        """Mark a trade as settled with its binary outcome (1=YES, 0=NO) and P&L."""
        self._conn.execute(
            """UPDATE trade_history
               SET outcome = ?, pnl = ?, settled_at = datetime('now')
               WHERE id = ?""",
            (outcome, pnl, trade_id),
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
