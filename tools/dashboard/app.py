"""Streamlit dashboard for the Polymarket weather alpha system — unified v2.

READ-ONLY view of weather.db + recent report/log files. Safe to run alongside
the live trading pipeline — opens SQLite with read-only PRAGMA so no write
contention with `autotrade` / `weather-calibrate`.

LAYOUT (Apr 27 2026, "v2 unified A+B")
--------------------------------------
Single page, three tabs:

  1. **Live**         — phone-glance ops view
                          ‑ live ticker (open count, MTM, realised + unrealised P&L)
                          ‑ open-positions tape with MTM, sorted by abs(pnl%)
                          ‑ activity feed (entries, exits, settlements) over a
                            configurable 1h / 6h / 24h / 3d window
  2. **Analytics**    — settled-trade analysis
                          ‑ equity curve + win/loss summary
                          ‑ recent settlements
                          ‑ walk-forward results (if last_run.csv present)
  3. **Calibration**  — model-health view
                          ‑ error-distribution health flags
                          ‑ forecast errors over the last 60 days
                          ‑ lag-monitor events (if logs/events.jsonl present)

CACHE TTL SPLIT (Phase 2)
-------------------------
Hot queries (open positions, market_prices, recent activity) → ttl=5s — render
fresh data on every autorefresh tick.

Cold queries (error_distributions, forecast_errors, walk-forward) → ttl=300s —
these don't change between hourly cron ticks, so re-querying every 10s wastes
SQLite read amps for no benefit.

CRON-TICK FRESHNESS
-------------------
The header reads mtimes of `/home/ubuntu/polymarket/logs/autotrade.log` and
`snap_mtm.log` (when running on EC2). On Mac dev machines those paths don't
exist and the widget shows "—" gracefully.

RUN LOCALLY
-----------
    streamlit run tools/dashboard/app.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import pandas as pd
    import streamlit as st
except ImportError:
    sys.stderr.write(
        "Missing dashboard dependencies. Install with:\n"
        "    pip install streamlit pandas\n"
    )
    raise

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
WALKFWD_24_CSV = ROOT / "tools" / "walk_forward" / "last_run_24h.csv"
WALKFWD_48_CSV = ROOT / "tools" / "walk_forward" / "last_run_48h.csv"

# Cron log files — only present on EC2. Used for the cron-tick freshness widget.
# On dev Mac the widget gracefully degrades to "—".
_CRON_LOG_CANDIDATES = [
    Path("/home/ubuntu/polymarket/logs/autotrade.log"),
    Path("/home/ubuntu/polymarket/logs/snap_mtm.log"),
]


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Polymarket Weather Alpha",
    page_icon="",
    layout="wide",
)

# Auto-refresh: 10s, same as v1. With the cache-TTL split below the heavy
# queries only re-run every 5min anyway, so 10s on the cheap queries is
# fine and gives a "live" feel on the Live tab.
_REFRESH_INTERVAL_MS = 10_000
if _AUTO_REFRESH_AVAILABLE:
    st_autorefresh(interval=_REFRESH_INTERVAL_MS, key="dashboard_autorefresh")
else:
    st.markdown(
        f'<meta http-equiv="refresh" content="{_REFRESH_INTERVAL_MS // 1000}">',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Data access (read-only; cached, with TTL split)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def ro_connection(path: Path) -> sqlite3.Connection:
    """Read-only SQLite connection. WAL-safe via PRAGMA query_only=1.

    Uses URI form WITHOUT mode=ro: under WAL the engine still needs to
    perform recovery on the -wal / -shm sidecars, which mode=ro forbids
    and which then errors as "attempt to write a readonly database".
    The PRAGMA below pins read-only at the engine layer, which is what
    we actually want.
    """
    uri = f"file:{path}"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


@st.cache_data(ttl=5, show_spinner=False)
def fetch_hot(_conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    """Hot path — trade_history, market_prices, recent activity. ttl=5s."""
    return pd.read_sql_query(sql, _conn, params=params)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_cold(_conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    """Cold path — error_distributions, forecast_errors. ttl=300s.

    These tables are written at most once per hour (calibration cron)
    so re-querying on every 10s autorefresh wastes SQLite I/O.
    """
    return pd.read_sql_query(sql, _conn, params=params)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_now_utc = datetime.now(timezone.utc)


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


def _ago_from_mtime(path: Path) -> str:
    """File mtime → human-readable 'ago'. Returns '—' if the path doesn't exist."""
    if not path.exists():
        return "—"
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        delta_s = (_now_utc - mtime).total_seconds()
        if delta_s < 60:
            return f"{int(delta_s)}s ago"
        if delta_s < 3600:
            return f"{int(delta_s/60)}m ago"
        if delta_s < 86400:
            return f"{int(delta_s/3600)}h ago"
        return f"{int(delta_s/86400)}d ago"
    except Exception:
        return "—"


