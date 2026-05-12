"""Weekly Strategy Review — Claude analyzes 7d data + code + hypothesis history.

Runs Wed + Sun 09:00 KST via cron. Compared to daily_brief, this is much
deeper: 7-day aggregates, calibration drift, code excerpts, prior
hypothesis outcomes, untried hypothesis register.

Outputs structured Telegram report with proposed changes the user can
approve via reply (YES_1, YES_1_2 etc.).

Cost: ~$0.25/run with Claude Opus, runs 2x/week = ~$2/month.

Env: ANTHROPIC_API_KEY (required), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")
ROOT = Path("/home/ubuntu/polymarket")
LOG_DIR = Path("/home/ubuntu/polymarket/logs")
REPORT_DIR = Path("/home/ubuntu/polymarket/reports/strategy_reviews")
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Env loading (shared with daily_brief)
# ============================================================================

def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ============================================================================
# Data collection (deeper than daily)
# ============================================================================

def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def collect_data() -> dict:
    c = conn()
    now = datetime.now(timezone.utc)
    out: dict = {"now_utc": now.isoformat()}

    # 7d P&L
    r = c.execute("""
        SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-7 days')
    """).fetchone()
    out["last_7d"] = dict(r)

    # 14d for trend
    r = c.execute("""
        SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-14 days')
    """).fetchone()
    out["last_14d"] = dict(r)

    # Lifetime
    r = c.execute("""
        SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history
    """).fetchone()
    out["lifetime"] = dict(r)

    # Per-day last 14d
    daily = []
    for d in range(14, 0, -1):
        day_kst = (now + timedelta(hours=9, days=-d)).strftime("%Y-%m-%d")
        r = c.execute(
            "SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n "
            "FROM trade_history WHERE settled_at LIKE ? || '%'",
            (day_kst,),
        ).fetchone()
        daily.append({"date": day_kst, "net": r["net"], "n": r["n"]})
    out["last_14d_daily"] = daily

    # Per-category × outcome (last 7d)
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               CASE WHEN outcome=2 THEN 'rebal'
                    WHEN pnl>0 THEN 'WIN' ELSE 'LOSS' END AS r,
               COUNT(*) AS n,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(pnl), 2) AS avg
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-7 days')
        GROUP BY cat, r
    """).fetchall()
    out["7d_by_cat"] = [dict(x) for x in rows]

    # Per-city (last 7d)
    rows = c.execute("""
        SELECT city, COUNT(*) AS n,
               SUM(CASE WHEN outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
               SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(model_prob), 3) AS avg_pred,
               ROUND(AVG(CASE WHEN outcome != 2 AND pnl > 0 THEN 1.0
                              WHEN outcome != 2 THEN 0.0 END), 3) AS realized_wr
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-7 days')
        GROUP BY city
        ORDER BY net
    """).fetchall()
    out["7d_by_city"] = [dict(x) for x in rows]

    # Exit reason breakdown
    rows = c.execute("""
        SELECT
          CASE
            WHEN outcome = 2 THEN COALESCE(exit_reason, 'unknown_rebal')
            WHEN pnl > 0 THEN 'WIN_settle'
            ELSE 'LOSS_settle'
          END AS exit_type,
          COUNT(*) AS n,
          ROUND(SUM(pnl), 2) AS net,
          ROUND(AVG(pnl), 2) AS avg
        FROM trade_history
        WHERE outcome IS NOT NULL
          AND datetime(settled_at) >= datetime('now', '-7 days')
        GROUP BY exit_type
        ORDER BY net
    """).fetchall()
    out["7d_by_exit_reason"] = [dict(x) for x in rows]

    # Open book
    rows = c.execute("""
        SELECT t.id, t.city, t.entry_price, t.notional, mp.best_bid AS bid
        FROM trade_history t
        LEFT JOIN (
            SELECT mp1.* FROM market_prices mp1
            JOIN (SELECT token_id, MAX(fetched_at_utc) AS mx
                  FROM market_prices GROUP BY token_id) lt
              ON lt.token_id = mp1.token_id AND lt.mx = mp1.fetched_at_utc
        ) mp ON mp.token_id = t.token_id
        WHERE t.outcome IS NULL
    """).fetchall()
    open_count = len(rows)
    open_notl = sum(r["notional"] or 0 for r in rows)
    open_upnl = 0.0
    for r in rows:
        e = r["entry_price"] or 0
        b = r["bid"]
        n = r["notional"] or 0
        if e > 0 and b is not None:
            shares = n / e
            gross = shares * (b - e)
            fee = 0.02 * gross if gross > 0 else 0
            open_upnl += gross - fee
    out["open_book"] = {
        "count": open_count,
        "notional": round(open_notl, 2),
        "unrealized_pnl": round(open_upnl, 2),
    }

    # Recent commits (last 7 days)
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--oneline", "--since=7 days ago"],
            capture_output=True, text=True, timeout=5,
        )
        out["recent_commits"] = result.stdout.strip().splitlines()[:20]
    except Exception:
        out["recent_commits"] = []

    # Hypothesis history (last 30 days, focus on shipped + evaluated)
    rows = c.execute("""
        SELECT id, proposed_at, hypothesis, expected_effect, confidence_pct,
               status, shipped_at, actual_effect, verdict, measured_pnl_delta
        FROM strategy_hypotheses
        WHERE proposed_at >= datetime('now', '-30 days')
        ORDER BY proposed_at DESC
        LIMIT 20
    """).fetchall()
    out["hypothesis_history"] = [dict(x) for x in rows]

    # Lessons learned
    rows = c.execute("""
        SELECT lesson, period, confidence, contradicted_at
        FROM lessons_learned
        WHERE contradicted_at IS NULL
        ORDER BY id DESC
        LIMIT 15
    """).fetchall()
    out["lessons"] = [dict(x) for x in rows]

    # Untried hypotheses (top 5 by EV)
    rows = c.execute("""
        SELECT id, idea, rationale, estimated_effort_hr,
               estimated_impact_usd, estimated_p_correct, expected_value
        FROM untried_hypotheses
        WHERE status = 'pending'
        ORDER BY expected_value DESC NULLS LAST
        LIMIT 5
    """).fetchall()
    out["untried"] = [dict(x) for x in rows]

    # Strategy snapshot
    try:
        sys.path.insert(0, str(ROOT))
        from polymarket_strat.domain.weather import (
            tail_no_strategy as tns,
            city_calibration as cc,
        )
        out["strategy_snapshot"] = {
            "edge_floor_pp": tns.EDGE_FLOOR_PP,
            "no_ask_min": tns.NO_ASK_MIN,
            "no_ask_max": tns.NO_ASK_MAX,
            "wide_bracket_f": tns.WIDE_BRACKET_F,
            "wide_bracket_min_no_ask": tns.WIDE_BRACKET_MIN_NO_ASK,
            "flip_threshold": tns.FLIP_NO_TO_YES_THRESHOLD,
            "city_bias": dict(cc.CITY_NO_BIAS),
            "city_bias_enabled": cc.ENABLED,
        }
    except Exception as e:
        out["strategy_snapshot"] = {"error": str(e)}

    # Code excerpts (key sections of strategy files for context)
    excerpts = {}
    files_to_excerpt = [
        ("polymarket_strat/domain/weather/city_calibration.py", "all"),
        ("polymarket_strat/domain/weather/tail_no_strategy.py", "constants"),
    ]
    for rel, mode in files_to_excerpt:
        full = ROOT / rel
        if not full.exists():
            continue
        text = full.read_text()
        if mode == "all":
            excerpts[rel] = text[:6000]
        elif mode == "constants":
            # Extract just the constants section (top 100 lines)
            excerpts[rel] = "\n".join(text.splitlines()[:120])
    out["code_excerpts"] = excerpts

    c.close()
    return out


