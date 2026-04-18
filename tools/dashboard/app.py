"""Streamlit dashboard for the Polymarket weather alpha system.

READ-ONLY view of weather.db + recent report/log files. Safe to run alongside
the live trading pipeline — opens SQLite with `mode=ro` so no write contention
with `autotrade` / `weather-calibrate`.

RUN LOCALLY
-----------
    streamlit run tools/dashboard/app.py

RUN REMOTELY (via Tailscale)
----------------------------
    # see tools/dashboard/README.md for full setup
    streamlit run tools/dashboard/app.py \
        --server.address 100.x.y.z \
        --server.port 8501 \
        --server.enableCORS false

    # then from your phone: http://your-mac-tailscale-name:8501

PANELS
------
1. KPI strip           — open positions, today P&L, total P&L, #signals today
2. Open positions      — trade_history WHERE outcome IS NULL
3. Recent settlements  — last 50 settled trades with per-trade PnL
4. Equity curve        — cumulative P&L over time
5. Calibration health  — error distributions per (city, model) with μ/σ/n
6. Forecast errors     — scatter of recent errors by city
7. Lag monitor         — if tools/lag_monitor/logs/events.jsonl exists
8. Walk-forward        — if walk-forward CSV exists
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import pandas as pd
    import streamlit as st
except ImportError as exc:
    sys.stderr.write(
        "Missing dashboard dependencies. Install with:\n"
        "    pip install streamlit pandas\n"
    )
    raise

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "data" / "weather" / "weather.db"
LAG_EVENTS = ROOT / "tools" / "lag_monitor" / "logs" / "events.jsonl"
WALKFWD_CSV = ROOT / "tools" / "walk_forward" / "last_run.csv"

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Polymarket Weather Alpha",
    page_icon="",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Data access (read-only; cached)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def ro_connection(path: Path) -> sqlite3.Connection:
    """Return a cached read-only SQLite connection.

    Opens the DB without `mode=ro` so SQLite can perform WAL recovery
    (required when the pipeline is writing concurrently), then enforces
    read-only at the engine level via `PRAGMA query_only = 1`. Using
    `mode=ro` on a WAL-journaled DB fails with "attempt to write a
    readonly database" on first SELECT because WAL recovery itself needs
    write access to the -wal / -shm sidecar files.
    """
    uri = f"file:{path}"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


@st.cache_data(ttl=60, show_spinner=False)
def fetch_df(_conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, _conn, params=params)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Polymarket Weather Alpha — Dashboard")
st.caption(f"DB: `{DB_PATH}` — read-only. Data cached 60s.")

if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. Run calibration first.")
    st.stop()

conn = ro_connection(DB_PATH)

# ---------------------------------------------------------------------------
# KPI strip
# ---------------------------------------------------------------------------

today = date.today().isoformat()
open_trades = fetch_df(conn, "SELECT * FROM trade_history WHERE outcome IS NULL")
settled = fetch_df(conn, "SELECT * FROM trade_history WHERE outcome IS NOT NULL ORDER BY settled_at DESC")
settled_today = settled[settled["settled_at"].fillna("").str.startswith(today)] if not settled.empty else settled
signals_today = fetch_df(conn, "SELECT * FROM trade_history WHERE date(created_at) = ?", (today,))

total_pnl = float(settled["pnl"].sum()) if not settled.empty else 0.0
pnl_today = float(settled_today["pnl"].sum()) if not settled_today.empty else 0.0
open_notional = float(open_trades["notional"].sum()) if not open_trades.empty else 0.0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Open positions", len(open_trades))
c2.metric("Open notional $", f"{open_notional:,.2f}")
c3.metric("Cumulative PnL $", f"{total_pnl:+.2f}")
c4.metric("PnL today $", f"{pnl_today:+.2f}")
c5.metric("Signals today", len(signals_today))

# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------

st.header("Open positions")
if open_trades.empty:
    st.info("No open positions.")
else:
    display_cols = [
        "city", "target_date", "question", "side", "entry_price",
        "notional", "model_prob", "market_prob", "edge", "mode", "created_at",
    ]
    st.dataframe(
        open_trades[[c for c in display_cols if c in open_trades.columns]],
        use_container_width=True,
        hide_index=True,
    )

# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

st.header("Equity curve (settled trades)")
if settled.empty:
    st.info("No settled trades yet.")
else:
    eq = settled.copy()
    eq["settled_at"] = pd.to_datetime(eq["settled_at"], errors="coerce")
    eq = eq.dropna(subset=["settled_at"]).sort_values("settled_at")
    eq["cum_pnl"] = eq["pnl"].cumsum()
    chart_df = eq.set_index("settled_at")[["cum_pnl"]]
    st.line_chart(chart_df, height=260)

    # Win-rate summary
    n = len(eq)
    wins = int((eq["pnl"] > 0).sum())
    wr = wins / n if n else 0.0
    avg_win = float(eq.loc[eq["pnl"] > 0, "pnl"].mean()) if wins else 0.0
    avg_loss = float(eq.loc[eq["pnl"] <= 0, "pnl"].mean()) if (n - wins) > 0 else 0.0
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Trades", n)
    k2.metric("Win rate", f"{wr * 100:.1f}%")
    k3.metric("Avg win $", f"{avg_win:+.2f}")
    k4.metric("Avg loss $", f"{avg_loss:+.2f}")

# ---------------------------------------------------------------------------
# Recent settlements
# ---------------------------------------------------------------------------

st.header("Recent settlements")
if not settled.empty:
    show_cols = [
        "settled_at", "city", "target_date", "question",
        "side", "entry_price", "notional", "outcome", "pnl", "mode",
    ]
    st.dataframe(
        settled[[c for c in show_cols if c in settled.columns]].head(50),
        use_container_width=True,
        hide_index=True,
    )

# ---------------------------------------------------------------------------
# Calibration health
# ---------------------------------------------------------------------------

st.header("Calibration health — error distributions")
dists = fetch_df(
    conn,
    """SELECT city, model, regime, lead_hours, family, mu, sigma, shape, nu, n_samples
       FROM error_distributions ORDER BY city, model, lead_hours""",
)

if dists.empty:
    st.info("No fitted distributions. Run `polymarket-strat weather-calibrate`.")
else:
    col_a, col_b = st.columns([2, 1])
    with col_a:
        st.dataframe(dists, use_container_width=True, hide_index=True)
    with col_b:
        # Flag outliers: |mu| > 5 or sigma > 5 — these are the distributions
        # strategy.py filters out at inference
        bad = dists[(dists["mu"].abs() > 5) | (dists["sigma"] > 5)]
        if bad.empty:
            st.success(f"All {len(dists)} distributions look clean (|μ| ≤ 5, σ ≤ 5).")
        else:
            st.warning(f"{len(bad)} distribution(s) flagged as outliers — excluded at inference.")
            st.dataframe(bad[["city", "model", "lead_hours", "mu", "sigma", "n_samples"]],
                         use_container_width=True, hide_index=True)

    st.caption(
        "σ_floor = 2.5°F (see CLAUDE.md §5.2). Distributions with σ below the "
        "floor are clamped before bracket CDF evaluation."
    )

# ---------------------------------------------------------------------------
# Recent forecast errors
# ---------------------------------------------------------------------------

st.header("Recent forecast errors (last 60 days)")
since = (date.today() - timedelta(days=60)).isoformat()
errs = fetch_df(
    conn,
    """SELECT city, model, obs_date, error_f FROM forecast_errors
       WHERE obs_date >= ? ORDER BY obs_date""",
    (since,),
)
if errs.empty:
    st.info("No forecast errors in the last 60 days.")
else:
    errs["obs_date"] = pd.to_datetime(errs["obs_date"])
    cities_all = sorted(errs["city"].unique().tolist())
    pick = st.multiselect("Cities", cities_all, default=cities_all[:4])
    view = errs[errs["city"].isin(pick)] if pick else errs
    # Pivot for line-per-city chart
    if not view.empty:
        pivot = view.pivot_table(
            index="obs_date", columns="city", values="error_f", aggfunc="mean",
        )
        st.line_chart(pivot, height=300)

# ---------------------------------------------------------------------------
# Lag monitor (optional)
# ---------------------------------------------------------------------------

if LAG_EVENTS.exists():
    st.header("Lag monitor — recent events")
    try:
        rows = []
        with open(LAG_EVENTS, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if rows:
            lag_df = pd.DataFrame(rows).tail(500)
            summary = lag_df["kind"].value_counts().reset_index()
            summary.columns = ["event", "count"]
            c_l, c_r = st.columns([1, 2])
            with c_l:
                st.dataframe(summary, use_container_width=True, hide_index=True)
            with c_r:
                st.caption(f"Last 500 of {len(rows)} events from {LAG_EVENTS.name}")
                st.dataframe(lag_df.tail(50), use_container_width=True, hide_index=True)
    except Exception as exc:
        st.warning(f"Could not parse lag events: {exc}")

# ---------------------------------------------------------------------------
# Walk-forward results (optional)
# ---------------------------------------------------------------------------

if WALKFWD_CSV.exists():
    st.header("Walk-forward calibration")
    try:
        wf = pd.read_csv(WALKFWD_CSV)
        if not wf.empty:
            agg = (wf.groupby(["city", "model", "bracket"])
                     .agg(n=("brier", "count"),
                          brier=("brier", "mean"),
                          log_loss=("log_loss", "mean"),
                          base_rate=("outcome", "mean"),
                          mean_pred=("predicted_prob", "mean"))
                     .reset_index())
            st.dataframe(agg, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.warning(f"Could not load walk-forward CSV: {exc}")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Read-only dashboard. For execution, use the CLI: "
    "`polymarket-strat autotrade / positions / settle --auto`. "
    "Rendered {}.".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
)