def _city_local_tz(city: str | None):
    """Look up CITY_REGISTRY for city → ZoneInfo. None on miss / import failure."""
    if not city:
        return None
    try:
        from zoneinfo import ZoneInfo
        from polymarket_strat.domain.weather.models import CITY_REGISTRY
        station = CITY_REGISTRY.get(city)
        tz_name = getattr(station, "timezone", None) if station else None
        if not tz_name:
            return None
        return ZoneInfo(tz_name)
    except Exception:
        return None


def _fmt_local_time(ts_iso, city: str | None, fmt: str = "%H:%M") -> str:
    """Format an ISO UTC timestamp into the city's local-time HH:MM."""
    if ts_iso is None or pd.isna(ts_iso) or ts_iso == "":
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = _city_local_tz(city)
        if tz is None:
            return dt.strftime(fmt + " UTC")
        return dt.astimezone(tz).strftime(fmt + " %Z")
    except Exception:
        return str(ts_iso)


# ---------------------------------------------------------------------------
# Header — title, freshness, cron-tick widget, manual refresh
# ---------------------------------------------------------------------------

st.title("Polymarket Weather Alpha")

if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. Run calibration first.")
    st.stop()

conn = ro_connection(DB_PATH)

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
    _latest_mtm = pd.to_datetime(
        conn.execute(
            "SELECT MAX(fetched_at_utc) FROM market_prices"
        ).fetchone()[0] if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_prices'"
        ).fetchone() else None,
        utc=True, errors="coerce",
    )
except Exception:
    _latest_create = pd.NaT
    _latest_settle = pd.NaT
    _latest_mtm = pd.NaT

# Header strip: 4 columns
#   col 0: DB freshness (last create / settle / MTM)
#   col 1: cron tick freshness (autotrade.log + snap_mtm.log mtimes)
#   col 2: render time + auto-refresh interval
#   col 3: manual refresh button
_h_cols = st.columns([3, 3, 3, 1])
with _h_cols[0]:
    st.caption(
        f"DB writes — created **{_ago(_latest_create)}** · "
        f"settled **{_ago(_latest_settle)}** · "
        f"MTM **{_ago(_latest_mtm)}**"
    )
with _h_cols[1]:
    autotrade_age = _ago_from_mtime(_CRON_LOG_CANDIDATES[0])
    snap_age = _ago_from_mtime(_CRON_LOG_CANDIDATES[1])
    st.caption(
        f"Cron — autotrade **{autotrade_age}** · snap-mtm **{snap_age}**"
    )
with _h_cols[2]:
    st.caption(
        f"Rendered {_now_utc.strftime('%H:%M:%S UTC')} · "
        f"auto-refresh {_REFRESH_INTERVAL_MS // 1000}s"
    )
with _h_cols[3]:
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# Pre-load shared queries (used across tabs)
# ---------------------------------------------------------------------------

