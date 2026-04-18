# Polymarket Quantitative Trading Toolkit

A multi-strategy Polymarket system. **Weather bracket alpha is the primary strategy** (under validation, paper mode). Legacy whale-following and mispricing strategies live behind the same runtime interface.

See `.claude/CLAUDE.md` for the full working spec — math, data sources, gates, deployment phases. This README is the operator-facing summary.

---

## What's in here

```
polymarket_strat/          Core library
├── domain/weather/        CDF bracket pricing, calibration, regime classifier
├── domain/strategies/     Whale-following, mispricing, insider detection
├── infrastructure/        Open-Meteo client, IEM station client, SQLite
├── application/           Strategy application service
├── presentation/          HTML report generator
├── notifications/         Telegram alerts
└── main.py                CLI entrypoint

tools/                     Operational tooling (new)
├── dashboard/             Streamlit read-only dashboard (app.py + README)
├── lag_monitor/           Forecast-publish ↔ price-reprice lag measurement
└── walk_forward/          No-look-ahead calibration backtest

scripts/                   Helpers
├── backup_db.py           SQLite online backup with rotation
├── backfill_calibration.py
├── prepare_real_data.py
└── start_lag_monitor.sh   Launch lag monitor under nohup

deploy/                    Deployment assets
├── lambda_setup.md                                Lambda packaging docs
├── package_lambda.sh                              Lambda zip builder
├── com.wonsang.polymarket-dashboard.plist.template   launchd agent (dashboard)
└── install_dashboard_agent.sh                     One-shot plist installer

data/weather/weather.db    SQLite (5 tables, WAL mode)
reports/                   Generated HTML analytics
runtime/                   Portfolio state (JSON)
tests/                     pytest (24 passing)
```

---

## Strategies

### Weather bracket alpha (primary)

Prices binary temperature-bracket contracts from an ensemble of GFS + ECMWF + HRRR + NAM forecasts (via Open-Meteo), compared to calibrated error distributions fit on 365-day history per `(city, model, regime, lead_hours)`. Edge vs market price, quarter-Kelly sizing with uncertainty shrinkage, tiered edge gates (5¢ at P=0.50 → 15¢ at P=0.75).

Key implementation notes live in `.claude/CLAUDE.md`. Critical pieces:

- `_SIGMA_FLOOR_F = 2.5` prevents reanalysis-derived overconfidence
- `min_entry_price = 0.02` filters bracket-parsing artifacts
- HRRR/NAM skipped in calibration (Open-Meteo aliases them to `gfs_seamless`), used at inference
- 22 cities across 7 correlation groups
- Paper-first through a 30-day validation gate before any real capital

### Legacy strategies (still available)

- `whale_following` — ranks profitable wallets, follows consensus flow
- `mispricing` — polling + momentum + behavioral signals, trades on calibrated edge

---

## Daily ops

### 1. Calibration

```bash
polymarket-strat weather-calibrate                         # all cities, 365-day lookback
polymarket-strat weather-calibrate --cities seoul,nyc      # comma-sep, no spaces
```

### 2. Signal generation + paper execution

```bash
polymarket-strat weather-analyze       # generate signals, no trades
polymarket-strat autotrade             # paper mode default
polymarket-strat positions             # view open positions
polymarket-strat settle --auto         # auto-settle resolved positions
polymarket-strat report                # generate HTML report
```

### 3. Legacy strategies

```bash
python -m polymarket_strat.main analyze --strategy whale_following --sample
python -m polymarket_strat.main backtest --strategy all --sample
python -m polymarket_strat.main execute --strategy mispricing --mode paper
```

### 4. Live mode (Phase 3+, after paper validation clears)

```bash
cp .env.example .env     # fill in POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER,
                         # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
./run_live.sh            # one-command plan + execute --confirm-live
```

Use `POLYMARKET_SIGNATURE_TYPE=0` for EOA wallets, `1` for Magic/email wallets.

---

## Tooling

### Dashboard (Streamlit + Tailscale)

Read-only view of everything: open positions, equity curve, calibration health with outlier flagging, recent forecast errors, lag events, walk-forward results.

Local:

```bash
pip install streamlit pandas
streamlit run tools/dashboard/app.py
```

Remote (phone over your tailnet):

```bash
# install Tailscale on Mac + phone, log in to same account
brew install --cask tailscale && open -a Tailscale

# one-shot: install + load launchd agent (auto-start on login, auto-restart on crash)
./deploy/install_dashboard_agent.sh

# then on your phone:
#   http://<your-mac-tailscale-name>:8501
```

Full setup walkthrough + troubleshooting: [`tools/dashboard/README.md`](tools/dashboard/README.md).

### Lag monitor (measure the speed-arbitrage window)

Polls Open-Meteo forecasts (180 s) and Polymarket prices (60 s) for a watchlist of cities, emits a JSONL event log, and lets you join the two streams to measure how long it takes Polymarket to reprice after a forecast change.

```bash
./scripts/start_lag_monitor.sh                     # default watchlist
./scripts/start_lag_monitor.sh seoul,nyc,london    # custom
./scripts/start_lag_monitor.sh --all-cities        # everything

# inspect
tail -f tools/lag_monitor/logs/monitor.stdout
python tools/lag_monitor/analyze.py

# stop
pkill -f tools/lag_monitor/monitor.py
```

The analyzer reports median / p25 / p75 / p90 lag in minutes per `(city, model)` with decision guidance:

