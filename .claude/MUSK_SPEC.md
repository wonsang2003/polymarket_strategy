# MUSK TWEET ALPHA — DESIGN SPEC (Not Yet Implemented)

**Status:** zero code. Frozen until weather alpha clears Phase 2 gate.
**Expected impact:** 1–3 trades/day, complementary to weather (uncorrelated).

---

## 1. Core Insight

Elon's tweet rate is not random — it's a regime-switching process driven by observable state variables. The market prices tweet-count brackets based on vibes and recent averages. We model the regimes, detect transitions in real time, and exploit **intra-window Bayesian updating** to reprice brackets faster than the market.

**Contract universe**: weekly count brackets ("tweets April 7–14"), multi-day brackets ("Apr 13–15"), and monthly. Liquidity $200K–$1M per contract. Brackets span ranges of 20 (300–319, 320–339...).

---

## 2. Hidden Markov Model

### 2.1 States

| # | Regime | Rate (tweets/hr) | σ | Content pattern |
|---|--------|-------------------|---|-----------------|
| 0 | Shitpost Spree | 8.0 | 3.0 | Short, memes, emoji, rapid-fire |
| 1 | Executive | 1.5 | 0.8 | Announcements, long posts |
| 2 | Political Rage | 5.0 | 2.5 | Political commentary, aggressive |
| 3 | Quiet | 0.3 | 0.3 | Travel, meetings, sleeping |
| 4 | Engagement Spiral | 12.0 | 4.0 | Replying to everyone, feuds |

### 2.2 Transition matrix A

`A[i][j] = P(regime_{t+1} = j | regime_t = i)`. Initialize with high self-transition (0.80 diagonal, 0.05 off-diagonal uniform). Learn from data via Baum-Welch or Bayesian inference.

### 2.3 Emission: Negative Binomial (not Poisson)

Tweet counts are overdispersed. Var > mean.

```
count_t | regime_t = r ~ NegBinomial(μ_r, α_r)
variance = μ_r + μ_r² / α_r
```

Typical α ≈ 5–10.

### 2.4 Forward algorithm (online)

```
α_t(j) = [Σ_i α_{t-1}(i) * A[i][j]] * P(obs_t | regime=j)
α_t(j) /= Σ_k α_t(k)    # normalize
```

O(K²) per timestep, K=5. Updates regime belief as new hourly counts arrive.

### 2.5 Viterbi (for backtest regime labeling)

```
δ_t(j) = max_i [δ_{t-1}(i) * A[i][j]] * P(obs_t | regime=j)
ψ_t(j) = argmax_i [δ_{t-1}(i) * A[i][j]]
```

Backtrack from `argmax δ_T`.

---

## 3. Intra-Window Bayesian Updating (the actual innovation)

Weekly contract runs Monday→Monday (168h). Let:
- `N_obs` = tweets observed so far
- `N_rem` = remaining (unknown)
- `N_total = N_obs + N_rem`

**Bracket probability reduces to:**
```
P(N_total ∈ [lower, upper]) = P(N_rem ∈ [lower - N_obs, upper - N_obs])
```

### 3.1 Monte Carlo for P(N_rem)

10,000 simulations:
```
for s in 1..10000:
    z = sample(regime_posterior α_t)         # current regime
    for h in 1..h_remaining:
        λ = max(0.01, N(μ_z, σ_z²))          # sample hourly rate
        n_h = Poisson(λ)
        z = sample(A[z, :])                   # transition
    N_rem[s] = sum(n_h)
```

Empirical PMF → bracket probabilities.

### 3.2 Normal approximation (if h_remaining > 10)

By CLT:
```
N_total ~ N(N_obs + μ_rem, σ²_rem)
P(bracket) = Φ((upper + 0.5 - N_obs - μ_rem)/σ_rem) - Φ((lower - 0.5 - N_obs - μ_rem)/σ_rem)
```

### 3.3 Why it works

Mid-window (Wed), we have 72h observed. If Elon posted 180 in first 3 days (~2.5/hr, political regime) and market prices 300–319 at 42%, our model — knowing political + 96h remaining — might put conditional peak at 340–360. The 340–359 bracket is underpriced.

---

## 4. Content-Based Rate Prediction

### 4.1 Low-effort patterns (regex)

- `^(lol|haha|exactly|true|yes|no|100|indeed|wow|nice|based)$`
- `^.{1,15}$` — very short
- `^(🔥|😂|💀|👀|🚀|❤️|👍)+$` — emoji-only

### 4.2 Rate multiplier

```
multiplier = 1.0
multiplier += (low_effort_fraction - 0.3) * 0.8    # fast
multiplier -= (long_form_fraction - 0.1) * 0.5     # slow
if sentiment_declining: multiplier *= 1.4           # rage spiral
multiplier = clamp(0.2, multiplier, 3.0)

adjusted_mean = remaining_mean * multiplier
```

