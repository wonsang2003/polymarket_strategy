# NO-SIDE SUPPORT — PHASE 1 IMPLEMENTATION SPEC

**Status:** Design approved Apr 21 2026. Zero code yet.
**Dependency:** Phase 1 refactor (Apr 19 flat-5¢ + bankroll-scaled risk) must be live on EC2. It is — deployed Apr 20 at 22:00 KST.
**Scope boundary:** This spec covers ONLY symmetric NO support + event cap. It does NOT cover the close-settlement farm, station-bias logging, or the two research monitors — those are separate documents.

---

## 1. Design decisions locked before implementation

### 1.1 The edge symmetry

For a binary contract with YES price = `P_yes`:

```
YES: pay P_yes,       win $1 if event happens
  edge_yes = (P_model - P_yes) - 0.02 * P_model * (1 - P_yes)

NO:  pay (1 - P_yes), win $1 if event does NOT happen
  edge_no  = (P_yes - P_model) - 0.02 * (1 - P_model) * P_yes
```

At most one is positive for any (P_model, P_yes). Same 5¢ flat gate applies to both. Market-band check `[0.15, 0.75]` applies to **the price you actually pay** — for NO that's `1 - P_yes`, not `P_yes`. A market at YES=0.10 (NO=0.90) fails the band on NO (crushed payoff) just as it fails on YES (penny artifact).

### 1.2 Multi-outcome markets skip NO

Two Polymarket market shapes exist (confirmed in `market_scanner.py:300-354`):

- **Binary** (line 313): `token_ids = [yes_token, no_token]` — NO is buyable directly.
- **Multi-outcome** (line 340): `token_ids = [outcome_a, outcome_b, ...]` — each outcome is its own YES. No per-bracket NO token exists. Implicit NO = buying all other outcomes; not supported in Phase 1.

Guard: if `contract.token_id_no == ""`, skip NO-side evaluation for that contract. Log the skip so we can measure how much alpha surface this leaves on the table. If it's >30% of NO signals, revisit in Phase 2.

### 1.3 Event cap bundled (not optional)

`max_event_notional_fraction: float = 0.10` in `TradingConstraints`. A single event (e.g. "highest temp Tokyo Apr 21") may have 10+ brackets. NO bets on multiple same-event brackets are correlated ≈ 1 (all lose on the same surprise direction). Must cap at event level to prevent single-surprise concentration.

Event identity for weather: `(city, target_date)`. Unique per Polymarket event by construction — no need to add `event_id` to `BracketContract` schema. Tracking in-cycle via `event_notional: dict[tuple[str, date], float]`.

### 1.4 Mutual exclusion: YES+NO on same bracket

If the analysis cycle flags both YES and NO signals on the same contract (should never happen mathematically — raw_edge has one sign), OR if state shows YES already open on a contract and NO fires (or vice versa), **skip the new one**. These positions are fully anti-correlated and net to zero — no reason to hold both.

Concretely: when loading `already_open_token_ids`, expand to `already_open_contracts`. A contract is "open" if either its YES or NO token_id appears in state. Signals of either side on already-open contracts are skipped.

### 1.5 Feature flag

`TradingConstraints.enable_no_side: bool = False` default. Flip to `True` on paper, leave `False` on live until Day-14 gate clears (14 days of paper NO signals with realized win rate ≥ 50% and non-negative cumulative P&L on the NO subset).

Why the flag and not a branch/commit gate: rollback is one config change, not a revert. On paper we can toggle it on immediately; if live Phase 3 starts in May at $200 capital, we keep the flag off in live config until we're confident.

---

## 2. File-by-file change list

Six files. Keep edits surgical; no architectural shifts.

### 2.1 `polymarket_strat/config.py`

Add to `TradingConstraints` dataclass:

```python
# NO-side support + event cap (Phase 1, spec: NO_SIDE_SPEC.md)
enable_no_side: bool = False
max_event_notional_fraction: float = 0.10   # 10% of bankroll per event
```

No removals. Existing `min_edge_flat = 0.05` applies to both sides.

### 2.2 `polymarket_strat/domain/weather/forecast.py`

**Replace** `BracketProbabilityCalculator.edge()` signature. New return shape: a dict with both sides, each carrying `(raw, adjusted, tradeable)`.

