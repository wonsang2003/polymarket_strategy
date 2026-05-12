"""Generate comprehensive per-city strategy report.

For each city in trade_history:
  1. Headline metrics (lifetime + post-fix)
  2. Era breakdown (legacy null vs categorized strategies)
  3. Category × outcome breakdown
  4. Side analysis (NO vs YES)
  5. Calibration profile
  6. Edge-bucket × NO-ask-bucket performance
  7. Best / worst specific trades
  8. Flip experiment status
  9. CONCRETE strategy recommendation:
     - Tier classification (A/B/C/D/E)
     - Primary strategy choice
     - Flip eligibility + threshold
     - Edge floor adjustments
     - Position-size multiplier
     - Block list

Output: city_strategy_report.md saved to /home/ubuntu/polymarket/reports/
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")
OUT = Path("/home/ubuntu/polymarket/reports/city_strategy_report.md")
POST_FIX_CUTOFF = "2026-04-26"


def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def fmt(v, decimals=2, sign=False):
    if v is None:
        return "—"
    if sign:
        s = "+" if v >= 0 else "−"
        return f"{s}${abs(v):,.{decimals}f}"
    return f"${v:,.{decimals}f}"


def fmt_pct(v):
    if v is None:
        return "—"
    return f"{v*100:.1f}%"


def fmt_signed_pct(v):
    if v is None:
        return "—"
    return f"{v*100:+.1f}pp"


def percent_bar(value: float, max_value: float = 1.0, width: int = 20) -> str:
    """Simple ASCII bar chart."""
    if value is None:
        return " " * width
    frac = max(0.0, min(1.0, value / max_value)) if max_value > 0 else 0
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


# ============================================================
# DATA EXTRACTION — one comprehensive pull per city
# ============================================================

def get_city_profile(c, city):
    """Return dict with all metrics for a city."""
    p = {"city": city}

    # Lifetime totals
    r = c.execute("""
        SELECT
          COUNT(*) AS n,
          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open,
          SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS settled,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
          SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
          SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
          ROUND(SUM(pnl), 2) AS pnl,
          ROUND(SUM(notional), 2) AS notl,
          ROUND(SUM(CASE WHEN outcome IS NULL THEN notional ELSE 0 END), 2) AS open_notl
        FROM trade_history WHERE city=?
    """, (city,)).fetchone()
    p["lifetime"] = dict(r)

    # By era
    p["legacy"] = dict(c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl),2) AS pnl,
               SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
               SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl
        FROM trade_history
        WHERE city=? AND (category IS NULL OR category='')
    """, (city,)).fetchone())
    p["post_fix"] = dict(c.execute("""
        SELECT COUNT(*) AS n, ROUND(SUM(pnl),2) AS pnl,
               SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
               SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history
        WHERE city=? AND category IS NOT NULL AND category != ''
    """, (city,)).fetchone())

    # By category
    rows = c.execute("""
        SELECT COALESCE(category, '<null>') AS cat, COUNT(*) AS n,
               ROUND(SUM(pnl),2) AS pnl,
               SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl > 0 THEN 1 ELSE 0 END) AS w,
               SUM(CASE WHEN outcome IS NOT NULL AND outcome != 2 AND pnl <= 0 THEN 1 ELSE 0 END) AS l,
               SUM(CASE WHEN outcome = 2 THEN 1 ELSE 0 END) AS rbl,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open
        FROM trade_history
        WHERE city=?
        GROUP BY cat
        ORDER BY cat
    """, (city,)).fetchall()
    p["by_category"] = [dict(r) for r in rows]

    # Calibration: post-fix only, settled non-rebal
    r = c.execute("""
        SELECT COUNT(*) AS n,
               ROUND(AVG(model_prob), 3) AS avg_pred,
               ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS wr,
               ROUND(AVG(pnl), 2) AS avg_pnl,
               ROUND(SUM(pnl), 2) AS net,
               ROUND(AVG(edge), 3) AS avg_edge,
               ROUND(MIN(model_prob), 3) AS min_pred,
               ROUND(MAX(model_prob), 3) AS max_pred
        FROM trade_history
        WHERE city=? AND outcome IS NOT NULL AND outcome != 2
          AND model_prob IS NOT NULL
          AND datetime(created_at) >= ?
    """, (city, POST_FIX_CUTOFF)).fetchone()
    p["calibration"] = dict(r) if r else {}

    # By side (post-fix)
    rows = c.execute("""
        SELECT
          CASE WHEN token_side='NO' THEN 'NO' WHEN token_side='YES' THEN 'YES' ELSE 'other' END AS ts,
          COUNT(*) AS n,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS wr,
          ROUND(SUM(pnl), 2) AS net,
          ROUND(AVG(pnl), 2) AS avg_pnl,
          ROUND(AVG(entry_price), 3) AS avg_entry
        FROM trade_history
        WHERE city=? AND outcome IS NOT NULL AND outcome != 2
          AND datetime(created_at) >= ?
        GROUP BY ts
    """, (city, POST_FIX_CUTOFF)).fetchall()
    p["by_side"] = [dict(r) for r in rows]

    # By edge bucket (post-fix non-rebal)
    rows = c.execute("""
        SELECT
          CASE
            WHEN edge < 0.05 THEN 'a_<0.05'
            WHEN edge < 0.10 THEN 'b_0.05-0.10'
            WHEN edge < 0.15 THEN 'c_0.10-0.15'
            WHEN edge < 0.20 THEN 'd_0.15-0.20'
            WHEN edge < 0.30 THEN 'e_0.20-0.30'
            ELSE 'f_>=0.30'
          END AS bucket,
          COUNT(*) AS n,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS wr,
          ROUND(SUM(pnl), 2) AS net,
          ROUND(AVG(pnl), 2) AS avg_pnl
        FROM trade_history
        WHERE city=? AND outcome IS NOT NULL AND outcome != 2
          AND edge IS NOT NULL
          AND datetime(created_at) >= ?
        GROUP BY bucket
        ORDER BY bucket
    """, (city, POST_FIX_CUTOFF)).fetchall()
    p["by_edge"] = [dict(r) for r in rows]

    # By NO-ask (entry_price for NO side) bucket
    rows = c.execute("""
        SELECT
          CASE
            WHEN entry_price < 0.30 THEN 'a_<0.30'
            WHEN entry_price < 0.40 THEN 'b_0.30-0.40'
            WHEN entry_price < 0.50 THEN 'c_0.40-0.50'
            WHEN entry_price < 0.60 THEN 'd_0.50-0.60'
            WHEN entry_price < 0.70 THEN 'e_0.60-0.70'
            WHEN entry_price < 0.80 THEN 'f_0.70-0.80'
            ELSE 'g_>=0.80'
          END AS bucket,
          COUNT(*) AS n,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS wr,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE city=? AND outcome IS NOT NULL AND outcome != 2
          AND token_side = 'NO'
          AND datetime(created_at) >= ?
        GROUP BY bucket
        ORDER BY bucket
    """, (city, POST_FIX_CUTOFF)).fetchall()
    p["by_no_entry"] = [dict(r) for r in rows]

    # Top wins
    rows = c.execute("""
        SELECT id, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional,
               ROUND(model_prob,3) AS mp, ROUND(edge,3) AS edge,
               ROUND(pnl,2) AS pnl, SUBSTR(question, 1, 60) AS q
        FROM trade_history
        WHERE city=? AND pnl > 0 AND outcome != 2
        ORDER BY pnl DESC LIMIT 5
    """, (city,)).fetchall()
    p["top_wins"] = [dict(r) for r in rows]

    # Top losses
    rows = c.execute("""
        SELECT id, target_date, COALESCE(category,'<null>') AS cat,
               side, ROUND(entry_price,3) AS px, notional,
               ROUND(model_prob,3) AS mp, ROUND(edge,3) AS edge,
               ROUND(pnl,2) AS pnl, SUBSTR(question, 1, 60) AS q
        FROM trade_history
        WHERE city=? AND outcome IS NOT NULL AND outcome != 2 AND pnl <= 0
        ORDER BY pnl ASC LIMIT 5
    """, (city,)).fetchall()
    p["top_losses"] = [dict(r) for r in rows]

    # Flip subset
    rows = c.execute("""
        SELECT id, target_date, side, ROUND(entry_price,3) AS px,
               notional, outcome, ROUND(pnl,2) AS pnl,
               ROUND(model_prob,3) AS mp, ROUND(edge,3) AS edge,
               COALESCE(exit_reason, '-') AS rsn
        FROM trade_history
        WHERE city=? AND category='weather_tail_no_flipped'
        ORDER BY id
    """, (city,)).fetchall()
    p["flips"] = [dict(r) for r in rows]

    # Open positions
    rows = c.execute("""
        SELECT id, target_date, COALESCE(category,'<null>') AS cat,
               token_side, ROUND(entry_price,3) AS px,
               notional, ROUND(model_prob,3) AS mp, ROUND(edge,3) AS edge
        FROM trade_history
        WHERE city=? AND outcome IS NULL
        ORDER BY target_date, id
    """, (city,)).fetchall()
    p["open"] = [dict(r) for r in rows]

    return p


