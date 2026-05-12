# Per-City Trading Strategy Report

**Generated**: 2026-04-30 16:26 UTC
**Database**: `weather.db` (read-only audit)
**Post-fix cutoff**: trades created on or after `2026-04-26` (when categories went live and recent strategy fixes were deployed)

## Executive Summary

- **Total trades (lifetime)**: 323
- **Open positions now**: 41
- **Lifetime realized P&L**: −$1,534.85

This report classifies each city into one of five tiers based on calibration gap and sample size, and provides concrete strategy recommendations including:
- Whether to use `weather_tail_no` (NO-direction tail strategy)
- Whether to use `weather_tail_no_flipped` (YES-flip experiment at no_ask>=0.70)
- Position-size multiplier vs default
- Edge-floor adjustment
- Edge × NO-ask × side performance breakdowns

### Tier Definitions

| Tier | Calibration Gap | Action |
|------|-----------------|--------|
| 🟢 A | gap ≥ +0.20 (model under-predicts) | SIZE UP, both sides OK |
| 🟢 A- | +0.10 ≤ gap < +0.20 | Slight size-up |
| 🟡 B | -0.10 < gap < +0.10 (well calibrated) | STANDARD settings |
| 🟠 C | -0.20 < gap ≤ -0.10 (over-predicts) | DOWNSIZE, tighten edge |
| 🔴 D | gap ≤ -0.20 | BLOCK new entries |
| ⚪ E | n < 4 | Use global priors |

### Cities Ranked by Lifetime P&L

| City | n | Net P&L |
|------|---|---------|
| munich | 32 | −$292.68 |
| sao_paulo | 33 | −$215.20 |
| milan | 24 | −$195.47 |
| london | 36 | −$173.84 |
| wellington | 26 | −$137.92 |
| amsterdam | 27 | −$116.65 |
| shanghai | 26 | −$116.60 |
| buenos_aires | 28 | −$106.50 |
| seattle | 3 | −$83.33 |
| chicago | 5 | −$82.77 |
| la | 1 | −$50.00 |
| mexico_city | 1 | −$50.00 |
| hong_kong | 23 | −$20.79 |
| sf | 3 | −$9.57 |
| nyc | 3 | +$1.43 |
| toronto | 21 | +$2.95 |
| seoul | 16 | +$49.03 |
| tokyo | 15 | +$63.07 |

### Tier Assignments

| City | Tier | Sample (post-fix) | Gap | Recommendation |
|------|------|-------------------|-----|----------------|
| munich | **C** | 7 | -0.11 | weather_tail_no DOWNSIZE + selective flip |
| sao_paulo | **B** | 5 | +0.02 | weather_tail_no STANDARD |
| milan | **B** | 4 | +0.03 | weather_tail_no STANDARD |
| london | **D** | 6 | -0.23 | BLOCK |
| wellington | **B** | 5 | +0.05 | weather_tail_no STANDARD |
| amsterdam | **A-** | 7 | +0.18 | weather_tail_no SIZE UP + flip OK |
| shanghai | **B** | 4 | -0.04 | weather_tail_no STANDARD |
| buenos_aires | **B** | 7 | +0.03 | weather_tail_no STANDARD |
| seattle | **E** | 0 | — | USE GLOBAL DEFAULTS |
| chicago | **E** | 1 | -0.67 | USE GLOBAL DEFAULTS |
| la | **E** | 0 | — | USE GLOBAL DEFAULTS |
| mexico_city | **E** | 0 | — | USE GLOBAL DEFAULTS |
| hong_kong | **A** | 4 | +0.28 | weather_tail_no SIZE UP + flip OK |
| sf | **E** | 2 | -0.11 | USE GLOBAL DEFAULTS |
| nyc | **E** | 0 | — | USE GLOBAL DEFAULTS |
| toronto | **A** | 5 | +0.36 | weather_tail_no SIZE UP + flip OK |
| seoul | **A** | 6 | +0.41 | weather_tail_no SIZE UP + flip OK |
| tokyo | **D** | 6 | -0.30 | BLOCK |

## Methodology

### Calibration Gap

```
gap = realized_winrate − model_predicted_winrate

realized_winrate     = fraction of trades that won (pnl > 0)
model_predicted_wr   = mean(model_prob) at entry, post-fix only
```

Positive gap → model under-predicts → size up (we win more often than expected)
Negative gap → model over-predicts → downsize (we lose more often than expected)

### Post-Fix Filter

All calibration analysis filters to `created_at >= 2026-04-26`. The pre-fix `<null>` legacy trades are reported but not used for forward calibration because the strategies and gates have changed materially.

### Sample Size Caveats

With current data (~10-30 trades per major city), 95% confidence intervals on win rates are **±15-25pp**. Recommendations are first-cut directional guidance, not statistical certainty. Refresh nightly and expect tier reclassifications as more data accumulates.

---

## City-by-City Detailed Analysis

(Cities ordered by lifetime net P&L, worst → best.)

## MUNICH  —  Tier C 🟠

**MODEL OVER-PREDICTS — DOWNSIZE**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 32 |
| Settled | 27 |
| Open | 5 |
| Wins | 3 |
| Losses | 7 |
| Rebal exits | 17 |
| **Lifetime P&L** | **−$292.68** |
| Total notional traded | $993.44 |
| Open notional | $160.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 15 | 0 | 5 | 10 | −$151.89 |
| Post-fix (Apr 26+) | 17 | 3 | 2 | 7 | −$140.79 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 15 | 0 | 0/5/10 | −$151.89 |
| `weather` | 2 | 0 | 0/1/1 | −$29.17 |
| `weather_tail_no` | 11 | 2 | 3/1/5 | −$90.06 |
| `weather_tail_no_flipped` | 4 | 3 | 0/0/1 | −$21.56 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 4 | 75.0% | −$0.70 | 0.678 | −$2.79 |
| YES | 3 | 0.0% | −$26.67 | 0.393 | −$80.00 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 7 settled non-rebal trades
- **Model predicted average win rate**: `0.541` (54.1%)
- **Realized win rate**: `0.429` (42.9%)
- **Calibration gap**: `-0.112` (-11.2pp) 🔴
- **Average pnl per trade**: −$11.83
- **Net P&L (post-fix)**: −$82.79
- **Average edge at entry**: +84.0pp

**Predicted vs Realized:**
```
  Predicted win rate : ██████████░░░░░░░░░░ 54.1%
  Realized win rate  : ████████░░░░░░░░░░░░ 42.9%
```

> ⚠️ Model over-predicts here. Reduce size and tighten edge floor.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 2 | 50.0% | −$15.10 | −$30.20 |
| 0.05-0.10 | 1 | 100.0% | +$16.54 | +$16.54 |
| 0.10-0.15 | 1 | 0.0% | −$25.00 | −$25.00 |
| 0.15-0.20 | 1 | 0.0% | −$25.00 | −$25.00 |
| 0.20-0.30 | 1 | 100.0% | +$10.87 | +$10.87 |
| >=0.30 | 1 | 0.0% | −$30.00 | −$30.00 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.50-0.60 | 1 | 0.0% | −$40.00 |
| 0.60-0.70 | 1 | 100.0% | +$16.54 |
| 0.70-0.80 | 2 | 100.0% | +$20.67 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #242 | 2026-04-29 | `weather_tail_no` | NO | 0.640 | +0.089 | +$16.54 | Will the highest temperature in Munich be 17°C on April 29?... |
| #225 | 2026-04-28 | `weather_tail_no` | NO | 0.730 | +0.208 | +$10.87 | Will the highest temperature in Munich be 21°C or higher on ... |
| #205 | 2026-04-27 | `weather_tail_no` | NO | 0.750 | +0.046 | +$9.80 | Will the highest temperature in Munich be 19°C on April 27?... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #189 | 2026-04-27 | `weather_tail_no` | NO | 0.590 | +0.045 | −$40.00 | Will the highest temperature in Munich be 20°C on April 27?... |
| #35 | 2026-04-22 | `<null>` | BUY | 0.190 | +0.104 | −$31.86 | Will the highest temperature in Munich be 13°C on April 22?... |
| #149 | 2026-04-26 | `<null>` | NO | 0.789 | +5.207 | −$30.00 | Will the highest temperature in Munich be 20°C on April 26?... |
| #38 | 2026-04-23 | `<null>` | BUY | 0.170 | +0.089 | −$26.95 | Will the highest temperature in Munich be 16°C on April 23?... |
| #131 | 2026-04-26 | `<null>` | BUY_YES | 0.170 | +0.168 | −$25.00 | Will the highest temperature in Munich be 18°C on April 26?... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #271 | 2026-04-30 | YES | 0.320 | out=2 | −$21.56 | catastrophic_flip |
| #296 | 2026-05-01 | YES | 0.320 | open | — | - |
| #325 | 2026-05-01 | YES | 0.260 | open | — | - |
| #338 | 2026-05-02 | YES | 0.300 | open | — | - |

