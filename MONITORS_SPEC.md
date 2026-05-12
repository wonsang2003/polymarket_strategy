# PHASE 1.5 RESEARCH MONITORS — SPEC

**Status:** Design approved Apr 21 2026. Zero code yet.
**Purpose:** Passive instrumentation. Collects diagnostic data on whether close-settlement NO farming (Phase 2) is a real alpha or a mirage. Not a path to launch — explicitly research-grade.
**Framing:** If after 2-3 months of data collection the monitors show no clean signal, Phase 2 stays shelved. That is an acceptable outcome.

---

## Context (non-negotiable preambles)

Perplexity's third-round critique established three constraints that must govern this spec:

1. **Sample sizes for tail calibration**: at true P=0.97, 1% precision on the gap requires n≈1,164; 2% requires n≈290; 3% requires n≈129. The original "n=30" threshold was triage, not proof.
2. **Persistence ≠ profitability**: even if we measure 60-minute edge persistence in quiet windows, that doesn't imply the edge survives fees + slippage + competition.
3. **Monitor design needs matched controls** to separate certainty-collapse alpha from four confounders: LP quote-refresh cadence, CLOB settlement latency, stale market making, and low-liquidity noise.

Both monitors below are built to respect these.

---

## Monitor A: Tail Calibration Audit

### A.1 Purpose

Measure the gap between `mean_predicted_probability` and `realized_hit_rate` in tail bins (0.01-0.10 and 0.90-0.99). Primary use: **exclude** broken (city, regime, lead) combos from any future farming consideration. NOT a green-light mechanism.

### A.2 Location

`scripts/tail_calibration_audit.py` — standalone, no live-path wiring.

### A.3 Data source

Walk-forward CSVs, already produced by `tools/walk_forward/backtest.py`:

- `tools/walk_forward/last_run_24h.csv` — 34,056 rows, 24h lead
- `tools/walk_forward/last_run_48h.csv` — 34,056 rows, 48h lead

Each row has `city, model, regime, lead_hours, target_date, model_prob, hit` (where hit = 1 if bracket resolved YES, 0 if NO).

If these files aren't in the EC2 deploy, rerun:
```bash
python tools/walk_forward/backtest.py --all-cities --all-models \
    --start 2026-01-15 --end 2026-04-10 --lead-hours 24 \
    --csv tools/walk_forward/last_run_24h.csv
# and same for --lead-hours 48
```

### A.4 Algorithm

```python
import pandas as pd
from scipy import stats

BINS = [
    (0.00, 0.05), (0.05, 0.10), (0.10, 0.20),   # left tail + near-tail
    (0.80, 0.90), (0.90, 0.95), (0.95, 1.00),   # right tail + near-tail
]
# Medium bins optional for diagnostic completeness — not used to gate farm.

def audit(csv_path, group_cols):
    """group_cols = ['city'] or ['city','regime'] or ['city','regime','lead_hours']."""
    df = pd.read_csv(csv_path)
    rows = []
    for keys, group in df.groupby(group_cols):
        for lo, hi in BINS:
            bin_df = group[(group.model_prob >= lo) & (group.model_prob < hi)]
            n = len(bin_df)
            if n == 0:
                continue
            mean_pred = bin_df.model_prob.mean()
            hit_rate = bin_df.hit.mean()
            gap = hit_rate - mean_pred            # signed: +overconfidence, -under
            # Wilson CI for proportion (not normal approx — bad at tails)
            ci_low, ci_high = stats.binomtest(
                int(bin_df.hit.sum()), n, p=mean_pred
            ).proportion_ci(confidence_level=0.95, method="wilson")
            rows.append({
                **dict(zip(group_cols, keys if isinstance(keys, tuple) else (keys,))),
                "bin_lo": lo, "bin_hi": hi,
                "n": n, "mean_pred": round(mean_pred, 4),
                "hit_rate": round(hit_rate, 4),
                "gap": round(gap, 4),
                "ci95_low": round(ci_low, 4), "ci95_high": round(ci_high, 4),
                "ci_width": round(ci_high - ci_low, 4),
            })
    return pd.DataFrame(rows)
```

Run at three partition granularities:
1. `['city']` — coarsest, highest n per bin
2. `['city', 'lead_hours']` — adds lead partition
3. `['city', 'regime', 'lead_hours']` — full partition (what the farm would require)

### A.5 Decision rule

A (city, regime, lead) combo is ELIGIBLE for farming consideration ONLY if, in the specific tail bin matching the intended farm price (e.g., 0.95-0.99 for buying NO at 0.01-0.05):

- `n ≥ 100` (hard floor — below this, CI width swamps any meaningful signal)
- `|gap| ≤ 2%` (calibration precision bar)
- `ci_width ≤ 3%` (confidence band tight enough to trust the gap estimate)

Honest expectation: few combos pass at the full `(city, regime, lead)` granularity. If none pass, collapse to `(city, lead)` and lose regime precision. If still none, the farm is data-bound and stays shelved.