today = date.today().isoformat()
open_trades = fetch_hot(conn, "SELECT * FROM trade_history WHERE outcome IS NULL")
settled = fetch_hot(
    conn,
    "SELECT * FROM trade_history WHERE outcome IS NOT NULL ORDER BY settled_at DESC",
)
settled_today = (
    settled[settled["settled_at"].fillna("").str.startswith(today)]
    if not settled.empty else settled
)
signals_today = fetch_hot(
    conn,
    "SELECT * FROM trade_history WHERE date(created_at) = ?",
    (today,),
)


# ---------------------------------------------------------------------------
# Open-positions MTM enrichment (used in Live tab + ticker)
# ---------------------------------------------------------------------------

def _build_open_mtm(conn: sqlite3.Connection, open_trades: pd.DataFrame) -> pd.DataFrame:
    """Join trade_history (open) with the latest market_prices snapshot per token."""
    if open_trades.empty:
        return open_trades
    mtm_sql = """
        SELECT
            t.id, t.token_id, t.city, t.target_date, t.question, t.side,
            t.token_side,
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
        return fetch_hot(conn, mtm_sql)
    except Exception:
        # Pre-Apr-24 DBs don't have market_prices. Fall back so legacy
        # copies still render — empty MTM columns.
        legacy = open_trades.copy()
        for col in ("current_price", "best_ask", "mid_price",
                    "fetched_at_utc", "entry_edge"):
            if col not in legacy.columns:
                legacy[col] = None
        if "token_side" not in legacy.columns:
            legacy["token_side"] = None
        return legacy


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
    """Conservative unrealised: shares × (best_bid − entry) net 2% fee on gain.

    Matches the paper-exit P&L formula in run_rebalance — what we'd realise
    if we sold every open token at best_bid right now.
    """
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


def _age_minutes(row) -> float | None:
    ca = row.get("created_at")
    if not ca or pd.isna(ca):
        return None
    try:
        dt = datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (_now_utc - dt).total_seconds() / 60.0
    except Exception:
        return None


def _age_label(row) -> str:
    m = _age_minutes(row)
    if m is None:
        return "—"
    if m < 60:
        return f"{int(m)}m"
    if m < 1440:
        return f"{m/60:.1f}h"
    return f"{m/1440:.1f}d"


def _last_priced_label(row) -> str:
    return _fmt_local_time(row.get("fetched_at_utc"), row.get("city"))


open_mtm = _build_open_mtm(conn, open_trades)
if not open_mtm.empty:
    open_mtm["mtm_value"] = open_mtm.apply(_mtm_value, axis=1)
    open_mtm["unrealized_pnl"] = open_mtm.apply(_unrealized_pnl, axis=1)
    open_mtm["pnl_pct"] = open_mtm.apply(_pnl_pct, axis=1)
    open_mtm["age"] = open_mtm.apply(_age_label, axis=1)
    open_mtm["last_priced"] = open_mtm.apply(_last_priced_label, axis=1)

# Aggregates used across tabs
total_pnl_realised = float(settled["pnl"].sum()) if not settled.empty else 0.0
pnl_today_realised = float(settled_today["pnl"].sum()) if not settled_today.empty else 0.0
open_notional = float(open_trades["notional"].sum()) if not open_trades.empty else 0.0
unrealised_total = (
    float(open_mtm["unrealized_pnl"].dropna().sum())
    if not open_mtm.empty and "unrealized_pnl" in open_mtm.columns else 0.0
)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_live, tab_analytics, tab_calibration = st.tabs([
    "Live", "Analytics", "Calibration",
])


# ===========================================================================
# LIVE TAB — phone-glance ops
# ===========================================================================

with tab_live:
    # ---- Ticker: 6 metrics in one row (wraps on phone)
    t1, t2, t3, t4, t5, t6 = st.columns(6)
    t1.metric("Open positions", len(open_trades))
    t2.metric("Open notional", f"${open_notional:,.0f}")
    t3.metric(
        "Unrealised P&L",
        f"${unrealised_total:+,.2f}",
        delta_color=("normal" if unrealised_total >= 0 else "inverse"),
    )
    t4.metric(
        "Realised today",
        f"${pnl_today_realised:+,.2f}",
        delta_color=("normal" if pnl_today_realised >= 0 else "inverse"),
    )
    t5.metric(
        "Cumulative P&L",
        f"${total_pnl_realised:+,.2f}",
        delta_color=("normal" if total_pnl_realised >= 0 else "inverse"),
    )
    t6.metric("Signals today", len(signals_today))

    st.divider()

    # ---- Open positions tape (refactored: triage-ordered columns + sort)
    st.subheader("Open positions")
    if open_mtm.empty:
        st.info("No open positions.")
    else:
        # Sort by abs(pnl_pct) desc — biggest movers float to the top
        sort_view = open_mtm.copy()
        sort_view["__abs_pnl_pct"] = sort_view["pnl_pct"].abs().fillna(-1)
        sort_view = sort_view.sort_values("__abs_pnl_pct", ascending=False)
        sort_view = sort_view.drop(columns=["__abs_pnl_pct"])

        # Triage-ordered display columns: identity → price tape → P&L → context
        display_cols = [
            "city", "target_date", "question",
            "token_side", "entry_price", "current_price", "best_ask",
            "notional", "unrealized_pnl", "pnl_pct",
            "age", "edge", "entry_edge", "model_prob",
            "mode", "last_priced",
        ]
        present = [c for c in display_cols if c in sort_view.columns]
        view = sort_view[present].copy()

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

        def _color_pnl_pct(v):
            """Stronger highlight for big movers: |pct| > 20% gets a bg tint."""
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return "color: #888"
            try:
                v = float(v)
            except (TypeError, ValueError):
                return ""
            if v >= 0.20:
                return "color: #1a7f37; font-weight: 700; background-color: #d1f5d3"
            if v <= -0.10:
                return "color: #cf222e; font-weight: 700; background-color: #ffd7d7"
            if v > 0:
                return "color: #1a7f37; font-weight: 600"
            if v < 0:
                return "color: #cf222e; font-weight: 600"
            return ""

        fmt: dict[str, str] = {}
        for c, f in [
            ("entry_price", "{:.3f}"),
            ("current_price", "{:.3f}"),
            ("best_ask", "{:.3f}"),
            ("notional", "${:,.2f}"),
            ("unrealized_pnl", "${:+,.2f}"),
            ("pnl_pct", "{:+.2%}"),
            ("model_prob", "{:.2%}"),
            ("edge", "{:+.2%}"),
            ("entry_edge", "{:+.2%}"),
        ]:
            if c in view.columns:
                fmt[c] = f

        styled = view.style.format(fmt, na_rep="—")
        if "unrealized_pnl" in view.columns:
            styled = styled.map(_color_pnl, subset=["unrealized_pnl"])
        if "pnl_pct" in view.columns:
            styled = styled.map(_color_pnl_pct, subset=["pnl_pct"])

        # Fixed height makes the header sticky-ish — table scrolls within
        # itself rather than pushing the activity feed below the fold.
        st.dataframe(styled, hide_index=True, height=min(560, 60 + 36 * len(view)))

        # Summary row
        mtm_sum = float(open_mtm["mtm_value"].dropna().sum())
        priced_ct = int(open_mtm["current_price"].notna().sum())
        total_ct = len(open_mtm)
        s1, s2, s3 = st.columns(3)
        s1.metric("Open MTM value", f"${mtm_sum:,.2f}")
        s2.metric(
            "Sum unrealised",
            f"${unrealised_total:+,.2f}",
            delta_color=("normal" if unrealised_total >= 0 else "inverse"),
        )
        s3.metric("Priced / total", f"{priced_ct} / {total_ct}")
        if priced_ct < total_ct:
            st.caption(
                f"{total_ct - priced_ct} position(s) have no price snapshot yet — "
                "next snap-mtm tick will fill them."
            )

    st.divider()

    # ---- Activity feed (NEW)
    st.subheader("Activity")
    feed_window = st.radio(
        "Window",
        options=["1h", "6h", "24h", "3d"],
        index=2,  # default 24h
        horizontal=True,
        label_visibility="collapsed",
    )
    _window_hours = {"1h": 1, "6h": 6, "24h": 24, "3d": 72}[feed_window]

    # UNION: entries (created_at) + exits (settled_at, with outcome).
    # outcome=2 = rebalance exit, outcome=1/0 = settlement (sign of pnl
    # tells us WIN vs LOSS regardless of YES/NO side).
    feed_sql = f"""
        SELECT created_at AS event_time,
               'ENTRY' AS event_type,
               city, question, side, token_side,
               edge, entry_price, notional,
               NULL AS pnl, mode, market_id, token_id
        FROM trade_history
        WHERE datetime(created_at) >= datetime('now', '-{_window_hours} hours')

        UNION ALL

        SELECT settled_at AS event_time,
               CASE
                   WHEN outcome = 2 THEN 'REBALANCE_EXIT'
                   WHEN pnl > 0 THEN 'WIN'
                   ELSE 'LOSS'
               END AS event_type,
               city, question, side, token_side,
               edge, entry_price, notional,
               pnl, mode, market_id, token_id
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND settled_at IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-{_window_hours} hours')

        ORDER BY event_time DESC
        LIMIT 200
    """
    try:
        feed = fetch_hot(conn, feed_sql)
    except Exception as exc:
        st.warning(f"Activity feed query failed: {exc}")
        feed = pd.DataFrame()

    if feed.empty:
        st.info(f"No activity in the last {feed_window}.")
    else:
        # Decorate: human-readable when, color-able event type
        def _when_label(row):
            ts = row.get("event_time")
            if ts is None or pd.isna(ts):
                return "—"
            try:
                dt = pd.to_datetime(ts, utc=True, errors="coerce")
                if pd.isna(dt):
                    return str(ts)
                ago = _ago(dt)
                local_hhmm = _fmt_local_time(ts, row.get("city"))
                return f"{ago} ({local_hhmm})"
            except Exception:
                return str(ts)

        feed["when"] = feed.apply(_when_label, axis=1)
        feed["question_short"] = feed["question"].fillna("").str.slice(0, 60)

        # Reorder & rename for the feed view
        feed_view = feed[[
            "when", "event_type", "city", "token_side", "question_short",
            "edge", "entry_price", "notional", "pnl", "mode",
        ]].copy()
        feed_view.columns = [
            "when", "event", "city", "side", "question",
            "edge", "entry_px", "notional", "pnl", "mode",
        ]

        def _color_event(v):
            if v == "ENTRY":
                return "color: #0969da; font-weight: 600"
            if v == "WIN":
                return "color: #1a7f37; font-weight: 700"
            if v == "LOSS":
                return "color: #cf222e; font-weight: 700"
            if v == "REBALANCE_EXIT":
                return "color: #9a6700; font-weight: 600"
            return ""

        def _color_pnl_simple(v):
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

        feed_fmt = {
            "edge": "{:+.2%}",
            "entry_px": "{:.3f}",
            "notional": "${:,.2f}",
            "pnl": "${:+,.2f}",
        }
        feed_styled = feed_view.style.format(feed_fmt, na_rep="—")
        feed_styled = feed_styled.map(_color_event, subset=["event"])
        feed_styled = feed_styled.map(_color_pnl_simple, subset=["pnl"])
        st.dataframe(
            feed_styled, hide_index=True,
            height=min(540, 60 + 36 * len(feed_view)),
        )

        # Window summary
        n_entry = int((feed["event_type"] == "ENTRY").sum())
        n_win = int((feed["event_type"] == "WIN").sum())
        n_loss = int((feed["event_type"] == "LOSS").sum())
        n_rebal = int((feed["event_type"] == "REBALANCE_EXIT").sum())
        window_pnl = float(feed["pnl"].dropna().sum())
        f1, f2, f3, f4, f5 = st.columns(5)
        f1.metric("Entries", n_entry)
        f2.metric("Wins", n_win)
        f3.metric("Losses", n_loss)
        f4.metric("Rebalance exits", n_rebal)
        f5.metric(
            f"Net P&L ({feed_window})",
            f"${window_pnl:+,.2f}",
            delta_color=("normal" if window_pnl >= 0 else "inverse"),
        )


# ===========================================================================
# ANALYTICS TAB — settled-trade analysis
# ===========================================================================

with tab_analytics:
    # ---- Equity curve
    st.subheader("Equity curve (settled trades)")
    if settled.empty:
        st.info("No settled trades yet.")
    else:
        eq = settled.copy()
        eq["settled_at"] = pd.to_datetime(eq["settled_at"], errors="coerce")
        eq = eq.dropna(subset=["settled_at"]).sort_values("settled_at")
        eq["cum_pnl"] = eq["pnl"].cumsum()
        chart_df = eq.set_index("settled_at")[["cum_pnl"]]
        st.line_chart(chart_df, height=280)

        n = len(eq)
        wins = int((eq["pnl"] > 0).sum())
        wr = wins / n if n else 0.0
        avg_win = float(eq.loc[eq["pnl"] > 0, "pnl"].mean()) if wins else 0.0
        avg_loss = float(eq.loc[eq["pnl"] <= 0, "pnl"].mean()) if (n - wins) > 0 else 0.0
        max_dd = 0.0
        if not eq.empty:
            running = eq["cum_pnl"].cummax()
            max_dd = float((eq["cum_pnl"] - running).min())
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Trades", n)
        k2.metric("Win rate", f"{wr * 100:.1f}%")
        k3.metric("Avg win", f"${avg_win:+.2f}")
        k4.metric("Avg loss", f"${avg_loss:+.2f}")
        k5.metric(
            "Max DD",
            f"${max_dd:,.2f}",
            delta_color="inverse" if max_dd < 0 else "normal",
        )

    st.divider()

    # ---- Recent settlements
    st.subheader("Recent settlements (last 50)")
    if not settled.empty:
        show_cols = [
            "settled_at", "city", "target_date", "question",
            "side", "token_side", "entry_price", "notional",
            "outcome", "pnl", "mode",
        ]
        present = [c for c in show_cols if c in settled.columns]
        s_view = settled[present].head(50).copy()

        def _color_pnl_simple(v):
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

        s_fmt = {}
        if "entry_price" in s_view.columns: s_fmt["entry_price"] = "{:.3f}"
        if "notional" in s_view.columns: s_fmt["notional"] = "${:,.2f}"
        if "pnl" in s_view.columns: s_fmt["pnl"] = "${:+,.2f}"
        s_styled = s_view.style.format(s_fmt, na_rep="—")
        if "pnl" in s_view.columns:
            s_styled = s_styled.map(_color_pnl_simple, subset=["pnl"])
        st.dataframe(s_styled, hide_index=True, height=min(560, 60 + 36 * len(s_view)))
    else:
        st.info("No settled trades yet.")

    st.divider()

    # ---- Walk-forward (24h + 48h side-by-side if both present, else fall back
    # to last_run.csv for legacy)
    st.subheader("Walk-forward calibration")
    have_split = WALKFWD_24_CSV.exists() and WALKFWD_48_CSV.exists()
    if have_split:
        wf24 = pd.read_csv(WALKFWD_24_CSV)
        wf48 = pd.read_csv(WALKFWD_48_CSV)

        def _summary(df: pd.DataFrame, label: str) -> pd.DataFrame:
            agg = (df.groupby(["city", "model"])
                     .agg(n=("brier", "count"),
                          brier=("brier", "mean"),
                          log_loss=("log_loss", "mean"),
                          base_rate=("outcome", "mean"),
                          mean_pred=("predicted_prob", "mean"))
                     .reset_index())
            agg.insert(2, "lead", label)
            return agg

        try:
            agg24 = _summary(wf24, "24h")
            agg48 = _summary(wf48, "48h")
            both = pd.concat([agg24, agg48], ignore_index=True)
            both = both.sort_values(["city", "model", "lead"])
            wf_fmt = {
                "brier": "{:.4f}",
                "log_loss": "{:.4f}",
                "base_rate": "{:.2%}",
                "mean_pred": "{:.2%}",
            }
            st.dataframe(both.style.format(wf_fmt), hide_index=True)
            st.caption(
                "Brier skill score interpretation: lower brier = better. "
                "Compare to base_rate (climatology baseline)."
            )
        except Exception as exc:
            st.warning(f"Could not aggregate walk-forward CSVs: {exc}")
    elif WALKFWD_CSV.exists():
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
    else:
        st.info(
            "No walk-forward CSV found. Run `python tools/walk_forward/backtest.py` "
            "and the dashboard will pick up the result."
        )


# ===========================================================================
# CALIBRATION TAB — model health + forecast errors + lag monitor
# ===========================================================================

with tab_calibration:
    st.subheader("Error distributions")
    dists = fetch_cold(
        conn,
        """SELECT city, model, regime, lead_hours, family, mu, sigma, shape, nu, n_samples
           FROM error_distributions ORDER BY city, model, lead_hours""",
    )
    if dists.empty:
        st.info("No fitted distributions. Run `polymarket-strat weather-calibrate`.")
    else:
        col_a, col_b = st.columns([2, 1])
        with col_a:
            st.dataframe(dists, hide_index=True, height=min(560, 60 + 36 * len(dists)))
        with col_b:
            bad = dists[(dists["mu"].abs() > 5) | (dists["sigma"] > 5)]
            if bad.empty:
                st.success(
                    f"All {len(dists)} distributions look clean (|μ| ≤ 5, σ ≤ 5)."
                )
            else:
                st.warning(
                    f"{len(bad)} distribution(s) flagged as outliers — "
                    "excluded at inference."
                )
                st.dataframe(
                    bad[["city", "model", "lead_hours", "mu", "sigma", "n_samples"]],
                    hide_index=True,
                )
            st.caption(
                "σ_floor = 2.5°F (CLAUDE.md §5.2). Distributions with σ below "
                "the floor are clamped before bracket CDF evaluation."
            )

    st.divider()

    # ---- Recent forecast errors
    st.subheader("Recent forecast errors (last 60 days)")
    since = (date.today() - timedelta(days=60)).isoformat()
    errs = fetch_cold(
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
        view_e = errs[errs["city"].isin(pick)] if pick else errs
        if not view_e.empty:
            pivot = view_e.pivot_table(
                index="obs_date", columns="city", values="error_f", aggfunc="mean",
            )
            st.line_chart(pivot, height=300)

    st.divider()

    # ---- Lag monitor (optional)
    if LAG_EVENTS.exists():
        st.subheader("Lag monitor — recent events")
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
                    st.caption(
                        f"Last 500 of {len(rows)} events from {LAG_EVENTS.name}"
                    )
                    st.dataframe(lag_df.tail(50), width="stretch", hide_index=True)
        except Exception as exc:
            st.warning(f"Could not parse lag events: {exc}")
    else:
        st.caption("Lag monitor not running (no events.jsonl found).")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Read-only dashboard. For execution, use the CLI: "
    "`polymarket-strat autotrade / positions / settle --auto / rebalance / snap-mtm`. "
    "Rendered {}.".format(_now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"))
)