**Flip net: −$21.56 over 1 settled (0 wins). 3 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: C — MODEL OVER-PREDICTS — DOWNSIZE**

- **Primary strategy**: weather_tail_no DOWNSIZE + selective flip
- **`weather_tail_no` (NO direction)**: ALLOW with size_x0.5 + edge_floor 0.10
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: TEST sparingly (size_x0.5)
- **Position size multiplier**: ×0.5
- **Edge floor**: 0.100 (10.0pp)

**Rationale**: Model over-predicts here (gap=-0.11). Reduce size 50% and tighten edge floor to filter weak signals. If flip data shows promise (n=0 settled, pnl=$-21.56), keep flip alive.

---

## SAO_PAULO  —  Tier B 🟡

**WELL-CALIBRATED — STANDARD**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 33 |
| Settled | 27 |
| Open | 6 |
| Wins | 4 |
| Losses | 6 |
| Rebal exits | 17 |
| **Lifetime P&L** | **−$215.20** |
| Total notional traded | $1,014.82 |
| Open notional | $185.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 15 | 0 | 5 | 10 | −$126.55 |
| Post-fix (Apr 26+) | 18 | 4 | 1 | 7 | −$88.66 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 15 | 0 | 0/5/10 | −$126.55 |
| `weather` | 2 | 1 | 0/0/1 | +$1.02 |
| `weather_tail_no` | 13 | 4 | 4/1/4 | −$52.54 |
| `weather_tail_no_flipped` | 3 | 1 | 0/0/2 | −$37.14 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 5 | 80.0% | +$4.02 | 0.692 | +$20.12 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 5 settled non-rebal trades
- **Model predicted average win rate**: `0.778` (77.8%)
- **Realized win rate**: `0.800` (80.0%)
- **Calibration gap**: `+0.022` (+2.2pp) 🟡
- **Average pnl per trade**: +$4.02
- **Net P&L (post-fix)**: +$20.12
- **Average edge at entry**: +8.3pp

**Predicted vs Realized:**
```
  Predicted win rate : ███████████████░░░░░ 77.8%
  Realized win rate  : ████████████████░░░░ 80.0%
```

> 🟡 Model is well-calibrated — use default settings.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 3 | 100.0% | +$12.51 | +$37.52 |
| 0.10-0.15 | 2 | 50.0% | −$8.70 | −$17.40 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.60-0.70 | 2 | 50.0% | −$11.98 |
| 0.70-0.80 | 2 | 100.0% | +$25.20 |
| >=0.80 | 1 | 100.0% | +$6.90 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #183 | 2026-04-26 | `weather_tail_no` | NO | 0.620 | +0.045 | +$18.02 | Will the highest temperature in Sao Paulo be 31°C on April 2... |
| #210 | 2026-04-27 | `weather_tail_no` | NO | 0.700 | +0.140 | +$12.60 | Will the highest temperature in Sao Paulo be 30°C on April 2... |
| #203 | 2026-04-28 | `weather_tail_no` | NO | 0.700 | +0.046 | +$12.60 | Will the highest temperature in Sao Paulo be 26°C on April 2... |
| #204 | 2026-04-28 | `weather_tail_no` | NO | 0.810 | +0.047 | +$6.90 | Will the highest temperature in Sao Paulo be 25°C or below o... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #215 | 2026-04-27 | `weather_tail_no` | NO | 0.630 | +0.138 | −$30.00 | Will the highest temperature in Sao Paulo be 29°C on April 2... |
| #22 | 2026-04-22 | `<null>` | BUY | 0.210 | +0.068 | −$21.64 | Will the highest temperature in Sao Paulo be 27°C on April 2... |
| #37 | 2026-04-22 | `<null>` | BUY | 0.180 | +0.067 | −$20.28 | Will the highest temperature in Sao Paulo be 29°C on April 2... |
| #45 | 2026-04-24 | `<null>` | BUY | 0.180 | +0.063 | −$19.06 | Will the highest temperature in Sao Paulo be 30°C on April 2... |
| #46 | 2026-04-23 | `<null>` | BUY | 0.250 | +0.053 | −$16.91 | Will the highest temperature in Sao Paulo be 28°C on April 2... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #303 | 2026-04-29 | YES | 0.210 | out=2 | −$17.14 | catastrophic_flip |
| #312 | 2026-04-30 | YES | 0.210 | out=2 | −$20.00 | catastrophic_flip |
| #323 | 2026-04-30 | YES | 0.310 | open | — | - |

**Flip net: −$37.14 over 2 settled (0 wins). 1 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: B — WELL-CALIBRATED — STANDARD**

- **Primary strategy**: weather_tail_no STANDARD
- **`weather_tail_no` (NO direction)**: ALLOW (default settings)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW per global default (insufficient city data)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: Model is well-calibrated (gap≈0). Use default thresholds. Flip experiment status: 0 settled, pnl=$-37.14.

---

## MILAN  —  Tier B 🟡

**WELL-CALIBRATED — STANDARD**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 24 |
| Settled | 21 |
| Open | 3 |
| Wins | 1 |
| Losses | 6 |
| Rebal exits | 14 |
| **Lifetime P&L** | **−$195.47** |
| Total notional traded | $764.24 |
| Open notional | $100.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 8 | 1 | 4 | 3 | −$2.89 |
| Post-fix (Apr 26+) | 16 | 0 | 2 | 11 | −$192.59 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 8 | 0 | 1/4/3 | −$2.89 |
| `weather` | 5 | 0 | 0/0/5 | +$0.68 |
| `weather_tail_no` | 9 | 3 | 0/1/5 | −$141.45 |
| `weather_tail_no_flipped` | 2 | 0 | 0/1/1 | −$51.82 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 1 | 0.0% | −$50.00 | 0.340 | −$50.00 |
| YES | 3 | 33.3% | +$14.82 | 0.413 | +$44.45 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 4 settled non-rebal trades
- **Model predicted average win rate**: `0.216` (21.6%)
- **Realized win rate**: `0.250` (25.0%)
- **Calibration gap**: `+0.034` (+3.4pp) 🟡
- **Average pnl per trade**: −$1.39
- **Net P&L (post-fix)**: −$5.55
- **Average edge at entry**: +84.5pp

**Predicted vs Realized:**
```
  Predicted win rate : ████░░░░░░░░░░░░░░░░ 21.6%
  Realized win rate  : █████░░░░░░░░░░░░░░░ 25.0%
```

> 🟡 Model is well-calibrated — use default settings.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 2 | 0.0% | −$40.00 | −$80.00 |
| 0.15-0.20 | 1 | 100.0% | +$104.45 | +$104.45 |
| >=0.30 | 1 | 0.0% | −$30.00 | −$30.00 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.30-0.40 | 1 | 0.0% | −$50.00 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #147 | 2026-04-26 | `<null>` | BUY_YES | 0.190 | +0.167 | +$104.45 | Will the highest temperature in Milan be 25°C on April 26?... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #187 | 2026-04-27 | `weather_tail_no` | NO | 0.340 | +0.045 | −$50.00 | Will the highest temperature in Milan be 26°C or higher on A... |
| #153 | 2026-04-26 | `<null>` | NO | 0.830 | +3.278 | −$30.00 | Will the highest temperature in Milan be 27°C on April 26?... |
| #292 | 2026-04-30 | `weather_tail_no_flipped` | YES | 0.220 | -0.108 | −$30.00 | Will the highest temperature in Milan be 18°C on April 30?... |
| #43 | 2026-04-23 | `<null>` | BUY | 0.240 | +0.067 | −$22.01 | Will the highest temperature in Milan be 21°C on April 23?... |
| #44 | 2026-04-24 | `<null>` | BUY | 0.210 | +0.065 | −$20.29 | Will the highest temperature in Milan be 23°C on April 24?... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #263 | 2026-04-29 | YES | 0.220 | out=2 | −$21.82 | catastrophic_flip |
| #292 | 2026-04-30 | YES | 0.220 | out=0 | −$30.00 | - |

**Flip net: −$51.82 over 2 settled (0 wins). 0 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: B — WELL-CALIBRATED — STANDARD**

