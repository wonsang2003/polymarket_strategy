---
name: polymarket-status
description: Pulls a one-shot live snapshot of the user's Polymarket weather alpha trading system (running on EC2 at 54.180.64.168). Shows open positions, today/yesterday/7d/lifetime P&L, walking-dead positions, recent rebal exits, recent settlements, pending strategy hypotheses awaiting user decision, recent evaluator verdicts, and Claude's cumulative calibration accuracy. Use this skill any time the user asks about their live trading state in any phrasing or language. Triggers include but are not limited to "how are my trades going", "check polymarket status", "trade status", "open positions 어때", "지금 트레이드 어떻게 되고 있어", "내 alpha 어때", "오늘 P&L 얼마", "walking-dead 있어", "cron 돌고 있어", "last autotrade 언제", "pending hypothesis 있어", "내가 결정할 거 있어", "ship 할 거 뭐 있어", "calibration 어때", "live 상태", "지금 어떻게 됐어", "포지션 상태", or any quick monitoring question about the live system. NOT for placing trades, NOT for running strategy_review, NOT for editing strategy code — read-only monitoring only.
---

# Polymarket Live Trading Status

Read-only monitoring skill that lets the user check the live state of their Polymarket weather alpha system from any Claude conversation — no need to return to the cowork session that built the system.

## Architecture

The user runs paper-mode autotrade on an EC2 host:
- **Host**: `54.180.64.168` (Seoul region)
- **User**: `ubuntu`
- **SSH key**: `~/.ssh/polymarket-seoul.pem` (on the user's Mac)
- **Working dir on EC2**: `/home/ubuntu/polymarket`
- **DB**: `/home/ubuntu/polymarket/data/weather/weather.db` (SQLite, WAL mode)

This skill fires a single bash command that calls a snapshot script already deployed on EC2. No DB locking risk — script opens with `mode=ro`.

## How to invoke (single bash command)

Run this from the Mac shell (Cowork mode required — this skill assumes Mac shell tool access):

```bash
ssh -i ~/.ssh/polymarket-seoul.pem -o ConnectTimeout=8 -o StrictHostKeyChecking=no \
    ubuntu@54.180.64.168 \
    'python3 /home/ubuntu/polymarket/scripts/status_snapshot.py'
```

Wrap in a 30-second timeout from the shell tool. The script itself is fast (< 1s on a healthy DB).

## Expected output

A monospace block looking like:

```
📊 POLYMARKET STATUS · 2026-05-09 23:09 KST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱ MTM snapshot   : 2min ago       ✓
⏱ Last trade evt : 9min ago       ✓

💰 P&L
   Today (KST)   : −$179.63        (n=24)
   Yesterday     : −$50.07         (n=42)
   7d            : −$967.30        (n=262)
   Lifetime      : −$2,418.20      (n=610)

📦 Open Book
   19 positions · $544 notional · upnl −$71.09
   ✓ Cruising (upnl > $1)   : 4

🚨 Recent rebals (last 5)
   #595  munich        catastrophic_flip      −$28.88
   ...

⚖ Last 5 settled
   🟢 #629  munich        WIN    +$25.04
   🔴 #613  wellington    LOSS   −$25.00
   ...

🔬 Pending hypotheses (6 awaiting your decision)
   H_2026_05_06_01    conf  50%  ...
   ...
   ⮡ ssh in → python scripts/ship_hypothesis.py <id>  to ship
   ⮡           --reject --reason "<why>"            to reject

📈 Verdicts: (none yet — first verdicts arrive 7d post-ship)

🎲 Calibration: (no completed hypotheses yet)
```

## How to present to the user

1. **Echo the snapshot block as-is** inside a triple-fenced code block. Use plain ` ``` ` (no language tag) so monospace alignment renders correctly on every Telegram / Claude client.
2. **Append a 1-line Korean summary** below the block flagging anything actionable. Direct, not ceremonial. Pattern:
   - Pending hypotheses → `"⚠️ {N}개 가설이 결정 기다림: {H_id_list}"` with IDs spelled out for copy-paste.
   - Walking-dead overflow (>5) → `"⚠️ walking-dead {N}개 — bid<0.10 누적, 검토 필요"`.
   - Stale cron (MTM > 30min) → `"🔴 MTM cron 정지 — autotrade 도 멈췄을 가능성. systemctl/cron 확인 필요"`.
   - Recent verdicts → `"📈 평가 결과 {N}개 — 다음 strategy_review 가 반영"`.
   - All clear → `"특이사항 없음. 7d {P&L 숫자}, 시스템 정상."`
3. **Do NOT interpret beyond the actionable summary**. The block speaks for itself; this skill is for monitoring, not analysis. For depth, point user to: *"strategy_review.py 다음 자동 실행 (수/일 09:00 KST), 또는 수동: ssh + python scripts/strategy_review.py."*

## Failure handling

- **SSH timeout (8s)** → respond: `"EC2 unreachable — Tailscale 또는 네트워크 확인 필요."`. Do NOT retry automatically.
- **Permission denied** → SSH key issue. Tell the user to verify `chmod 400 ~/.ssh/polymarket-seoul.pem`.
- **Script returns non-zero** → echo stderr verbatim. Suggest `tail -20 /home/ubuntu/polymarket/logs/autotrade.log` for additional diagnostics.
- **Empty output** → DB write contention (rare). Wait 5 seconds and retry once.
- **DB not found** → EC2 host may have lost its data dir. Tell user to check `ls -la /home/ubuntu/polymarket/data/weather/`.

## What this skill does NOT do

- Does NOT modify any data on EC2 (read-only).
- Does NOT ship/reject hypotheses (that's `ship_hypothesis.py`).
- Does NOT run strategy_review (that's the Wed/Sun cron).
- Does NOT interpret strategy logic — for analysis, point user to the next strategy_review or `daily_brief.py` log.
- Does NOT chain multiple snapshots — one call = one snapshot. If user wants a refresh, re-invoke.

## When to extend this skill

Edit `~/Downloads/polymarket_strat/scripts/status_snapshot.py` on the user's Mac, then SCP to EC2:

```bash
scp -i ~/.ssh/polymarket-seoul.pem \
    ~/Downloads/polymarket_strat/scripts/status_snapshot.py \
    ubuntu@54.180.64.168:/home/ubuntu/polymarket/scripts/
```

Common extensions:
- Per-city 24h breakdown
- "Next scheduled events" block (next daily_brief at 06:00 KST, next strategy_review on next Wed/Sun)
- Un-MTM'd open positions explicitly (bid is None)
- Recent untried_hypotheses register state

The skill MD itself does not need updating when the script changes — it just re-runs the script.

## Installation

To make this skill globally discoverable across all Cowork sessions, place this folder at:
- macOS Cowork user skills directory (varies by Cowork version)
- OR install via the skill-creator skill: `/skill-creator install polymarket-status`

If neither is available, keep this folder in the project and reference it manually:
1. Open Cowork in any new chat
2. Type: *"Use the polymarket-status skill at `~/Downloads/polymarket_strat/.claude-skill/polymarket-status/SKILL.md` to check my trades"* — Claude will read the file and follow its instructions.