```python
@staticmethod
def edge(
    *,
    model_prob: float,
    market_prob: float,       # YES price; NO price derived
    fee_rate: float = 0.02,
    min_edge: float = 0.05,
) -> dict[str, tuple[float, float, bool]]:
    """Compute fee-adjusted edge for both sides.

    Returns dict with keys 'yes' and 'no'. Each value is
    (raw_edge, edge_after_fees, is_tradeable). At most one side
    is tradeable for any (P_model, P_market) pair.

    Market-band gate applies to the PAID price:
      YES pays P_market      → band check on P_market
      NO  pays (1 - P_market) → band check on (1 - P_market)
    """
    # YES
    raw_yes = model_prob - market_prob
    fee_yes = fee_rate * model_prob * (1.0 - market_prob)
    adj_yes = raw_yes - fee_yes
    yes_band = 0.15 <= market_prob <= 0.75
    yes_ok = yes_band and adj_yes >= min_edge

    # NO
    no_price = 1.0 - market_prob
    raw_no = market_prob - model_prob
    fee_no = fee_rate * (1.0 - model_prob) * market_prob
    adj_no = raw_no - fee_no
    no_band = 0.15 <= no_price <= 0.75
    no_ok = no_band and adj_no >= min_edge

    return {
        "yes": (raw_yes, adj_yes, yes_ok),
        "no":  (raw_no, adj_no, no_ok),
    }
```

**Add** a backward-compat wrapper so anything that still calls the old 3-tuple signature doesn't break (backtest.py, tests, external tooling):

```python
@staticmethod
def edge_yes_only(
    *, model_prob, market_prob, fee_rate=0.02, min_edge=0.05
) -> tuple[float, float, bool]:
    """Legacy YES-only edge. Prefer `edge()` for new callers."""
    return BracketProbabilityCalculator.edge(
        model_prob=model_prob, market_prob=market_prob,
        fee_rate=fee_rate, min_edge=min_edge,
    )["yes"]
```

Audit callers: `grep -rn "BracketProbabilityCalculator.edge\|\.edge(" polymarket_strat/ tests/` — redirect stale callers to `edge_yes_only()` or update to new API.

### 2.3 `polymarket_strat/domain/models.py`

Add one field to `StrategySignal` and `TradePlan`:

```python
direction: str = "YES"   # "YES" or "NO". Kept separate from `side` (always "BUY").
```

Default "YES" preserves backward compat for non-weather strategies (whale_following, mispricing, etc.) that don't set it. `side` stays "BUY" always — we never short/sell on Polymarket CLOB.

### 2.4 `polymarket_strat/domain/weather/strategy.py`

Three edits inside `_analyze_weather_brackets`:

**Edit A: branch on side** (replaces existing edge() unpacking around line 365-375).

```python
sides = BracketProbabilityCalculator.edge(
    model_prob=bp.model_prob,
    market_prob=contract.market_price_yes,
    min_edge=effective_min_edge,
)

# Skip multi-outcome markets for NO side (no NO token exists)
no_tradeable = sides["no"][2] and contract.token_id_no

if sides["yes"][2]:
    direction = "YES"
    chosen_token = contract.token_id_yes
    paid_price = contract.market_price_yes
    edge_adj = sides["yes"][1]
elif constraints.enable_no_side and no_tradeable:
    direction = "NO"
    chosen_token = contract.token_id_no
    paid_price = 1.0 - contract.market_price_yes
    edge_adj = sides["no"][1]
else:
    # Neither side tradeable — classify rejection
    if not sides["yes"][2] and not sides["no"][2]:
        yes_band = 0.15 <= contract.market_price_yes <= 0.75
        if not yes_band:
            gate_rejects["gate1_market_band"] += 1
        else:
            gate_rejects["gate2_min_edge"] += 1
    elif sides["no"][2] and not contract.token_id_no:
        gate_rejects["no_side_unavailable_multi_outcome"] += 1
    else:
        # NO tradeable but flag off
        gate_rejects["no_side_disabled"] += 1
    continue
```

**Edit B: mutual-exclusion guard.** Where `already_open_token_ids` is built from state, change to also block on the opposite token:

```python
# Build a set of condition_ids (market_ids) with any open position,
# regardless of side. A YES+NO pair on the same bracket is anti-correlated.
already_open_markets: set[str] = set()
for token_id in portfolio_state.open_positions:
    for city_key_scan in [city]:
        for contract_scan in weather_brackets_by_city[city_key_scan]:
            if token_id in (contract_scan.token_id_yes, contract_scan.token_id_no):
                already_open_markets.add(contract_scan.market_id)
                break
```

Then in the per-contract loop:

```python
if contract.market_id in already_open_markets:
    gate_rejects["duplicate_position"] += 1
    continue
```

(Simpler alternative: pass a `market_id → token_ids` lookup in from main.py so strategy doesn't re-scan. Premature optimization though — leave the double-loop until it's demonstrably slow.)

**Edit C: event cap tracking.** Precompute and enforce:

```python
event_notional_cap = (
    constraints.bankroll * constraints.max_event_notional_fraction
)
event_notional: dict[tuple[str, date], float] = defaultdict(float)

# ... in the per-contract loop, BEFORE appending the plan:
event_key = (city, contract.target_date)
if event_notional[event_key] + target_notional > event_notional_cap:
    gate_rejects["event_cap"] += 1
    continue
event_notional[event_key] += target_notional
```

