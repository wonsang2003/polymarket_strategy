# Weather Data Sources — Survey & Decisions

**Apr 25 2026** — written after the climatology leak audit. Honest OOS ECE
went from 8.14% (1-yr leaky climo) → 4.45% (ERA5 30-yr climo). Documents
what data we use, what's available we're not using, and what's worth
adding.

---

## Currently in use

| Source | What | Window | Notes |
|--------|------|--------|-------|
| **Open-Meteo `forecast` API** | Live GFS / ECMWF / HRRR / NAM forecasts | live | Primary inference signal |
| **Open-Meteo `previous-runs` API** | True operational forecasts as issued | last 90 days | Real forecast errors for calibration |
| **Open-Meteo `archive` API** | ERA5 reanalysis Tmax | 1940–present | Now used for 30-yr climatology |
| **Open-Meteo ensemble API** | 82-member GEFS+ECMWF | live | Ensemble spread for σ scaling |
| **IEM ASOS** | Hourly station observations | rolling | US settlement; primary obs source |
| **ERA5 fallback** (Open-Meteo archive) | Reanalysis Tmax | rolling | Non-US settlement when IEM empty |

---

## Available but NOT yet integrated

These are the highest-leverage additions, ranked by expected ECE impact.

### 1. NOAA Big Data archive of operational GFS — REAL HISTORICAL FORECASTS

**Why it matters.** Right now only ~90 days of `previous-runs` data exists per
(city, model, lead) bucket. The other ~275 days come from reanalysis (which
peeks at the answer). A σ floor of 2.5°F was added precisely to compensate.
With more real-forecast days, σ floor can be relaxed and the model learns
from genuine forecast errors.

**Source.** AWS S3 bucket `noaa-gfs-bdp-pds`. Operational GFS GRIB files since
~2007 (~19 years × 4 cycles/day = ~28K runs available). No auth, free egress
within AWS.

**Effort.** Medium. Need a GRIB ingestion pipeline (cfgrib + xarray), point
extraction at our 22 city coordinates, then derive `forecast_high_f` from
the 6-hourly Tmax variable. Estimate: 1-2 days to wire end-to-end, then a
batch backfill that takes ~6-12 hours to fetch 5 years of historical runs.

**Expected ECE delta.** σ floor relaxation alone could close ~2% of the gap
in the [0.5, 0.8) calibration bin. With 5+ years of real-forecast errors,
the quantile model captures real seasonal/regime variability instead of
extrapolating from 90 days. Plausibly ECE → 3-3.5% from current 4.45%.

**Cost.** Free (AWS NOAA partnership). Storage: GRIB files are ~5MB each
× 28K = ~140GB if we keep all of it; ~5GB if we extract just the city
points and discard the rest.

### 2. ECMWF TIGGE archive — historical operational ECMWF forecasts

**Why it matters.** ECMWF runs the highest-skill global model. Currently we
only have 90 days of true operational ECMWF via `previous-runs`. TIGGE
archives all major operational global models since 2006.

**Source.** TIGGE via ECMWF's web API. **Requires registration** at
https://apps.ecmwf.int/datasets/data/tigge/ . Free for research/non-commercial.

**Effort.** Medium-high. The TIGGE API is more involved than NOAA's S3 — needs
an account, terms acceptance, and the data is GRIB1 format with a specific
ECMWF tooling chain. Estimate: 2-3 days to integrate.

**Expected ECE delta.** ECMWF is ~10-15% more skilled than GFS in walk-forward
backtest. More real ECMWF forecast errors means tighter calibration on
the model that already dominates the ensemble weight (0.50). Plausibly
another 1-1.5% ECE reduction.

**Cost.** Free. License is research/non-commercial — needs a check on whether
paper/live trading qualifies (probably yes; ECMWF licenses are permissive
for non-redistribution use).

### 3. Synoptic Data API (Mesonet) — high-density real-time obs

**Why it matters.** Currently US obs come from KLGA, KORD, etc. — single
airport stations per city. Mesonet/Synoptic aggregates ~30K weather stations
including private weather networks. For each city we could pull 5-20 nearby
stations, compute ensemble mean, and get a more robust observation that's
less subject to single-station microclimate noise.