- **Primary strategy**: weather_tail_no STANDARD
- **`weather_tail_no` (NO direction)**: ALLOW (default settings)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW per global default (insufficient city data)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: Model is well-calibrated (gap≈0). Use default thresholds. Flip experiment status: 1 settled, pnl=$-51.82.

---

## LONDON  —  Tier D 🔴

**MODEL BROKEN — BLOCK**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 36 |
| Settled | 33 |
| Open | 3 |
| Wins | 3 |
| Losses | 8 |
| Rebal exits | 22 |
| **Lifetime P&L** | **−$173.84** |
| Total notional traded | $1,193.90 |
| Open notional | $90.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 16 | 1 | 5 | 10 | −$110.11 |
| Post-fix (Apr 26+) | 20 | 2 | 3 | 12 | −$63.73 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 16 | 0 | 1/5/10 | −$110.11 |
| `weather` | 8 | 0 | 0/1/7 | −$18.59 |
| `weather_tail_no` | 8 | 2 | 1/2/3 | −$85.21 |
| `weather_tail_no_flipped` | 4 | 1 | 1/0/2 | +$40.06 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 4 | 25.0% | −$19.62 | 0.583 | −$78.46 |
| YES | 2 | 50.0% | +$29.34 | 0.360 | +$58.68 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 6 settled non-rebal trades
- **Model predicted average win rate**: `0.566` (56.6%)
- **Realized win rate**: `0.333` (33.3%)
- **Calibration gap**: `-0.233` (-23.3pp) 🔴
- **Average pnl per trade**: −$3.30
- **Net P&L (post-fix)**: −$19.79
- **Average edge at entry**: +5.4pp

**Predicted vs Realized:**
```
  Predicted win rate : ███████████░░░░░░░░░ 56.6%
  Realized win rate  : ██████░░░░░░░░░░░░░░ 33.3%
```

> 🚨 **Model is significantly OVER-confident.** Predictions fail to materialize >20pp below model expectation. Consider blocking new entries until calibration improves OR shrinking size by 50%+ and bumping edge floor.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 3 | 33.3% | +$4.56 | +$13.68 |
| 0.05-0.10 | 1 | 0.0% | −$25.00 | −$25.00 |
| 0.10-0.15 | 1 | 100.0% | +$16.54 | +$16.54 |
| 0.20-0.30 | 1 | 0.0% | −$25.00 | −$25.00 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.40-0.50 | 2 | 0.0% | −$65.00 |
| 0.60-0.70 | 1 | 100.0% | +$16.54 |
| 0.70-0.80 | 1 | 0.0% | −$30.00 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #298 | 2026-04-29 | `weather_tail_no_flipped` | YES | 0.260 | -0.180 | +$83.68 | Will the highest temperature in London be 16°C on April 29?... |
| #23 | 2026-04-21 | `<null>` | BUY | 0.240 | +0.068 | +$66.04 | Will the highest temperature in London be 13°C on April 21?... |
| #250 | 2026-04-29 | `weather_tail_no` | NO | 0.640 | +0.115 | +$16.54 | Will the highest temperature in London be 17°C on April 29?... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #110 | 2026-04-25 | `<null>` | NO | 0.006 | +7489.462 | −$50.00 | Will the lowest temperature in London be 5°C on April 25?... |
| #113 | 2026-04-25 | `<null>` | NO | 0.004 | +10839.889 | −$50.00 | Will the lowest temperature in London be 5°C on April 25?... |
| #161 | 2026-04-26 | `weather_tail_no` | NO | 0.470 | +0.044 | −$40.00 | Will the highest temperature in London be 18°C on April 26?... |
| #169 | 2026-04-26 | `weather_tail_no` | NO | 0.730 | +0.046 | −$30.00 | Will the highest temperature in London be 19°C on April 26?... |
| #104 | 2026-04-25 | `<null>` | BUY_YES | 0.390 | +0.202 | −$25.00 | Will the highest temperature in London be 20°C on April 25?... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #260 | 2026-04-29 | YES | 0.240 | out=2 | −$22.50 | catastrophic_flip |
| #293 | 2026-04-30 | YES | 0.260 | out=2 | −$21.12 | catastrophic_flip |
| #298 | 2026-04-29 | YES | 0.260 | out=1 | +$83.68 | - |
| #322 | 2026-05-01 | YES | 0.250 | open | — | - |

**Flip net: +$40.06 over 3 settled (1 wins). 1 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: D — MODEL BROKEN — BLOCK**

- **Primary strategy**: BLOCK
- **`weather_tail_no` (NO direction)**: BLOCK
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: BLOCK
- **Position size multiplier**: ×0.0

**Rationale**: Sample n=6, gap is severely negative. Model is broken in this city. Suspend new entries until n>=15 settles or recalibration.

---

## WELLINGTON  —  Tier B 🟡

**WELL-CALIBRATED — STANDARD**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 26 |
| Settled | 24 |
| Open | 2 |
| Wins | 5 |
| Losses | 5 |
| Rebal exits | 14 |
| **Lifetime P&L** | **−$137.92** |
| Total notional traded | $861.54 |
| Open notional | $70.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 15 | 1 | 4 | 10 | −$106.71 |
| Post-fix (Apr 26+) | 11 | 4 | 1 | 4 | −$31.22 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 15 | 0 | 1/4/10 | −$106.71 |
| `weather` | 4 | 0 | 1/1/2 | −$21.96 |
| `weather_tail_no` | 6 | 1 | 3/0/2 | −$9.25 |
| `weather_tail_no_flipped` | 1 | 1 | 0/0/0 | — |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 5 | 80.0% | +$7.19 | 0.649 | +$35.97 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 5 settled non-rebal trades
- **Model predicted average win rate**: `0.749` (74.9%)
- **Realized win rate**: `0.800` (80.0%)
- **Calibration gap**: `+0.051` (+5.1pp) 🟡
- **Average pnl per trade**: +$7.19
- **Net P&L (post-fix)**: +$35.97
- **Average edge at entry**: +9.7pp

**Predicted vs Realized:**
```
  Predicted win rate : ██████████████░░░░░░ 74.9%
  Realized win rate  : ████████████████░░░░ 80.0%
```

> 🟡 Model is well-calibrated — use default settings.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 2 | 100.0% | +$15.84 | +$31.67 |
| 0.05-0.10 | 1 | 100.0% | +$15.02 | +$15.02 |
| 0.10-0.15 | 1 | 0.0% | −$25.00 | −$25.00 |
| 0.15-0.20 | 1 | 100.0% | +$14.29 | +$14.29 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.50-0.60 | 2 | 50.0% | +$1.07 |
| 0.60-0.70 | 2 | 100.0% | +$29.30 |
| >=0.80 | 1 | 100.0% | +$5.60 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #85 | 2026-04-25 | `<null>` | BUY_NO | 0.490 | +0.309 | +$51.00 | Will the highest temperature in Wellington be 16°C on April ... |
| #206 | 2026-04-27 | `weather_tail_no` | NO | 0.530 | +0.045 | +$26.07 | Will the highest temperature in Wellington be 17°C on April ... |
| #213 | 2026-04-28 | `weather` | BUY_NO | 0.620 | +0.091 | +$15.02 | Will the highest temperature in Wellington be 16°C on April ... |
| #287 | 2026-04-30 | `weather_tail_no` | NO | 0.673 | +0.174 | +$14.29 | Will the highest temperature in Wellington be 16°C on April ... |
| #207 | 2026-04-28 | `weather_tail_no` | NO | 0.840 | +0.047 | +$5.60 | Will the highest temperature in Wellington be 18°C on April ... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #88 | 2026-04-25 | `<null>` | BUY_NO | 0.700 | +0.103 | −$50.00 | Will the highest temperature in Wellington be 15°C on April ... |
| #41 | 2026-04-23 | `<null>` | BUY | 0.170 | +0.110 | −$33.73 | Will the highest temperature in Wellington be 15°C on April ... |
| #32 | 2026-04-22 | `<null>` | BUY | 0.160 | +0.097 | −$28.64 | Will the highest temperature in Wellington be 13°C on April ... |
| #284 | 2026-04-30 | `weather` | BUY_NO | 0.580 | +0.128 | −$25.00 | Will the highest temperature in Wellington be 15°C on April ... |
| #50 | 2026-04-24 | `<null>` | BUY | 0.260 | +0.052 | −$17.49 | Will the highest temperature in Wellington be 16°C on April ... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #313 | 2026-05-01 | YES | 0.200 | open | — | - |

**Flip net: +$0.00 over 0 settled (0 wins). 1 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: B — WELL-CALIBRATED — STANDARD**