Also propagate `direction` into both the `StrategySignal` and `TradePlan` constructors (use `chosen_token` and `direction` from Edit A; YES path keeps current token_id_yes).

**Edit D: diagnostics output.** Add to the diagnostics dict at the bottom:

```python
"event_notional": {f"{c}@{d.isoformat()}": round(v, 2)
                   for (c, d), v in event_notional.items()},
"event_notional_cap": round(event_notional_cap, 2),
"no_side_enabled": constraints.enable_no_side,
```

And track direction counts:

```python
"direction_counts": {"yes": yes_count, "no": no_count},
```

### 2.5 `polymarket_strat/execution.py`

`PaperExecutor.execute(plan)`: no logic change needed — it already uses `plan.token_id` which Edit A sets correctly. Add `direction` to the `simulated` record for audit:

```python
record = {
    # ... existing fields
    "direction": plan.direction,  # new
}
```

`LiveExecutor.execute(plan)`: same — `py_clob_client` order takes `token_id`, which is already correctly set to NO token for NO plans. Verify no hardcoded YES assumption in the order construction. Likely a one-line `grep -n "token_id_yes\|outcome.*Yes" polymarket_strat/execution.py` — patch any.

### 2.6 `polymarket_strat/main.py`

Settlement win-check flips based on direction. In `_settle_from_iem` (or wherever YES resolution is currently computed):

```python
yes_won = (lower <= observed < upper)
won = yes_won if position.direction == "YES" else not yes_won
```

