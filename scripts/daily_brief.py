"""Daily Brief — Claude analyzes yesterday's trading activity.

Runs daily at 06:00 KST via cron. Aggregates last 24h DB stats, current
strategy snapshot, recent change history, then asks Claude to:
  1. Summarize what happened (factual)
  2. Identify one bias/pattern showing up
  3. Recommend one action (or "no change needed")

Posts result to Telegram.

Cost: ~$0.05/day with Claude Sonnet 4.5.
Robustness: catches API errors, falls back to "API down" Telegram message.

Env: ANTHROPIC_API_KEY (required), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")
ROOT = Path("/home/ubuntu/polymarket")
LOG_DIR = Path("/home/ubuntu/polymarket/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Env loading
# ============================================================================

def load_env() -> None:
    """Load .env into os.environ (manual since we don't depend on python-dotenv)."""
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
# Data collection
# ============================================================================

def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def collect_data() -> dict:
    """Aggregate everything the daily brief needs into a structured dict."""
    c = conn()
    now = datetime.now(timezone.utc)
    today_kst_str = (now + timedelta(hours=9)).strftime("%Y-%m-%d")
    yesterday_kst_str = (now + timedelta(hours=9, days=-1)).strftime("%Y-%m-%d")

    out: dict = {"now_utc": now.isoformat(), "yesterday_kst": yesterday_kst_str}

    # Lifetime
    r = c.execute("""
        SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history
    """).fetchone()
    out["lifetime"] = dict(r)

    # Yesterday (KST)
    r = c.execute(
        "SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE settled_at LIKE ? || '%'",
        (yesterday_kst_str,),
    ).fetchone()
    out["yesterday"] = dict(r)

    # Today so far
    r = c.execute(
        "SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n "
        "FROM trade_history WHERE settled_at LIKE ? || '%'",
        (today_kst_str,),
    ).fetchone()
    out["today_so_far"] = dict(r)

    # Last 7d daily breakdown for context
    daily = []
    for d in range(7, 0, -1):
        day_kst = (now + timedelta(hours=9, days=-d)).strftime("%Y-%m-%d")
        r = c.execute(
            "SELECT ROUND(SUM(pnl), 2) AS net, COUNT(*) AS n "
            "FROM trade_history WHERE settled_at LIKE ? || '%'",
            (day_kst,),
        ).fetchone()
        daily.append({"date": day_kst, "net": r["net"], "n": r["n"]})
    out["last_7d_daily"] = daily

    # Yesterday by category × outcome
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat,
               CASE WHEN outcome=2 THEN 'rebal'
                    WHEN pnl>0 THEN 'WIN' ELSE 'LOSS' END AS r,
               COUNT(*) AS n,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(pnl), 2) AS avg
        FROM trade_history
        WHERE settled_at LIKE ? || '%'
        GROUP BY cat, r
        ORDER BY cat, r
    """, (yesterday_kst_str,)).fetchall()
    out["yesterday_by_cat"] = [dict(x) for x in rows]

    # Yesterday by city
    rows = c.execute("""
        SELECT city, COUNT(*) AS n,
               SUM(CASE WHEN outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
               SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
               ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE settled_at LIKE ? || '%'
        GROUP BY city
        ORDER BY net
    """, (yesterday_kst_str,)).fetchall()
    out["yesterday_by_city"] = [dict(x) for x in rows]

    # Yesterday by exit_reason
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
        WHERE settled_at LIKE ? || '%'
        GROUP BY exit_type
        ORDER BY net
    """, (yesterday_kst_str,)).fetchall()
    out["yesterday_by_exit_reason"] = [dict(x) for x in rows]

    # Open book current state
    rows = c.execute("""
        SELECT t.id, t.city, t.target_date, COALESCE(t.category, '<null>') AS cat,
               t.token_side, ROUND(t.entry_price, 3) AS entry,
               t.notional, mp.best_bid AS bid
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
    walking_dead = 0
    cruising = 0
    for r in rows:
        e = r["entry"] or 0
        b = r["bid"]
        n = r["notional"] or 0
        if e > 0 and b is not None:
            shares = n / e
            gross = shares * (b - e)
            fee = 0.02 * gross if gross > 0 else 0
            upnl = gross - fee
            open_upnl += upnl
            if b < 0.10:
                walking_dead += 1
            if upnl > 1:
                cruising += 1
    out["open_book"] = {
        "count": open_count,
        "notional": round(open_notl, 2),
        "unrealized_pnl": round(open_upnl, 2),
        "walking_dead": walking_dead,
        "cruising": cruising,
    }

    # Recent commits (strategy changes context)
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--oneline", "--since=2 days ago"],
            capture_output=True, text=True, timeout=5,
        )
        out["recent_commits"] = result.stdout.strip().splitlines()[:10]
    except Exception:
        out["recent_commits"] = []

    # Current strategy constants (key values from city_calibration + tail_no_strategy)
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
            "flip_threshold": tns.FLIP_NO_TO_YES_THRESHOLD,
            "city_bias_count": len(cc.CITY_NO_BIAS),
            "city_bias_enabled": cc.ENABLED,
            "sample_corrections": {
                k: cc.CITY_NO_BIAS.get(k, 0.0)
                for k in ["london", "tokyo", "munich", "seoul", "hong_kong", "la"]
            },
        }
    except Exception as e:
        out["strategy_snapshot"] = {"error": str(e)}

    c.close()
    return out


# ============================================================================
# Prompt construction
# ============================================================================

def build_prompt(data: dict) -> str:
    """Compose the daily brief prompt."""
    lt = data["lifetime"]
    y = data["yesterday"]
    today = data["today_so_far"]
    ob = data["open_book"]
    snap = data["strategy_snapshot"]

    # Daily 7d trend
    daily_trend = "\n".join(
        f"  {d['date']}: ${d['net'] or 0:+.2f} ({d['n']} events)"
        for d in data["last_7d_daily"]
    )

    # By category
    cat_lines = "\n".join(
        f"  {x['cat']:<26} {x['r']:<6} n={x['n']:>3}  net=${x['net'] or 0:+.2f}"
        for x in data["yesterday_by_cat"]
    ) or "  (no settled events yesterday)"

    # By city
    city_lines = "\n".join(
        f"  {x['city']:<14} n={x['n']:>3} W{x['w']:>2}/L{x['l']:>2}/R{x['rbl']:>2} "
        f"net=${x['net'] or 0:+.2f}"
        for x in data["yesterday_by_city"]
    ) or "  (no city activity)"

    # By exit reason
    exit_lines = "\n".join(
        f"  {x['exit_type']:<26} n={x['n']:>3} "
        f"net=${x['net'] or 0:+.2f} avg=${x['avg'] or 0:+.2f}"
        for x in data["yesterday_by_exit_reason"]
    ) or "  (no exits)"

    bias_summary = ", ".join(
        f"{k}={v:+.2f}" for k, v in snap.get("sample_corrections", {}).items()
    )

    commits = "\n".join(f"  {c}" for c in (data["recent_commits"] or ["  (no recent commits)"]))

    return f"""You are a senior quantitative analyst at a top-tier hedge fund — Harvard MBA, ex-Two Sigma. You write the daily brief for the trader running this Polymarket weather alpha. He reads it on his phone in 30 seconds. Every word must earn its place.

═══════════════════════════════════════════════════════════════
PHILOSOPHY (binding rules)
═══════════════════════════════════════════════════════════════
1. BLUF — the FIRST line of every message is the conclusion. Evidence follows.
2. Quantify or don't claim. Every assertion carries $, %, σ, or n. No "성과가 좋았다" without dollars.
3. Distinguish skill from luck. With n < 20, flag the noise band: "n=12, ±2σ band ≈ ±$X — inside noise". Don't manufacture patterns from thin data.
4. Mechanism > outcome. Don't say "London made money" — say "London +$4.10 because fog stickiness held NO bid 0.18 → 0.62 over 6h."
5. No hedging when data is clear. Explicit hedging when it isn't. No fake humility.
6. Korean / English mix OK — professional, not casual. Sentences should be tight.
7. Each message stands alone. No "이제 다음으로" transitions.

═══════════════════════════════════════════════════════════════
VISUAL HIERARCHY (binding — phone-readable on first glance)
═══════════════════════════════════════════════════════════════
Plain-text only (no markdown bold/italic — Telegram parse_mode is "" so * and _ render literally).

Use Unicode anchors:
  📊 = scorecard / data        🎯 = attribution / target
  ⚠️ = warning / pattern        📌 = action / decision
  🧠 = analyst's interpretation 🔴 = negative number
  🟢 = positive number          🟡 = neutral / hold
  🚨 = signal (n × p-value pop)  💀 = biggest loss source
  📁 = file ref (path:line)    🔁 = trigger / loop condition
  ⮡ = follow-up / consequence   ◀ ▲ ▼ → = directional

Section divider:  ━━━━━━━━━━━━━━━━━━━━  (20 dashes)
Sub-bullet structure under a parent line:
  ├ 1st sub-item
  ├ 2nd sub-item
  └ last sub-item
Indent body content 3 spaces under its emoji-anchored header.

Numbers: use $X.XX format. Right-align inside monospace blocks. Use spaces, not tabs.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT — 5 SEPARATE TELEGRAM MESSAGES
═══════════════════════════════════════════════════════════════
Output EXACTLY 5 messages, separated by this delimiter on its own line:

---MSG---

Each ≤ 800 chars. Required order (do NOT change):

────────────────────────────────────────────────────────────
[MSG 1] HEADLINE — pure numbers, zero commentary
────────────────────────────────────────────────────────────
Format (use this exact skeleton, fill in numbers):

📊 DAILY · {data['yesterday_kst']} KST
━━━━━━━━━━━━━━━━━━━━

🔴 −$X.XX   24h realized
   N trades │ Wn Ln Rn
   win rate XX%

📈 7d 트렌드 (Nw Nl)
   {{date}}  {{$X}}
   {{date}}  {{$X}}
   ... 7 lines, mark today with ◀

📦 Book
   N open · $XXX notl
   Upnl: −$X (cruising N)

────────────────────────────────────────────────────────────
[MSG 2] ATTRIBUTION — where money came from / went, with MECHANISM
────────────────────────────────────────────────────────────
Required structure (3 blocks):

🎯 ATTRIBUTION
━━━━━━━━━━━━━━━━━━━━

💀 도시 #1   {{city}}  −$X
   ├ N× settle loss  −$X
   ├ N× cat_flip     −$X
   └ city_bias = X.XX (보호 X / 효과)

   ⮡ <one line: cluster context, e.g.
      "아시아 클러스터가 −$X 중 절반">

💀 카테고리 #1   {{category}}
   N trades · NW / NL · −$X

   ⮡ <one line: mechanism hypothesis, e.g.
      "5/5 전패. NO 플립 fade">

🟢 살린 곳
   {{cat}}    +$X (NW)
   {{city}}   +$X (이유)

────────────────────────────────────────────────────────────
[MSG 3] PATTERN WATCH — one signal, n + noise band MANDATORY
────────────────────────────────────────────────────────────
⚠️ PATTERN WATCH
━━━━━━━━━━━━━━━━━━━━

🚨 <pattern title>

   기대 wr ~XX% 였다면
   {{result}}/{{n}} 확률 ≈ X%  (binomial)
   → signal / not luck.

🔬 가설
   <2-3 lines: mechanism>

📁 <file>:<line> ({{symbol}})
   confidence  XX%
   sample      n=X (sufficient / 부족)
   verdict     ship / 보류 · n=N 까지

🟡 (optional) <other observations inside noise>
   <1-line: "n=X, 2σ band ±$X → noise. Action: 없음">

If n < 20 on every candidate pattern, write
   "🟡 No tradeable pattern emerged.
    Largest deviation: <X>, n=N, inside ±2σ band ±$Y."
and stop. Do NOT manufacture signal.

────────────────────────────────────────────────────────────
[MSG 4] CALL — one decision, BLUF first line
────────────────────────────────────────────────────────────
📌 CALL · <HOLD / SHIP R<n> / DISABLE X>
━━━━━━━━━━━━━━━━━━━━

⚖ <one-line decision summary>

근거 N개
   1. <numeric reason>
   2. <numeric reason>
   3. <numeric reason>

🔁 트리거 (이거 깨지면 변경)
   • <metric> <threshold> → <action>
   • <metric> <threshold> → <action>
   • <metric> <threshold> → <action>

────────────────────────────────────────────────────────────
[MSG 5] ANALYST'S VIEW — Claude's interpretation layer
────────────────────────────────────────────────────────────
This is your VOICE — what does the senior analyst think AFTER reading the data?

🧠 ANALYST'S VIEW
━━━━━━━━━━━━━━━━━━━━

📉 <2-3 lines: your read of the situation. Where the
   distribution shape sits vs lifetime variance.
   Skew / asymmetry signals.>

🎯 의심 우선순위 (확신 순)
   ① <hypothesis>
      <1-line evidence + mechanism>
   ② <hypothesis>
      <1-line evidence + mechanism>
   ③ <hypothesis>
      <1-line evidence + mechanism>

💡 내가 너라면
   • <action 1: data collection / wait / etc.>
   • <action 2: when to revisit, what to ship>
   • <action 3: phase / capital decision>

🕐 다음 brief 까지 watch
   ├ <metric 1 to monitor>
   ├ <metric 2 to monitor>
   └ <metric 3 to monitor>

═══════════════════════════════════════════════════════════════
DATA (yesterday = {data['yesterday_kst']} KST)
═══════════════════════════════════════════════════════════════

LIFETIME
  Realized      : ${lt.get('net') or 0:+.2f}  (n={lt.get('n')} settled, {lt.get('open')} open)
  Open notl     : ${ob['notional']:.2f}
  Open unreal   : ${ob['unrealized_pnl']:+.2f}
  TRUE total    : ${(lt.get('net') or 0) + ob['unrealized_pnl']:+.2f}

YESTERDAY ({data['yesterday_kst']} KST)
  Realized      : ${y.get('net') or 0:+.2f}  (n={y.get('n')})
  Today so far  : ${today.get('net') or 0:+.2f}  (n={today.get('n')})

7d daily P&L:
{daily_trend}

YESTERDAY × CATEGORY × OUTCOME:
{cat_lines}

YESTERDAY × CITY (W=settle win, L=settle loss, R=rebal exit):
{city_lines}

YESTERDAY × EXIT REASON:
{exit_lines}

OPEN BOOK
  count={ob['count']}  cruising(upnl>$1)={ob['cruising']}  walking-dead(bid<0.10)={ob['walking_dead']}

CURRENT STRATEGY (key constants)
  edge_floor_pp     : {snap.get('edge_floor_pp')}
  no_ask band       : [{snap.get('no_ask_min')}, {snap.get('no_ask_max')}]
  wide_bracket_F    : {snap.get('wide_bracket_f')}
  flip_threshold    : {snap.get('flip_threshold')}
  city_bias_enabled : {snap.get('city_bias_enabled')} ({snap.get('city_bias_count')} cities)
  Sample biases     : {bias_summary}

RECENT COMMITS (last 2 days)
{commits}

═══════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════
Output ONLY the 5 messages separated by ---MSG--- on its own line.
- No preamble, no postamble.
- No markdown bold (*text*) or italic (_text_) — Telegram parse_mode is "" so it renders literal asterisks.
- No code fences except inside [MSG 5] if quoting a code line.
- Use emoji anchors and ━ dividers as visual punch (NOT decoration — every emoji must have semantic meaning per the legend above).
- Tight, professional. The trader will glance at this for 30 seconds — make every char earn space."""


# ============================================================================
# Anthropic API call
# ============================================================================

def call_claude(prompt: str, model: str = "claude-sonnet-4-5") -> tuple[str, dict]:
    """Call Anthropic API. Returns (text, usage_stats)."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=2500,  # 5 messages × ~800 chars × ~0.7 tokens/char ≈ 2,800 tokens headroom
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    usage = {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "model": model,
    }
    return text, usage


# ============================================================================
# Telegram sender
# ============================================================================

def send_telegram(text: str) -> bool:
    """Send via existing telegram notifier as MULTIPLE messages.

    Splits on the ---MSG--- delimiter so each conceptual section lands as
    its own Telegram message. Each part is then size-chunked (Telegram
    hard limit ~4096) and sent with a small inter-message delay so the
    client renders them in order. parse_mode="" avoids HTML escape
    issues with $/% characters.

    Falls back to single-message send if the delimiter is absent.
    """
    import re
    import time
    try:
        sys.path.insert(0, str(ROOT))
        from polymarket_strat.notifications.telegram import TelegramNotifier
        from polymarket_strat.config import TelegramConfig
        cfg = TelegramConfig.from_env()
        notifier = TelegramNotifier(cfg)

        # Lenient split — tolerates surrounding whitespace / extra dashes.
        parts = re.split(r"\s*-{3,}\s*MSG\s*-{3,}\s*", text)
        parts = [p.strip() for p in parts if p and p.strip()]
        if not parts:
            return False

        for part in parts:
            for j in range(0, len(part), 3800):
                notifier.send_message(part[j:j+3800], parse_mode="")
                time.sleep(0.4)  # preserve client-side ordering
        return True
    except Exception as e:
        print(f"[daily_brief] Telegram send failed: {e}", file=sys.stderr)
        return False


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    load_env()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("[daily_brief] ANTHROPIC_API_KEY missing in .env", file=sys.stderr)
        send_telegram("⚠️ Daily Brief skipped: ANTHROPIC_API_KEY missing.")
        return 1

    try:
        data = collect_data()
    except Exception as e:
        print(f"[daily_brief] data collection failed: {e}", file=sys.stderr)
        send_telegram(f"⚠️ Daily Brief data collection failed: {e}")
        return 1

    prompt = build_prompt(data)

    # Save prompt for debugging / replay
    prompt_log = LOG_DIR / f"daily_brief_prompt_{datetime.now(timezone.utc):%Y%m%d}.txt"
    prompt_log.write_text(prompt)

    try:
        response, usage = call_claude(prompt)
    except Exception as e:
        msg = f"⚠️ Daily Brief API call failed: {type(e).__name__}: {e}"
        print(f"[daily_brief] {msg}", file=sys.stderr)
        send_telegram(msg)
        return 1

    # Build final telegram message — header + Claude's multi-part response + footer.
    # Each piece is its own ---MSG--- segment so it lands as a standalone Telegram bubble.
    now_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M KST")
    cost_est = (usage["input_tokens"] * 3 + usage["output_tokens"] * 15) / 1_000_000
    header = f"📊 DAILY BRIEF · {now_kst}"
    footer = (f"— {usage['input_tokens']} in / {usage['output_tokens']} out · "
              f"~${cost_est:.4f} · {usage.get('model', 'sonnet')}")

    full_message = f"{header}\n\n---MSG---\n\n{response}\n\n---MSG---\n\n{footer}"
    sent = send_telegram(full_message)

    # Log result. Full response also dumped to a per-day file for debugging.
    log_path = LOG_DIR / "daily_brief.log"
    with log_path.open("a") as f:
        f.write(f"\n[{datetime.now(timezone.utc).isoformat()}] "
                f"input={usage['input_tokens']} output={usage['output_tokens']} "
                f"cost~${cost_est:.4f} sent={sent}\n")
        f.write(response[:500] + "...\n")
    # Persist FULL response separately so we can audit format / depth.
    (LOG_DIR / f"daily_brief_response_{datetime.now(timezone.utc):%Y%m%d_%H%M}.txt").write_text(response)

    print("Daily brief generated. Tokens:", usage)
    print("Telegram sent:", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