- `< 15 min` → edge already arbitraged, skip the scheduled-reprice idea
- `< 60 min` → modest edge, Lambda + EventBridge worth considering
- `> 60 min` → high value, prioritize scheduled reprice

### Walk-forward backtest (no-look-ahead calibration score)

For each eval date D in a window, refits the error distribution using ONLY errors from obs_date < D, then scores 9 synthetic bracket geometries against what actually happened. Reports Brier / log-loss / reliability diagram with skill score against a base-rate predictor.

```bash
python tools/walk_forward/backtest.py \
    --city seoul --model gfs \
    --start 2025-08-01 --end 2026-04-10

# all cities, both models, CSV for dashboard
python tools/walk_forward/backtest.py \
    --all-cities --all-models \
    --start 2026-01-01 --end 2026-04-10 \
    --csv tools/walk_forward/last_run.csv

# Bayesian (PyMC, slow — only after Phase 2 clears)
python tools/walk_forward/backtest.py \
    --city seoul --bayesian \
    --start 2026-01-01 --end 2026-04-10
```

**Last run (22 cities × 2 models × 100 days = 38,934 bracket predictions):**

| Metric | Value |
|---|---|
| Brier | 0.136 (baseline 0.250) |
| Brier skill score | **+0.455** |
| Log-loss | 0.415 (chance = 0.693) |

The CSV at `tools/walk_forward/last_run.csv` is auto-picked up by the dashboard.

### Database maintenance

WAL mode is enabled in code (with a graceful fallback for filesystems that don't support it). Back up with rotation:

```bash
python scripts/backup_db.py                              # default: 14 newest backups kept
python scripts/backup_db.py --dest /Volumes/External/backups --keep 30
python scripts/backup_db.py --dry-run                    # show what would happen
```

Cron daily:

```cron
0 3 * * *  cd /ABS/PATH/polymarket_strat && /usr/bin/python3 scripts/backup_db.py
```

---

## Risk controls

The planner enforces:

- reserve cash so the strategy never deploys the whole bankroll
- per-position cap (2%), per-correlation-group cap (8%)
- max simultaneous open positions
- 5% daily drawdown brake, 8% cumulative soft brake
- `min_entry_price = 0.02` (filters bracket-parsing artifacts → fake 50,000× leverage)
- duplicate-token-id guard (no re-entry on open positions same day)
- whale quality thresholds on win rate and ROI (for whale strategy)
- liquidity-aware sizing (scales down in wide spreads, thin books)

Designed around survival first, upside second.

---

## Realistic performance

Monte Carlo on the actual edge/price distribution the pipeline produces:

| Scenario | Win rate | Median final on $200 | Trades to 95% sig |
|---|---|---|---|
| Conservative (edge 10¢) | 60% | $408 | ~200 |
| Sweet spot (edge 15¢) | 70% | $552 | ~80 |
| Realistic mix | 58% | $812 | ~147 |

**Honest annual projection on $200 capital: ~$1,500/year** at realistic mix, 3 real signals/day, no regime break. Not $500–1500/week — that only applies if capital scales to $5k+, calibration matures, AND liquidity cooperates.

Full Monte Carlo math + equity curves: `reports/profitability_analysis.html`.

---

## Deployment phases

1. **Phase 1 (current, 30+ days):** paper. Log every signal. Verify suspicious edges (>20¢) manually before they fire. Expand calibration window.
2. **Phase 2 gate (Day 30):** win rate ≥ 55%, cumulative paper P&L positive, no bracket-parsing artifacts. Go / No-Go.
3. **Phase 3:** $200 real capital. $10–20/position, max 2 cities. Verify paper→live price replication.
4. **Phase 4:** $50 max/bracket, 5–15 trades/day across 9+ cities.

---

## Public endpoints used

- `https://gamma-api.polymarket.com/markets`
- `https://data-api.polymarket.com/holders`
- `https://data-api.polymarket.com/trades`
- `https://data-api.polymarket.com/closed-positions`
- `https://api.open-meteo.com/v1/forecast`
- `https://previous-runs-api.open-meteo.com/v1/forecast`
- `https://archive-api.open-meteo.com/v1/archive`
- `https://mesonet.agron.iastate.edu/...` (IEM ASOS station obs)

No authenticated endpoints used outside of the live CLOB order path (`py_clob_client`).

---

## What this doesn't do yet

- authenticated WebSocket feed for live whale monitoring
- dynamic correlation matrix (currently static per CLAUDE.md §5.8)
- historical regime reconstruction (all calibration samples currently labeled `STABLE_HIGH`)
- HKO live-observation fetch for Hong Kong settlement (falls through to ERA5)
- station microclimate bias correction (spec'd, unimplemented)
- `fit_bayesian()` is implemented but not yet wired into the live calibration path (frozen until Phase 2 clears per CLAUDE.md §14)

---

## Reading order for a new contributor

1. `.claude/CLAUDE.md` — the working spec. Everything else is implementation of what's in there.
2. `polymarket_strat/domain/weather/forecast.py` — bracket CDF math, σ floor, tiered edge gates.
3. `polymarket_strat/domain/weather/strategy.py` — `calibrate()` + `analyze()`, how the pieces fit.
4. `tools/walk_forward/backtest.py` — understand the no-look-ahead validation story.
5. `tools/dashboard/app.py` — how operators actually watch the system.
6. `reports/weather_alpha_workflow.html` — end-to-end pipeline diagram.
