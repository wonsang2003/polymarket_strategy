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

# Apr 25 2026 (LATE) — auto-refresh every 30s so live trades show up
# without manual F5. Soft-fails to a meta-refresh tag if the package
# isn't installed (cleaner UX than a hard ImportError).
try:
    from streamlit_autorefresh import st_autorefresh  # type: ignore
    _AUTO_REFRESH_AVAILABLE = True
except ImportError:
    _AUTO_REFRESH_AVAILABLE = False

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

# Auto-refresh wiring — invalidates st.cache_data and re-runs the script
# every 10s so new trades appear without F5. The component returns a
# count of refreshes; we don't use it but the call must happen.
#
# Apr 27 2026 — interval shortened from 30s to 10s after a "dashboard not
# updating" report. With cron writes happening at top of hour and tail-NO
# placements firing within the same minute, 30s could leave the user
# staring at 30-90s-old data depending on the auto-refresh phase. 10s
# means worst-case staleness ≤ 15s (10s refresh + 5s cache TTL).
_REFRESH_INTERVAL_MS = 10_000  # 10 seconds
if _AUTO_REFRESH_AVAILABLE:
    st_autorefresh(interval=_REFRESH_INTERVAL_MS, key="dashboard_autorefresh")
else:
    # Fallback: HTML meta-refresh tag forces full page reload. Less elegant
    # than st_autorefresh (full page reload vs targeted rerun) but works
    # without a dependency.
    st.markdown(
        f'<meta http-equiv="refresh" content="{_REFRESH_INTERVAL_MS // 1000}">',
        unsafe_allow_html=True,
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


@st.cache_data(ttl=5, show_spinner=False)
def fetch_df(_conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    # Apr 27 2026 — ttl=5 (was 15). With auto-refresh at 10s the cache only
    # serves the queries within ONE rerun (deduplicating identical reads
    # within a single render). Next rerun always re-fetches.
    return pd.read_sql_query(sql, _conn, params=params)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Polymarket Weather Alpha — Dashboard")

if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. Run calibration first.")
    st.stop()

conn = ro_connection(DB_PATH)

# Apr 27 2026 — explicit freshness header. Tells the user three things at a
# glance:
#   - When this page rendered (browser-perspective)
#   - When the DB was last written to (pipeline-perspective)
#   - A manual "Refresh now" button that forces a full cache+rerun cycle
#
# Why this matters: the auto-refresh + cache TTL combo can leave up to 15s
# of staleness, and Chrome throttles backgrounded tabs (auto-refresh stops
# firing entirely). Without an explicit freshness indicator, the user
# can't distinguish "dashboard is stuck" from "no new trades to show".
from datetime import timezone as _tz
_now_utc = datetime.now(_tz.utc)
try:
    _latest_create = pd.to_datetime(
        conn.execute("SELECT MAX(created_at) FROM trade_history").fetchone()[0],
        utc=True, errors="coerce",
    )
    _latest_settle = pd.to_datetime(
        conn.execute(
            "SELECT MAX(settled_at) FROM trade_history WHERE settled_at IS NOT NULL"
        ).fetchone()[0],
        utc=True, errors="coerce",
    )
except Exception:
    _latest_create = pd.NaT
    _latest_settle = pd.NaT

def _ago(ts) -> str:
    if pd.isna(ts):
        return "—"
    delta_s = (_now_utc - ts.to_pydatetime()).total_seconds()
    if delta_s < 60:
        return f"{int(delta_s)}s ago"
    if delta_s < 3600:
        return f"{int(delta_s/60)}m ago"
    if delta_s < 86400:
        return f"{int(delta_s/3600)}h ago"
    return f"{int(delta_s/86400)}d ago"

_h_cols = st.columns([3, 3, 3, 1])
with _h_cols[0]:
    st.caption(f"DB: `{DB_PATH.name}` — read-only")
with _h_cols[1]:
    st.caption(f"Last DB write: **{_ago(_latest_create)}** (created), "
               f"**{_ago(_latest_settle)}** (settled)")
with _h_cols[2]:
    st.caption(
        f"Rendered: {_now_utc.strftime('%H:%M:%S UTC')} • "
        f"auto-refresh every {_REFRESH_INTERVAL_MS // 1000}s"
    )
with _h_cols[3]:
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

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
    # Join the latest market_prices row per token_id. Uses a correlated
    # subquery to grab the MAX(fetched_at_utc) snapshot per token and
    # left-joins so trades without a snapshot (first-tick, book fetch
    # failure) still render — they just have blank MTM columns.
    mtm_sql = """
        SELECT
            t.id, t.token_id, t.city, t.target_date, t.question, t.side,
            t.entry_price, t.notional, t.model_prob, t.market_prob, t.edge,
            t.entry_edge, t.mode, t.created_at,
            mp.best_bid  AS current_price,
            mp.best_ask,
            mp.mid_price,
            mp.fetched_at_utc
        FROM trade_history t
        LEFT JOIN (
            SELECT mp1.*
            FROM market_prices mp1
            JOIN (
                SELECT token_id, MAX(fetched_at_utc) AS mx
                FROM market_prices
                GROUP BY token_id
            ) latest
              ON latest.token_id = mp1.token_id
             AND latest.mx = mp1.fetched_at_utc
        ) mp ON mp.token_id = t.token_id
        WHERE t.outcome IS NULL
        ORDER BY t.created_at DESC
    """
    try:
        open_mtm = fetch_df(conn, mtm_sql)
    except Exception as exc:
        # market_prices table didn't exist on pre-Apr-24 DBs — fall back
        # to the plain open-positions view so the dashboard still loads
        # on legacy copies.
        st.warning(f"MTM join failed ({exc}); showing legacy view.")
        open_mtm = open_trades.copy()
        for col in ("current_price", "best_ask", "mid_price", "fetched_at_utc", "entry_edge"):
            if col not in open_mtm.columns:
                open_mtm[col] = None

    # Compute shares, MTM, unrealized P&L. Uses best_bid as current_price
    # (the conservative "could-I-exit-now" mark), matching the paper-exit
    # formula in run_rebalance. Fee applies only to gains.
    def _shares_safe(row):
        ep = row.get("entry_price") or 0
        nt = row.get("notional") or 0
        return (nt / ep) if ep > 0 else 0.0

    def _mtm_value(row):
        cp = row.get("current_price")
        if cp is None or pd.isna(cp):
            return None
        return _shares_safe(row) * float(cp)

    def _unrealized_pnl(row):
        cp = row.get("current_price")
        if cp is None or pd.isna(cp):
            return None
        shares = _shares_safe(row)
        ep = float(row.get("entry_price") or 0)
        gross = shares * (float(cp) - ep)
        fee = 0.02 * gross if gross > 0 else 0.0
        return gross - fee

    def _pnl_pct(row):
        pnl = _unrealized_pnl(row)
        if pnl is None:
            return None
        nt = float(row.get("notional") or 0)
        if nt <= 0:
            return None
        return pnl / nt

    def _last_updated_local(row):
        fetched = row.get("fetched_at_utc")
        city = row.get("city")
        if not fetched or pd.isna(fetched) or not city:
            return None
        try:
            from zoneinfo import ZoneInfo
            from polymarket_strat.domain.weather.models import CITY_REGISTRY
            station = CITY_REGISTRY.get(city)
            if not station or not getattr(station, "timezone", None):
                return str(fetched)
            dt = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                from datetime import timezone as _tz
                dt = dt.replace(tzinfo=_tz.utc)
            local = dt.astimezone(ZoneInfo(station.timezone))
            return local.strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            return str(fetched) if fetched is not None else None

    open_mtm["mtm_value"] = open_mtm.apply(_mtm_value, axis=1)
    open_mtm["unrealized_pnl"] = open_mtm.apply(_unrealized_pnl, axis=1)
    open_mtm["pnl_pct"] = open_mtm.apply(_pnl_pct, axis=1)
    open_mtm["last_updated_local"] = open_mtm.apply(_last_updated_local, axis=1)

    display_cols = [
        "city", "target_date", "question", "side",
        "entry_price", "current_price", "notional", "mtm_value",
        "unrealized_pnl", "pnl_pct",
        "model_prob", "market_prob", "edge", "entry_edge",
        "mode", "last_updated_local",
    ]
    present = [c for c in display_cols if c in open_mtm.columns]
    view = open_mtm[present].copy()

    # Styler for coloring unrealized P&L (green positive / red negative /
    # grey None). `map` avoids the deprecated applymap path.
    def _color_pnl(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "color: #888"
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        if v > 0:
            return "color: #1a7f37; font-weight: 600"
        if v < 0:
            return "color: #cf222e; font-weight: 600"
        return ""

    fmt: dict[str, str] = {}
    if "entry_price" in view.columns: fmt["entry_price"] = "{:.3f}"
    if "current_price" in view.columns: fmt["current_price"] = "{:.3f}"
    if "notional" in view.columns: fmt["notional"] = "${:,.2f}"
    if "mtm_value" in view.columns: fmt["mtm_value"] = "${:,.2f}"
    if "unrealized_pnl" in view.columns: fmt["unrealized_pnl"] = "${:+,.2f}"
    if "pnl_pct" in view.columns: fmt["pnl_pct"] = "{:+.2%}"
    if "model_prob" in view.columns: fmt["model_prob"] = "{:.2%}"
    if "market_prob" in view.columns: fmt["market_prob"] = "{:.2%}"
    if "edge" in view.columns: fmt["edge"] = "{:+.2%}"
    if "entry_edge" in view.columns: fmt["entry_edge"] = "{:+.2%}"

    styled = view.style.format(fmt, na_rep="—")
    if "unrealized_pnl" in view.columns:
        styled = styled.map(_color_pnl, subset=["unrealized_pnl"])
    if "pnl_pct" in view.columns:
        styled = styled.map(_color_pnl, subset=["pnl_pct"])

    st.dataframe(styled, hide_index=True)

    # Summary row: sum of MTM, unrealized P&L, position count
    mtm_sum = float(open_mtm["mtm_value"].dropna().sum())
    upnl_sum = float(open_mtm["unrealized_pnl"].dropna().sum())
    priced_ct = int(open_mtm["current_price"].notna().sum())
    total_ct = len(open_mtm)
    s1, s2, s3 = st.columns(3)
    s1.metric("Open MTM value $", f"{mtm_sum:,.2f}")
    s2.metric(
        "Unrealized P&L $",
        f"{upnl_sum:+,.2f}",
        delta_color=("normal" if upnl_sum >= 0 else "inverse"),
    )
    s3.metric("Priced / total", f"{priced_ct} / {total_ct}")
    if priced_ct < total_ct:
        st.caption(
            f"{total_ct - priced_ct} position(s) have no price snapshot yet. "
            "run autotrade (or `rebalance --dry-run`) to populate."
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
        st.dataframe(dists, hide_index=True)
    with col_b:
        # Flag outliers: |mu| > 5 or sigma > 5 — these are the distributions
        # strategy.py filters out at inference
        bad = dists[(dists["mu"].abs() > 5) | (dists["sigma"] > 5)]
        if bad.empty:
            st.success(f"All {len(dists)} distributions look clean (|μ| ≤ 5, σ ≤ 5).")
        else:
            st.warning(f"{len(bad)} distribution(s) flagged as outliers — excluded at inference.")
            st.dataframe(bad[["city", "model", "lead_hours", "mu", "sigma", "n_samples"]],
                         hide_index=True)

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
                st.dataframe(summary, hide_index=True)
            with c_r:
                st.caption(f"Last 500 of {len(rows)} events from {LAG_EVENTS.name}")
                st.dataframe(lag_df.tail(50), width="stretch", hide_index=True)
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
            st.dataframe(agg, width="stretch", hide_index=True)
    except Exception as exc:
        st.warning(f"Could not load walk-forward CSV: {exc}")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Read-only dashboard. For execution, use the CLI: "
    "`polymarket-strat autotrade / positions / settle --auto`. "
    "Rendered {}.".format(
        datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
)