**Source.** https://synopticdata.com/ — free tier 5,000 requests/day.

**Effort.** Low. JSON API like Open-Meteo. ~1 day to integrate as an
optional obs source in `station_client.py`.

**Expected ECE delta.** Marginal for ECE (calibration is about prediction
quality, not obs quality). But improves **settlement accuracy** — if Polymarket
resolves on KLGA but our obs comes from KLGA + 4 surrounding stations,
disagreement reveals that the resolution station had an anomalous reading
worth contesting/avoiding. Could reduce settlement disputes/losses.

**Cost.** Free tier sufficient for daily use.

### 4. Weather Underground PWS network — citizen weather stations

**Why it matters.** Microclimate signal in dense urban areas. Useful for
the urban heat island / wind-solar correction model that's spec'd in
CLAUDE.md but unbuilt.

**Source.** Wunderground API requires API key + revenue share. Less attractive
than Synoptic for this use case.

**Effort.** Medium.

**Expected ECE delta.** Small (<1%). Microclimate matters most at the obs
side, less at the model-prediction side.

**Cost.** Costs money. Skip unless other sources exhausted.

### 5. JMA / KMA / MetOffice / BoM national weather services

**Why it matters.** Country-specific obs and forecasts for our non-US
cities (Tokyo, Seoul, London, Sydney). Each national met service has
its own API.

**Source.** Multiple (KMA, JMA, MetOffice, BoM, etc.). Free tiers vary.

**Effort.** High. Each service has different APIs, auth, rate limits. ~3-5
days to integrate all four. Translation/units handling per service.

**Expected ECE delta.** Tokyo and Seoul currently have higher ECE than
average (per-city honest_ece numbers showed Tokyo 9.86%, Seoul 19.36%).
Native-source forecasts may add diversity to the ensemble.

**Cost.** Mostly free. Skip for now; revisit if specific cities prove
hard to calibrate.

### 6. NEXRAD / GOES — radar & satellite

**Why it matters.** Leading-indicator nowcasting. Cloud cover trend from
GOES, precip from NEXRAD. Could feed an L3 nowcast layer.

**Source.** NOAA, free.

**Effort.** Very high. Image data, custom processing pipeline, lots of
storage.

**Expected ECE delta.** Specialized — helps in the T-6h to T-1h window,
when forecasts are stale and obs trend dominates. Falls under L3 (live
nowcast) which is post-Phase-2 work in CLAUDE.md.

**Cost.** Compute/storage non-trivial.

---

## Decisions

**Phase 2 (now):**
- ERA5 30-yr climatology landed today → ECE 4.45% honest OOS
- Continue accumulating `previous-runs` daily; in 90 days the entire
  365-day calibration window will be real forecasts (no reanalysis)

**Phase 3 (post-Phase-2-clear):**
- Top priority: integrate **NOAA Big Data GFS archive** (#1). Backfill 5 years
  of operational GFS into `forecast_errors`. Expected ECE → ~3%.
- Second priority: **TIGGE ECMWF archive** (#2). Adds another 1-1.5%.

**Backlog (no current plan):**
- Synoptic API for obs robustness (#3) — improves settlement, not ECE
- National weather services (#5) — only if specific cities prove sticky
- Wunderground (#4), NEXRAD/GOES (#6) — high effort, marginal ECE benefit

---

## Honest ECE History

| Method | Aggregate ECE | Notes |
|--------|---------------|-------|
| Naive measure_ece (Apr 25 morning) | 5.44% | Random split + leaky climo. Optimistic. |
| Honest temporal split + 1-yr train-only climo | 8.14% | True OOS but climo too noisy. |
| Same with `--mask-climatology` | 6.92% | Removed climo features but Brier 0.222 (lost discrimination). |
| **Honest temporal split + ERA5 30-yr climo** | **4.45%** | **Production setup.** Brier 0.215, [0.5, 0.8) bins all <3% gap. |