def classify_tier(p):
    """Tier A/B/C/D/E based on calibration gap + sample size."""
    cal = p.get("calibration", {})
    n = cal.get("n", 0) or 0
    pred = cal.get("avg_pred")
    wr = cal.get("wr")
    if n < 4:
        return "E", "INSUFFICIENT DATA"
    if pred is None or wr is None:
        return "E", "INSUFFICIENT DATA"
    gap = wr - pred
    if gap >= 0.20:
        return "A", "MODEL UNDER-PREDICTS — SIZE UP"
    if gap >= 0.10:
        return "A-", "MODEL UNDER-PREDICTS — slight size-up"
    if abs(gap) < 0.10:
        return "B", "WELL-CALIBRATED — STANDARD"
    if gap >= -0.20:
        return "C", "MODEL OVER-PREDICTS — DOWNSIZE"
    return "D", "MODEL BROKEN — BLOCK"


def generate_recommendation(p):
    """Concrete strategy recommendation per city."""
    tier, label = classify_tier(p)
    rec = {"tier": tier, "tier_label": label}
    cal = p.get("calibration", {})
    n = cal.get("n", 0) or 0

    # Flip eligibility based on flipped trade results + tier
    flips = p.get("flips", [])
    flip_settled = [f for f in flips if f["outcome"] is not None and f["outcome"] != 2]
    flip_wins = [f for f in flip_settled if (f["pnl"] or 0) > 0]
    flip_pnl = sum((f["pnl"] or 0) for f in flips)

    if tier == "D":
        rec["primary_strategy"] = "BLOCK"
        rec["weather_tail_no"] = "BLOCK"
        rec["flip"] = "BLOCK"
        rec["size_multiplier"] = 0.0
        rec["edge_floor"] = None
        rec["rationale"] = (
            f"Sample n={n}, gap is severely negative. Model is broken in this city. "
            f"Suspend new entries until n>=15 settles or recalibration."
        )
    elif tier == "C":
        rec["primary_strategy"] = "weather_tail_no DOWNSIZE + selective flip"
        rec["weather_tail_no"] = "ALLOW with size_x0.5 + edge_floor 0.10"
        if len(flip_settled) >= 2 and flip_pnl > 0:
            rec["flip"] = "ALLOW (flip data positive in this city)"
        else:
            rec["flip"] = "TEST sparingly (size_x0.5)"
        rec["size_multiplier"] = 0.5
        rec["edge_floor"] = 0.10
        rec["rationale"] = (
            f"Model over-predicts here (gap={cal.get('wr',0) - cal.get('avg_pred',0):+.2f}). "
            f"Reduce size 50% and tighten edge floor to filter weak signals. "
            f"If flip data shows promise (n={len(flip_settled)} settled, "
            f"pnl=${flip_pnl:.2f}), keep flip alive."
        )
    elif tier in ("A", "A-"):
        rec["primary_strategy"] = "weather_tail_no SIZE UP + flip OK"
        rec["weather_tail_no"] = "ALLOW with size_x1.5 + edge_floor 0.03"
        rec["flip"] = "ALLOW (model under-predicts; both sides have edge)"
        rec["size_multiplier"] = 1.5 if tier == "A" else 1.2
        rec["edge_floor"] = 0.03
        rec["rationale"] = (
            f"Model under-predicts here (gap=+{cal.get('wr',0) - cal.get('avg_pred',0):.2f}). "
            f"Both NO and YES sides should profit. Size up 50%; keep edge floor at "
            f"current 0.03."
        )
    elif tier == "B":
        rec["primary_strategy"] = "weather_tail_no STANDARD"
        rec["weather_tail_no"] = "ALLOW (default settings)"
        if len(flip_settled) >= 2:
            rec["flip"] = ("ALLOW (positive flip data)" if flip_pnl > 0
                           else "PAUSE (negative flip data)")
        else:
            rec["flip"] = "ALLOW per global default (insufficient city data)"
        rec["size_multiplier"] = 1.0
        rec["edge_floor"] = 0.05
        rec["rationale"] = (
            f"Model is well-calibrated (gap≈0). Use default thresholds. "
            f"Flip experiment status: {len(flip_settled)} settled, pnl=${flip_pnl:.2f}."
        )
    else:
        rec["primary_strategy"] = "USE GLOBAL DEFAULTS"
        rec["weather_tail_no"] = "ALLOW (no city-specific signal)"
        rec["flip"] = "ALLOW (per global default)"
        rec["size_multiplier"] = 1.0
        rec["edge_floor"] = 0.05
        rec["rationale"] = f"n={n} too small for city-specific calibration. Use global priors."

    return rec