- **Primary strategy**: weather_tail_no STANDARD
- **`weather_tail_no` (NO direction)**: ALLOW (default settings)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW per global default (insufficient city data)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: Model is well-calibrated (gap≈0). Use default thresholds. Flip experiment status: 0 settled, pnl=$0.00.

---

## AMSTERDAM  —  Tier A- 🟢

**MODEL UNDER-PREDICTS — slight size-up**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 27 |
| Settled | 24 |
| Open | 3 |
| Wins | 6 |
| Losses | 5 |
| Rebal exits | 13 |
| **Lifetime P&L** | **−$116.65** |
| Total notional traded | $826.99 |
| Open notional | $110.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 10 | 2 | 4 | 4 | −$44.56 |
| Post-fix (Apr 26+) | 17 | 4 | 1 | 9 | −$72.09 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 10 | 0 | 2/4/4 | −$44.56 |
| `weather` | 5 | 0 | 0/0/5 | −$19.69 |
| `weather_tail_no` | 10 | 2 | 4/1/3 | −$27.22 |
| `weather_tail_no_flipped` | 2 | 1 | 0/0/1 | −$25.18 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 5 | 80.0% | +$11.33 | 0.672 | +$56.65 |
| YES | 2 | 100.0% | +$36.66 | 0.510 | +$73.33 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 7 settled non-rebal trades
- **Model predicted average win rate**: `0.673` (67.3%)
- **Realized win rate**: `0.857` (85.7%)
- **Calibration gap**: `+0.184` (+18.4pp) 🟢
- **Average pnl per trade**: +$18.57
- **Net P&L (post-fix)**: +$129.98
- **Average edge at entry**: +68.9pp

**Predicted vs Realized:**
```
  Predicted win rate : █████████████░░░░░░░ 67.3%
  Realized win rate  : █████████████████░░░ 85.7%
```

> ✅ Model slightly under-predicts here. Mild positive signal.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| 0.05-0.10 | 1 | 0.0% | −$30.00 | −$30.00 |
| 0.10-0.15 | 3 | 100.0% | +$29.81 | +$89.44 |
| 0.20-0.30 | 1 | 100.0% | +$36.18 | +$36.18 |
| >=0.30 | 2 | 100.0% | +$17.18 | +$34.36 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.50-0.60 | 1 | 100.0% | +$36.18 |
| 0.60-0.70 | 2 | 100.0% | +$37.86 |
| 0.70-0.80 | 1 | 100.0% | +$12.60 |
| >=0.80 | 1 | 0.0% | −$30.00 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #128 | 2026-04-26 | `<null>` | BUY_YES | 0.280 | +0.139 | +$63.00 | Will the highest temperature in Amsterdam be 14°C on April 2... |
| #214 | 2026-04-27 | `weather_tail_no` | NO | 0.520 | +0.281 | +$36.18 | Will the highest temperature in Amsterdam be 15°C on April 2... |
| #318 | 2026-04-30 | `weather_tail_no` | NO | 0.620 | +0.320 | +$24.03 | Will the highest temperature in Amsterdam be 19°C on April 3... |
| #218 | 2026-04-28 | `weather_tail_no` | NO | 0.680 | +0.111 | +$13.84 | Will the highest temperature in Amsterdam be 18°C on April 2... |
| #241 | 2026-04-29 | `weather_tail_no` | NO | 0.700 | +0.124 | +$12.60 | Will the highest temperature in Amsterdam be 19°C on April 2... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #36 | 2026-04-23 | `<null>` | BUY | 0.190 | +0.180 | −$50.00 | Will the highest temperature in Amsterdam be 15°C on April 2... |
| #25 | 2026-04-21 | `<null>` | BUY | 0.180 | +0.135 | −$36.85 | Will the highest temperature in Amsterdam be 13°C on April 2... |
| #228 | 2026-04-27 | `weather_tail_no` | NO | 0.840 | +0.093 | −$30.00 | Will the highest temperature in Amsterdam be 14°C on April 2... |
| #48 | 2026-04-24 | `<null>` | BUY | 0.160 | +0.066 | −$18.69 | Will the highest temperature in Amsterdam be 14°C on April 2... |
| #31 | 2026-04-22 | `<null>` | BUY | 0.180 | +0.059 | −$18.02 | Will the highest temperature in Amsterdam be 15°C on April 2... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #267 | 2026-04-30 | YES | 0.560 | out=2 | −$25.18 | catastrophic_flip |
| #311 | 2026-05-01 | YES | 0.370 | open | — | - |

**Flip net: −$25.18 over 1 settled (0 wins). 1 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: A- — MODEL UNDER-PREDICTS — slight size-up**

- **Primary strategy**: weather_tail_no SIZE UP + flip OK
- **`weather_tail_no` (NO direction)**: ALLOW with size_x1.5 + edge_floor 0.03
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (model under-predicts; both sides have edge)
- **Position size multiplier**: ×1.2
- **Edge floor**: 0.030 (3.0pp)

**Rationale**: Model under-predicts here (gap=+0.18). Both NO and YES sides should profit. Size up 50%; keep edge floor at current 0.03.

---

## SHANGHAI  —  Tier B 🟡

**WELL-CALIBRATED — STANDARD**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 26 |
| Settled | 23 |
| Open | 3 |
| Wins | 4 |
| Losses | 3 |
| Rebal exits | 16 |
| **Lifetime P&L** | **−$116.60** |
| Total notional traded | $854.82 |
| Open notional | $95.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 11 | 1 | 2 | 8 | −$38.45 |
| Post-fix (Apr 26+) | 15 | 3 | 1 | 8 | −$78.15 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 11 | 0 | 1/2/8 | −$38.45 |
| `weather` | 5 | 1 | 0/1/3 | −$26.02 |
| `weather_tail_no` | 8 | 1 | 3/0/4 | −$31.44 |
| `weather_tail_no_flipped` | 2 | 1 | 0/0/1 | −$20.69 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 4 | 75.0% | +$3.06 | 0.663 | +$12.23 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 4 settled non-rebal trades
- **Model predicted average win rate**: `0.788` (78.8%)
- **Realized win rate**: `0.750` (75.0%)
- **Calibration gap**: `-0.038` (-3.8pp) 🟡
- **Average pnl per trade**: +$3.06
- **Net P&L (post-fix)**: +$12.23
- **Average edge at entry**: +12.2pp

**Predicted vs Realized:**
```
  Predicted win rate : ███████████████░░░░░ 78.8%
  Realized win rate  : ███████████████░░░░░ 75.0%
```

> 🟡 Model is well-calibrated — use default settings.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 1 | 100.0% | +$13.21 | +$13.21 |
| 0.10-0.15 | 2 | 50.0% | −$6.50 | −$12.99 |
| 0.15-0.20 | 1 | 100.0% | +$12.01 | +$12.01 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.50-0.60 | 1 | 0.0% | −$25.00 |
| 0.60-0.70 | 1 | 100.0% | +$13.21 |
| 0.70-0.80 | 2 | 100.0% | +$24.02 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #101 | 2026-04-25 | `<null>` | BUY_NO | 0.650 | +0.187 | +$26.38 | Will the highest temperature in Shanghai be 21°C on April 25... |
| #192 | 2026-04-27 | `weather_tail_no` | NO | 0.690 | +0.045 | +$13.21 | Will the highest temperature in Shanghai be 27°C on April 27... |
| #239 | 2026-04-29 | `weather_tail_no` | NO | 0.710 | +0.177 | +$12.01 | Will the highest temperature in Shanghai be 15°C on April 29... |
| #248 | 2026-04-29 | `weather_tail_no` | NO | 0.710 | +0.130 | +$12.01 | Will the highest temperature in Shanghai be 14°C on April 29... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #84 | 2026-04-25 | `<null>` | BUY_YES | 0.150 | +0.120 | −$32.24 | Will the highest temperature in Shanghai be 19°C on April 25... |
| #102 | 2026-04-25 | `<null>` | BUY_YES | 0.150 | +0.118 | −$31.56 | Will the highest temperature in Shanghai be 20°C on April 25... |
| #259 | 2026-04-29 | `weather` | BUY_NO | 0.540 | +0.137 | −$25.00 | Will the highest temperature in Shanghai be 13°C on April 29... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #273 | 2026-04-30 | YES | 0.290 | out=2 | −$20.69 | catastrophic_flip |
| #324 | 2026-05-01 | YES | 0.324 | open | — | - |

**Flip net: −$20.69 over 1 settled (0 wins). 1 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: B — WELL-CALIBRATED — STANDARD**