**Critical:** eligibility is NECESSARY, not SUFFICIENT. Passing the audit does not mean "launch the farm." It means "this combo is not obviously broken; proceed to next validation step."

### A.6 Output

Write three CSVs to `tools/calibration_audit/`:
- `audit_by_city.csv`
- `audit_by_city_lead.csv`
- `audit_by_city_regime_lead.csv`

Plus a summary text report:
```
=== Tail Calibration Audit — 2026-04-21 ===
Data source: last_run_24h.csv (34,056 rows) + last_run_48h.csv (34,056 rows)

Eligible combos (n≥100, |gap|≤2%, ci_width≤3%) per bin:
  Bin [0.90, 0.95]:   city-only: 4/22  |  city+lead: 2/44  |  full: 0/352
  Bin [0.95, 0.99]:   city-only: 2/22  |  city+lead: 0/44  |  full: 0/352

Conclusion: farming at the full (city, regime, lead) partition is not supported
by current data. Collapsing to (city, lead) yields 2 eligible combos in the
[0.90, 0.95] bin, zero above. At best, farm can be tested on those 2 combos
after additional monitor validation; zero combos cleared in the higher-confidence
[0.95, 0.99] bin.
```

Cron weekly (Sunday night, after the hourly autotrade cycle completes):
```
0 3 * * 0 cd /home/ubuntu/polymarket && venv/bin/python scripts/tail_calibration_audit.py >> logs/audit.log 2>&1
```

### A.7 What to ignore

- Do NOT use the audit output to dynamically size positions or flip flags on live signals. It's a research artifact.
- Do NOT re-interpret a narrow gap in one bin as "the city is well-calibrated" — neighboring bins may be off.
- Do NOT weight audit results by bin size. Each bin is an independent calibration hypothesis.

---

## Monitor B: Standing-Edge Monitor (matched-pair design)

### B.1 Purpose

Test whether **certainty-collapse alpha** (market underreacts to σ narrowing as lead shrinks, absent any new forecast info) exists in Polymarket weather books beyond what the existing lag monitor captures.

Key methodology commitment: matched-pair sampling to separate certainty-collapse from four confounders.

### B.2 Location

`tools/standing_edge_monitor/monitor.py` + `analyze.py` — mirrors `tools/lag_monitor/` structure. JSONL event log at `tools/standing_edge_monitor/logs/events.jsonl`.

### B.3 What's measured

For each bracket contract on the watchlist, at regular intervals (every 10 min), record:

```json
{
  "ts": "2026-04-21T14:00:00+09:00",
  "city": "tokyo",
  "target_date": "2026-04-21",
  "bracket": "59-61F",
  "lead_hours": 3.0,
  "market_price_yes": 0.18,
  "model_prob_yes": 0.12,
  "edge_yes_raw": -0.06,
  "edge_no_raw": 0.06,
  "best_ask_yes": 0.19,
  "best_bid_yes": 0.17,
  "last_trade_ts": "2026-04-21T13:47:00+09:00",
  "last_trade_volume": 25.0,
  "forecast_ts": "2026-04-21T12:00:00+09:00",
  "minutes_since_forecast_update": 120,
  "minutes_since_last_trade": 13
}
```

The model_prob and forecast_ts come from Open-Meteo calls every 10 min (same cadence as lag monitor forecast poll). `minutes_since_forecast_update` is computed from the forecast's `last_modified` header or response `run_time` field.

### B.4 Matched-pair definition

A "matched pair" for bracket X at time t is:

- **Quiet window**: [t, t+30min] where `minutes_since_forecast_update ≥ 60` at both t and t+30min (no forecast update in prior hour, none arrives during window).
- **Update window**: [t', t'+30min] for the SAME bracket X where `minutes_since_forecast_update < 10` at t' (a fresh forecast just arrived).