# ============================================================
# REPORT GENERATION
# ============================================================

def render_city_section(p):
    """Render markdown for one city."""
    out = []
    city = p["city"]
    lt = p["lifetime"]
    cal = p.get("calibration", {})
    rec = generate_recommendation(p)
    tier = rec["tier"]

    # Header with tier badge
    tier_emoji = {"A": "🟢", "A-": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "E": "⚪"}.get(tier, "⚪")
    out.append(f"## {city.upper()}  —  Tier {tier} {tier_emoji}")
    out.append("")
    out.append(f"**{rec['tier_label']}**")
    out.append("")

    # 1. Headline
    out.append("### 1. Headline Stats")
    out.append("")
    out.append("| Metric | Value |")
    out.append("|--------|-------|")
    out.append(f"| Total trades | {lt['n']} |")
    out.append(f"| Settled | {lt['settled']} |")
    out.append(f"| Open | {lt['open']} |")
    out.append(f"| Wins | {lt['w']} |")
    out.append(f"| Losses | {lt['l']} |")
    out.append(f"| Rebal exits | {lt['rbl']} |")
    out.append(f"| **Lifetime P&L** | **{fmt(lt['pnl'], sign=True)}** |")
    out.append(f"| Total notional traded | {fmt(lt['notl'])} |")
    out.append(f"| Open notional | {fmt(lt['open_notl'])} |")
    out.append("")

    # 2. Era breakdown
    out.append("### 2. Era Breakdown")
    out.append("")
    out.append("| Era | n | W | L | RBL | Net |")
    out.append("|-----|---|---|---|-----|-----|")
    leg = p["legacy"]
    out.append(f"| Legacy `<null>` | {leg['n'] or 0} | {leg['w'] or 0} | "
               f"{leg['l'] or 0} | {leg['rbl'] or 0} | {fmt(leg['pnl'], sign=True)} |")
    pf = p["post_fix"]
    out.append(f"| Post-fix (Apr 26+) | {pf['n'] or 0} | {pf['w'] or 0} | "
               f"{pf['l'] or 0} | {pf['rbl'] or 0} | {fmt(pf['pnl'], sign=True)} |")
    out.append("")

    # 3. Category
    out.append("### 3. By Category")
    out.append("")
    out.append("| Category | n | Open | W/L/RBL | Net |")
    out.append("|----------|---|------|---------|-----|")
    for r in p["by_category"]:
        wlr = f"{r['w']}/{r['l']}/{r['rbl']}"
        out.append(f"| `{r['cat']}` | {r['n']} | {r['open']} | {wlr} | "
                   f"{fmt(r['pnl'], sign=True)} |")
    out.append("")

    # 4. Side analysis (post-fix)
    if p["by_side"]:
        out.append("### 4. By Side (Post-Fix Era)")
        out.append("")
        out.append("| Side | n | Win Rate | Avg PnL | Avg Entry | Net |")
        out.append("|------|---|----------|---------|-----------|-----|")
        for r in p["by_side"]:
            out.append(f"| {r['ts']} | {r['n']} | {fmt_pct(r['wr'])} | "
                       f"{fmt(r['avg_pnl'], sign=True)} | "
                       f"{r['avg_entry'] or 0:.3f} | "
                       f"{fmt(r['net'], sign=True)} |")
        out.append("")

    # 5. Calibration
    out.append("### 5. Calibration Profile (Post-Fix)")
    out.append("")
    if cal.get("n", 0) >= 1:
        n = cal["n"]
        pred = cal.get("avg_pred", 0) or 0
        wr = cal.get("wr", 0) or 0
        gap = wr - pred
        out.append(f"- **Sample size**: {n} settled non-rebal trades")
        out.append(f"- **Model predicted average win rate**: `{pred:.3f}` ({pred*100:.1f}%)")
        out.append(f"- **Realized win rate**: `{wr:.3f}` ({wr*100:.1f}%)")
        gap_emoji = "🟢" if gap > 0.10 else ("🔴" if gap < -0.10 else "🟡")
        out.append(f"- **Calibration gap**: `{gap:+.3f}` ({gap*100:+.1f}pp) {gap_emoji}")
        out.append(f"- **Average pnl per trade**: {fmt(cal.get('avg_pnl'), sign=True)}")
        out.append(f"- **Net P&L (post-fix)**: {fmt(cal.get('net'), sign=True)}")
        out.append(f"- **Average edge at entry**: {fmt_signed_pct(cal.get('avg_edge', 0))}")
        out.append("")

        # Bar chart for predicted vs realized
        out.append("**Predicted vs Realized:**")
        out.append("```")
        out.append(f"  Predicted win rate : {percent_bar(pred)} {pred*100:.1f}%")
        out.append(f"  Realized win rate  : {percent_bar(wr)} {wr*100:.1f}%")
        out.append("```")
        out.append("")

        if gap > 0.20:
            out.append("> ⚠️ **Model is significantly UNDER-confident here.** "
                       "Realized wins exceed predictions by 20pp+. This is alpha to "
                       "lean into — increase position size and consider both NO and "
                       "YES sides (flipping at high NO ask works).")
            out.append("")
        elif gap > 0.10:
            out.append("> ✅ Model slightly under-predicts here. Mild positive signal.")
            out.append("")
        elif gap < -0.20:
            out.append("> 🚨 **Model is significantly OVER-confident.** Predictions "
                       "fail to materialize >20pp below model expectation. Consider "
                       "blocking new entries until calibration improves OR shrinking "
                       "size by 50%+ and bumping edge floor.")
            out.append("")
        elif gap < -0.10:
            out.append("> ⚠️ Model over-predicts here. Reduce size and tighten edge floor.")
            out.append("")
        else:
            out.append("> 🟡 Model is well-calibrated — use default settings.")
            out.append("")
    else:
        out.append("*Insufficient post-fix data for calibration analysis.*")
        out.append("")

    # 6. Edge bucket
    if p["by_edge"]:
        out.append("### 6. Performance by Edge Bucket (Post-Fix)")
        out.append("")
        out.append("| Edge band | n | Win Rate | Avg PnL | Net |")
        out.append("|-----------|---|----------|---------|-----|")
        for r in p["by_edge"]:
            label = r["bucket"][2:]  # strip prefix
            out.append(f"| {label} | {r['n']} | {fmt_pct(r['wr'])} | "
                       f"{fmt(r['avg_pnl'], sign=True)} | {fmt(r['net'], sign=True)} |")
        out.append("")

    # 7. NO entry bucket
    if p["by_no_entry"]:
        out.append("### 7. Performance by NO Entry Price (Post-Fix, NO trades only)")
        out.append("")
        out.append("| Entry band | n | Win Rate | Net |")
        out.append("|------------|---|----------|-----|")
        for r in p["by_no_entry"]:
            label = r["bucket"][2:]
            out.append(f"| {label} | {r['n']} | {fmt_pct(r['wr'])} | "
                       f"{fmt(r['net'], sign=True)} |")
        out.append("")

    # 8. Best & worst
    out.append("### 8. Best & Worst Trades")
    out.append("")
    if p["top_wins"]:
        out.append("**Top wins:**")
        out.append("")
        out.append("| ID | Target | Cat | Side | Entry | Edge | PnL | Question |")
        out.append("|----|--------|-----|------|-------|------|-----|----------|")
        for r in p["top_wins"]:
            out.append(f"| #{r['id']} | {r['target_date']} | `{r['cat']}` | "
                       f"{r['side']} | {r['px']:.3f} | "
                       f"{(r['edge'] or 0):+.3f} | {fmt(r['pnl'], sign=True)} | "
                       f"{r['q']}... |")
        out.append("")
    if p["top_losses"]:
        out.append("**Top losses:**")
        out.append("")
        out.append("| ID | Target | Cat | Side | Entry | Edge | PnL | Question |")
        out.append("|----|--------|-----|------|-------|------|-----|----------|")
        for r in p["top_losses"]:
            out.append(f"| #{r['id']} | {r['target_date']} | `{r['cat']}` | "
                       f"{r['side']} | {r['px']:.3f} | "
                       f"{(r['edge'] or 0):+.3f} | {fmt(r['pnl'], sign=True)} | "
                       f"{r['q']}... |")
        out.append("")

    # 9. Flip experiment
    if p["flips"]:
        out.append("### 9. Flip Experiment (weather_tail_no_flipped)")
        out.append("")
        out.append("| ID | Target | Side | Entry | Outcome | PnL | Reason |")
        out.append("|----|--------|------|-------|---------|-----|--------|")
        flip_pnl_total = 0.0
        for r in p["flips"]:
            outcome_s = "open" if r["outcome"] is None else (
                f"out={r['outcome']}"
            )
            pnl_s = fmt(r["pnl"], sign=True) if r["pnl"] is not None else "—"
            out.append(f"| #{r['id']} | {r['target_date']} | {r['side']} | "
                       f"{r['px']:.3f} | {outcome_s} | {pnl_s} | {r['rsn']} |")
            if r["pnl"] is not None:
                flip_pnl_total += r["pnl"]
        out.append("")
        flip_settled = sum(1 for f in p["flips"] if f["outcome"] is not None)
        flip_wins = sum(1 for f in p["flips"] if f["pnl"] and f["pnl"] > 0)
        out.append(f"**Flip net: {fmt(flip_pnl_total, sign=True)} over {flip_settled} settled "
                   f"({flip_wins} wins). {sum(1 for f in p['flips'] if f['outcome'] is None)} open.**")
        out.append("")

    # 10. Recommendation
    out.append("### 10. STRATEGY RECOMMENDATION")
    out.append("")
    out.append(f"**Tier: {tier} — {rec['tier_label']}**")
    out.append("")
    out.append(f"- **Primary strategy**: {rec['primary_strategy']}")
    out.append(f"- **`weather_tail_no` (NO direction)**: {rec['weather_tail_no']}")
    out.append(f"- **Flip (`weather_tail_no_flipped`, YES at no_ask>=0.70)**: {rec['flip']}")
    out.append(f"- **Position size multiplier**: ×{rec['size_multiplier']}")
    if rec["edge_floor"] is not None:
        out.append(f"- **Edge floor**: {rec['edge_floor']:.3f} ({rec['edge_floor']*100:.1f}pp)")
    out.append("")
    out.append(f"**Rationale**: {rec['rationale']}")
    out.append("")

    out.append("---")
    out.append("")
    return out