- **Primary strategy**: weather_tail_no STANDARD
- **`weather_tail_no` (NO direction)**: ALLOW (default settings)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW per global default (insufficient city data)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: Model is well-calibrated (gap≈0). Use default thresholds. Flip experiment status: 0 settled, pnl=$-20.69.

---

## BUENOS_AIRES  —  Tier B 🟡

**WELL-CALIBRATED — STANDARD**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 28 |
| Settled | 24 |
| Open | 4 |
| Wins | 6 |
| Losses | 3 |
| Rebal exits | 15 |
| **Lifetime P&L** | **−$106.50** |
| Total notional traded | $877.61 |
| Open notional | $140.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 9 | 0 | 2 | 7 | −$76.53 |
| Post-fix (Apr 26+) | 19 | 6 | 1 | 8 | −$29.97 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 9 | 0 | 0/2/7 | −$76.53 |
| `weather` | 3 | 0 | 0/0/3 | +$4.58 |
| `weather_tail_no` | 13 | 2 | 6/1/4 | −$12.88 |
| `weather_tail_no_flipped` | 3 | 2 | 0/0/1 | −$21.67 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 7 | 85.7% | +$6.96 | 0.719 | +$48.73 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 7 settled non-rebal trades
- **Model predicted average win rate**: `0.826` (82.6%)
- **Realized win rate**: `0.857` (85.7%)
- **Calibration gap**: `+0.031` (+3.1pp) 🟡
- **Average pnl per trade**: +$6.96
- **Net P&L (post-fix)**: +$48.73
- **Average edge at entry**: +10.5pp

**Predicted vs Realized:**
```
  Predicted win rate : ████████████████░░░░ 82.6%
  Realized win rate  : █████████████████░░░ 85.7%
```

> 🟡 Model is well-calibrated — use default settings.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 3 | 66.7% | −$2.57 | −$7.72 |
| 0.05-0.10 | 1 | 100.0% | +$10.87 | +$10.87 |
| 0.10-0.15 | 1 | 100.0% | +$5.60 | +$5.60 |
| 0.15-0.20 | 1 | 100.0% | +$13.84 | +$13.84 |
| 0.20-0.30 | 1 | 100.0% | +$26.13 | +$26.13 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.60-0.70 | 3 | 100.0% | +$55.80 |
| 0.70-0.80 | 2 | 50.0% | −$19.13 |
| >=0.80 | 2 | 100.0% | +$12.05 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #209 | 2026-04-27 | `weather_tail_no` | NO | 0.600 | +0.227 | +$26.13 | Will the highest temperature in Buenos Aires be 16°C on Apri... |
| #191 | 2026-04-26 | `weather_tail_no` | NO | 0.650 | +0.045 | +$15.83 | Will the highest temperature in Buenos Aires be 17°C on Apri... |
| #286 | 2026-04-29 | `weather_tail_no` | NO | 0.680 | +0.190 | +$13.84 | Will the highest temperature in Buenos Aires be 24°C on Apri... |
| #229 | 2026-04-28 | `weather_tail_no` | NO | 0.730 | +0.081 | +$10.87 | Will the highest temperature in Buenos Aires be 19°C on Apri... |
| #200 | 2026-04-27 | `weather_tail_no` | NO | 0.820 | +0.047 | +$6.45 | Will the highest temperature in Buenos Aires be 18°C or high... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #33 | 2026-04-21 | `<null>` | BUY | 0.200 | +0.183 | −$37.73 | Will the highest temperature in Buenos Aires be 23°C or high... |
| #20 | 2026-04-22 | `<null>` | BUY | 0.170 | +0.118 | −$31.32 | Will the highest temperature in Buenos Aires be 19°C on Apri... |
| #196 | 2026-04-27 | `weather_tail_no` | NO | 0.710 | +0.046 | −$30.00 | Will the highest temperature in Buenos Aires be 17°C on Apri... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #265 | 2026-04-30 | YES | 0.490 | open | — | - |
| #275 | 2026-04-29 | YES | 0.180 | out=2 | −$21.67 | catastrophic_flip |
| #302 | 2026-05-01 | YES | 0.370 | open | — | - |

**Flip net: −$21.67 over 1 settled (0 wins). 2 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: B — WELL-CALIBRATED — STANDARD**

- **Primary strategy**: weather_tail_no STANDARD
- **`weather_tail_no` (NO direction)**: ALLOW (default settings)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW per global default (insufficient city data)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: Model is well-calibrated (gap≈0). Use default thresholds. Flip experiment status: 0 settled, pnl=$-21.67.

---

## SEATTLE  —  Tier E ⚪

**INSUFFICIENT DATA**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 3 |
| Settled | 2 |
| Open | 1 |
| Wins | 0 |
| Losses | 1 |
| Rebal exits | 1 |
| **Lifetime P&L** | **−$83.33** |
| Total notional traded | $130.00 |
| Open notional | $30.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 2 | 0 | 1 | 1 | −$83.33 |
| Post-fix (Apr 26+) | 1 | 0 | 0 | 0 | — |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 2 | 0 | 0/1/1 | −$83.33 |
| `weather_tail_no` | 1 | 1 | 0/0/0 | — |

### 5. Calibration Profile (Post-Fix)

*Insufficient post-fix data for calibration analysis.*

### 8. Best & Worst Trades

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #19 | 2026-04-21 | `<null>` | BUY | 0.180 | +0.518 | −$50.00 | Will the highest temperature in Seattle be 55°F or below on ... |

### 10. STRATEGY RECOMMENDATION

**Tier: E — INSUFFICIENT DATA**

- **Primary strategy**: USE GLOBAL DEFAULTS
- **`weather_tail_no` (NO direction)**: ALLOW (no city-specific signal)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (per global default)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: n=0 too small for city-specific calibration. Use global priors.

---

## CHICAGO  —  Tier E ⚪

**INSUFFICIENT DATA**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 5 |
| Settled | 4 |
| Open | 1 |
| Wins | 0 |
| Losses | 1 |
| Rebal exits | 3 |
| **Lifetime P&L** | **−$82.77** |
| Total notional traded | $184.03 |
| Open notional | $30.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 2 | 0 | 0 | 2 | −$24.02 |
| Post-fix (Apr 26+) | 3 | 0 | 1 | 1 | −$58.75 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 2 | 0 | 0/0/2 | −$24.02 |
| `weather_tail_no` | 3 | 1 | 0/1/1 | −$58.75 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 1 | 0.0% | −$40.00 | 0.410 | −$40.00 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 1 settled non-rebal trades
- **Model predicted average win rate**: `0.665` (66.5%)
- **Realized win rate**: `0.000` (0.0%)
- **Calibration gap**: `-0.665` (-66.5pp) 🔴
- **Average pnl per trade**: −$40.00
- **Net P&L (post-fix)**: −$40.00
- **Average edge at entry**: +25.5pp

**Predicted vs Realized:**
```
  Predicted win rate : █████████████░░░░░░░ 66.5%
  Realized win rate  : ░░░░░░░░░░░░░░░░░░░░ 0.0%
```

> 🚨 **Model is significantly OVER-confident.** Predictions fail to materialize >20pp below model expectation. Consider blocking new entries until calibration improves OR shrinking size by 50%+ and bumping edge floor.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| 0.20-0.30 | 1 | 0.0% | −$40.00 | −$40.00 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.40-0.50 | 1 | 0.0% | −$40.00 |

### 8. Best & Worst Trades

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #254 | 2026-04-29 | `weather_tail_no` | NO | 0.410 | +0.255 | −$40.00 | Will the highest temperature in Chicago be 56°F or higher on... |

### 10. STRATEGY RECOMMENDATION

**Tier: E — INSUFFICIENT DATA**

- **Primary strategy**: USE GLOBAL DEFAULTS
- **`weather_tail_no` (NO direction)**: ALLOW (no city-specific signal)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (per global default)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: n=1 too small for city-specific calibration. Use global priors.

---

## LA  —  Tier E ⚪

**INSUFFICIENT DATA**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 1 |
| Settled | 1 |
| Open | 0 |
| Wins | 0 |
| Losses | 1 |
| Rebal exits | 0 |
| **Lifetime P&L** | **−$50.00** |
| Total notional traded | $50.00 |
| Open notional | $0.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 1 | 0 | 1 | 0 | −$50.00 |
| Post-fix (Apr 26+) | 0 | 0 | 0 | 0 | — |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 1 | 0 | 0/1/0 | −$50.00 |

### 5. Calibration Profile (Post-Fix)

*Insufficient post-fix data for calibration analysis.*

### 8. Best & Worst Trades

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #47 | 2026-04-23 | `<null>` | BUY | 0.660 | +0.337 | −$50.00 | Will the highest temperature in Los Angeles be 72°F or highe... |