Match pairs must satisfy:
- Same `city`, `bracket`, `lead_bucket` (±1 hour).
- Liquidity proxy within 2× (`last_trade_volume` in the 30 min prior to t vs t' differs by ≤ 2x).
- Both pairs observed within the same 7-day window (to control for market-wide trend changes).

`analyze.py` constructs matched pairs post-hoc from the event log. Expect ~20-50 pairs per (city, bracket) per month.

### B.5 Decision metric

For each matched pair, compute `edge_decay_quiet = |edge(t) - edge(t+30min)|` vs `edge_decay_update`. Null hypothesis: `mean(decay_quiet) = mean(decay_update)` — i.e., edge moves the same amount whether or not a forecast update fired, meaning certainty-collapse is not driving it.

Alternative hypothesis (certainty-collapse alpha exists): `mean(decay_quiet) > 0` and specifically tracks lead-collapse timescales (edges decay slower at lead=24h, faster at lead=3h) rather than LP refresh cadences (which would be lead-invariant).

Sample size needed: to detect a 2¢ mean decay difference with power=0.8, α=0.05, assuming decay std ≈ 1.5¢, need ~35 matched pairs per (city, lead_bucket) combo. 2-3 months of data gets us there on the 5-8 most liquid markets.

### B.6 Watchlist

Start narrow: 5 cities × 2 target_dates ahead = 10 events, ~100 brackets total. Cities: nyc, tokyo, london, seoul, miami (mix of TZs, mix of regimes).

Scale up only after validating the pipeline writes clean JSONL and `analyze.py` produces interpretable matched pairs on the first week's data.

### B.7 Confounder controls

The four confounders Perplexity flagged:

1. **LP quote-refresh cadence** — controlled by matched-pair design: same bracket at same liquidity level, quiet vs update. If decay is LP-driven, it shows in both pairs equally; if it's info-driven, only update windows decay.
2. **CLOB settlement latency** — controlled by requiring 30-min windows (vs the 30-second settlement tick). Latency is noise at this timescale.
3. **Stale market making** — measured directly via `minutes_since_last_trade`. Stratify analysis by trade-recency bucket to see if stale brackets have systematically different decay.
4. **Low-liquidity noise** — controlled by liquidity-proxy match (trade volume within 2× between pair members). Exclude pairs where both windows have zero trades (noise dominates signal).

### B.8 Launch sequence

```bash
# On Mac (not EC2 — same caffeinate pattern as lag monitor)
caffeinate -i python tools/standing_edge_monitor/monitor.py \
    --watchlist nyc,tokyo,london,seoul,miami \
    --poll-interval-sec 600 &
disown
```

After 1 week of data:
```bash
python tools/standing_edge_monitor/analyze.py \
    --min-pairs 10 \
    --report-path tools/standing_edge_monitor/reports/week1.md
```

Expected week-1 output: mostly "insufficient data" rows. First meaningful signal: week 4-6.

### B.9 What success looks like

After 2-3 months:
- Matched-pair count per (city, lead_bucket) ≥ 35 on at least 3 cities.
- Quiet-window decay significantly > 0 on tail brackets (p < 0.05 via paired t-test).
- Decay magnitude scales with lead_hours — larger decay on 24h→18h transitions than on 6h→3h transitions (because σ-narrowing is geometrically larger early).
- Decay is NOT explained by `minutes_since_last_trade` bucket (i.e., not just staleness).

If all four conditions hold for at least 1 (city, lead) combo that also passed the tail calibration audit, Phase 2 farm gets a limited deployment plan. If fewer than all four hold, farm stays shelved. That's the honest bar.

### B.10 What success does NOT mean

- Does not mean Phase 2 is green-lit. Still need to argue edge > fees + slippage + competition after measurement.
- Does not mean the alpha scales — persistence on 5 cities doesn't imply persistence on 20.
- Does not mean the alpha is durable — other traders may enter next week and eliminate it.

---

## Common operational notes

### Disk footprint

- Tail audit: CSVs ~5 MB each, 3 files per run, weekly cron. Under 1 GB/year.
- Standing-edge: JSONL at ~100 brackets × 6 obs/hour × 24h × 365 days ≈ 5 M events/year. At ~500 bytes each → 2.5 GB/year. Rotate weekly with gzip.

### What the monitors do NOT do

- Do not emit signals into the autotrade pipeline.
- Do not adjust calibration fits or distributions.
- Do not replace the existing lag monitor (different hypothesis — forecast-change response).
- Do not substitute for walk-forward backtesting on the main YES/NO path.

### Code review bar before merge

Both monitors are passive, so bar is lower than production code:
- Tail audit: must use Wilson CI (not normal approximation — wrong at tails), must not fail silently on missing columns.
- Standing-edge: JSONL writes must be atomic per-line (no partial writes), matched-pair analyzer must be deterministic given same input.

### Effort estimate

- Tail audit: 3 hours (pandas + scipy, no live-path integration).
- Standing-edge monitor: 6 hours (forecast poll reuse from lag_monitor, matched-pair logic is the hard part).
- Standing-edge analyze.py: 4 hours.
- Total: 1.5 days of focused work. Both can ship in parallel with Phase 1.

### Sequencing with Phase 1

Phase 1 (NO_SIDE_SPEC.md) can ship first — it doesn't need these monitors.

Monitors should start collecting data as early as possible because they're useful only at scale (weeks of samples). Ideal order:
1. Today: build tail audit (fast, operates on existing CSVs).
2. This week: build standing-edge monitor and launch on Mac.
3. Next 1-2 weeks: build Phase 1 NO-side support.
4. Day 14 after Phase 1 deploy: flip live-mode NO flag if paper passes.
5. Week 8-12 after monitors launched: review monitor outputs, decide on Phase 2.

Phase 2 remains improbable. Plan lives as if it never ships. If it does, that's a positive surprise driven by the data, not a foregone conclusion.