# ============================================================================
# Prompt construction
# ============================================================================

def fmt_signed(v):
    if v is None:
        return "—"
    return f"{v:+.2f}"


def build_prompt(data: dict) -> str:
    lt = data["lifetime"]
    w7 = data["last_7d"]
    w14 = data["last_14d"]
    ob = data["open_book"]
    snap = data["strategy_snapshot"]

    # Daily 14d
    daily = "\n".join(
        f"  {d['date']}: ${fmt_signed(d['net'] or 0)} ({d['n']} events)"
        for d in data["last_14d_daily"]
    )

    # Per cat
    cat_lines = "\n".join(
        f"  {x['cat']:<26} {x['r']:<6} n={x['n']:>3}  net=${fmt_signed(x['net'] or 0)}"
        for x in data["7d_by_cat"]
    ) or "  (none)"

    # Per city
    city_lines = "\n".join(
        f"  {x['city']:<14} n={x['n']:>3} W{x['w']:>2}/L{x['l']:>2}/R{x['rbl']:>2} "
        f"net=${fmt_signed(x['net'] or 0)} "
        f"pred={x['avg_pred'] or 0:.2f} real={x['realized_wr'] or 0:.2f}"
        for x in data["7d_by_city"]
    ) or "  (none)"

    # Exit reason
    exit_lines = "\n".join(
        f"  {x['exit_type']:<26} n={x['n']:>3} net=${fmt_signed(x['net'] or 0)} "
        f"avg=${fmt_signed(x['avg'] or 0)}"
        for x in data["7d_by_exit_reason"]
    ) or "  (none)"

    # Bias dict (compact)
    bias_str = "\n".join(
        f"    {k:<14}: {v:+.3f}" for k, v in
        sorted(snap.get("city_bias", {}).items(), key=lambda x: x[1])
    )

    # Hypothesis history
    if data["hypothesis_history"]:
        hyp_lines = []
        for h in data["hypothesis_history"]:
            hyp_lines.append(
                f"  [{h['id']}] {h['hypothesis']}\n"
                f"    proposed: {h['proposed_at']}, conf={h['confidence_pct']}%, "
                f"status={h['status']}\n"
                f"    expected: {h['expected_effect']}\n"
                f"    actual: {h['actual_effect'] or '(not yet evaluated)'}\n"
                f"    verdict: {h['verdict'] or '(pending)'}\n"
                f"    measured pnl delta: {h['measured_pnl_delta']}"
            )
        hyp_str = "\n".join(hyp_lines)
    else:
        hyp_str = "  (no hypotheses logged yet — this is a fresh start)"

    # Lessons
    if data["lessons"]:
        lesson_str = "\n".join(
            f"  - [{l['period']}] {l['lesson']} (conf: {l['confidence']})"
            for l in data["lessons"]
        )
    else:
        lesson_str = "  (no lessons distilled yet)"

    # Untried
    if data["untried"]:
        untried_str = "\n".join(
            f"  [{u['id']}] {u['idea']}\n"
            f"    rationale: {u['rationale']}\n"
            f"    impact_est: ${u['estimated_impact_usd']} × p={u['estimated_p_correct']} "
            f"= EV ${u['expected_value']}"
            for u in data["untried"]
        )
    else:
        untried_str = "  (register empty)"

    # Code excerpts
    code_blocks = []
    for path, content in data["code_excerpts"].items():
        code_blocks.append(f"--- {path} ---\n{content}\n")
    code_str = "\n".join(code_blocks)

    commits = "\n".join(f"  {c}" for c in
                        (data["recent_commits"] or ["  (no recent commits)"]))

    return f"""You are Head of Quantitative Research for the Polymarket weather alpha system. Harvard MBA + PhD in Statistics. Ten years at Renaissance Technologies. Now you advise this trader on his self-evolving paper-mode strategy.

This is the bi-weekly DEEP review (Wed/Sun 09:00 KST). Unlike the daily brief — which is a 30-second phone glance — this is a Sunday-morning analytical deliverable. The trader sits down with coffee and reads it twice. You have permission to be long, mathematical, and rigorous. Reward for depth and synthesis exceeds reward for brevity here.

═══════════════════════════════════════════════════════════════
PHILOSOPHY (binding rules)
═══════════════════════════════════════════════════════════════
1. BLUF every message — first 1-2 lines are the conclusion. Then derive.
2. Quantify or don't claim. Every assertion needs $, σ, n, or a binomial-style p-value. No "성과가 개선됨" without dollar number AND sample size.
3. Skill vs luck. With n < 30 flag as noise unless effect size is huge (z > 2). State the noise band explicitly: "n=18, ±2σ band ≈ ±$X". With n ≥ 30, state the binomial p-value of the observed outcome under the null hypothesis.
4. Mechanism > outcome. ALWAYS provide the causal chain: "City X bleeds because regime classifier mislabels frontal_passage as stable_high → σ underestimated → narrow brackets overpriced → −$X." Three steps minimum.
5. Pre-mortem every recommendation: list 3+ failure modes. "If this fails, the failure mode is ___" repeated three times.
6. Red-team yourself: what would falsify each hypothesis? What's the next data point that changes my mind?
7. Cite file:line for every code change. Show the actual diff in a fenced ```python block, not prose.
8. Confidence calibration. Reference your prior batting avg explicitly: "raw confidence X%, calibrated to Y% (my X-bucket historically hit Y%)". If first review with no prior data, state "raw X% — calibration unmeasured, treat as approximate."
9. Hypothesis genealogy. When a current proposal supersedes a prior shipped change, name the parent ID (e.g. "supersedes H_2026_05_03_02"). When proposing something the lessons_learned table already contradicts, NAME the lesson and explain why this case is different.
10. Math, not vibes. For every "+$X/wk expected" claim, show the derivation: "(N current trades × avg loss $X) − (estimated retained winners) = +$Y net".

═══════════════════════════════════════════════════════════════
VISUAL HIERARCHY (binding — render as plain text on Telegram)
═══════════════════════════════════════════════════════════════
parse_mode is "" so * and _ render literal. No markdown bold/italic.

Use Unicode anchors as semantic tokens (one per line, NOT decorative):
  📈 = performance / equity        📊 = scorecard / data
  🎯 = attribution / target        📁 = file / code locus
  💀 = biggest loss source         🚨 = signal (n × p-value confirms)
  🔬 = mechanism / hypothesis      ⚠️ = warning / pattern
  📌 = decision / pin              🛠️ = recommendation
  💰 = magnitude in $              🧮 = derivation / arithmetic
  💡 = idea / direction            🎲 = calibrated confidence
  🛡️ = risk decomposition         ⏱️ = ship cost / timeline
  🔁 = trigger / loop              🧠 = analyst's interpretation
  🌑 = shadow strategy             🆕 = new artifact (hypothesis)
  ↗ ↘ ▲ ▼ ◀ → = directional      ★ = priority marker
  ① ② ③ ④ = ranked items          ├ │ └ = sub-bullets
  🟢 🟡 🔴 = severity (good/watch/critical)

Section divider:  ━━━━━━━━━━━━━━━━━━━━━━━━━━  (28 dashes)
Subsection divider: ──────────────────────  (24 light dashes)
Numbers: $X.XX format, right-align in monospace blocks, spaces not tabs.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT — 14 SEPARATE TELEGRAM MESSAGES (depth mode)
═══════════════════════════════════════════════════════════════
Output EXACTLY 14 messages separated by EXACTLY this delimiter on its own line:

---MSG---

🪙 OUTPUT BUDGET (binding — failure to budget = ALL 14 messages don't ship)
   You have ~16,000 output tokens TOTAL across 14 messages.
   Average target: 1,100 tokens / 1,500 chars per message.
   Acceptable range: 800-1,800 chars per message.
   Variation rule: a longer message must be paid for by shorter siblings.
   NEVER let MSG 1-8 be verbose at the cost of skipping MSG 9-14.
   If you find yourself near 12,000 output tokens before reaching MSG 11,
   compress remaining messages aggressively rather than truncating.
   The COMPLETE 14-message structure is more important than any single
   message being maximally rich.

Use 3+ spaces of indent under each emoji-anchored block. Whitespace is part of the visual punch — but it counts toward your budget.

────────────────────────────────────────────────────────────
[MSG 1] EXEC SUMMARY · BLUF + headline rec
────────────────────────────────────────────────────────────
📈 STRATEGY REVIEW · <date> KST
━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 7d performance
   Realized   <$X>  (n=N)
   vs lifetime avg  <$Y>/7d
   → <Z×> <better/worse>.  Z-score = <±X>σ.
   <one-line interp: noise / regime shift / improvement>

🎯 Single biggest driver
   <category or city + details>
   = X% of weekly P&L.
   Mechanism (proven in MSG <n>): <one line>

🚨 Headline recommendation (R1, MSG 8)
   <title>
   <file:line>
   Risk <level>, reversible.
   Expected <+/-$X>/wk.
   Calibrated conf <X>%
   (raw <Y>%, batting-avg shrinkage applied)

📋 This review proposes
   N ships · M reverts · K new hypotheses · L untried promotions

────────────────────────────────────────────────────────────
[MSG 2] P&L DECOMPOSITION — attribution math
────────────────────────────────────────────────────────────
🎯 P&L DECOMPOSITION (7d)
━━━━━━━━━━━━━━━━━━━━━━━━━━

💀 Where it went TO (losses, ranked)
   ① <cat/city>  −$X  (% of bleed, n=N)
     Mechanism: <one line>
   ② <cat/city>  −$X  (%, n=N)
     Mechanism: ...
   ③ <cat/city>  −$X  (%, n=N)
     Mechanism: ...

🟢 Where it came FROM (wins, ranked)
   ① <cat/city>  +$X  (n=N)  Mechanism: ...
   ② ...
   ③ ...

🧮 Attribution math
   Net 7d           : −$X
   Top-1 loss share : XX%
   Hit rate         : N/total = X% (vs lifetime XX%)
   Avg trade size   : $X (vs spec $X)
   Sharpe-per-trade : X.XX

────────────────────────────────────────────────────────────
[MSG 3] PER-CITY DEEP SCORECARD
────────────────────────────────────────────────────────────
📊 PER-CITY SCORECARD (7d, n ≥ 3 only)
━━━━━━━━━━━━━━━━━━━━━━━━━━

city          n   wr%  $7d    pred/real  tier
─────────────────────────────────────────────
<city>        N   XX%  $±X    .XX/.XX    A↑/B↓/=
<city>        N   XX%  $±X    .XX/.XX    A
... one row per city ...

🔄 Tier movement (이번 주 변동)
   ↑ <city>: <reason for upgrade>
   ↓ <city>: <reason for downgrade>
   = <city>: <held but worth noting>

⚠️ Outlier watch
   <city> predicted .XX vs realized .XX
   gap = <Δ>, n=N → <noise / signal verdict>

────────────────────────────────────────────────────────────
[MSG 4] HYPOTHESIS VERDICTS — DAG + counterfactual
────────────────────────────────────────────────────────────
🧠 HYPOTHESIS VERDICTS  (last 30d, ranked by recency)
━━━━━━━━━━━━━━━━━━━━━━━━━━

For each hypothesis in DATA "HYPOTHESIS HISTORY":

[<ID>] <hypothesis text, 1 line>
  proposed   <date> · conf <X>% · status <X>
  expected   <expected_effect>
  observed   <measured_pnl_delta or "(not yet 7d post-ship)">
  verdict    <WORKED / DIDN'T / PARTIAL / TOO_EARLY>
  lesson     <invariant or "TBD — needs n more days">
  parent     <parent ID or "root">

If history empty: "Fresh tracker. Baseline established this review — first verdicts arrive at <date + 7d>. Until then I'm flying without calibration data; treat my confidences as approximate."

🎲 Cumulative batting avg (so far)
   N% confidence bucket: X/Y hit (Z%)
   ... or "no completed hypotheses yet"

────────────────────────────────────────────────────────────
[MSG 5] ISSUE #1 — deepest bug/bias
────────────────────────────────────────────────────────────
🚨 ISSUE #1 · <title>
━━━━━━━━━━━━━━━━━━━━━━━━━━

💰 Magnitude
   <$X>/7d  (XX% of weekly bleed)
   Annualized run-rate: <$X>
   on $200 paper cap = <X×> drawdown.

🔬 Mechanism (causal chain, 3+ steps)
   ① <step 1>
   ② <step 2>
   ③ <step 3 — actual cause>

📊 Statistical evidence
   n=N, observed wr X% (vs expected Y%)
   Under H₀: P(X ≤ k | p, n) = <binomial p>
   = <interpretation: noise / 2-sigma / 3-sigma signal>

   Subgroup analysis (if relevant):
   ├ <segment 1>: ...
   ├ <segment 2>: ...
   └ <segment 3>: ...

📁 Code locus
   <file:line>  <symbol>
   <file:line>  <related symbol>

⚠️ Why earlier reviews missed this
   <1-2 lines: power analysis at the time of prior review>
   Lesson for the tracker: <invariant>

🎲 Confidence: X% (calibrated)

────────────────────────────────────────────────────────────
[MSG 6] ISSUE #2 — same format
────────────────────────────────────────────────────────────

────────────────────────────────────────────────────────────
[MSG 7] ISSUE #3 — same format
────────────────────────────────────────────────────────────

────────────────────────────────────────────────────────────
[MSG 8] RECOMMENDATION R1 ★ — full proposal
────────────────────────────────────────────────────────────
🛠️ R1 · <title>  ★
━━━━━━━━━━━━━━━━━━━━━━━━━━

📁 File: <path>:<line>

📝 Proposed diff
──────────────────────────
```python
- <old code>
+ <new code with comment dating + reasoning>
```
──────────────────────────

🎯 Expected effect (with derivation)
   <math line>
   <math line>
   ─────────
   NET <+$X>/7d
   50% CI: [<low>, <high>]
   30-day forward projection: <$X>

💀 Pre-mortem — 3+ failure modes
   ① <failure mode 1>: <why + likelihood + cost>
   ② <failure mode 2>: ...
   ③ <failure mode 3>: ...
   ④ (optional)

🛡️ Risk decomposition
   Reversibility    : <description>
   Data risk        : <description>
   Live order risk  : <description>
   Test coverage    : <files / count>
   Net risk         : LOW / MED / HIGH

🎲 Confidence: X% (calibrated)
   raw Y%, ΔZpp shrinkage from <bucket> historical hit rate.

⏱️ Ship cost: <minutes> · <description>

⤷ Reply YES_1 to ship · NO_1 reject · MODIFY_1 "<change>"

────────────────────────────────────────────────────────────
[MSG 9] RECOMMENDATION R2 — same format, reply key YES_2
────────────────────────────────────────────────────────────

────────────────────────────────────────────────────────────
[MSG 10] RECOMMENDATION R3 — same format, reply key YES_3
────────────────────────────────────────────────────────────

────────────────────────────────────────────────────────────
[MSG 11] COUNTERFACTUAL ANALYSIS — math-backed what-ifs
────────────────────────────────────────────────────────────
🔄 COUNTERFACTUAL ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario A: if we had NOT shipped <prior change>
   Realized 7d              : <$X>
   Estimated under no-ship  : <$Y>
   Δ = <$Z>  → ship was net <+/-$Z>

Scenario B: if R1 ships today and runs 7d
   Backward replay on last 7d trades affected:
   <math>
   Estimated improvement    : <$X>

Scenario C: if all 3 recs ship simultaneously
   Compound effect          : <$X>
   Interaction warnings     : <cross-rec failure modes>

🌑 Shadow strategy state
   Live params today vs "all-recs-shipped" world:
   <param>: <current> → <proposed>
   <param>: <current> → <proposed>
   ...
   Equity in shadow @ today: <$X> (vs realized <$Y>)

────────────────────────────────────────────────────────────
[MSG 12] NEW HYPOTHESIS — well-formed, with EV calc
────────────────────────────────────────────────────────────
💡 NEW UNTRIED HYPOTHESIS
━━━━━━━━━━━━━━━━━━━━━━━━━━

🆕 ID:  U_<YYYY_MM_DD>_<n>

Idea
   <2-3 lines: precise statement>

Rationale
   <2-3 lines: why this would work, what data motivates it>

EV calculation
   estimated_impact_usd    : <$X>
   estimated_p_correct     : <X.XX>
   estimated_effort_hr     : <X>
   expected_value          : <$X>  = impact × p_correct
   $/hr                    : <$X>  = EV / effort

Promotion criterion
   <when to promote untried → active>

Adversarial: what would change my mind
   <1 line: "If <observation>, idea is dead">

────────────────────────────────────────────────────────────
[MSG 13] ANALYST'S VIEW — strategic interpretation, 3-week outlook, calibration self-audit
────────────────────────────────────────────────────────────
🧠 ANALYST'S VIEW
━━━━━━━━━━━━━━━━━━━━━━━━━━

📉 What I'm actually seeing
   <3-5 lines: read of regime, distribution shape, asymmetry,
   what feels off vs what's noise. Your VOICE, not data recap.>

🔍 Patterns no single section captured
   <2-3 cross-cutting observations>

🌑 Shadow strategy after R1+R2+R3 ship
   <bulleted state of params + expected daily/wk run-rate>

🎯 3-week strategic outlook
   Week +1: <forecast / focus>
   Week +2: <forecast / focus>
   Week +3: <Phase 3 readiness gate criteria>

🎲 Calibration self-audit
   Of my past <X>%-confident hypotheses, <Y>/<Z> worked
   → my <X>% truly means <Y>%.
   Adjustment applied to today's confidences: <±X pp>.
   <if no history: "First-cycle review — calibration accumulates from this point. Trust today's numbers loosely.">

💡 If I had unlimited budget
   <1 experiment that would meaningfully cut uncertainty
   but is currently blocked on cost/time/data>

────────────────────────────────────────────────────────────
[MSG 14] RED FLAGS + SYSTEM HEALTH + REPLY GUIDE
────────────────────────────────────────────────────────────
🚨 RED FLAGS  (or "none")
━━━━━━━━━━━━━━━━━━━━━━━━━━

🔴 Critical (act immediately)
   <list or "none">

🟡 Watch (act within 7d)
   <list or "none">

🟢 System health
   • DB integrity      : <PASS/FAIL>
   • Cron freshness    : <last autotrade tick>
   • Open book hygiene : <walking-dead count, age>
   • Calibration drift : <obs vs expected wr 14d>

──────────────────────────
🔁 REPLY GUIDE
   YES_1 / YES_2 / YES_3   ship recommendation
   NO_<n>                  reject
   MODIFY_<n> "<change>"   amend then ship
   PARK_U_<id>             demote new hypothesis to untried-only
   PROMOTE_U_<id>          promote untried → active hypothesis

═══════════════════════════════════════════════════════════════
DATA
═══════════════════════════════════════════════════════════════

PERFORMANCE SUMMARY
  Lifetime    : ${fmt_signed(lt.get('net') or 0)}  (n={lt.get('n')} settled, {lt.get('open')} open)
  Last 7d     : ${fmt_signed(w7.get('net') or 0)}  (n={w7.get('n')})
  Last 14d    : ${fmt_signed(w14.get('net') or 0)}  (n={w14.get('n')})
  Open book   : {ob['count']} positions · ${ob['notional']:.2f} notl · unrealized=${fmt_signed(ob['unrealized_pnl'])}
  TRUE total  : ${fmt_signed((lt.get('net') or 0) + ob['unrealized_pnl'])}

DAILY P&L (14d, oldest → newest):
{daily}

PER-CATEGORY × OUTCOME (7d):
{cat_lines}

PER-CITY (7d) — pred=avg model_prob, real=realized winrate:
{city_lines}

EXIT REASON DISTRIBUTION (7d):
{exit_lines}

────────────────────────────────────────────
HYPOTHESIS HISTORY (last 30d)
────────────────────────────────────────────
{hyp_str}

────────────────────────────────────────────
LESSONS LEARNED (cumulative, non-contradicted)
────────────────────────────────────────────
{lesson_str}

────────────────────────────────────────────
UNTRIED HYPOTHESES (priority queue, top 5 by EV)
────────────────────────────────────────────
{untried_str}

────────────────────────────────────────────
CURRENT STRATEGY CONFIG
────────────────────────────────────────────
edge_floor_pp        : {snap.get('edge_floor_pp')}
no_ask_band          : [{snap.get('no_ask_min')}, {snap.get('no_ask_max')}]
wide_bracket         : reject if width>{snap.get('wide_bracket_f')}°F AND no_ask<{snap.get('wide_bracket_min_no_ask')}
flip_threshold       : no_ask >= {snap.get('flip_threshold')} → buy YES
city_bias_enabled    : {snap.get('city_bias_enabled')}
city_bias dict (sorted by bias):
{bias_str}

────────────────────────────────────────────
RECENT COMMITS (7d)
────────────────────────────────────────────
{commits}

────────────────────────────────────────────
CODE EXCERPTS (read these before recommending changes)
────────────────────────────────────────────
{code_str[:8000]}

═══════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════
Output ONLY the 14 messages separated by ---MSG--- on its own line.
- No preamble, no postamble.
- No markdown bold (*text*) or italic (_text_) — Telegram parse_mode is "" so it renders literal asterisks. ONLY exception: ```python code fences inside R<n> diff blocks.
- Korean/English mix OK — professional, mathematical, no casual.
- Be ruthless. Treat this as if real capital is already deployed; the trader will print this for the desk drawer.
- Visual hierarchy is binding (emoji semantic anchors, ━ dividers, └├│ sub-bullets, monospace tables with space alignment). Use whitespace for punch.
- DEPTH > BREVITY. 1500-2500 chars per message is the floor; do not compress for brevity. Show your work."""