def main():
    c = conn()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out = []

    # ========== HEADER ==========
    now = datetime.now(timezone.utc)
    out.append("# Per-City Trading Strategy Report")
    out.append("")
    out.append(f"**Generated**: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    out.append(f"**Database**: `{DB.name}` (read-only audit)")
    out.append(f"**Post-fix cutoff**: trades created on or after `{POST_FIX_CUTOFF}` "
               "(when categories went live and recent strategy fixes were deployed)")
    out.append("")

    # ========== EXECUTIVE SUMMARY ==========
    grand = c.execute("""
        SELECT COUNT(*) AS n, SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS open,
               ROUND(SUM(pnl),2) AS pnl
        FROM trade_history
    """).fetchone()
    out.append("## Executive Summary")
    out.append("")
    out.append(f"- **Total trades (lifetime)**: {grand['n']}")
    out.append(f"- **Open positions now**: {grand['open']}")
    out.append(f"- **Lifetime realized P&L**: {fmt(grand['pnl'], sign=True)}")
    out.append("")
    out.append("This report classifies each city into one of five tiers based on "
               "calibration gap and sample size, and provides concrete strategy "
               "recommendations including:")
    out.append("- Whether to use `weather_tail_no` (NO-direction tail strategy)")
    out.append("- Whether to use `weather_tail_no_flipped` (YES-flip experiment "
               "at no_ask>=0.70)")
    out.append("- Position-size multiplier vs default")
    out.append("- Edge-floor adjustment")
    out.append("- Edge × NO-ask × side performance breakdowns")
    out.append("")
    out.append("### Tier Definitions")
    out.append("")
    out.append("| Tier | Calibration Gap | Action |")
    out.append("|------|-----------------|--------|")
    out.append("| 🟢 A | gap ≥ +0.20 (model under-predicts) | SIZE UP, both sides OK |")
    out.append("| 🟢 A- | +0.10 ≤ gap < +0.20 | Slight size-up |")
    out.append("| 🟡 B | -0.10 < gap < +0.10 (well calibrated) | STANDARD settings |")
    out.append("| 🟠 C | -0.20 < gap ≤ -0.10 (over-predicts) | DOWNSIZE, tighten edge |")
    out.append("| 🔴 D | gap ≤ -0.20 | BLOCK new entries |")
    out.append("| ⚪ E | n < 4 | Use global priors |")
    out.append("")

    # ========== CITY RANKING TABLE ==========
    rows = c.execute("""
        SELECT city,
               ROUND(SUM(pnl), 2) AS pnl,
               COUNT(*) AS n
        FROM trade_history
        GROUP BY city
        ORDER BY pnl
    """).fetchall()
    out.append("### Cities Ranked by Lifetime P&L")
    out.append("")
    out.append("| City | n | Net P&L |")
    out.append("|------|---|---------|")
    for r in rows:
        out.append(f"| {r['city']} | {r['n']} | {fmt(r['pnl'], sign=True)} |")
    out.append("")

    # ========== TIER ASSIGNMENTS ==========
    cities = [r["city"] for r in rows]
    profiles = []
    for city in cities:
        prof = get_city_profile(c, city)
        prof["recommendation"] = generate_recommendation(prof)
        profiles.append(prof)

    out.append("### Tier Assignments")
    out.append("")
    out.append("| City | Tier | Sample (post-fix) | Gap | Recommendation |")
    out.append("|------|------|-------------------|-----|----------------|")
    for p in profiles:
        rec = p["recommendation"]
        cal = p.get("calibration", {})
        n = cal.get("n", 0) or 0
        gap = ((cal.get("wr") or 0) - (cal.get("avg_pred") or 0))
        gap_s = f"{gap:+.2f}" if n > 0 else "—"
        out.append(f"| {p['city']} | **{rec['tier']}** | {n} | {gap_s} | "
                   f"{rec['primary_strategy']} |")
    out.append("")

    # ========== METHODOLOGY ==========
    out.append("## Methodology")
    out.append("")
    out.append("### Calibration Gap")
    out.append("")
    out.append("```")
    out.append("gap = realized_winrate − model_predicted_winrate")
    out.append("")
    out.append("realized_winrate     = fraction of trades that won (pnl > 0)")
    out.append("model_predicted_wr   = mean(model_prob) at entry, post-fix only")
    out.append("```")
    out.append("")
    out.append("Positive gap → model under-predicts → size up (we win more often than expected)")
    out.append("Negative gap → model over-predicts → downsize (we lose more often than expected)")
    out.append("")
    out.append("### Post-Fix Filter")
    out.append("")
    out.append("All calibration analysis filters to `created_at >= 2026-04-26`. The "
               "pre-fix `<null>` legacy trades are reported but not used for forward "
               "calibration because the strategies and gates have changed materially.")
    out.append("")
    out.append("### Sample Size Caveats")
    out.append("")
    out.append("With current data (~10-30 trades per major city), 95% confidence "
               "intervals on win rates are **±15-25pp**. Recommendations are first-cut "
               "directional guidance, not statistical certainty. Refresh nightly and "
               "expect tier reclassifications as more data accumulates.")
    out.append("")

    # ========== PER-CITY SECTIONS ==========
    out.append("---")
    out.append("")
    out.append("## City-by-City Detailed Analysis")
    out.append("")
    out.append("(Cities ordered by lifetime net P&L, worst → best.)")
    out.append("")

    for p in profiles:
        out.extend(render_city_section(p))

    # ========== CROSS-CITY SUMMARY ==========
    out.append("## Cross-City Summary & Action Plan")
    out.append("")

    # Tier groupings
    tier_a = [p for p in profiles if p["recommendation"]["tier"] in ("A", "A-")]
    tier_b = [p for p in profiles if p["recommendation"]["tier"] == "B"]
    tier_c = [p for p in profiles if p["recommendation"]["tier"] == "C"]
    tier_d = [p for p in profiles if p["recommendation"]["tier"] == "D"]
    tier_e = [p for p in profiles if p["recommendation"]["tier"] == "E"]

    out.append("### 🟢 Tier A — Lean In (size up + both sides)")
    out.append("")
    if tier_a:
        for p in tier_a:
            cal = p.get("calibration", {})
            gap = ((cal.get("wr") or 0) - (cal.get("avg_pred") or 0))
            out.append(f"- **{p['city']}** (gap +{gap:.2f}, n={cal.get('n',0)}): "
                       f"net={fmt(cal.get('net'), sign=True)}. "
                       f"size×{p['recommendation']['size_multiplier']}, "
                       f"both NO and flip OK.")
    else:
        out.append("*(none)*")
    out.append("")

    out.append("### 🟡 Tier B — Standard Settings")
    out.append("")
    if tier_b:
        for p in tier_b:
            cal = p.get("calibration", {})
            out.append(f"- **{p['city']}** (gap≈0, n={cal.get('n',0)}): "
                       f"net={fmt(cal.get('net'), sign=True)}. Default settings.")
    else:
        out.append("*(none)*")
    out.append("")

    out.append("### 🟠 Tier C — Downsize")
    out.append("")
    if tier_c:
        for p in tier_c:
            cal = p.get("calibration", {})
            gap = ((cal.get("wr") or 0) - (cal.get("avg_pred") or 0))
            out.append(f"- **{p['city']}** (gap {gap:+.2f}, n={cal.get('n',0)}): "
                       f"net={fmt(cal.get('net'), sign=True)}. "
                       f"size×0.5, edge floor 0.10.")
    else:
        out.append("*(none)*")
    out.append("")

    out.append("### 🔴 Tier D — BLOCK")
    out.append("")
    if tier_d:
        for p in tier_d:
            cal = p.get("calibration", {})
            gap = ((cal.get("wr") or 0) - (cal.get("avg_pred") or 0))
            out.append(f"- **{p['city']}** (gap {gap:+.2f}, n={cal.get('n',0)}): "
                       f"net={fmt(cal.get('net'), sign=True)}. STOP new entries.")
    else:
        out.append("*(none)*")
    out.append("")

    out.append("### ⚪ Tier E — Insufficient Data")
    out.append("")
    if tier_e:
        for p in tier_e:
            cal = p.get("calibration", {})
            out.append(f"- **{p['city']}** (n={cal.get('n',0)}): "
                       f"net={fmt(cal.get('net'), sign=True)}. Use global priors.")
    else:
        out.append("*(none)*")
    out.append("")

    # ========== IMPLEMENTATION NOTES ==========
    out.append("## Implementation Plan — Per-City Data Class")
    out.append("")
    out.append("Code architecture for shipping these recommendations:")
    out.append("")
    out.append("```python")
    out.append("# polymarket_strat/domain/weather/city_calibration.py")
    out.append("")
    out.append("@dataclass(frozen=True, slots=True)")
    out.append("class CityProfile:")
    out.append('    city: str')
    out.append('    tier: str               # "A" / "A-" / "B" / "C" / "D" / "E"')
    out.append("    n_settled: int")
    out.append("    avg_pred: float")
    out.append("    realized_wr: float")
    out.append("    calibration_gap: float")
    out.append("    size_multiplier: float  # 0.0 (block) to 1.5 (size up)")
    out.append("    edge_floor: float")
    out.append("    flip_eligible: bool")
    out.append("    last_recalibrated: datetime")
    out.append("")
    out.append("# Usage in evaluate_tail_no_bracket:")
    out.append("profile = CITY_PROFILES.get(contract.city)")
    out.append("if profile.tier == 'D':")
    out.append("    return None, RejectReason.city_blocked")
    out.append("effective_edge_floor = profile.edge_floor or EDGE_FLOOR_PP")
    out.append("target_notional = base_notional * profile.size_multiplier")
    out.append("```")
    out.append("")
    out.append("Data refreshed nightly via cron from `trade_history` (settled, post-fix only).")
    out.append("")
    out.append("---")
    out.append("")
    out.append("*End of report.*")

    OUT.write_text("\n".join(out))
    print(f"Wrote {len(out)} lines to {OUT}")
    print(f"File size: {OUT.stat().st_size} bytes")
    c.close()


if __name__ == "__main__":
    main()