P&L formula is already symmetric. On win: `n_shares * (1 - entry_price) * 0.98`. On loss: `-notional`. Entry_price for a NO position is the NO price paid, which the TradePlan already records as `reference_price` (under Edit A's `paid_price`).

**Persistence:** `trade_history.side` currently stores "BUY". Add `direction` column — schema change required:

```sql
ALTER TABLE trade_history ADD COLUMN direction TEXT DEFAULT 'YES';
```

Backfill existing rows to 'YES' (all current trades are YES). New inserts populate from `TradePlan.direction`. Apply in `infrastructure/weather/persistence.py::WeatherDatabase.__init__` or via a one-shot migration script.

---

## 3. Tests to add

Three new test files, ~15 cases total.

### 3.1 `tests/test_edge_no_side.py`

Table-driven tests on `edge()`:

| case | P_model | P_market | expected_side | expected_adj_edge | expected_tradeable |
|------|---------|----------|---------------|-------------------|---------------------|
| A — YES medium | 0.60 | 0.50 | yes | ≈ 0.094 | True (yes only) |
| B — NO medium | 0.30 | 0.40 | no | ≈ 0.096 | True (no only) |
| C — neither | 0.50 | 0.50 | — | — | False on both |
| D — tight YES fails min | 0.54 | 0.50 | yes | ≈ 0.036 | False (< 5¢) |
| E — YES band violation | 0.90 | 0.80 | yes | > 0.05 | False (market > 0.75) |
| F — NO band violation | 0.05 | 0.10 | no | > 0.05 | False (no_price 0.90 > 0.75) |
| G — symmetric zero | 0.50 | 0.30 | yes | ≈ 0.193 | True (yes only) |

Plus: assert `sides["yes"]` and `sides["no"]` are never simultaneously tradeable.

### 3.2 `tests/test_event_cap.py`

Mock strategy with 5 synthetic brackets, all on same `(city, target_date)`, all with edge > 5¢, all requesting $30 notional at bankroll=$1000. Expect:
- 3 pass (cumulative 90 < 100)
- 4th rejected: `gate_rejects["event_cap"] == 1`
- 5th also rejected: `gate_rejects["event_cap"] == 2`

### 3.3 `tests/test_mutual_exclusion.py`

State has YES token for bracket X open. Cycle scans bracket X again with NO-side edge. Expect:
- No plan for bracket X
- `gate_rejects["duplicate_position"] == 1`

Flip-case: state has NO token for bracket Y. Cycle finds YES edge on Y. Same result.

### 3.4 Update existing tests

- `tests/test_strategy.py` — add `direction="YES"` to any hand-constructed TradePlan fixtures (dataclass default handles most; only override checks break).
- `tests/test_settle.py` — add one case settling a NO position (bracket didn't hit → NO wins → P&L per formula).

Target: all 33 existing + ~12 new = 45 tests passing before merge.

---

## 4. Rollout

### 4.1 Local build

```bash
cd /Users/wonsangchang/Downloads/polymarket_strat
# Implement edits 2.1 → 2.6 + tests 3.1 → 3.4
pytest -x        # expect 45 passing
python -c "from polymarket_strat.domain.weather.forecast import BracketProbabilityCalculator as C; \
  print(C.edge(model_prob=0.30, market_prob=0.40))"
# Expected: {'yes': (..., negative, False), 'no': (0.10, ≈0.096, True)}
```

### 4.2 Commit sequence

Three commits, each independently revertable:

1. `feat(models): add direction field to StrategySignal and TradePlan` (2.3)
2. `feat(weather): symmetric edge() + NO-side signal path + event cap` (2.1, 2.2, 2.4)
3. `feat(exec+settle): route NO plans to NO token and flip win-check` (2.5, 2.6)
4. `test: NO-side, event cap, mutual exclusion` (3.1-3.4)

### 4.3 Deploy

```bash
bash deploy/upload_to_ec2.sh
```

The flag starts `False` on EC2 by default. To enable on paper:

```bash
ssh -i ~/.ssh/polymarket-seoul.pem ubuntu@54.180.64.168 \
    "echo 'ENABLE_NO_SIDE=true' >> /home/ubuntu/polymarket/.env"
```

And wire `.env` reading into `TradingConstraints.from_env()` if it doesn't already pick up `ENABLE_NO_SIDE`.

Restart isn't needed — cron picks up fresh env on next cycle.

### 4.4 Day-14 paper gate

Criteria to flip `enable_no_side=True` on live:
- ≥ 14 days of paper signals with NO trades (not "enabled but zero trades")
- NO-subset win rate ≥ 50%
- NO-subset cumulative P&L ≥ 0 after fees
- No asymmetric bug signature (e.g. NO trades settling as if YES)

Tracking: query `trade_history` by `direction = 'NO'` nightly. One-liner:

```sql
SELECT COUNT(*), AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), SUM(pnl)
FROM trade_history WHERE direction = 'NO' AND mode = 'paper' AND settled_at IS NOT NULL;
```

If Day 14 fails, debug before enabling on live. Possible failure modes:
- Win-check flip bug (NO marked as loss when it won, or vice versa)
- Fee drag formula error on NO side
- Scanner populating wrong `token_id_no`
- Multi-outcome market accidentally fed into NO path (shouldn't happen per guard, but verify)

### 4.5 Rollback

Single config change: `enable_no_side=False` → next cron cycle stops generating NO signals. Existing open NO positions settle normally under the unchanged settlement logic.

If a bug is in the settlement path itself (win-check flip), that's more serious — emergency revert to pre-NO commits and manually replay the affected trades. Low probability given test coverage, but plan for it.

---

## 5. Gotchas and non-goals

**Gotchas:**
- Kelly sizing uses `paid_price` not `market_prob`. Critical — Kelly with wrong odds overstates bet size by `1/(1-P)` factor on NO side.
- Duplicate-position guard must expand state lookup to cover both sides' token_ids. Missing this = YES+NO pair stacking on same bracket.
- Scanner's multi-outcome branch leaves `token_id_no=""`. Not a bug — guard handles it.
- DB migration must run before any NO trade is persisted. Ordering: ALTER TABLE first, then deploy code.
- `event_key = (city, target_date)` — verify `target_date` is consistent across same-event brackets. Spot-check with Tokyo Apr 21: all 11 brackets should share `(city='tokyo', target_date=date(2026,4,21))`.

**Non-goals explicitly out of scope for Phase 1:**
- Close-settlement NO farm with relaxed edge (3¢) on tail brackets. See Phase 2 in discussion thread — blocked on tail calibration audit + standing-edge monitor data.
- Station vs city-center bias correction. See Phase 3 — passive logger only for 90 days.
- True short-selling (selling YES you don't own). Not supported by Polymarket CLOB.
- Cross-event correlation modeling (e.g., "cold front hits Tokyo AND Seoul"). Keep correlation groups as static approximation.

**Unknowns to watch after deploy:**
- How often does the multi-outcome guard fire? If >30% of NO candidate signals land on multi-outcome markets, we're leaving meaningful alpha on the table. Consider synthetic NO (buy all other outcomes) in a later phase — expensive at current spreads.
- Does NO-side liquidity look materially worse than YES? If NO fills consistently at 1-2¢ worse than quoted reference, tighten `max_spread` for NO-side only.
- Does event cap bind more often on NO-heavy days? If yes, consider raising to 15% and reducing per-position to 3%. Re-tune after Day 14 paper data.

---

## 6. Effort estimate

~1.5 days solo engineering:
- Config + models + forecast.py: 2 hours
- strategy.py edits (most complex): 4 hours
- execution.py + main.py + persistence: 2 hours
- Tests: 3 hours
- Local verification + commit: 1 hour
- Deploy + observation for 4 hours: 4 hours

Research monitor scripts (Phase 1.5) specced separately — `MONITORS_SPEC.md`.