# ============================================================================
# Anthropic API call (Opus for deep review)
# ============================================================================

def call_claude(prompt: str, model: str = "claude-opus-4-6") -> tuple[str, dict]:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        # 16000 output tokens ≈ 22-24K chars across 14 messages.
        # Empirical: first run at 8000 truncated mid-R1 (pre-mortem failure mode #2).
        # Cost ceiling: 10K in × $15/M + 16K out × $75/M = ~$1.35 max per run.
        # × 2 runs/week ≈ $11/month for weekly reviews.
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    usage = {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "model": model,
    }
    return text, usage


def send_telegram(text: str) -> bool:
    """Multi-message Telegram send. Splits on the ---MSG--- delimiter so each
    section lands as its own bubble. Tolerates extra whitespace / dashes.
    parse_mode="" avoids HTML escape collisions with $ / % / `<` chars.
    """
    import re
    import time
    try:
        sys.path.insert(0, str(ROOT))
        from polymarket_strat.notifications.telegram import TelegramNotifier
        from polymarket_strat.config import TelegramConfig
        cfg = TelegramConfig.from_env()
        notifier = TelegramNotifier(cfg)

        parts = re.split(r"\s*-{3,}\s*MSG\s*-{3,}\s*", text)
        parts = [p.strip() for p in parts if p and p.strip()]
        if not parts:
            return False

        for part in parts:
            for j in range(0, len(part), 3800):
                notifier.send_message(part[j:j+3800], parse_mode="")
                time.sleep(0.4)
        return True
    except Exception as e:
        print(f"[strategy_review] Telegram send failed: {e}", file=sys.stderr)
        return False


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    load_env()

    if "ANTHROPIC_API_KEY" not in os.environ:
        send_telegram("⚠️ Strategy Review skipped: ANTHROPIC_API_KEY missing.")
        return 1

    try:
        data = collect_data()
    except Exception as e:
        send_telegram(f"⚠️ Strategy Review data collection failed: {e}")
        return 1

    prompt = build_prompt(data)

    # Save prompt for debug
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    (LOG_DIR / f"strategy_review_prompt_{ts}.txt").write_text(prompt)

    try:
        response, usage = call_claude(prompt)
    except Exception as e:
        send_telegram(f"⚠️ Strategy Review API failed: {type(e).__name__}: {e}")
        return 1

    # Save full report
    (REPORT_DIR / f"review_{ts}.md").write_text(
        f"# Strategy Review {ts}\n\n"
        f"Tokens: {usage['input_tokens']} in / {usage['output_tokens']} out\n\n"
        f"## PROMPT\n\n```\n{prompt}\n```\n\n## RESPONSE\n\n{response}"
    )

    # Parse Claude's structured output into the hypothesis tracker DB.
    # This closes the J-curve loop — next review's prompt sees its own past
    # proposals via the DATA section. Failures here are non-fatal: we still
    # ship to Telegram so the user sees the review even if the parser missed
    # something, and the response file at REPORT_DIR is the source of truth
    # for manual replay via `python scripts/parse_review_to_db.py <path>`.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from parse_review_to_db import parse_and_persist
        parse_result = parse_and_persist(response)
        print(f"[strategy_review] DB writeback: {parse_result}")
    except Exception as e:
        print(f"[strategy_review] parse_and_persist failed: {e}", file=sys.stderr)
        parse_result = {"error": str(e)}

    # Telegram — header / Claude's multi-part response / footer, each as its own bubble.
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M KST")
    cost = (usage["input_tokens"] * 15 + usage["output_tokens"] * 75) / 1_000_000
    header = f"📈 STRATEGY REVIEW · {now_kst} · {usage.get('model', 'opus')}"
    footer = (f"— {usage['input_tokens']} in / {usage['output_tokens']} out · "
              f"~${cost:.4f}\n"
              f"Reply: YES_<n> ship · NO_<n> reject · MODIFY_<n> \"<change>\" amend")

    full_message = f"{header}\n\n---MSG---\n\n{response}\n\n---MSG---\n\n{footer}"
    sent = send_telegram(full_message)

    log_path = LOG_DIR / "strategy_review.log"
    with log_path.open("a") as f:
        f.write(f"\n[{datetime.now(timezone.utc).isoformat()}] "
                f"input={usage['input_tokens']} output={usage['output_tokens']} "
                f"cost=${cost:.4f} sent={sent}\n")

    print("Strategy review generated.")
    print("Tokens:", usage)
    print("Telegram sent:", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