---

## 5. Trigger Event Detection

### 5.1 Rate increasers

- Tesla earnings day (SEC EDGAR calendar)
- SpaceX launch (publicly available manifest)
- Public attack on Elon (news API)
- Political controversy (Google Trends spike)
- Trump mentions him (Truth Social monitoring)

### 5.2 Rate decreasers

- International travel (timezone-shifted patterns)
- Factory visits, board meetings
- Rocket launches he attends

### 5.3 Implementation

Event calendar. When trigger falls within contract window:
```python
adjusted[target_regime] += confidence * 0.3
renormalize()
```

---

## 6. Cross-Window Autocorrelation

Weekly tweet count has ρ ≈ 0.3–0.4 with previous week. High-volume weeks cluster.

**Monthly contract implication**: sum of 4 correlated weekly draws →
```
Var(monthly) = 4 * Var(weekly) + 2 * Σ_{i<j} Cov(w_i, w_j) > 4 * Var(weekly)
```

Monthly tail brackets are **underpriced** by the market if it treats weeks as independent. Model as AR(1): `weekly_t = c + φ * weekly_{t-1} + ε` with φ ≈ 0.3–0.4.

---

## 7. Bayesian HMM in PyMC

```python
with pm.Model() as musk_model:
    # Transition matrix: Dirichlet conjugate prior
    for i in range(5):
        alpha_vec = np.full(5, 1.0)
        alpha_vec[i] = 10.0   # encourage self-persistence
        A_row_i = pm.Dirichlet(f"A_{i}", a=alpha_vec)

    # Emission parameters per regime
    mu_regime = pm.LogNormal("mu_regime", mu=np.log(prior_rates), sigma=0.5, shape=5)
    alpha_regime = pm.Gamma("alpha_regime", alpha=5, beta=1, shape=5)  # overdispersion

    # NegBinomial likelihood, conditioned on Viterbi regime labels
    for r in range(5):
        counts_r = hourly_counts[regime_labels == r]
        pm.NegativeBinomial(
            f"counts_{r}",
            n=alpha_regime[r],
            p=alpha_regime[r] / (alpha_regime[r] + mu_regime[r]),
            observed=counts_r
        )

    trace = pm.sample(draws=2000, chains=4, tune=1000, target_accept=0.9)
```

**Prediction with posterior uncertainty**:
```python
for s in simulations:
    rate = np.random.normal(posterior_rate_mean, posterior_rate_std)  # not point
    count = np.random.poisson(max(0.01, rate))
```

---

## 8. Execution Cadence

- Tweet stream polled every 60s (X API v2 or archive)
- Signal generation every 10 min (aggregate hourly, run forward algo, recompute brackets)
- Most valuable mid-window (40–70% elapsed)
- Intra-day bracket re-pricing as new data arrives

---

## 9. Integration Points

When wired in, this plugs into the same shared infra as weather:
- `StrategyApplicationService` — register as second strategy
- `PortfolioRiskManager` — quarter-Kelly, 2% per-position, same drawdown brake
- New correlation group: **Musk** (all musk contracts share risk)
- `py_clob_client` — same execution layer
- `weather.db` schema extension: `musk_tweets`, `musk_regime_log` tables
- Dedicated `alpha_musk/engine.py` mirroring `domain/weather/strategy.py` structure

---

## 10. Failure Modes

- **Elon changes behavior**: ghostwriter, scheduling tool, account suspension. Model breaks silently.
- **X API deprecation**: fallback to scraping or community archives.
- **Trigger event recall is imperfect**: missing a SpaceX launch shifts regime prior wrongly.
- **Overdispersion assumption breaks under shitpost sprees**: NegBin tail may still underestimate. Consider Poisson-Gamma mixture.
- **Liquidity is real but shallow**: typical bracket has $200K–$1M volume, but most of it gets eaten by a handful of orders.

---

## 11. Expected Performance

| Metric | Value |
|--------|-------|
| Signals/day | 1–3 |
| Avg edge/trade | 5–15¢ |
| Position size | $300–800 |
| Weekly P&L expected | $150–480 at $5k bankroll |
| Time to significance | ~60 days |
| Scaling ceiling | Low (single underlying) |

---

## 12. Go-Ahead Criteria

Do NOT start coding until:
1. Weather alpha Phase 2 gate passed (30 days paper, WR ≥ 55%, no artifacts)
2. Weather Phase 3 real capital replicating paper results within ±20%
3. At least one calibration cycle uses purely real operational forecasts (90 days from today)

Only then does Musk become worth the effort. The architecture here is sound; the opportunity cost of building it now (instead of tightening weather) is high.