### 10. STRATEGY RECOMMENDATION

**Tier: E — INSUFFICIENT DATA**

- **Primary strategy**: USE GLOBAL DEFAULTS
- **`weather_tail_no` (NO direction)**: ALLOW (no city-specific signal)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (per global default)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: n=0 too small for city-specific calibration. Use global priors.

---

## MEXICO_CITY  —  Tier E ⚪

**INSUFFICIENT DATA**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 1 |
| Settled | 1 |
| Open | 0 |
| Wins | 0 |
| Losses | 1 |
| Rebal exits | 0 |
| **Lifetime P&L** | **−$50.00** |
| Total notional traded | $50.00 |
| Open notional | $0.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 1 | 0 | 1 | 0 | −$50.00 |
| Post-fix (Apr 26+) | 0 | 0 | 0 | 0 | — |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 1 | 0 | 0/1/0 | −$50.00 |

### 5. Calibration Profile (Post-Fix)

*Insufficient post-fix data for calibration analysis.*

### 8. Best & Worst Trades

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #28 | 2026-04-21 | `<null>` | BUY | 0.170 | +0.468 | −$50.00 | Will the highest temperature in Mexico City be 27°C or highe... |

### 10. STRATEGY RECOMMENDATION

**Tier: E — INSUFFICIENT DATA**

- **Primary strategy**: USE GLOBAL DEFAULTS
- **`weather_tail_no` (NO direction)**: ALLOW (no city-specific signal)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (per global default)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: n=0 too small for city-specific calibration. Use global priors.

---

## HONG_KONG  —  Tier A 🟢

**MODEL UNDER-PREDICTS — SIZE UP**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 23 |
| Settled | 19 |
| Open | 4 |
| Wins | 4 |
| Losses | 2 |
| Rebal exits | 13 |
| **Lifetime P&L** | **−$20.79** |
| Total notional traded | $813.97 |
| Open notional | $140.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 9 | 0 | 2 | 7 | −$62.32 |
| Post-fix (Apr 26+) | 14 | 4 | 0 | 6 | +$41.53 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 9 | 0 | 0/2/7 | −$62.32 |
| `weather` | 1 | 0 | 0/0/1 | −$2.17 |
| `weather_tail_no` | 12 | 3 | 4/0/5 | +$43.70 |
| `weather_tail_no_flipped` | 1 | 1 | 0/0/0 | — |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 4 | 100.0% | +$19.52 | 0.655 | +$78.08 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 4 settled non-rebal trades
- **Model predicted average win rate**: `0.716` (71.6%)
- **Realized win rate**: `1.000` (100.0%)
- **Calibration gap**: `+0.284` (+28.4pp) 🟢
- **Average pnl per trade**: +$19.52
- **Net P&L (post-fix)**: +$78.08
- **Average edge at entry**: +5.7pp

**Predicted vs Realized:**
```
  Predicted win rate : ██████████████░░░░░░ 71.6%
  Realized win rate  : ████████████████████ 100.0%
```

> ⚠️ **Model is significantly UNDER-confident here.** Realized wins exceed predictions by 20pp+. This is alpha to lean into — increase position size and consider both NO and YES sides (flipping at high NO ask works).

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 3 | 100.0% | +$21.20 | +$63.60 |
| 0.05-0.10 | 1 | 100.0% | +$14.48 | +$14.48 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.50-0.60 | 1 | 100.0% | +$39.20 |
| 0.60-0.70 | 2 | 100.0% | +$33.28 |
| >=0.80 | 1 | 100.0% | +$5.60 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #188 | 2026-04-27 | `weather_tail_no` | NO | 0.500 | +0.045 | +$39.20 | Will the highest temperature in Hong Kong be 28°C on April 2... |
| #185 | 2026-04-28 | `weather_tail_no` | NO | 0.610 | +0.045 | +$18.80 | Will the highest temperature in Hong Kong be 28°C on April 2... |
| #222 | 2026-04-29 | `weather_tail_no` | NO | 0.670 | +0.092 | +$14.48 | Will the highest temperature in Hong Kong be 27°C on April 2... |
| #199 | 2026-04-27 | `weather_tail_no` | NO | 0.840 | +0.047 | +$5.60 | Will the highest temperature in Hong Kong be 29°C on April 2... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #103 | 2026-04-25 | `<null>` | BUY_NO | 0.430 | +0.376 | −$25.00 | Will the highest temperature in Hong Kong be 25°C on April 2... |
| #34 | 2026-04-22 | `<null>` | BUY | 0.230 | +0.073 | −$23.97 | Will the highest temperature in Hong Kong be 28°C on April 2... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #299 | 2026-05-01 | YES | 0.330 | open | — | - |

**Flip net: +$0.00 over 0 settled (0 wins). 1 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: A — MODEL UNDER-PREDICTS — SIZE UP**

- **Primary strategy**: weather_tail_no SIZE UP + flip OK
- **`weather_tail_no` (NO direction)**: ALLOW with size_x1.5 + edge_floor 0.03
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (model under-predicts; both sides have edge)
- **Position size multiplier**: ×1.5
- **Edge floor**: 0.030 (3.0pp)

**Rationale**: Model under-predicts here (gap=+0.28). Both NO and YES sides should profit. Size up 50%; keep edge floor at current 0.03.

---

## SF  —  Tier E ⚪

**INSUFFICIENT DATA**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 3 |
| Settled | 2 |
| Open | 1 |
| Wins | 1 |
| Losses | 1 |
| Rebal exits | 0 |
| **Lifetime P&L** | **−$9.57** |
| Total notional traded | $90.00 |
| Open notional | $30.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 0 | 0 | 0 | 0 | — |
| Post-fix (Apr 26+) | 3 | 1 | 1 | 0 | −$9.57 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `weather_tail_no` | 3 | 1 | 1/1/0 | −$9.57 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 2 | 50.0% | −$4.78 | 0.495 | −$9.57 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 2 settled non-rebal trades
- **Model predicted average win rate**: `0.607` (60.7%)
- **Realized win rate**: `0.500` (50.0%)
- **Calibration gap**: `-0.107` (-10.7pp) 🔴
- **Average pnl per trade**: −$4.78
- **Net P&L (post-fix)**: −$9.57
- **Average edge at entry**: +11.2pp

**Predicted vs Realized:**
```
  Predicted win rate : ████████████░░░░░░░░ 60.7%
  Realized win rate  : ██████████░░░░░░░░░░ 50.0%
```

> ⚠️ Model over-predicts here. Reduce size and tighten edge floor.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| 0.10-0.15 | 2 | 50.0% | −$4.78 | −$9.57 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.40-0.50 | 1 | 0.0% | −$30.00 |
| 0.50-0.60 | 1 | 100.0% | +$20.43 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #262 | 2026-04-29 | `weather_tail_no` | NO | 0.590 | +0.111 | +$20.43 | Will the highest temperature in San Francisco be 68°F or hig... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #224 | 2026-04-28 | `weather_tail_no` | NO | 0.400 | +0.113 | −$30.00 | Will the highest temperature in San Francisco be 66°F or hig... |

### 10. STRATEGY RECOMMENDATION

**Tier: E — INSUFFICIENT DATA**

- **Primary strategy**: USE GLOBAL DEFAULTS
- **`weather_tail_no` (NO direction)**: ALLOW (no city-specific signal)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (per global default)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: n=2 too small for city-specific calibration. Use global priors.

---

## NYC  —  Tier E ⚪

**INSUFFICIENT DATA**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 3 |
| Settled | 3 |
| Open | 0 |
| Wins | 1 |
| Losses | 0 |
| Rebal exits | 2 |
| **Lifetime P&L** | **+$1.43** |
| Total notional traded | $113.89 |
| Open notional | $0.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 3 | 1 | 0 | 2 | +$1.43 |
| Post-fix (Apr 26+) | 0 | 0 | 0 | 0 | — |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 3 | 0 | 1/0/2 | +$1.43 |

### 5. Calibration Profile (Post-Fix)

*Insufficient post-fix data for calibration analysis.*

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #24 | 2026-04-22 | `<null>` | BUY | 0.560 | +0.062 | +$20.04 | Will the highest temperature in New York City be 55°F or bel... |

### 10. STRATEGY RECOMMENDATION

**Tier: E — INSUFFICIENT DATA**

- **Primary strategy**: USE GLOBAL DEFAULTS
- **`weather_tail_no` (NO direction)**: ALLOW (no city-specific signal)
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (per global default)
- **Position size multiplier**: ×1.0
- **Edge floor**: 0.050 (5.0pp)

