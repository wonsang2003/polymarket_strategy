# POLYMARKET QUANTITATIVE TRADING SYSTEM — WORKING SPEC

**Last Updated:** April 18, 2026
**Current Mode:** PAPER — Weather alpha under validation
**Secondary Alpha (Musk Tweets):** spec'd in `MUSK_SPEC.md`, zero code. Do not wire into main pipeline until weather clears its 30-day gate.

---

## 1. Who You're Building For

장원상 (Wonsang Jang) — UC Berkeley Data Science junior, software engineering intern at Deeply (Seoul). Holds Korean 투자자산운용사 license, studying CFA Level 1. Strong Python/asyncio, has built automated trading systems before (KB Securities Open API with DCF/Monte Carlo). Bilingual Korean/English.

**Style preferences**
- Direct, analytical responses. No reassurance, no hand-holding.
- Show formulas, explain intuition, flag failure modes upfront.
- He owns key implementation himself. Provide scaffolding + `YOU IMPLEMENT` markers, not full API glue code.
- Stress-tests everything. Expect pushback and engage with it.

---

## 2. What's Built, What Works, What Doesn't

### Built and working
- **Market discovery**: Polymarket Gamma API scanner, bracket-contract parser (`infrastructure/weather/market_scanner.py`).
- **Forecast ingest**: Open-Meteo JSON API — forecast, previous-runs (true operational forecasts), archive (reanalysis), 82-member ensemble (31 GFS + 51 ECMWF), ERA5. **No raw GRIB parsing** — deliberate choice to reduce dependency weight.
- **Station observations**: IEM ASOS (US) + ERA5 fallback (non-US stations IEM doesn't cover).
- **Calibration**: MLE fits of Normal / Student-t / Skew-Normal per `(city, model, regime, lead_hours)`. Stored in SQLite.
- **Bayesian calibration**: `fit_bayesian()` implemented with PyMC (Student-t likelihood, Normal/HalfNormal/Exponential-shifted-2 priors). `summarize_posterior()` collapses posterior to a single `ErrorDistribution` using law of total variance. **Not yet wired into live calibration path** — frozen behind Phase 2 gate.
- **Regime classification**: heuristic (spread-based) + optional GRIB-derived (500mb gradient, CAPE, pressure tendency) — but in practice all historical errors are currently labeled `STABLE_HIGH` because historical ensemble spread is not reconstructed.
- **Bracket pricing**: CDF-based with ensemble weighting, normalization, σ floor, outlier filter.
- **Risk**: `PortfolioRiskManager` — quarter-Kelly, 2% per-position, 8% per-correlation-group, 5% daily drawdown brake, min entry price 0.02.
- **Execution**: `PaperExecutor` + `LiveExecutor` (`py_clob_client`, FOK limit only).
- **Persistence**: SQLite (5 tables — see §9). **WAL mode** enabled with graceful fallback for filesystems that don't support it. `WeatherDatabase.backup()` uses SQLite online-backup API (safe during writes).
- **DB backup rotation**: `scripts/backup_db.py` — timestamped online backups, `--keep N` rotation, cron-ready.
- **Read-only dashboard**: `tools/dashboard/app.py` — Streamlit, opens DB with `mode=ro` so zero write contention. 8 panels (KPIs, positions, equity curve, settlements, calibration health with outlier flags, forecast errors, lag events, walk-forward results). Auto-picks up `tools/walk_forward/last_run.csv` and `tools/lag_monitor/logs/events.jsonl` if present.
- **Dashboard auto-start**: `deploy/install_dashboard_agent.sh` writes and loads a `launchd` agent that auto-restarts on crash and auto-starts at login, bound to the Mac's tailnet IP. Setup walkthrough in `tools/dashboard/README.md`.
- **Lag monitor**: `tools/lag_monitor/monitor.py` polls Open-Meteo forecast (180 s) + Polymarket prices (60 s) on a watchlist, emits JSONL events. `analyze.py` joins forecast-change → subsequent price-change, reports median/p25/p75/p90 lag with decision guidance. Launch with `./scripts/start_lag_monitor.sh`.
- **Walk-forward backtest**: `tools/walk_forward/backtest.py` — for each eval date D refits the error distribution using ONLY errors from obs_date < D, scores 9 synthetic bracket geometries (±1°F, ±2°F, ±3°F, ±5°F, ±10°F, one-sided above/below/±5). Reports Brier / log-loss / reliability diagram with skill score vs base-rate predictor. Last full run: 22 cities × 2 models × 100 days = 38,934 predictions, Brier skill score **+0.455** (overall; varies by city).
- **Ops**: Telegram alerts, interactive HTML reports, Lambda handler, CLI (`autotrade`, `weather-calibrate`, `weather-analyze`, `backtest`, `positions`, `settle`, `report`).
- **Tests**: 24 passing. Cover settlement math, strategy gates, payout accounting. **Not covered**: calibration pipeline, API parsing, Monte Carlo.

### Not built yet
- **Bayesian posterior in live path**: `fit_bayesian()` exists but live `calibrate()` still uses MLE. Wire in only after Phase 2 clears.
- **Musk tweet alpha**: zero code. Architecture in `MUSK_SPEC.md`.
- **Dynamic correlation matrix**: static assumption.
- **Station microclimate bias correction**: urban heat island / wind-solar model spec'd, no code.
- **Historical regime classification (backfill tool ready, awaiting run)**: all existing `forecast_errors` rows are labeled `STABLE_HIGH`. The fix — `scripts/backfill_regimes.py` — fetches historical 82-member ensemble stats + CAPE from Open-Meteo, classifies each (city, obs_date) via `RegimeClassifier.classify_from_ensemble`, and `UPDATE`s rows in place. Optional `--refit` pass wipes `error_distributions` per city and re-fits per (model, regime, lead) bucket. Script tested against real DB (24,056 rows, 367 distinct dates across all cities). YOU RUN on Mac (Open-Meteo access). Two minor YOU IMPLEMENT markers flag where to verify Open-Meteo's ensemble response key-prefix and archive CAPE variable name.
- **HKO live observation fetch**: Hong Kong settlement falls through to ERA5 approximation. Real fix is scraping `www.weather.gov.hk` Daily Extract.
- **Scheduled reprice trigger** (idea 6): waiting on 2 weeks of lag-monitor data before deciding if this is worth building.

---

## 3. Realistic Performance Expectations

From Monte Carlo (5,000 paths × 200 trades) on the actual edge/price distribution produced by the pipeline:

| Scenario | Win rate (at edge) | Median final on $200 | % profitable | Time to 95% sig |
|----------|--------------------|-----------------------|--------------|------------------|
| Conservative (P=0.60 / market 0.50, edge 10¢) | ~60% | $408 | 99.3% | ~200 trades |
| Sweet spot (P=0.70 / 0.55, edge 15¢) | ~70% | $552 | 100% | ~80 trades |
| High-prob (P=0.85 / 0.70) | ~85% | $452 | 100% | ~40 trades |
| Realistic mix (P=0.58 / 0.42) | ~58% | $812 | — | ~147 trades |

**Honest annual projection on $200 capital: ~$1,500/year** at realistic mix, assuming 3 real signals/day and no regime break. **NOT** $500–1500/week — that figure only applies if capital scales to $5k+ AND calibration matures AND liquidity cooperates.

Paper results to date (Apr 15 scan): 44 contracts scanned → 7 flagged signals → 3 real (Seoul 18°C+ being the prototype) after bracket-parsing artifacts filtered. Seoul 30-day backtest: 2W/1L, −$10 net on $50 positions. The loss was a 2.7σ tail event (Mar 29), not model failure.

---

## 4. Polymarket Contract Mechanics

A bracket contract is a binary on Polygon. Pays $1 if the event happens, $0 otherwise. Market price = implied probability. Polymarket charges **2% fee on winnings only**, not on losses.

### 4.1 Payout math

```
n_shares = notional / entry_price

WIN:  pnl = n_shares * (1 - entry_price) * 0.98 - notional
LOSS: pnl = -notional
```

**Why `min_entry_price = 0.02` is enforced**: at entry_price=0.001 a WIN on $50 gives $48,950 (50,000× leverage). These entries are always bracket-parsing artifacts or dead markets. Filter at `strategy.py:analyze()`.

### 4.2 Edge equation

```
raw_edge = P_model - P_market
edge_after_fees ≈ raw_edge - 0.02 * p * (1 - P)
```

### 4.3 Tradeability gates (all must pass, in order)

Defined in `forecast.py:BracketProbabilityCalculator.edge()`:

1. Market price ∈ [0.15, 0.75] — avoid penny artifacts and crushed payoffs
2. Model probability ≥ 0.55
3. Tiered edge: `min_edge = 0.05 + max(0, (P - 0.50) × 0.40)` → 5¢ at P=0.50, 13¢ at P=0.70, 15¢ at P=0.75
4. Sharpe per trade ≥ 0.15: `edge / sqrt(p × (1-p))`
5. Quarter-Kelly × uncertainty shrinkage `1/(1 + CV²)` where `CV = prob_std / model_prob`
6. Per-position cap 2% of bankroll
7. Correlation group cap 8% of bankroll
8. Daily drawdown ≤ 5%
9. Token-id duplicate guard (don't re-enter already-open positions within the day)

---

## 5. Weather Alpha — Core Math

### 5.1 Bracket probability via CDF

For a forecast F with calibrated error distribution ε ~ D(μ, σ):

```
error = forecast - observed        (positive = model ran hot)

P(lower ≤ observed < upper)
  = P(lower ≤ F - ε < upper)
  = P(F - upper < ε ≤ F - lower)
  = CDF_error(F - lower) - CDF_error(F - upper)
```

**Distribution family selection** (MLE, fit per city/model/regime/lead):
- `|skew| < 0.3` and `excess_kurt < 1` → Normal
- `excess_kurt ≥ 1` → Student-t (fat tails, frontal events)
- `|skew| ≥ 0.3` → Skew-Normal (asymmetric, marine layer / cold-air damming)

**Ensemble**:
```
P(bracket) = Σ_i w_i * P(bracket | model i) / Σ_i w_i
```
Weights: GFS=0.30, ECMWF=0.50, HRRR=0.10, NAM=0.10. Because HRRR/NAM resolve to `gfs_seamless` in Open-Meteo's archive/previous-runs, they are **not** independent calibration sources — see §5.4. Normalize across all brackets in the market after computing.

### 5.2 The σ floor — non-negotiable safety net

```python
_SIGMA_FLOOR_F = 2.5  # in forecast.py
```

Applied inside `bracket_probability()` before the CDF is evaluated:
```python
sigma = max(error_dist.sigma, _SIGMA_FLOOR_F)
```

**Why it exists**: the archive API (`archive-api.open-meteo.com`) returns reanalysis — the model ran *after* the event with observations already assimilated. Errors vs station obs are ~0.9–1.5°F (artificially tight, 2–4× smaller than real operational forecast errors of ~2.5–4°F). Without a floor, bracket probabilities for 1°C brackets hit 71% spuriously.

Max `P(1°C bracket)` at σ=2.5°F is ~28%, which is why **narrow exact-degree brackets almost never fire** — see §5.7.

**Floor will become less necessary** as more real-forecast days (previous-runs API) accumulate in the 90-day window. After 90 days of daily operation the entire window is real forecasts and the floor can be relaxed.

### 5.3 Outlier filter

In `strategy.py:analyze()`, when loading calibrated distributions:
```python
if abs(dist.mu) > 5.0 or dist.sigma > 5.0: skip
```

Prevents corrupted distributions from leaking into inference. Typical offenders: LA/ECMWF (marine-layer over-forecast, μ=+6.18°F), Hong Kong/GFS pre-station-fix (μ=-8.78°F).

### 5.4 Calibration data sources — the critical distinction

| Source | API | What it is | Typical σ |
|--------|-----|------------|-----------|
| `archive-api.open-meteo.com` | `gfs_seamless` / `ecmwf_ifs025` | **Reanalysis** — model re-ran post-event with observations assimilated. NOT a real forecast. | 0.9–1.5°F (artificially tight) |
| `previous-runs-api.open-meteo.com` | same models | **True operational forecast** — stored output as issued 24h before | 2.5–4°F (correct, matches NWS) |

Pipeline in `strategy.py:calibrate()`:
- Last **90 days** → `fetch_archived_forecasts()` → previous-runs (real forecasts)
- Days **91–365** → `fetch_historical_highs()` → archive (reanalysis; protected by σ floor)
- Merge: real forecasts override reanalysis where both exist.

### 5.5 Why HRRR/NAM are skipped in calibration

Open-Meteo's archive maps HRRR and NAM both to `gfs_seamless`. Calibrating them separately creates identical distributions and effectively triples GFS weight. Fix:

```python
_ARCHIVE_CALIBRATION_MODELS = {WeatherModel.GFS, WeatherModel.ECMWF}
# at start of each city's calibration loop:
self.db.delete_distributions_for_models(city_key, [WeatherModel.HRRR, WeatherModel.NAM])
```

At inference, HRRR/NAM **are** used (Open-Meteo serves their live operational runs separately) but they borrow GFS error distributions via `_load_dists()` fallback. Update the ensemble weights in §5.1 accordingly — the effective independent-model weighting is GFS vs ECMWF, 0.50/0.50 on independent signal.

### 5.6 Synoptic regimes (5)

| Regime | Signature | Error shape | Trade intuition |
|--------|-----------|-------------|-----------------|
| STABLE_HIGH | spread < 1.5°C, clear | Tight, symmetric | Usually NO edge — market is right |
| FRONTAL_PASSAGE | spread > 4°C | Wide, bimodal | Buy both wings, sell middle (straddle) |
| CONVECTIVE | CAPE > 1000 J/kg | Right-skewed (cool surprises) | Cloud/storm timing uncertainty |
| MARINE_INFLUENCE | coastal, onshore flow | Left-skewed | Fog burn-off timing |
| TRANSITION | dP/dt > 4 hPa/12h | Fat-tailed | Largest edges, highest uncertainty |

Heuristic fallback when full classifier unavailable:
```python
if spread < 1.5: STABLE_HIGH
elif spread > 4.0: FRONTAL_PASSAGE
elif spread > 2.5: TRANSITION
else: STABLE_HIGH
```

**Current limitation**: historical ensemble spread is not reconstructed, so all calibration samples are labeled `STABLE_HIGH`. At inference the live regime is computed correctly, but the distribution used is always the `STABLE_HIGH`-labeled one. This under-prices frontal events.

### 5.7 Bracket geometry — "Why Seoul works, others don't"

Seoul "18°C or higher" is a bracket from 64.4°F to +∞ — span ~135°F. If forecast is 68°F, even σ=1°F gives P ≈ 99.9%. The signal is **geometric**, not a reflection of calibration quality.

Narrow exact-degree brackets (span 1–2°C, ~1.8–3.6°F) at σ=2.5°F (the floor) cap out at `P ≈ 28%`. These will **almost never** cross the 55% model-confidence gate. **This is correct behavior** — these brackets demand very precise forecasting, which requires real operational forecast data that's still accumulating.

**Rule of thumb for market selection**: prioritize wide brackets ("X or higher", "Y or below") until the calibration window is entirely real-forecast days.

### 5.8 Correlation groups

Defined in `risk.py`. Cap exposure per group at 8% of bankroll.
- East Asia: seoul, tokyo, shanghai, hong_kong
- Western Europe: london, amsterdam, munich, milan
- US Northeast: nyc, toronto, chicago
- US West: la, sf, seattle
- South Central: miami, atlanta, mexico_city
- South America: buenos_aires, sao_paulo
- Oceania/Middle East: wellington, sydney, dubai

---

## 6. Station Mappings (verified Apr 2026 from live Polymarket market pages)

All markets resolve via Weather Underground using the station listed in each market's rules.

| City | Station | Lat | Lon | TZ | °C? |
|------|---------|-----|-----|------|-----|
| nyc | KLGA (LaGuardia) | 40.7772 | -73.8726 | America/New_York | No |
| chicago | KORD (O'Hare) | 41.98 | -87.90 | America/Chicago | No |
| toronto | CYYZ (Pearson) | 43.68 | -79.63 | America/Toronto | Yes |
| miami | KMIA | 25.79 | -80.29 | America/New_York | No |
| atlanta | KATL (Hartsfield) | 33.64 | -84.43 | America/New_York | No |
| la | KLAX | 33.94 | -118.41 | America/Los_Angeles | No |
| sf | KSFO | 37.62 | -122.37 | America/Los_Angeles | No |
| seattle | KSEA | 47.45 | -122.31 | America/Los_Angeles | No |
| london | EGLC (City) | 51.5048 | 0.0498 | Europe/London | Yes |
| amsterdam | EHAM (Schiphol) | 52.31 | 4.76 | Europe/Amsterdam | Yes |
| munich | EDDM | 48.35 | 11.79 | Europe/Berlin | Yes |
| milan | LIMC (Malpensa) | 45.63 | 8.72 | Europe/Rome | Yes |
| seoul | RKSI (Incheon) | 37.4609 | 126.4407 | Asia/Seoul | Yes |
| tokyo | RJTT (Haneda) | 35.55 | 139.78 | Asia/Tokyo | Yes |
| hong_kong | HKO (Observatory) | 22.3023 | 114.1747 | Asia/Hong_Kong | Yes |
| shanghai | ZSPD (Pudong) | 31.1434 | 121.8083 | Asia/Shanghai | Yes |
| buenos_aires | SAEZ (Ezeiza) | -34.82 | -58.54 | America/Argentina/Buenos_Aires | Yes |
| sao_paulo | SBGR (Guarulhos) | -23.43 | -46.47 | America/Sao_Paulo | Yes |
| mexico_city | MMMX (Benito Juárez) | 19.44 | -99.07 | America/Mexico_City | Yes |
| wellington | NZWN | -41.33 | 174.81 | Pacific/Auckland | Yes |
| sydney | YSSY (Kingsford Smith) | -33.95 | 151.18 | Australia/Sydney | Yes |
| dubai | OMDB | 25.25 | 55.36 | Asia/Dubai | Yes |

**Exceptions that matter** (wrong station → systematic bias against us):
- **NYC**: KLGA, **NOT KJFK**. JFK reads 2–4°F cooler in summer (Atlantic sea breeze).
- **London**: EGLC (east, City Airport), **NOT EGLL** (Heathrow, west). EGLC ~1–2°C warmer on calm days.
- **Shanghai**: ZSPD (Pudong, east), **NOT ZSSS** (Hongqiao, west). 1–2°C delta from coastal exposure.
- **Seoul**: RKSI (Incheon), **NOT RKSS** (Gimpo). ~20km apart, stronger coastal influence at Incheon.
- **Hong Kong**: HKO (Kowloon), **NOT VHHH** (Lantau airport, 30km away). Polymarket uses `www.weather.gov.hk` Daily Extract. IEM has no HKO → forces ERA5 fallback at observatory coords.

---

## 7. File Structure

```
polymarket_strat/
├── main.py                       # CLI: autotrade / weather-calibrate / weather-analyze / backtest / positions / settle / report
├── config.py                     # TradingConstraints, WeatherConfig, PortfolioState, AccountConfig
├── strategy.py                   # Strategy registry
├── risk.py                       # PortfolioRiskManager
├── execution.py                  # PaperExecutor + LiveExecutor
├── api.py                        # PolymarketPublicClient (Gamma, Data API)
├── backtest.py                   # Historical validation (thin wrapper)
├── monitor.py                    # WhaleMonitor + InsiderAnomalyDetector (secondary alphas)
├── whale_positions.py
├── sample_data.py
├── domain/
│   ├── models.py                 # StrategySignal, TradePlan, etc.
│   ├── weather/
│   │   ├── models.py             # CITY_REGISTRY, CityStation, ErrorDistribution, BracketContract
│   │   ├── forecast.py           # BracketProbabilityCalculator (CDF, Kelly, edge gates, σ floor)
│   │   ├── calibration.py        # ErrorDistributionFitter (MLE + fit_bayesian with PyMC)
│   │   └── strategy.py           # WeatherBracketStrategy
│   └── strategies/
│       ├── mispricing.py
│       ├── whale_following.py
│       └── insider_detection.py
├── infrastructure/
│   ├── real_data.py
│   └── weather/
│       ├── grib_client.py        # Open-Meteo JSON API wrapper (forecast/archive/previous-runs/era5/ensemble)
│       ├── station_client.py     # IEM ASOS fetcher
│       ├── market_scanner.py     # Polymarket weather contract parser
│       └── persistence.py        # SQLite CRUD (WAL + online backup())
├── application/service.py        # StrategyApplicationService
├── presentation/reporting.py     # HTML report generator
├── notifications/telegram.py
├── data/weather/weather.db
├── reports/                      # Generated HTML
├── runtime/                      # Portfolio state JSON
├── tests/                        # test_strategy.py, test_settle.py (24 passing)
└── lambda_handler.py

tools/                            # Operator tooling (read-only from the DB)
├── dashboard/
│   ├── app.py                    # Streamlit read-only dashboard (8 panels)
│   ├── README.md                 # Tailscale + launchd walkthrough
│   └── logs/                     # dashboard.stdout / dashboard.stderr
├── lag_monitor/
│   ├── monitor.py                # polls forecast + price, emits JSONL events
│   ├── analyze.py                # joins events, reports lag percentiles + decision
│   └── logs/events.jsonl         # JSONL event log
└── walk_forward/
    ├── backtest.py               # no-look-ahead refit at each eval date
    └── last_run.csv              # picked up by the dashboard

scripts/
├── backup_db.py                  # SQLite online-backup with rotation
├── backfill_calibration.py
├── backfill_regimes.py           # classify historical obs_dates into 5 regimes, UPDATE forecast_errors, optional --refit
├── prepare_real_data.py
├── test_insider.py
└── start_lag_monitor.sh          # nohup-friendly lag-monitor launcher

deploy/
├── lambda_setup.md                                     # Lambda packaging docs
├── package_lambda.sh                                   # Lambda zip builder
├── com.wonsang.polymarket-dashboard.plist.template     # launchd agent template
└── install_dashboard_agent.sh                          # one-shot: fill template, load, start
```

---

## 8. Commands

### Pipeline
```bash
polymarket-strat weather-calibrate                          # all cities, 365-day lookback
polymarket-strat weather-calibrate --cities seoul,nyc       # comma-sep, no spaces
polymarket-strat weather-analyze                            # generate signals, no execution
polymarket-strat autotrade                                  # paper mode default
polymarket-strat positions
polymarket-strat settle --auto                              # auto-settle resolved positions
polymarket-strat backtest --city seoul --days 30
polymarket-strat report
```

### Tooling
```bash
# Dashboard (Tailscale + launchd auto-start)
./deploy/install_dashboard_agent.sh                   # auto-detect IP + streamlit
./deploy/install_dashboard_agent.sh --ip 100.x.y.z    # explicit tailnet IP
./deploy/install_dashboard_agent.sh --uninstall

# Lag monitor
./scripts/start_lag_monitor.sh                        # default watchlist
./scripts/start_lag_monitor.sh seoul,nyc,london
./scripts/start_lag_monitor.sh --all-cities
python tools/lag_monitor/analyze.py                   # report

# Walk-forward backtest
python tools/walk_forward/backtest.py --city seoul --model gfs \
    --start 2025-08-01 --end 2026-04-10
python tools/walk_forward/backtest.py --all-cities --all-models \
    --start 2026-01-01 --end 2026-04-10 \
    --csv tools/walk_forward/last_run.csv             # dashboard picks this up
python tools/walk_forward/backtest.py --city seoul --bayesian \
    --start 2026-01-01 --end 2026-04-10               # PyMC, slow

# DB backups (cron-able)
python scripts/backup_db.py                           # default --keep 14
python scripts/backup_db.py --dest /Volumes/External --keep 30
python scripts/backup_db.py --dry-run

# Historical regime backfill (fixes the all-STABLE_HIGH bug)
python scripts/backfill_regimes.py --dry-run                         # preview histogram, no writes
python scripts/backfill_regimes.py --cities seoul --refit            # one city, then re-fit dists
python scripts/backfill_regimes.py --refit                           # all cities, full year, refit
python scripts/backfill_regimes.py --start 2025-04-01 --end 2026-04-10 --refit
```

---

## 9. SQLite Schema (`data/weather/weather.db`)

| Table | Columns |
|-------|---------|
| `forecasts` | city, model, init_time, valid_time, lead_hours, forecast_high_f, ensemble_spread_f |
| `observations` | city, station_id, obs_date, observed_high_f, source ("IEM" or "ERA5") |
| `forecast_errors` | city, model, regime, lead_hours, error_f, obs_date |
| `error_distributions` | city, model, regime, lead_hours, family, mu, sigma, shape, nu, n_samples |
| `trade_history` | city, target_date, bracket bounds, model_prob, market_prob, edge, kelly_fraction, notional, entry_price, side, mode, market_id, token_id, question, regime, outcome, pnl, settled_at |

---

## 10. Bug Fixes Applied (history — read once, don't repeat)

1. **Test patch path** — `_settle_from_iem` imports `StationObservationClient` locally inside the function; patched symbol at `polymarket_strat.infrastructure.weather.station_client.StationObservationClient`, not at `main`. All 24 tests green.
2. **Fake P&L from sub-2¢ entries** → added `TradingConstraints.min_entry_price = 0.02`, filter in `strategy.analyze()`.
3. **σ 2–4× too tight (reanalysis artifact)** → `_SIGMA_FLOOR_F = 2.5` in `forecast.py`, applied in `bracket_probability()`. Also wired `fetch_archived_forecasts()` via previous-runs for last 90 days.
4. **HRRR/NAM 3× GFS weighting** → skipped in calibration; at inference they borrow GFS distributions.
5. **Outlier distributions** → filter `|μ| > 5°F or σ > 5°F` in `_load_dists()`.
6. **ERA5 fallback for non-US** → `fetch_era5_observations()` in `grib_client.py`; used when IEM returns empty.
7. **Duplicate-position guard** → `run_execute()` loads `already_open_token_ids` from DB, skips re-entries.
8. **CLI `--cities` format** → `type=lambda s: s.split(",")` requires `--cities a,b,c` (comma-separated, no spaces). Help text clarified.
9. **Wrong station IDs** → five corrected in `CITY_REGISTRY` (NYC→KLGA, London→EGLC, Shanghai→ZSPD, Seoul→RKSI, Hong Kong→HKO). Re-run calibration for all 5.
10. **Python 3.10 `datetime.UTC`** → replaced with `from datetime import timezone; UTC = timezone.utc` across 7 files.

---

## 11. Deployment Phases

- **Phase 1 (now, 30+ days)**: Paper. Log every signal. Verify suspicious edges (>20¢) manually before they fire. Expand calibration window.
- **Phase 2 gate (Day 30)**: Win rate ≥ 55%, cumulative P&L positive on paper, no bracket parsing artifacts. Go/No-Go.
- **Phase 3**: $200 real capital. $10–20/position, max 2 cities. Verify paper→live price replication.
- **Phase 4**: $50 max/bracket, 5–15 trades/day across 9+ cities. Expected steady-state: $30–100/week at $200, scaling with bankroll.

`.env` required for live mode: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

---

## 12. Where Edge Comes From (and Where It Dies)

**Sources of edge** (honest ranking):
1. **Temporal**: GFS/ECMWF update → Polymarket repricing lag 30 min – 2 hours. Speed edge, not analytical superiority.
2. **Better ensemble**: skill-weighted GFS+ECMWF beats checking one weather app. Genuine.
3. **Bracket geometry**: market often misprices wide "X or higher" brackets when forecast is clearly outside threshold.
4. **Weak competition**: recreational bettors, low liquidity. Bar is low.

**Where it dies**:
1. Most contracts have no tradeable edge (90%+ or 10%- brackets, crushed payoffs).
2. Sweet spot is 30–70% probability. Narrow brackets fail the σ-floor math — correctly.
3. Realistic frequency: 3–8 weather signals/day across all cities, NOT 15+.
4. Liquidity: $20K–$400K/contract. Position-size ceiling binds fast.
5. If a meteorology PhD or prop shop enters, edge evaporates.
6. Compounding is real but lumpy — single-day −$50 drawdowns normal.

---

## 13. Dependencies

```
numpy, scipy, pandas
requests, aiohttp                     # Open-Meteo, IEM, Polymarket
py_clob_client, web3                  # execution + wallet
pymc, arviz                           # Bayesian (fit_bayesian still stub)
sqlite3                               # stdlib
```

**NOT in use** (despite earlier spec implying otherwise): `xarray`, `cfgrib`, `eccodes`. No raw GRIB parsing. All model data comes through Open-Meteo's JSON endpoints.

---

## 14. Today's Known Priorities (in order)

1. **Backfill historical regimes** (the #1 calibration bug). Run `python scripts/backfill_regimes.py --dry-run` first to inspect the per-city regime histogram; if distributions look sane (expect roughly 50–70% STABLE_HIGH, 10–20% TRANSITION, 5–15% FRONTAL_PASSAGE, remainder CONVECTIVE / MARINE), run `python scripts/backfill_regimes.py --refit` to apply UPDATEs and re-fit `error_distributions`. After the refit, re-run the walk-forward backtest — skill score should bump from +0.455 toward +0.48–0.50 because FRONTAL_PASSAGE days finally get a wider σ.
2. **Re-run calibration** for the 5 stations that changed (nyc, london, shanghai, seoul, hong_kong). After re-calibration, re-run `python tools/walk_forward/backtest.py --all-cities --all-models --csv tools/walk_forward/last_run.csv` so the dashboard shows current skill scores.
2. **Start the lag monitor** (`./scripts/start_lag_monitor.sh`) and let it collect ≥ 2 weeks of data. Then run `python tools/lag_monitor/analyze.py` to decide whether scheduled-reprice (idea 6) is worth building.
3. **Install Tailscale + dashboard agent** on the Mac that runs the pipeline. One-shot: `./deploy/install_dashboard_agent.sh`. Phone-accessible monitoring is required discipline through Phase 2 — if you can't see the account from anywhere, you won't catch a live issue.
4. **Verify** the 2 suspiciously-large-edge signals (London 17–18°C +45.9¢, Toronto sub-16°C +57.9¢) are bracket-parsing artifacts before letting them fire even in paper.
5. **Log every paper signal** through Day 30 gate. Do not deploy real capital before the gate.
6. **Wire `fit_bayesian()` into live `calibrate()`** only after Phase 2 clears. The function is implemented (`calibration.py:fit_bayesian` + `summarize_posterior`) but live calibration still uses MLE. Toggle only when paper P&L earns the right to a more complex pipeline.
7. Musk alpha stays frozen until weather clears Phase 2.

### Completed since last spec update
- WAL mode + rotated SQLite backups (`scripts/backup_db.py`).
- Lag monitor (`tools/lag_monitor/`) — ready to deploy; waiting on 2 weeks of data.
- `fit_bayesian()` + `summarize_posterior()` implemented with PyMC.
- Walk-forward backtest (`tools/walk_forward/backtest.py`). Last full run: overall Brier skill score +0.455 across 22 cities × 2 models × 100 days.
- Read-only Streamlit dashboard + Tailscale + launchd auto-start.

Cross-referenced files: `MUSK_SPEC.md` for tweet alpha design, `reports/profitability_analysis.html` for MC equity-curve math, `reports/weather_alpha_workflow.html` for end-to-end pipeline diagram, `tools/dashboard/README.md` for remote-monitoring setup.
