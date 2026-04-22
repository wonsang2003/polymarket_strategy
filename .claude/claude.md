# POLYMARKET QUANTITATIVE TRADING SYSTEM — WORKING SPEC

**Last Updated:** April 19, 2026
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

Polymarket charges 2% on WINNINGS ONLY (profit above notional), not on the full payout. So the correct derivation is:

```
n_shares    = notional / entry_price            # shares bought with notional at entry_price
gross_profit = n_shares - notional              # = n_shares * (1 - entry_price)  (notional = n_shares * entry_price)
fee          = 0.02 * gross_profit
pnl_on_win   = gross_profit - fee = n_shares * (1 - entry_price) * 0.98

WIN:  pnl = n_shares * (1 - entry_price) * 0.98
LOSS: pnl = -notional
```

**Worked example** — $50 notional at entry_price 0.81:
- n_shares = 50 / 0.81 = 61.73
- gross payout on WIN = $61.73  →  gross profit = $11.73  →  fee = $0.23
- **Net WIN P&L = $11.50** (matches `n_shares * (1 - entry_price) * 0.98 = 61.73 * 0.19 * 0.98`)

**⚠ KNOWN BUG (Apr 19 2026):** `main.py:143` currently computes `n_shares * (1 - fee) - notional = n_shares * 0.98 - notional`, which applies the 2% fee to the *entire payout* rather than just winnings. This under-reports P&L by exactly `notional * 0.02` (= $1 per $50 win). The tests in `tests/test_settle.py:TestPnl` encode the same wrong formula, so fixing the bug in `main.py` requires updating those tests to the correct expected values. Live paper P&L is therefore ~$1/win more conservative than real Polymarket payout — clearing the Day 30 gate on paper means live will perform slightly better than the tape shows.

**Why `min_entry_price = 0.02` is enforced**: at entry_price=0.001 a WIN on $50 gives ~$49,000 in gross profit (50,000× leverage before fee). These entries are always bracket-parsing artifacts or dead markets. Filter at `strategy.py:analyze()`.

### 4.2 Edge equation

```
raw_edge = P_model - P_market
edge_after_fees ≈ raw_edge - 0.02 * p * (1 - P)
```

### 4.3 Tradeability gates (Apr 19 2026 refactor — all must pass, in order)

Defined in `forecast.py:BracketProbabilityCalculator.edge()` + `strategy.py:analyze()`:

1. Market price ∈ [0.15, 0.75] — avoid penny artifacts and crushed payoffs
2. **Flat min-edge ≥ 5¢ after fees** — replaces the prior tiered schedule
3. Quarter-Kelly × uncertainty shrinkage `1/(1 + CV²)` where `CV = prob_std / model_prob`
4. **Per-position cap: `max(0.05 × bankroll, $10)`** — 5% sizing with $10 floor (was 2% fixed)
5. **Correlation group cap: 15% of bankroll notional** (was 8%, count-based 2 positions/group)
6. **Daily drawdown brake: 14% cumulative settled P&L** (was 5%, scaled to new per-position sizing)
7. Token-id duplicate guard (don't re-enter already-open positions within the day)
8. Min entry price 0.02 (50×+ implied leverage filter)

**Gates EXPLICITLY REMOVED in this refactor:**
- **`P_model ≥ 0.55` dropped.** Killed multi-bracket spread trades where no single bracket
  crosses 55% but edge is real (Tokyo Apr 20 example: three brackets at market ~33% each,
  model says one is 40% — valid 7¢ edge that the old gate rejected).
- **Tiered edge schedule dropped.** Prior formula `0.05 + max(0, (P-0.50) × 0.40)` demanded
  up to 15¢ at P=0.75. Walk-forward evidence (Apr 19, n=34,056 per lead) showed this
  over-filters legitimate edges.
- **Sharpe-per-trade ≥ 0.15 dropped.** Redundant at flat 5¢: the worst achievable
  Sharpe under market-band + 5¢ floor is `0.05 / sqrt(0.75 × 0.25) = 0.115`, so any
  threshold ≤ 0.115 is dead code. Dropping avoids false precision.

**Side-bias:** currently one-sided (buy-YES only). Negative-edge markets where the
market overprices YES (→ buy-NO opportunity) are NOT flagged. Shorting is a separate
implementation pass — tracked as backlog.

**Bankroll scaling of per-position cap:** at $500 bankroll → $25, at $1k → $50, at $5k → $250.
Consistent risk profile across bankroll sizes, no $50 hard ceiling that capped growth
past $1k. Floor at $10 for small bankrolls where Polymarket's order-size mechanics bite.

**Daily DD interaction with per-position:** at 14% DD / 5% per-position, account tolerates
2.8 full-loss positions before the brake locks the day. If you tighten per-position or
loosen DD, preserve this ratio.

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
- **Multi-lead (Apr 19 2026)**: the loop now iterates `_CALIBRATION_LEAD_SCHEDULE = [(24, 1, True), (48, 2, False)]`. For each (lead_hours, lead_days, allow_reanalysis) tuple it calls `fetch_archived_forecasts(..., lead_days=N)` — Open-Meteo exposes N-day-lead previous runs via the `temperature_2m_max_previous_day{N-1}` daily variable. Reanalysis fill-in is 24h-only (a "48h reanalysis" is conceptually undefined — archive is observation-assimilated). `forecast_errors` rows are saved tagged with the correct `lead_hours`, so `_load_dists()` at inference hits the right bucket directly for today (24h) and tomorrow (48h) contracts. The √(lead/24) σ-scaling fallback in `_load_dists()` is now a safety net, not the primary path. 72h+ leads still fall through to σ-scaling.

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
python scripts/backfill_regimes.py --lead-hours 48 --refit           # 48h-only (preserves 24h labels)
python scripts/backfill_regimes.py --lead-hours 48 --dry-run         # preview 48h reclassification
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
  - **Scope**: ALL 22 cities × both leads (24h + 48h). Do NOT pre-filter cities by walk-forward brier — let the strategy gates (`forecast.py:edge()`) and risk manager (`risk.py`) filter signals. Pre-filtering is premature optimization and cuts upside.
  - **Post-hoc triage only**: after n ≥ 30 trades on a specific (city, lead) pair, if realized EV is negative, block that pair in `strategy.py:analyze()`. `trade_history` already logs per-trade city + lead + PnL — query it for the block decision. This is data-driven vs. pre-hoc brier cutoff.
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

1. **Run the 48h regime backfill (remaining calibration gap).** 24h leads already have proper regime-aware fits — April 19 breakdown showed `stable_high`, `transition`, and `frontal_passage` at 24h for both GFS and ECMWF. **Only 48h rows are still all `STABLE_HIGH`** because the new multi-lead calibration loop writes that label by default. Script extended (Apr 19 2026) — run: `python scripts/backfill_regimes.py --lead-hours 48 --refit` (preview first with `--lead-hours 48 --dry-run`). The `--lead-hours 48` flag scopes both the SELECT and UPDATE to the 48h bucket so already-correct 24h labels are preserved. After refit, re-run walk-forward at 48h to validate. Expected: 48h Brier skill ↑ from +0.421 toward ~+0.45 because FRONTAL_PASSAGE / TRANSITION days get correct σ instead of the narrow stable_high fit.
2. **Start the lag monitor** and let it collect ≥ **4 days** of data (not 2 weeks). Use `caffeinate -i` on Mac so laptop-sleep doesn't kill the run: `caffeinate -i ./scripts/start_lag_monitor.sh &`. Then run `python tools/lag_monitor/analyze.py` to decide whether scheduled-reprice (idea 6) is worth building. Rationale for 4 days: at ~30 price_change events/hour × 96h = ~2,880 samples, percentile estimates are ±8% CI — sharp enough for a "30 min vs 2 hr" repricing-cadence decision. If result is borderline (e.g., median lag ~45 min), extend the run rather than guess.
3. **Install Tailscale + dashboard agent** on the Mac that runs the pipeline. One-shot: `./deploy/install_dashboard_agent.sh`. Phone-accessible monitoring is required discipline through Phase 2 — if you can't see the account from anywhere, you won't catch a live issue.
4. **Verify** the 2 suspiciously-large-edge signals (London 17–18°C +45.9¢, Toronto sub-16°C +57.9¢) are bracket-parsing artifacts before letting them fire even in paper.
5. **Log every paper signal** through Day 30 gate. Do not deploy real capital before the gate.
6. **Wire `fit_bayesian()` into live `calibrate()`** only after Phase 2 clears. The function is implemented (`calibration.py:fit_bayesian` + `summarize_posterior`) but live calibration still uses MLE. Toggle only when paper P&L earns the right to a more complex pipeline.
7. **Shrink 24h reliability gap (under-confidence in [0.4, 0.7) bins).** April 19 walk-forward showed systematic −10% gap in mid-confidence bins — model predicts 65%, reality is 77%. 48h is already well-calibrated (gap < 8%); this is a 24h-specific issue. Ranked fixes:
   - **7a. σ floor relaxation** (highest-leverage structural fix). `_SIGMA_FLOOR_F = 2.5` in `forecast.py` was originally protection against reanalysis-tight σ. Now that 91 days of real-forecast previous-runs data exists per (city, model, 24h), drop the floor to 2.0°F when `n_real ≥ 60`, or remove entirely for those buckets. Expected gap shrinkage 30-50%.
   - **7b. Real-forecast sample weighting in MLE.** `fit_error_distribution()` currently weights all samples equally. In the 365-day window, 91 real-forecast days are drowned by 275 reanalysis days (artificially tight σ inflates the reanalysis contribution, pulling the fit toward a narrower distribution than truth). Fix: pass `sample_weight = 1.0 for real, 0.3 for reanalysis` into `scipy.optimize.minimize` MLE. Expected gap shrinkage 20-40%.
   - **7c. Post-hoc isotonic calibration.** Fit `sklearn.isotonic.IsotonicRegression` on last walk-forward CSV (`mean_pred → hit_rate`), save as per-(city, lead) lookup, apply in `bracket_probability()` as the last step before returning P. Dataset-free to re-fit nightly. Textbook answer. Expected gap shrinkage 60-80%.
   - **7d. Temperature scaling.** Single-param sharpening `P' = P^τ / (P^τ + (1-P)^τ)`, fit τ to minimize log-loss on walk-forward CSV. Under-confidence → τ > 1 will sharpen mid-range predictions. Interpretable. Expected gap shrinkage 70-90% in [0.4, 0.7) specifically.
   - **Recommended sequence**: 7a first (structural, compounds with everything else), then 7c or 7d (post-hoc safety net). Don't do 7b and 7a simultaneously — verify each independently via walk-forward rerun so you know which lever moved the gap.
8. Musk alpha stays frozen until weather clears Phase 2.

### Completed since last spec update
- **Settlement conditionId-vs-id fix (Apr 23 2026)**: root cause of "positions never resolve on EC2 even when outcomePrices is 1.0". `_resolve_via_polymarket` in `main.py` calls `client.get_market(mkt_id)` which hit `GET /markets/{mkt_id}`. Gamma's path parameter accepts only the numeric `id`, but `market_scanner.py:291` was saving `str(market.get("conditionId") or market.get("id"))` → every existing trade_history row has a 0x-hex conditionId in `market_id`, and every hourly settle call returned 404 → `_resolve_via_polymarket → None` → IEM fallback (gated on `target_date < today`) → same-day resolutions stuck forever. Two-part fix:
  - `api.py::get_market` now detects `0x`-prefix and routes to `GET /markets?condition_ids=<id>&limit=1` (list endpoint with filter, which *does* accept the hash form), returning the single hit or `{}`. Numeric ids still hit the path endpoint. Empty string → `{}` with no RPC (guards against NULL legacy rows).
  - `market_scanner.py:291` flipped the fallback so new rows prefer numeric `id`; `conditionId` is now the fallback. Legacy rows remain resolvable via the `api.py` dispatch — no migration script needed.
  - Tests: 6 new cases in `tests/test_settle.py::TestGammaGetMarketDispatch` pin numeric→path, conditionId→list, uppercase `0X`, whitespace strip, empty-id short-circuit, empty-list-returns-empty-dict. Existing 10 `TestResolveViaPolymarket` cases still green (they mock `get_market` so they're orthogonal to the dispatch). Full suite: 210 passed, 2 pre-existing sandbox PermissionErrors (not caused by this fix).
  - **EC2 deploy**: `git pull && python -m polymarket_strat.main settle --auto` on the hourly-cron Mac/EC2. First post-fix run should clear the backlog of same-day-resolved-but-never-settled rows in one pass. No DB migration needed.
- WAL mode + rotated SQLite backups (`scripts/backup_db.py`).
- Lag monitor (`tools/lag_monitor/`) — ready to deploy; waiting on 2 weeks of data.
- `fit_bayesian()` + `summarize_posterior()` implemented with PyMC.
- Walk-forward backtest (`tools/walk_forward/backtest.py`). Last full run: overall Brier skill score +0.455 across 22 cities × 2 models × 100 days. `--lead-hours` flag already present — run with `--lead-hours 48` after the recalibration below to validate tomorrow-contract skill.
- Read-only Streamlit dashboard + Tailscale + launchd auto-start.
- **Payout formula fix (Apr 19 2026)**: `main.py::_pnl` now computes `n_shares * (1 - entry_price) * (1 - fee)` — 2% fee on winnings only, matching Polymarket's actual payout. `tests/test_settle.py` asserts updated accordingly. See §4.1.
- **Forecast horizon fix (Apr 19 2026)**: `strategy.py::_analyze_weather_brackets` now computes `lead_hours` per contract from `(target_date - today).days` and threads it into `fetch_all_models(..., lead_hours=lead_hours)`. Forecast cache re-keyed on `(city, lead_hours)` so today-24h and tomorrow-48h contracts each get the correct-horizon forecast. `_load_dists` has a √(lead/24) σ-scaling safety net for leads missing from calibration data.
- **Local-time lead + 48h hard cap (Apr 19 2026 late)**: `strategy.py::analyze` now derives `lead_hours` from station-local wall-clock time against a 17:00-station-local lock-in point instead of a UTC date-diff. Three failure modes of the UTC formula are eliminated: (a) Asian-station timezone off-by-one where 09:00 local / 00:00 UTC read as "yesterday", (b) same-contract lead_hours at 06:00 vs 20:00 local despite >14h of real horizon difference, (c) the documented `+ 1` bump that routed D+2 contracts into the 72h bucket. Root cause of the Apr 19 2026 Mexico City 27°C+ false positive (model_prob 72.66% vs market 20%, 51.5¢ edge): D+2 contract hit the 72h bucket, σ-scaling fallback scaled the 24h frontal_passage dist by √3, σ floor clamped to 2.5°F, and the 72h forecast itself ran hotter (GFS 81.6°F vs 78.0°F at 48h; ECMWF 83.7°F vs 81.4°F) — compounding into the inflated probability. New formula: `settlement_local = datetime.combine(target_date, time(17,0), tzinfo=ZoneInfo(station.timezone)); raw_lead_h = (settlement_local - datetime.now(tz)).total_seconds()/3600`. Four new `gate_rejects` counters propagate into `StrategyAnalysis.diagnostics`: `missing_timezone`, `past_settlement` (raw_lead ≤ 0), `too_close_to_settlement` (< 6h — observation likely captured or noise dominates), `beyond_calibration_horizon` (> 48h — hard cap, see below). Module constant `_LOCK_IN_LOCAL = time(hour=17)` is a defensible temperate-latitude default; tropical stations (Dubai, Singapore, Hong Kong summer) max earlier so 17:00 over-buffers, with cost limited to a few extra `too_close_to_settlement` skips near the boundary. Imports added: `time` from `datetime`, `ZoneInfo` from `zoneinfo`.
- **48h hard cap on tradeable horizon (Apr 19 2026 late)**: `strategy.py::analyze` now refuses to process any contract with `raw_lead_h > 48.0`, bumping `gate_rejects["beyond_calibration_horizon"]` instead. Rationale: `_CALIBRATION_LEAD_SCHEDULE = [(24, 1, True), (48, 2, False)]` only fits 24h and 48h buckets from real-forecast previous-runs data. Beyond 48h, `_load_dists` falls through to the `ErrorDistribution(sigma=base.sigma * √(bucket/24))` scaling approximation, which assumes random-walk error growth but in practice pairs a too-narrow σ with a potentially-hot long-lead forecast (the Mexico City pathology). Better to skip than to trade on a distribution we didn't calibrate. When/if calibration coverage is extended to 72h, relax this cap and the σ-scaling path reverts to a true safety net. The `_bucket_lead` list `[6, 12, 24, 48, 72]` remains unchanged since intermediate buckets (6, 12, 36) can still be hit by today/tomorrow contracts at specific times of day — those fall through to √(lead/24) scaling from 24h, which is far less inflationary than √3 scaling at 72h.
- **Multi-lead calibration (Apr 19 2026)**: calibration loop in `strategy.py::calibrate()` iterates `_CALIBRATION_LEAD_SCHEDULE = [(24, 1, True), (48, 2, False)]`, fetching both 24h and 48h previous-runs forecasts from Open-Meteo and saving `forecast_errors` with correct `lead_hours`. `fetch_archived_forecasts` in `grib_client.py` now accepts `lead_days` and uses Open-Meteo's `temperature_2m_max_previous_day{N}` variable-suffix convention. YOU VERIFY on first run: confirm Open-Meteo response keys for `previous_day2` — some endpoints echo the suffix, some collapse it. Response parsing accepts any key prefixed with `temperature_2m_max`.
- **Market cutoff fix (Apr 19 2026, revised)**: `market_scanner.py` uses Polymarket's authoritative `endDateIso`/`closed`/`acceptingOrders` fields as the primary trade-cutoff check. The fixed 16:00-station-local heuristic remains as a fallback only when those fields are missing. Also drops stale resolution-date contracts (>1 day in the past) defensively. **Refined Apr 19 late**: `endDate` (note: no `Iso` suffix) is date-only (e.g. `"2026-04-19"`) and parses as midnight UTC, which caused the scanner to treat every same-day-resolution market as "already closed" for the first 16+ hours of UTC — silently dropping the entire Asia/Europe trading window of 24h-lead contracts. Fix: only accept `endDateIso`/`end_date_iso`, and additionally require a `"T"` in the string (so any future date-only field can't regress this). Fallback to the 16:00 station-local heuristic when no ISO timestamp is present. This was the root cause of the "0 signals every hour" EC2 cron behavior for the 24h-lead bucket.
- **48h hourly-based fetch landed (Apr 19 2026)**: `grib_client.py::fetch_archived_forecasts` for `lead_days >= 2` now uses the hourly endpoint with `temperature_2m_previous_day{N-1}` and groups by station-local date to recover daily max. Open-Meteo's `_previous_day{N}` suffix only exists on hourly variables, not on daily aggregates. First live run (Apr 19): 91 real-forecast days / (city, model) at 48h lead, 44 distributions fit at 48h. `scripts/verify_48h_fetch.py` is a 5-second smoke test for future regressions.
- **Walk-forward verified at both leads (Apr 19 2026)**: 22 cities × 2 models, window 2026-01-15 → 2026-04-10, n = 34,056 predictions per lead. **24h Brier skill +0.479**, **48h Brier skill +0.421**. Skill drop only 12% at 48h lead — tomorrow contracts are tradeable. Reliability analysis: 24h is systematically **under-confident** in [0.4, 0.7) bins (−10% gap), 48h is well-calibrated (gap < 8%). CSVs at `tools/walk_forward/last_run_24h.csv` and `last_run_48h.csv`. See §14 priority 7 for gap-shrinkage options.
- **Phase 2 scope confirmed (Apr 19 2026)**: paper trading at ALL 22 cities × both leads. Per-city × lead realized P&L logged in `trade_history`. Post-hoc block if (city, lead) EV < 0 after n ≥ 30 trades. No pre-hoc pruning — upstream gates handle the filtering.
- **Tradeability gate + risk-param refactor (Apr 19 2026 late)**: simplified `forecast.py::edge()` from four gates to two (market-band + flat 5¢ min-edge), and re-parameterized position sizing / correlation-group / daily-DD to scale with bankroll. Motivation: the prior config was a "$200 tuning" — fixed $50 per-position cap capped compounding past $1k, 2% per-position was quarter-Kelly-implied but ignored the $10 Polymarket floor at small bankrolls, and the tiered edge + Sharpe gates over-filtered multi-bracket spread markets where no single bracket crossed the 0.55 model-prob floor (Tokyo Apr 20 was the motivating case). Changes wired:
  - `config.py`: added `max_position_fraction=0.05`, `min_position_notional_usd=10.0`, `max_correlation_group_fraction=0.15`, `max_daily_drawdown=0.14`, `min_edge_flat=0.05` to `TradingConstraints`. All old fields preserved for backward compat.
  - `forecast.py::edge()`: removed `model_prob ≥ 0.55` gate, removed tiered-edge formula, removed Sharpe-per-trade ≥ 0.15 gate. Signature now `edge(*, model_prob, market_prob, fee_rate=0.02, min_edge=0.05)` — explicit `min_edge` parameter (defaults to 5¢) so callers can override per-cycle. Worst-case Sharpe under the remaining market-band + flat-edge filter is `0.05 / sqrt(0.75×0.25) = 0.115`, making any Sharpe gate ≤ 0.115 dead code — dropping it rather than keeping vestigial logic.
  - `strategy.py::analyze()`: gate_rejects histogram reduced from `gate1_market_band / gate2_model_conf / gate3_min_edge / gate4_sharpe / strategy_min_edge` to `gate1_market_band / gate2_min_edge`. Correlation-group enforcement switched from count-based (`max_positions_per_group=2`) to notional-based (`constraints.bankroll × 0.15`) — a new trade is rejected if `group_notional[group] + target_notional > group_notional_cap`. Per-position sizing is now `min(kelly × bankroll, max(bankroll × 0.05, $10))` — the floor prevents the cap from degenerating below $10 on small bankrolls (at $200 bankroll, 5% = $10 exactly, so the floor only bites below $200). Diagnostics output renamed `correlation_group_counts` → `correlation_group_notional`, with new `per_position_cap` + `group_notional_cap` fields for dashboard visibility.
  - `main.py::run_autotrade`: `drawdown_brake_pct` default changed from hardcoded `0.05` to `None`, resolved at call-time from `constraints.max_daily_drawdown` (14%). Daily-DD brake now tolerates 2.8 full-loss positions before locking, preserving the ratio with the new per-position sizing. Explicit override via function kwarg still works for backtests / CLI experimentation.
  - **Side-bias unchanged**: still one-sided (buy-YES only). Negative-edge markets (market overpricing YES → buy-NO) are NOT flagged. Shorting remains a separate implementation pass — tracked in backlog.
  - Tests (`tests/test_settle.py` + `test_strategy.py`) do not exercise the dropped gates, so 24 passing tests remain green. Worth adding a future `test_forecast_edge.py` to pin the new 2-gate contract against regressions.
  - Expected signal volume: should rise 2-4× per cycle. Tokyo-style spread markets (no bracket ≥ 0.55) now pass, and `0.15 ≤ P_market ≤ 0.75` × flat-5¢ is a much wider acceptance set than `0.55 ≤ P_model` × tiered-up-to-15¢. Phase 2 post-hoc (city, lead) EV block remains the backstop if bad trades leak through.
- **HRRR/NAM long-lead inference gate (Apr 19 2026)**: `strategy.py::_analyze_weather_brackets` now drops HRRR/NAM forecasts when `lead_hours > 36` before regime classification + `_load_dists`. First live e2e_verify run showed HRRR at 48h returning values 14-28°F colder than GFS/ECMWF agreement for NYC/Chicago/Toronto/Atlanta (contaminating the 0.10-weighted ensemble). HRRR's designed horizon is ~18h (extended ~36h); NAM similar. Beyond that Open-Meteo serves degraded/sentinel output with no marginal information vs GFS+ECMWF. `scripts/e2e_verify.py` mirrors the same gate for diagnostic consistency. Stderr logs each drop with city + model + observed value for visibility.
- **backfill_regimes.py `--lead-hours` flag (Apr 19 2026)**: script now accepts `--lead-hours {6,12,24,48,72}` to scope the `SELECT` + `UPDATE` to a single lead bucket. Required for the remaining 48h STABLE_HIGH gap: running the full backfill unscoped would overwrite already-correct 24h frontal_passage/transition labels with the re-classified value. Run `python scripts/backfill_regimes.py --lead-hours 48 --refit` to close the gap without collateral damage.
- **Event-based market discovery (Apr 19 2026 late)**: `market_scanner.py::find_weather_bracket_markets` pivoted from flat-`/markets` scanning to event-first discovery. Root cause diagnosed from live Tokyo data: Polymarket bundles all brackets for a resolution day into one event (e.g. `highest-temperature-in-tokyo-on-april-20-2026` → 11 child markets from 15°C through 26°C-or-higher). The flat `/markets?order=volume24hr` feed only surfaces high-volume child markets; low-volume middle brackets (21°C, 22°C for Tokyo Apr 20) fell past offset=3000 and were silently dropped. New flow: (1) paginate `/events?active=true&closed=false` up to `max_events=5000`, (2) filter events whose title/slug contains a weather keyword, (3) walk each event's embedded `markets` array (or fall back to `/events/{id}` hydration if list response omits children), (4) union with flat `/markets` scan as legacy safety net (handles events without standard slug pattern), (5) dedupe by `conditionId`/`id`, then run existing per-market filters (closed/acceptingOrders/endDateIso/question-regex/bracket-parse). `PolymarketPublicClient` gained `get_events()` + `get_event()`. Scanner stderr now logs: `events scanned=N matched=M / flat=K / union=U`. Expected coverage jump: today's 66 contracts should climb closer to 100+ once every event's full bracket set is enumerated. See Tokyo Apr 19 diagnostic — event 387435 had 11 children, flat scan captured only the 4 open tail brackets by rank.

Cross-referenced files: `MUSK_SPEC.md` for tweet alpha design, `reports/profitability_analysis.html` for MC equity-curve math, `reports/weather_alpha_workflow.html` for end-to-end pipeline diagram, `tools/dashboard/README.md` for remote-monitoring setup.