**Rationale**: n=0 too small for city-specific calibration. Use global priors.

---

## TORONTO  —  Tier A 🟢

**MODEL UNDER-PREDICTS — SIZE UP**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 21 |
| Settled | 20 |
| Open | 1 |
| Wins | 5 |
| Losses | 3 |
| Rebal exits | 12 |
| **Lifetime P&L** | **+$2.95** |
| Total notional traded | $811.94 |
| Open notional | $30.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 11 | 0 | 3 | 8 | −$120.79 |
| Post-fix (Apr 26+) | 10 | 5 | 0 | 4 | +$123.74 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 11 | 0 | 0/3/8 | −$120.79 |
| `weather_tail_no` | 9 | 1 | 4/0/4 | +$6.14 |
| `weather_tail_no_flipped` | 1 | 0 | 1/0/0 | +$117.60 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 4 | 100.0% | +$16.39 | 0.663 | +$65.54 |
| YES | 1 | 100.0% | +$117.60 | 0.200 | +$117.60 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 5 settled non-rebal trades
- **Model predicted average win rate**: `0.637` (63.7%)
- **Realized win rate**: `1.000` (100.0%)
- **Calibration gap**: `+0.363` (+36.3pp) 🟢
- **Average pnl per trade**: +$36.63
- **Net P&L (post-fix)**: +$183.14
- **Average edge at entry**: +6.6pp

**Predicted vs Realized:**
```
  Predicted win rate : ████████████░░░░░░░░ 63.7%
  Realized win rate  : ████████████████████ 100.0%
```

> ⚠️ **Model is significantly UNDER-confident here.** Realized wins exceed predictions by 20pp+. This is alpha to lean into — increase position size and consider both NO and YES sides (flipping at high NO ask works).

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 2 | 100.0% | +$69.35 | +$138.71 |
| 0.05-0.10 | 1 | 100.0% | +$13.21 | +$13.21 |
| 0.10-0.15 | 1 | 100.0% | +$18.02 | +$18.02 |
| 0.15-0.20 | 1 | 100.0% | +$13.21 | +$13.21 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.60-0.70 | 4 | 100.0% | +$65.54 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #280 | 2026-04-29 | `weather_tail_no_flipped` | YES | 0.200 | -0.128 | +$117.60 | Will the highest temperature in Toronto be 13°C on April 29?... |
| #190 | 2026-04-26 | `weather_tail_no` | NO | 0.650 | +0.045 | +$21.11 | Will the highest temperature in Toronto be 14°C on April 26?... |
| #261 | 2026-04-29 | `weather_tail_no` | NO | 0.620 | +0.135 | +$18.02 | Will the highest temperature in Toronto be 12°C on April 29?... |
| #220 | 2026-04-27 | `weather_tail_no` | NO | 0.690 | +0.091 | +$13.21 | Will the highest temperature in Toronto be 17°C on April 27?... |
| #234 | 2026-04-28 | `weather_tail_no` | NO | 0.690 | +0.186 | +$13.21 | Will the highest temperature in Toronto be 19°C or higher on... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #42 | 2026-04-24 | `<null>` | BUY | 0.190 | +0.189 | −$50.00 | Will the highest temperature in Toronto be 9°C on April 24?... |
| #29 | 2026-04-22 | `<null>` | BUY | 0.160 | +0.166 | −$28.01 | Will the highest temperature in Toronto be 19°C or higher on... |
| #119 | 2026-04-25 | `<null>` | BUY_NO | 0.580 | +0.246 | −$25.00 | Will the highest temperature in Toronto be 8°C on April 25?... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #280 | 2026-04-29 | YES | 0.200 | out=1 | +$117.60 | - |

**Flip net: +$117.60 over 1 settled (1 wins). 0 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: A — MODEL UNDER-PREDICTS — SIZE UP**

- **Primary strategy**: weather_tail_no SIZE UP + flip OK
- **`weather_tail_no` (NO direction)**: ALLOW with size_x1.5 + edge_floor 0.03
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (model under-predicts; both sides have edge)
- **Position size multiplier**: ×1.5
- **Edge floor**: 0.030 (3.0pp)

**Rationale**: Model under-predicts here (gap=+0.36). Both NO and YES sides should profit. Size up 50%; keep edge floor at current 0.03.

---

## SEOUL  —  Tier A 🟢

**MODEL UNDER-PREDICTS — SIZE UP**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 16 |
| Settled | 14 |
| Open | 2 |
| Wins | 5 |
| Losses | 4 |
| Rebal exits | 5 |
| **Lifetime P&L** | **+$49.03** |
| Total notional traded | $493.80 |
| Open notional | $60.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 4 | 0 | 3 | 1 | −$77.13 |
| Post-fix (Apr 26+) | 12 | 5 | 1 | 4 | +$126.17 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 4 | 0 | 0/3/1 | −$77.13 |
| `weather_tail_no` | 6 | 0 | 3/0/3 | +$10.29 |
| `weather_tail_no_flipped` | 6 | 2 | 2/1/1 | +$115.88 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 3 | 100.0% | +$13.07 | 0.697 | +$39.22 |
| YES | 3 | 66.7% | +$45.03 | 0.260 | +$135.08 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 6 settled non-rebal trades
- **Model predicted average win rate**: `0.423` (42.3%)
- **Realized win rate**: `0.833` (83.3%)
- **Calibration gap**: `+0.410` (+41.0pp) 🟢
- **Average pnl per trade**: +$29.05
- **Net P&L (post-fix)**: +$174.30
- **Average edge at entry**: -5.6pp

**Predicted vs Realized:**
```
  Predicted win rate : ████████░░░░░░░░░░░░ 42.3%
  Realized win rate  : ████████████████░░░░ 83.3%
```

> ⚠️ **Model is significantly UNDER-confident here.** Realized wins exceed predictions by 20pp+. This is alpha to lean into — increase position size and consider both NO and YES sides (flipping at high NO ask works).

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 5 | 80.0% | +$31.26 | +$156.28 |
| 0.05-0.10 | 1 | 100.0% | +$18.02 | +$18.02 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| 0.60-0.70 | 1 | 100.0% | +$18.02 |
| 0.70-0.80 | 2 | 100.0% | +$21.20 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #274 | 2026-04-30 | `weather_tail_no_flipped` | YES | 0.240 | -0.178 | +$93.10 | Will the highest temperature in Seoul be 19°C on April 30?... |
| #278 | 2026-04-29 | `weather_tail_no_flipped` | YES | 0.290 | -0.173 | +$71.98 | Will the highest temperature in Seoul be 18°C on April 29?... |
| #256 | 2026-04-29 | `weather_tail_no` | NO | 0.620 | +0.069 | +$18.02 | Will the highest temperature in Seoul be 19°C or higher on A... |
| #197 | 2026-04-28 | `weather_tail_no` | NO | 0.730 | +0.046 | +$10.87 | Will the highest temperature in Seoul be 16°C on April 28?... |
| #193 | 2026-04-27 | `weather_tail_no` | NO | 0.740 | +0.046 | +$10.33 | Will the highest temperature in Seoul be 18°C on April 27?... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #83 | 2026-04-25 | `<null>` | BUY_NO | 0.190 | +0.253 | −$50.00 | Will the highest temperature in Seoul be 21°C or higher on A... |
| #257 | 2026-04-29 | `weather_tail_no_flipped` | YES | 0.250 | -0.147 | −$30.00 | Will the highest temperature in Seoul be 17°C on April 29?... |
| #39 | 2026-04-23 | `<null>` | BUY | 0.170 | +0.053 | −$13.30 | Will the highest temperature in Seoul be 23°C or higher on A... |
| #21 | 2026-04-21 | `<null>` | BUY | 0.190 | +0.073 | −$10.50 | Will the highest temperature in Seoul be 15°C on April 21?... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #257 | 2026-04-29 | YES | 0.250 | out=0 | −$30.00 | - |
| #274 | 2026-04-30 | YES | 0.240 | out=1 | +$93.10 | - |
| #278 | 2026-04-29 | YES | 0.290 | out=1 | +$71.98 | - |
| #281 | 2026-04-30 | YES | 0.250 | out=2 | −$19.20 | catastrophic_flip |
| #309 | 2026-05-01 | YES | 0.290 | open | — | - |
| #340 | 2026-05-02 | YES | 0.280 | open | — | - |

**Flip net: +$115.88 over 4 settled (2 wins). 2 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: A — MODEL UNDER-PREDICTS — SIZE UP**

