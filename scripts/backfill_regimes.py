"""Backfill synoptic regime labels onto historical forecast_errors rows.

Problem
-------
Every row in `forecast_errors` was written with `regime='stable_high'` (see
`domain/weather/strategy.py:calibrate()` — the save_forecast_error call hard-codes
SynopticRegime.STABLE_HIGH because upper-air classification wasn't available at
the time the historical pipeline ran).

That means every error_distribution we fit today is a *mixture* of five regimes
pretending to be STABLE_HIGH. At inference, a live FRONTAL_PASSAGE day pulls a
distribution calibrated on calm days → σ is artificially tight → we over-price
narrow brackets → tail events (e.g. Seoul 2026-03-29, -$10 on a 2.7σ move) eat
our edge.

What this script does
---------------------
1. Find every distinct (city, obs_date) with `regime='stable_high'`.
2. For each city, fetch historical ensemble-member statistics in 30-day chunks:
   - 82 members (31 GFS + 51 ECMWF) daily max temperature spread / std / skew
   - Historical CAPE from the archive endpoint
3. Classify regime using the existing `RegimeClassifier.classify_from_ensemble`.
4. `UPDATE forecast_errors SET regime=? WHERE city=? AND obs_date=?` —
   one logical update per (city, obs_date), atomic per city.
5. If --refit is passed, delete the old error_distributions for the city and
   re-fit per (model, regime, lead_bucket) via `ErrorDistributionFitter.fit`.

Flags
-----
    --cities seoul,nyc,london       Subset (default: all)
    --start YYYY-MM-DD              Earliest obs_date to backfill
    --end   YYYY-MM-DD              Latest obs_date (default: today-5)
    --dry-run                       Classify only; print histogram; do not UPDATE
    --refit                         After backfill, re-fit error_distributions
    --db PATH                       Override DB path (default: data/weather/weather.db)
    --chunk-days N                  Ensemble-fetch window size (default: 30)
    --lead-hours {6,12,24,48,72}    Restrict query + UPDATE to one lead bucket
                                    (default: all leads)

Typical run
-----------
    # Dry run, all cities, last year:
    python scripts/backfill_regimes.py --dry-run

    # Real run, single city, with refit:
    python scripts/backfill_regimes.py --cities seoul --refit

    # Real run, all cities, with refit, explicit window:
    python scripts/backfill_regimes.py \
        --start 2025-01-01 --end 2026-04-10 --refit

    # Fix the remaining 48h STABLE_HIGH gap (CLAUDE.md §14 #1) without
    # overwriting already-correct 24h labels:
    python scripts/backfill_regimes.py --lead-hours 48 --refit

Notes
-----
- Cached per (city, obs_date) within the run so a given date is classified
  exactly once regardless of how many (model, lead) rows it has.
- Open-Meteo's ensemble-api serves historical runs for recent dates;
  older dates fall through to archive-api with models=gfs_seamless + CAPE.
  Two YOU IMPLEMENT markers below call out where to verify response shape.
- The σ floor in `BracketProbabilityCalculator` is unchanged — still acts as
  a safety net until Phase 2 closes.
"""
from __future__ import annotations

import argparse
import json
import ssl
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Add repo root to path so imports resolve when running as a plain script
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_strat.domain.weather.calibration import (  # noqa: E402
    ErrorDistributionFitter,
    RegimeClassifier,
)
from polymarket_strat.domain.weather.models import (  # noqa: E402
    CITY_REGISTRY,
    CityStation,
    SynopticRegime,
    WeatherModel,
)
from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase  # noqa: E402

UTC = timezone.utc

# Open-Meteo endpoints
_OM_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_OM_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo ensemble model IDs for bulk pull
_ENSEMBLE_MODELS = ["gfs_seamless", "ecmwf_ifs025"]

# One archive query is enough for CAPE — no ensemble needed for that field
_ARCHIVE_MODEL_FOR_CAPE = "era5"

# Only these models' distributions need refit; HRRR/NAM alias to gfs_seamless
# in the archive (see CLAUDE.md §5.5) — we don't re-fit them.
_REFIT_MODELS = (WeatherModel.GFS, WeatherModel.ECMWF)

# Lead-hour buckets (must match domain/weather/strategy.py:_bucket_lead)
_LEAD_BUCKETS = (6, 12, 24, 48, 72)

