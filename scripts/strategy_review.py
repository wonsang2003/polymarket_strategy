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

    return f"""You are the lead quantitative strategist for the Polymarket weather alpha system.
This is the bi-weekly deep review (Wednesday or Sunday 09:00 KST).

═══════════════════════════════════════════════════════════════
1. PERFORMANCE SUMMARY
═══════════════════════════════════════════════════════════════
Lifetime    : ${fmt_signed(lt.get('net') or 0)} ({lt.get('n')} settled, {lt.get('open')} open)
Last 7d     : ${fmt_signed(w7.get('net') or 0)} ({w7.get('n')} events)
Last 14d    : ${fmt_signed(w14.get('net') or 0)} ({w14.get('n')} events)
Open book   : {ob['count']} positions, ${ob['notional']:.2f} notional, unrealized=${fmt_signed(ob['unrealized_pnl'])}
TRUE total  : ${fmt_signed((lt.get('net') or 0) + ob['unrealized_pnl'])}

DAILY P&L (last 14d):
{daily}

═══════════════════════════════════════════════════════════════
2. PER-CATEGORY × OUTCOME (last 7d)
═══════════════════════════════════════════════════════════════
{cat_lines}

═══════════════════════════════════════════════════════════════
3. PER-CITY (last 7d, with predicted vs realized win rate)
═══════════════════════════════════════════════════════════════
{city_lines}

═══════════════════════════════════════════════════════════════
4. EXIT REASON DISTRIBUTION (last 7d)
═══════════════════════════════════════════════════════════════
{exit_lines}

═══════════════════════════════════════════════════════════════
5. HYPOTHESIS HISTORY (last 30 days)
═══════════════════════════════════════════════════════════════
{hyp_str}

═══════════════════════════════════════════════════════════════
6. LESSONS LEARNED (cumulative wisdom)
═══════════════════════════════════════════════════════════════
{lesson_str}

═══════════════════════════════════════════════════════════════
7. UNTRIED HYPOTHESES (priority queue)
═══════════════════════════════════════════════════════════════
{untried_str}

═══════════════════════════════════════════════════════════════
8. CURRENT STRATEGY CONFIG
═══════════════════════════════════════════════════════════════
edge_floor_pp        : {snap.get('edge_floor_pp')}
no_ask_band          : [{snap.get('no_ask_min')}, {snap.get('no_ask_max')}]
wide_bracket         : reject if width>{snap.get('wide_bracket_f')}°F AND no_ask<{snap.get('wide_bracket_min_no_ask')}
flip_threshold       : no_ask >= {snap.get('flip_threshold')} → buy YES
city_bias_enabled    : {snap.get('city_bias_enabled')}
city_bias dict:
{bias_str}

═══════════════════════════════════════════════════════════════
9. RECENT COMMITS (last 7 days)
═══════════════════════════════════════════════════════════════
{commits}

═══════════════════════════════════════════════════════════════
10. CODE EXCERPTS
═══════════════════════════════════════════════════════════════
{code_str[:8000]}

═══════════════════════════════════════════════════════════════
TASKS
═══════════════════════════════════════════════════════════════

Output a structured weekly review in this exact format:

A. VERDICT ON PRIOR HYPOTHESES (≤200 words)
   For each hypothesis from section #5, classify: WORKED / DIDN'T / PARTIAL / TOO_EARLY.
   For WORKED: extract the lesson — what's now invariant.
   For DIDN'T: extract why — what was the failure mode.

B. TOP 3 ISSUES with current strategy (≤300 words)
   Each issue must reference: (a) which section above motivates it, (b) magnitude in $, (c) confidence.

C. PER-CITY STATUS UPDATE (≤200 words)
   For each city in section #3 with significant change vs prior expectations.

D. RECOMMENDED CHANGES (priority order, ≤500 words)
   Format each as:
     N. ★/□  TITLE
        File: <path>:<line>
        Change: <concrete diff or new constant value>
        Risk: low/medium/high
        Confidence: 0-100%
        Expected effect: $/wk impact
        Motivated by: section # above
        Pre-mortem: "If this fails, why?"

E. ONE COUNTERFACTUAL (≤100 words)
   "If we had [reverted X / not shipped Y / kept old version], 7d P&L would have been..."

F. ONE NEW UNTRIED HYPOTHESIS (≤100 words)
   Add to register with rationale.

G. CONFIDENCE CALIBRATION SELF-CHECK (≤100 words)
   Of past hypotheses with X% confidence, what fraction succeeded?
   Adjust your confidence here accordingly.

H. RED FLAGS (≤50 words or "none")
   Anything genuinely concerning.

Constraints:
- Total ≤2500 words
- Korean or mixed Korean/English OK
- Plain text (Telegram doesn't render markdown well)
- Be ruthless — no hedging
- Reference specific trade IDs, $ amounts, file:line
- For each recommendation, the user will reply YES_<num> to ship

Output the review only, no preamble."""


# ============================================================================
# Anthropic API call (Opus for deep review)
# ============================================================================

def call_claude(prompt: str, model: str = "claude-opus-4-6") -> tuple[str, dict]:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
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
    try:
        sys.path.insert(0, str(ROOT))
        from polymarket_strat.notifications.telegram import TelegramNotifier
        from polymarket_strat.config import TelegramConfig
        cfg = TelegramConfig.from_env()
        notifier = TelegramNotifier(cfg)
        chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
        for chunk in chunks:
            notifier.send_message(chunk, parse_mode="")
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

    # Telegram message (with header + cost footer)
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M KST")
    cost = (usage["input_tokens"] * 15 + usage["output_tokens"] * 75) / 1_000_000
    header = f"📈 Weekly Review — {now_kst}\n\n"
    footer = (f"\n\n— tokens: {usage['input_tokens']} in / "
              f"{usage['output_tokens']} out, ~${cost:.4f}\n"
              f"Reply YES_<num> to ship recommendation, NO_<num> to reject.")

    sent = send_telegram(header + response + footer)

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