- **Primary strategy**: weather_tail_no SIZE UP + flip OK
- **`weather_tail_no` (NO direction)**: ALLOW with size_x1.5 + edge_floor 0.03
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: ALLOW (model under-predicts; both sides have edge)
- **Position size multiplier**: ×1.5
- **Edge floor**: 0.030 (3.0pp)

**Rationale**: Model under-predicts here (gap=+0.41). Both NO and YES sides should profit. Size up 50%; keep edge floor at current 0.03.

---

## TOKYO  —  Tier D 🔴

**MODEL BROKEN — BLOCK**

### 1. Headline Stats

| Metric | Value |
|--------|-------|
| Total trades | 15 |
| Settled | 13 |
| Open | 2 |
| Wins | 3 |
| Losses | 4 |
| Rebal exits | 6 |
| **Lifetime P&L** | **+$63.07** |
| Total notional traded | $413.59 |
| Open notional | $60.00 |

### 2. Era Breakdown

| Era | n | W | L | RBL | Net |
|-----|---|---|---|-----|-----|
| Legacy `<null>` | 5 | 1 | 0 | 4 | +$125.95 |
| Post-fix (Apr 26+) | 10 | 2 | 4 | 2 | −$62.88 |

### 3. By Category

| Category | n | Open | W/L/RBL | Net |
|----------|---|------|---------|-----|
| `<null>` | 5 | 0 | 1/0/4 | +$125.95 |
| `weather` | 4 | 0 | 1/3/0 | +$13.84 |
| `weather_tail_no` | 5 | 2 | 1/1/1 | −$55.72 |
| `weather_tail_no_flipped` | 1 | 0 | 0/0/1 | −$21.00 |

### 4. By Side (Post-Fix Era)

| Side | n | Win Rate | Avg PnL | Avg Entry | Net |
|------|---|----------|---------|-----------|-----|
| NO | 4 | 50.0% | +$8.47 | 0.510 | +$33.87 |
| YES | 2 | 0.0% | −$24.01 | 0.275 | −$48.02 |

### 5. Calibration Profile (Post-Fix)

- **Sample size**: 6 settled non-rebal trades
- **Model predicted average win rate**: `0.638` (63.8%)
- **Realized win rate**: `0.333` (33.3%)
- **Calibration gap**: `-0.305` (-30.5pp) 🔴
- **Average pnl per trade**: −$2.36
- **Net P&L (post-fix)**: −$14.15
- **Average edge at entry**: +20.1pp

**Predicted vs Realized:**
```
  Predicted win rate : ████████████░░░░░░░░ 63.8%
  Realized win rate  : ██████░░░░░░░░░░░░░░ 33.3%
```

> 🚨 **Model is significantly OVER-confident.** Predictions fail to materialize >20pp below model expectation. Consider blocking new entries until calibration improves OR shrinking size by 50%+ and bumping edge floor.

### 6. Performance by Edge Bucket (Post-Fix)

| Edge band | n | Win Rate | Avg PnL | Net |
|-----------|---|----------|---------|-----|
| <0.05 | 1 | 0.0% | −$40.00 | −$40.00 |
| 0.10-0.15 | 2 | 0.0% | −$24.01 | −$48.02 |
| 0.15-0.20 | 1 | 0.0% | −$25.00 | −$25.00 |
| 0.20-0.30 | 1 | 100.0% | +$12.01 | +$12.01 |
| >=0.30 | 1 | 100.0% | +$86.86 | +$86.86 |

### 7. Performance by NO Entry Price (Post-Fix, NO trades only)

| Entry band | n | Win Rate | Net |
|------------|---|----------|-----|
| <0.30 | 1 | 100.0% | +$86.86 |
| 0.50-0.60 | 2 | 0.0% | −$65.00 |
| 0.70-0.80 | 1 | 100.0% | +$12.01 |

### 8. Best & Worst Trades

**Top wins:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #27 | 2026-04-21 | `<null>` | BUY | 0.160 | +0.088 | +$131.56 | Will the highest temperature in Tokyo be 23°C on April 21?... |
| #208 | 2026-04-27 | `weather` | BUY_NO | 0.220 | +0.492 | +$86.86 | Will the highest temperature in Tokyo be 18°C on April 27?... |
| #246 | 2026-04-29 | `weather_tail_no` | NO | 0.710 | +0.217 | +$12.01 | Will the highest temperature in Tokyo be 21°C on April 29?... |

**Top losses:**

| ID | Target | Cat | Side | Entry | Edge | PnL | Question |
|----|--------|-----|------|-------|------|-----|----------|
| #182 | 2026-04-27 | `weather_tail_no` | NO | 0.530 | +0.045 | −$40.00 | Will the highest temperature in Tokyo be 17°C on April 27?... |
| #245 | 2026-04-28 | `weather` | BUY_YES | 0.360 | +0.188 | −$25.00 | Will the highest temperature in Tokyo be 22°C on April 28?... |
| #306 | 2026-04-30 | `weather` | BUY_NO | 0.580 | +0.128 | −$25.00 | Will the highest temperature in Tokyo be 16°C on April 30?... |
| #252 | 2026-04-29 | `weather` | BUY_YES | 0.190 | +0.134 | −$23.02 | Will the highest temperature in Tokyo be 19°C on April 29?... |

### 9. Flip Experiment (weather_tail_no_flipped)

| ID | Target | Side | Entry | Outcome | PnL | Reason |
|----|--------|------|-------|---------|-----|--------|
| #279 | 2026-04-30 | YES | 0.300 | out=2 | −$21.00 | catastrophic_flip |

**Flip net: −$21.00 over 1 settled (0 wins). 0 open.**

### 10. STRATEGY RECOMMENDATION

**Tier: D — MODEL BROKEN — BLOCK**

- **Primary strategy**: BLOCK
- **`weather_tail_no` (NO direction)**: BLOCK
- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: BLOCK
- **Position size multiplier**: ×0.0

**Rationale**: Sample n=6, gap is severely negative. Model is broken in this city. Suspend new entries until n>=15 settles or recalibration.

---

## Cross-City Summary & Action Plan

### 🟢 Tier A — Lean In (size up + both sides)

- **amsterdam** (gap +0.18, n=7): net=+$129.98. size×1.2, both NO and flip OK.
- **hong_kong** (gap +0.28, n=4): net=+$78.08. size×1.5, both NO and flip OK.
- **toronto** (gap +0.36, n=5): net=+$183.14. size×1.5, both NO and flip OK.
- **seoul** (gap +0.41, n=6): net=+$174.30. size×1.5, both NO and flip OK.

### 🟡 Tier B — Standard Settings

- **sao_paulo** (gap≈0, n=5): net=+$20.12. Default settings.
- **milan** (gap≈0, n=4): net=−$5.55. Default settings.
- **wellington** (gap≈0, n=5): net=+$35.97. Default settings.
- **shanghai** (gap≈0, n=4): net=+$12.23. Default settings.
- **buenos_aires** (gap≈0, n=7): net=+$48.73. Default settings.

### 🟠 Tier C — Downsize

- **munich** (gap -0.11, n=7): net=−$82.79. size×0.5, edge floor 0.10.

### 🔴 Tier D — BLOCK

- **london** (gap -0.23, n=6): net=−$19.79. STOP new entries.
- **tokyo** (gap -0.30, n=6): net=−$14.15. STOP new entries.

### ⚪ Tier E — Insufficient Data

- **seattle** (n=0): net=—. Use global priors.
- **chicago** (n=1): net=−$40.00. Use global priors.
- **la** (n=0): net=—. Use global priors.
- **mexico_city** (n=0): net=—. Use global priors.
- **sf** (n=2): net=−$9.57. Use global priors.
- **nyc** (n=0): net=—. Use global priors.

## Implementation Plan — Per-City Data Class

Code architecture for shipping these recommendations:

```python
# polymarket_strat/domain/weather/city_calibration.py

@dataclass(frozen=True, slots=True)
class CityProfile:
    city: str
    tier: str               # "A" / "A-" / "B" / "C" / "D" / "E"
    n_settled: int
    avg_pred: float
    realized_wr: float
    calibration_gap: float
    size_multiplier: float  # 0.0 (block) to 1.5 (size up)
    edge_floor: float
    flip_eligible: bool
    last_recalibrated: datetime

# Usage in evaluate_tail_no_bracket:
profile = CITY_PROFILES.get(contract.city)
if profile.tier == 'D':
    return None, RejectReason.city_blocked
effective_edge_floor = profile.edge_floor or EDGE_FLOOR_PP
target_notional = base_notional * profile.size_multiplier
```

Data refreshed nightly via cron from `trade_history` (settled, post-fix only).

---

*End of report.*