# Polite pause between Open-Meteo chunks (seconds).
# Their free tier allows ~10k req/day; with 22 cities × 13 chunks × 3 queries
# we land around 860 requests, so 0.5s is ample.
_REQUEST_SLEEP = 0.5


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get_json(url: str, params: dict) -> dict:
    """GET a URL with query params, return parsed JSON. Raises on HTTP error."""
    full_url = f"{url}?{urlencode(params)}"
    req = Request(full_url, headers={"User-Agent": "polymarket-strat-backfill/1.0"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, timeout=60, context=ctx) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Ensemble stats per date range
# ---------------------------------------------------------------------------

def fetch_ensemble_members_ranged(
    station: CityStation,
    *,
    start: date,
    end: date,
) -> dict[date, list[float]]:
    """Fetch daily-max-temperature forecasts for a date range.

    Returns {obs_date: [gfs_val_f, ecmwf_val_f]} — a 2-member "duo" pulled from
    archive-api's deterministic runs. True 31+51 member ensemble would require
    ensemble-api which only retains ~60–90 days of history; archive-api goes
    back years but only exposes the mean deterministic forecast per model.

    Downstream: `classify_from_ensemble(n_members=2)` auto-falls back to
    `classify_from_spread` (see calibration.py:296-298). `compute_regime_for_date`
    additionally consults CAPE + pressure tendency to promote duo classifications
    into CONVECTIVE / TRANSITION when appropriate.
    """
    out: dict[date, list[float]] = defaultdict(list)

    for om_model in _ENSEMBLE_MODELS:
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max",
            "models": om_model,
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        try:
            data = _http_get_json(_OM_ARCHIVE_URL, params)
        except Exception as exc:
            print(
                f"  [archive:{om_model}] {station.city} {start}→{end}: {exc}",
                file=sys.stderr,
            )
            continue
        time.sleep(_REQUEST_SLEEP)

        daily = data.get("daily", {})
        dates_list: list[str] = daily.get("time", []) or []
        # Deterministic archive runs return a single scalar series
        values = daily.get("temperature_2m_max", []) or []
        if not dates_list or not values:
            continue

        for i, d_str in enumerate(dates_list):
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                continue
            if i < len(values) and values[i] is not None:
                out[d].append(float(values[i]))

    return dict(out)


def fetch_pressure_tendency_ranged(
    station: CityStation,
    *,
    start: date,
    end: date,
) -> dict[date, float]:
    """Fetch max |dP/dt over 12h| per day from ERA5 archive.

    TRANSITION regime per CLAUDE.md §5.6 requires |dP/dt| > 4 hPa / 12h.
    We fetch hourly `pressure_msl`, compute rolling 12h absolute deltas, and
    report the max per local date. Returns {obs_date: max_abs_delta_hpa}.
    """
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": "pressure_msl",
        "models": _ARCHIVE_MODEL_FOR_CAPE,
        "timezone": station.timezone,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    try:
        data = _http_get_json(_OM_ARCHIVE_URL, params)
    except Exception as exc:
        print(f"  [pmsl] {station.city} {start}→{end}: {exc}", file=sys.stderr)
        return {}
    time.sleep(_REQUEST_SLEEP)

    hourly = data.get("hourly", {})
    times = hourly.get("time", []) or []
    pressures = hourly.get("pressure_msl", []) or []
    if len(times) != len(pressures):
        return {}

    # Parse once
    parsed: list[tuple[date, datetime, float]] = []
    for t_str, p in zip(times, pressures):
        if p is None:
            continue
        try:
            dt = datetime.fromisoformat(t_str)
        except ValueError:
            continue
        parsed.append((dt.date(), dt, float(p)))

    # Rolling 12h delta: for each hour i, compare to i-12.
    # Map back to the date of the *later* sample (the transition "belongs" to that day).
    daily_max: dict[date, float] = {}
    for i in range(12, len(parsed)):
        d_later, _, p_later = parsed[i]
        _, _, p_prior = parsed[i - 12]
        delta = abs(p_later - p_prior)
        if delta > daily_max.get(d_later, 0.0):
            daily_max[d_later] = delta

    return daily_max


def fetch_cape_max_ranged(
    station: CityStation,
    *,
    start: date,
    end: date,
) -> dict[date, float]:
    """Fetch daily-max CAPE from the archive endpoint for a date range.

    ERA5 reanalysis exposes CAPE on the archive-api. Returns {obs_date: cape_max}.

    YOU IMPLEMENT (minor): verify Open-Meteo's archive-api exposes `cape` in the
    `hourly` payload under model=era5. If it returns `cape_mean` or
    `convective_inhibition` instead, adapt the variable name.
    """
    params = {
        "latitude": station.lat,
        "longitude": station.lon,
        "hourly": "cape",
        "models": _ARCHIVE_MODEL_FOR_CAPE,
        "timezone": station.timezone,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    try:
        data = _http_get_json(_OM_ARCHIVE_URL, params)
    except Exception as exc:
        print(f"  [cape] {station.city} {start}→{end}: {exc}", file=sys.stderr)
        return {}
    time.sleep(_REQUEST_SLEEP)

    hourly = data.get("hourly", {})
    times = hourly.get("time", []) or []
    capes = hourly.get("cape", []) or []
    if len(times) != len(capes):
        return {}

    daily_max: dict[date, float] = {}
    for t_str, c in zip(times, capes):
        if c is None:
            continue
        try:
            # Open-Meteo serves ISO strings like "2025-11-14T00:00"
            d = datetime.fromisoformat(t_str).date()
        except ValueError:
            continue
        prev = daily_max.get(d, 0.0)
        if float(c) > prev:
            daily_max[d] = float(c)

    return daily_max


def compute_regime_for_date(
    *,
    members: list[float],
    cape_max: float,
    pressure_tendency_hpa_12h: float,
    pressure_frontal_threshold_hpa: float,
    pressure_transition_threshold_hpa: float,
    classifier: RegimeClassifier,
) -> SynopticRegime:
    """Classify one date's regime from pressure-tendency tiers + CAPE + spread.

    Priority order (derived from CLAUDE.md §5.6, tuned for deterministic duo):
      1. CAPE > 1000 J/kg                                                → CONVECTIVE
      2. |dP/dt_12h| > pressure_frontal_threshold_hpa (top ~5%)          → FRONTAL_PASSAGE
      3. |dP/dt_12h| > pressure_transition_threshold_hpa (next ~12%)     → TRANSITION
      4. duo spread > 7.2°F (both determ. models disagree strongly)      → FRONTAL_PASSAGE
      5. else                                                            → STABLE_HIGH

    Why tier pressure-tendency for BOTH frontal and transition:
      - A real 82-member ensemble would give real temperature spread, but
        ensemble-api's retention (~60-90 days) cuts off calibration windows.
        `fetch_ensemble_members_ranged` therefore returns the GFS+ECMWF
        deterministic duo from archive-api. Two deterministics rarely disagree
        by more than 7.2°F, so `classify_from_spread`'s FRONTAL branch almost
        never fires for the duo — leaving FRONTAL share ≈ 0 for quiet climates
        (verified: London/NYC dry-runs show 0% FRONTAL under spread rule).
      - The largest frontal passages in mid-latitudes ARE the days with the
        biggest pressure changes — synoptic-scale cyclogenesis drives both.
        Tiering |dP/dt_12h| gives us a direct, non-degenerate frontal signal.
      - The spread-rule FRONTAL fallback (step 4) still catches the rare days
        where pressure stayed calm but the two models diverge on temperature
        (e.g. marine-layer timing).

    Both thresholds are computed per-city in `backfill_one_city` from this
    window's pressure climatology (percentiles 95 and 83 of the daily
    max |dP/dt_12h|), so the calibration is robust across tropical/temperate/
    subarctic cities without hand-tuning.
    """
    # Priority 1: convective — overrides everything
    if cape_max > 1000.0:
        return SynopticRegime.CONVECTIVE

    # Priority 2: most extreme pressure change → frontal passage
    abs_dpdt = abs(pressure_tendency_hpa_12h)
    if abs_dpdt > pressure_frontal_threshold_hpa:
        return SynopticRegime.FRONTAL_PASSAGE

    # Priority 3: moderate pressure change → synoptic transition
    if abs_dpdt > pressure_transition_threshold_hpa:
        return SynopticRegime.TRANSITION

    # Priority 4: strong duo-spread disagreement → frontal (rare with duo)
    if len(members) >= 2:
        spread_val = max(members) - min(members)
        if spread_val > 7.2:
            return SynopticRegime.FRONTAL_PASSAGE

    # Priority 5: default
    return SynopticRegime.STABLE_HIGH


# ---------------------------------------------------------------------------
# DB queries (raw SQL — avoids touching persistence.py)
# ---------------------------------------------------------------------------

def query_dates_to_backfill(
    db: WeatherDatabase,
    *,
    city: str,
    start: date,
    end: date,
    lead_hours: int | None = None,
) -> list[date]:
    """Return distinct obs_dates for this city where regime is still 'stable_high'.

    If lead_hours is supplied, restrict to rows at that exact lead bucket —
    crucial so a 48h-only backfill doesn't sweep up obs_dates that have correctly
    classified 24h rows (CLAUDE.md §14 #1: 24h buckets already have frontal/
    transition labels from live calibration; only 48h is stuck at stable_high).
    """
    sql = (
        "SELECT DISTINCT obs_date "
        "FROM forecast_errors "
        "WHERE city = ? "
        "  AND regime = 'stable_high' "
        "  AND obs_date >= ? "
        "  AND obs_date <= ? "
    )
    params: list[object] = [city, start.isoformat(), end.isoformat()]
    if lead_hours is not None:
        sql += "  AND lead_hours = ? "
        params.append(int(lead_hours))
    sql += "ORDER BY obs_date"
    rows = db._conn.execute(sql, params).fetchall()
    out: list[date] = []
    for r in rows:
        try:
            out.append(date.fromisoformat(r["obs_date"]))
        except ValueError:
            continue
    return out


def update_regime_for_date(
    db: WeatherDatabase,
    *,
    city: str,
    obs_date: date,
    regime: SynopticRegime,
    lead_hours: int | None = None,
) -> int:
    """UPDATE forecast_errors for (city, obs_date) — returns row count changed.

    If lead_hours is supplied, restrict the UPDATE to that exact bucket so a
    48h backfill leaves already-correct 24h labels intact.
    """
    sql = "UPDATE forecast_errors SET regime = ? WHERE city = ? AND obs_date = ?"
    params: list[object] = [regime.value, city, obs_date.isoformat()]
    if lead_hours is not None:
        sql += " AND lead_hours = ?"
        params.append(int(lead_hours))
    cur = db._conn.execute(sql, params)
    return cur.rowcount


def delete_error_distributions(db: WeatherDatabase, *, city: str) -> int:
    """Wipe all error_distributions for a city. Used before --refit."""
    cur = db._conn.execute(
        "DELETE FROM error_distributions WHERE city = ?",
        (city,),
    )
    return cur.rowcount


def fetch_errors_for_bucket(
    db: WeatherDatabase,
    *,
    city: str,
    model: WeatherModel,
    regime: SynopticRegime,
    lead_hours: int,
) -> list[float]:
    """Same logic as WeatherDatabase.get_forecast_errors but keeps this script
    self-contained (and explicit about what we're pulling)."""
    rows = db._conn.execute(
        """
        SELECT error_f
        FROM forecast_errors
        WHERE city = ?
          AND model = ?
          AND regime = ?
          AND lead_hours = ?
        """,
        (city, model.value, regime.value, lead_hours),
    ).fetchall()
    return [r["error_f"] for r in rows]


# ---------------------------------------------------------------------------
# Per-city backfill
# ---------------------------------------------------------------------------

def chunk_date_range(start: date, end: date, *, chunk_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into contiguous chunks of at most `chunk_days` days."""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=chunk_days - 1), end)
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return chunks


def backfill_one_city(
    db: WeatherDatabase,
    *,
    city: str,
    start: date,
    end: date,
    chunk_days: int,
    classifier: RegimeClassifier,
    dry_run: bool,
    lead_hours: int | None = None,
) -> dict[str, int]:
    """Classify and (unless dry_run) relabel every stable_high row for this city.

    When lead_hours is provided, the query/update are scoped to that single
    lead bucket — use this for a 48h-only backfill that preserves 24h labels.
    """
    station = CITY_REGISTRY.get(city)
    if station is None:
        print(f"[{city}] unknown city — skipping", file=sys.stderr)
        return {"skipped": 1}

    dates_to_backfill = query_dates_to_backfill(
        db, city=city, start=start, end=end, lead_hours=lead_hours,
    )
    if not dates_to_backfill:
        scope = f"lead={lead_hours}h" if lead_hours is not None else "all leads"
        print(
            f"[{city}] nothing to backfill (0 stable_high rows in window, {scope})",
            file=sys.stderr,
        )
        return {"dates_processed": 0}

    earliest, latest = dates_to_backfill[0], dates_to_backfill[-1]
    scope = f"lead={lead_hours}h" if lead_hours is not None else "all leads"
    print(
        f"[{city}] {len(dates_to_backfill)} distinct dates to classify "
        f"({earliest} → {latest}, {scope})",
        file=sys.stderr,
    )

    # Batch archive fetches by chunk
    members_by_date: dict[date, list[float]] = {}
    cape_by_date: dict[date, float] = {}
    pressure_by_date: dict[date, float] = {}
    for c_start, c_end in chunk_date_range(earliest, latest, chunk_days=chunk_days):
        print(
            f"  fetching duo + CAPE + pressure for {c_start} → {c_end}...",
            file=sys.stderr,
        )
        members_by_date.update(
            fetch_ensemble_members_ranged(station, start=c_start, end=c_end)
        )
        cape_by_date.update(
            fetch_cape_max_ranged(station, start=c_start, end=c_end)
        )
        pressure_by_date.update(
            fetch_pressure_tendency_ranged(station, start=c_start, end=c_end)
        )

    # Derive per-city FRONTAL and TRANSITION pressure-tendency thresholds from
    # this window's pressure climatology (§5.6 target: frontal 5-15%, transition
    # 10-20%). We target the top 5% as FRONTAL and the next 12% (p83-p95) as
    # TRANSITION. Absolute floors of 8 / 4 hPa are retained so tropical stations
    # with very quiet pressure climatologies never get spuriously-labeled days.
    _frontal_floor = 8.0
    _transition_floor = 4.0
    _frontal_percentile = 95.0
    _transition_percentile = 83.0
    if pressure_by_date:
        _pressures_sorted = sorted(pressure_by_date.values())

        def _pct(p: float) -> float:
            idx = int(len(_pressures_sorted) * p / 100.0)
            idx = max(0, min(idx, len(_pressures_sorted) - 1))
            return _pressures_sorted[idx]

        pressure_frontal_threshold_hpa = max(_frontal_floor, _pct(_frontal_percentile))
        pressure_transition_threshold_hpa = max(
            _transition_floor, _pct(_transition_percentile)
        )
    else:
        pressure_frontal_threshold_hpa = _frontal_floor
        pressure_transition_threshold_hpa = _transition_floor
    print(
        f"[{city}] pressure thresholds: frontal={pressure_frontal_threshold_hpa:.2f} hPa "
        f"(p{int(_frontal_percentile)}), transition={pressure_transition_threshold_hpa:.2f} hPa "
        f"(p{int(_transition_percentile)}) over {len(pressure_by_date)} daily max |dP/dt_12h|",
        file=sys.stderr,
    )

    # Classify every date
    regime_counts: Counter[SynopticRegime] = Counter()
    updates: list[tuple[date, SynopticRegime]] = []
    for d in dates_to_backfill:
        members = members_by_date.get(d, [])
        cape = cape_by_date.get(d, 0.0)
        pressure_tendency = pressure_by_date.get(d, 0.0)
        regime = compute_regime_for_date(
            members=members,
            cape_max=cape,
            pressure_tendency_hpa_12h=pressure_tendency,
            pressure_frontal_threshold_hpa=pressure_frontal_threshold_hpa,
            pressure_transition_threshold_hpa=pressure_transition_threshold_hpa,
            classifier=classifier,
        )
        regime_counts[regime] += 1
        if regime is not SynopticRegime.STABLE_HIGH:
            # Only queue updates that actually change a row
            updates.append((d, regime))

    # Report histogram
    total = len(dates_to_backfill)
    print(f"[{city}] regime distribution over {total} days:", file=sys.stderr)
    for r in SynopticRegime:
        n = regime_counts[r]
        pct = (n / total * 100) if total else 0.0
        print(f"    {r.value:<20} {n:>4}  ({pct:5.1f}%)", file=sys.stderr)

    if dry_run:
        return {"dates_processed": total, "would_update": len(updates)}

    # Apply UPDATEs inside a single transaction per city
    rows_changed = 0
    try:
        for d, regime in updates:
            rows_changed += update_regime_for_date(
                db, city=city, obs_date=d, regime=regime, lead_hours=lead_hours,
            )
        db._conn.commit()
    except Exception as exc:
        db._conn.rollback()
        print(f"[{city}] UPDATE failed, rolled back: {exc}", file=sys.stderr)
        return {"dates_processed": total, "rows_changed": 0, "error": str(exc)}

    print(
        f"[{city}] applied {len(updates)} date-level changes "
        f"(→ {rows_changed} forecast_errors rows)",
        file=sys.stderr,
    )
    return {
        "dates_processed": total,
        "date_changes": len(updates),
        "rows_changed": rows_changed,
    }


# ---------------------------------------------------------------------------
# Refit error distributions
# ---------------------------------------------------------------------------

def refit_one_city(db: WeatherDatabase, *, city: str) -> int:
    """After relabeling, re-fit error_distributions per (model, regime, lead)."""
    deleted = delete_error_distributions(db, city=city)
    db._conn.commit()
    print(f"[{city}] deleted {deleted} stale distributions; refitting...",
          file=sys.stderr)

    fitter = ErrorDistributionFitter()
    n_fitted = 0
    for model in _REFIT_MODELS:
        for regime in SynopticRegime:
            for lead in _LEAD_BUCKETS:
                errors = fetch_errors_for_bucket(
                    db, city=city, model=model, regime=regime, lead_hours=lead,
                )
                if len(errors) < 5:
                    continue
                try:
                    dist = fitter.fit(
                        errors,
                        city=city,
                        model=model,
                        regime=regime,
                        lead_hours=lead,
                    )
                    db.save_error_distribution(dist)
                    n_fitted += 1
                except Exception as exc:
                    print(
                        f"  [{city}/{model.value}/{regime.value}/{lead}h] "
                        f"fit failed: {exc}",
                        file=sys.stderr,
                    )
    print(f"[{city}] refit {n_fitted} distributions", file=sys.stderr)
    return n_fitted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill synoptic regime labels on historical forecast_errors."
    )
    p.add_argument(
        "--cities",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=None,
        help="Comma-separated city keys (default: all 22).",
    )
    p.add_argument("--start", type=date.fromisoformat, default=None,
                   help="Earliest obs_date (default: 365d ago).")
    p.add_argument("--end", type=date.fromisoformat, default=None,
                   help="Latest obs_date (default: today - 5).")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify + print histogram, do not UPDATE.")
    p.add_argument("--refit", action="store_true",
                   help="After backfill, re-fit error_distributions per city.")
    p.add_argument("--db", default="data/weather/weather.db",
                   help="Path to weather.db (default: data/weather/weather.db).")
    p.add_argument("--chunk-days", type=int, default=30,
                   help="Ensemble-fetch chunk size (default: 30).")
    p.add_argument(
        "--lead-hours",
        type=int,
        default=None,
        choices=[6, 12, 24, 48, 72],
        help=(
            "Only backfill rows at this exact lead bucket. "
            "Use --lead-hours 48 to fix the 48h STABLE_HIGH gap without "
            "disturbing already-correct 24h regime labels. Default: all leads."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    today_utc = datetime.now(UTC).date()
    end = args.end or (today_utc - timedelta(days=5))
    start = args.start or (end - timedelta(days=365))
    if start > end:
        print(f"start ({start}) > end ({end}) — aborting", file=sys.stderr)
        return 2

    cities = args.cities or list(CITY_REGISTRY.keys())
    db = WeatherDatabase(args.db)
    classifier = RegimeClassifier()

    lead_scope = f"lead={args.lead_hours}h only" if args.lead_hours else "all leads"
    print(
        f"backfill_regimes: {len(cities)} cities, window {start} → {end}, "
        f"{lead_scope}, "
        f"{'DRY RUN' if args.dry_run else 'LIVE'}"
        f"{' + refit' if args.refit and not args.dry_run else ''}",
        file=sys.stderr,
    )

    totals: Counter[str] = Counter()
    per_city: dict[str, dict[str, int]] = {}
    for city in cities:
        res = backfill_one_city(
            db,
            city=city,
            start=start,
            end=end,
            chunk_days=args.chunk_days,
            classifier=classifier,
            dry_run=args.dry_run,
            lead_hours=args.lead_hours,
        )
        per_city[city] = res
        for k, v in res.items():
            if isinstance(v, int):
                totals[k] += v

        if args.refit and not args.dry_run and res.get("rows_changed", 0) > 0:
            refit_n = refit_one_city(db, city=city)
            totals["distributions_refit"] += refit_n

    print("\n=== summary ===", file=sys.stderr)
    print(f"  cities_processed:     {len(per_city)}", file=sys.stderr)
    print(f"  dates_processed:      {totals['dates_processed']}", file=sys.stderr)
    print(f"  date_changes:         {totals['date_changes']}", file=sys.stderr)
    print(f"  rows_changed:         {totals['rows_changed']}", file=sys.stderr)
    if args.refit:
        print(f"  distributions_refit:  {totals['distributions_refit']}",
              file=sys.stderr)
    if args.dry_run:
        print(f"  would_update (rows):  {totals['would_update']} (not applied)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
