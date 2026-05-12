"""Parse a strategy_review response and write proposals to the hypothesis DB.

Closes the writeback loop on the J-curve learning system: Claude proposes,
this parser ingests, and the next review's prompt sees its own past thinking.

Three things get written per review run:
  1. strategy_hypotheses    rows for each R<n> recommendation (status='proposed')
  2. untried_hypotheses     row for the new untried hypothesis (MSG 12)
  3. claude_calibration     rows for each R<n> with confidence_pct (was_correct=NULL until evaluator runs)

The parser is tolerant — it parses what it can find and silently skips
malformed sections rather than crashing. Worst case: no rows inserted,
review still landed in Telegram, user can manually backfill via CLI.

Usage (programmatic, called from strategy_review.py main()):
    from parse_review_to_db import parse_and_persist
    parse_and_persist(response_text, review_run_id="2026_05_05_W")

Usage (standalone, replay a saved response):
    python scripts/parse_review_to_db.py /path/to/review_YYYYMMDD_HHMM.md
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


# ============================================================================
# Section splitting
# ============================================================================

MSG_DELIM = re.compile(r"\s*-{3,}\s*MSG\s*-{3,}\s*")


def split_messages(response: str) -> list[str]:
    """Split Claude's response into the 14 messages (or fewer if Claude truncated)."""
    parts = MSG_DELIM.split(response)
    return [p.strip() for p in parts if p and p.strip()]


# ============================================================================
# Recommendation parser  (MSG 8/9/10 → strategy_hypotheses)
# ============================================================================

# Lenient match: title line of an R<n> message.
# Accepts "🛠️ R1 · Title  ★", "R1 ★ Title", "🛠 R1 · Title", "R1 · ...".
R_TITLE_RE = re.compile(
    r"(?:🛠️|🛠)?\s*R(\d+)\s*[·:.\-]\s*(.+?)(?:\s*★)?\s*$",
    re.MULTILINE,
)
FILE_RE = re.compile(r"📁\s*File:\s*(.+?)(?:\n|$)", re.IGNORECASE)
CONFIDENCE_RE = re.compile(r"🎲?\s*Confidence:\s*(\d{1,3})\s*%", re.IGNORECASE)
RISK_RE = re.compile(r"Net\s*risk\s*[:.]\s*(LOW|MEDIUM|MED|HIGH)", re.IGNORECASE)
EXPECTED_RE = re.compile(r"NET\s*([+-]?\$[\d.,]+)/(?:7d|wk)", re.IGNORECASE)
DIFF_RE = re.compile(r"```python\s*(.*?)```", re.DOTALL)


def parse_recommendation(msg: str) -> dict | None:
    """Extract one R<n> proposal from a single message body.

    Returns None if this message isn't a recommendation card.
    """
    title_match = R_TITLE_RE.search(msg)
    if not title_match:
        return None

    rank = int(title_match.group(1))
    title = title_match.group(2).strip()

    file_loc = FILE_RE.search(msg)
    confidence = CONFIDENCE_RE.search(msg)
    risk = RISK_RE.search(msg)
    expected = EXPECTED_RE.search(msg)
    diff = DIFF_RE.search(msg)

    return {
        "rank": rank,
        "title": title,
        "file_loc": file_loc.group(1).strip() if file_loc else "",
        "confidence_pct": int(confidence.group(1)) if confidence else 50,
        "risk_level": (risk.group(1).lower() if risk else "medium")[:6],
        "expected_effect": (expected.group(1) + "/wk") if expected else "(unparsed)",
        "proposed_change": (diff.group(1).strip() if diff else "(no diff block found)")[:4000],
        "raw_body": msg[:8000],
    }


# ============================================================================
# Untried hypothesis parser  (MSG 12 → untried_hypotheses)
# ============================================================================

UNTRIED_ID_RE = re.compile(r"🆕\s*ID:\s*(U_\d{4}_\d{2}_\d{2}_\d+)", re.IGNORECASE)
IMPACT_RE = re.compile(r"estimated_impact_usd\s*[:.]\s*\$?([\d.,]+)", re.IGNORECASE)
P_CORRECT_RE = re.compile(r"estimated_p_correct\s*[:.]\s*([\d.]+)", re.IGNORECASE)
EFFORT_RE = re.compile(r"estimated_effort_hr\s*[:.]\s*([\d.]+)", re.IGNORECASE)
EV_RE = re.compile(r"expected_value\s*[:.]\s*\$?([\d.,]+)", re.IGNORECASE)


def parse_untried(msg: str) -> dict | None:
    """Extract the new untried hypothesis from MSG 12 body."""
    id_match = UNTRIED_ID_RE.search(msg)
    if not id_match:
        return None

    # Pull idea + rationale by sectional regex (lenient — Claude may format slightly differently)
    idea_match = re.search(r"Idea\s*\n\s*(.+?)(?:\n\s*Rationale|\n\s*EV)", msg, re.DOTALL | re.IGNORECASE)
    rationale_match = re.search(r"Rationale\s*\n\s*(.+?)(?:\n\s*EV|\n\s*Promotion|\n\s*Adversarial|$)", msg, re.DOTALL | re.IGNORECASE)

    impact = IMPACT_RE.search(msg)
    p_correct = P_CORRECT_RE.search(msg)
    effort = EFFORT_RE.search(msg)
    ev = EV_RE.search(msg)

    return {
        "id": id_match.group(1),
        "idea": (idea_match.group(1).strip()[:1000] if idea_match else "(unparsed)"),
        "rationale": (rationale_match.group(1).strip()[:1000] if rationale_match else "(unparsed)"),
        "estimated_impact_usd": float(impact.group(1).replace(",", "")) if impact else None,
        "estimated_p_correct": float(p_correct.group(1)) if p_correct else None,
        "estimated_effort_hr": float(effort.group(1)) if effort else None,
        "expected_value": float(ev.group(1).replace(",", "")) if ev else None,
    }


# ============================================================================
# Persist
# ============================================================================

def conn():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


def hypothesis_id(rank: int, now: datetime) -> str:
    """e.g. H_2026_05_05_01 — collisions resolved by appending _b/_c manually if needed."""
    return f"H_{now:%Y_%m_%d}_{rank:02d}"


def persist(recs: list[dict], untried: dict | None, now: datetime) -> dict:
    """Write all recommendations + untried hypothesis to DB. Idempotent on (id) PK collision."""
    inserted = {"hypotheses": 0, "untried": 0, "calibration": 0, "skipped": 0}
    c = conn()
    try:
        for rec in recs:
            hid = hypothesis_id(rec["rank"], now)
            try:
                c.execute(
                    """
                    INSERT INTO strategy_hypotheses
                      (id, proposed_at, proposed_by, motivating_signal,
                       hypothesis, proposed_change, expected_effect,
                       confidence_pct, risk_level, status)
                    VALUES (?, ?, 'claude_weekly', ?,
                            ?, ?, ?, ?, ?, 'proposed')
                    """,
                    (
                        hid,
                        now.isoformat(),
                        f"R{rec['rank']} from weekly review {now:%Y-%m-%d}",
                        rec["title"],
                        f"File: {rec['file_loc']}\n\n{rec['proposed_change']}",
                        rec["expected_effect"],
                        rec["confidence_pct"],
                        rec["risk_level"],
                    ),
                )
                inserted["hypotheses"] += 1

                # Also log calibration entry (was_correct=NULL until evaluator fills it)
                c.execute(
                    """
                    INSERT INTO claude_calibration
                      (review_at, review_type, hypothesis_id, claude_confidence_pct)
                    VALUES (?, 'weekly', ?, ?)
                    """,
                    (now.isoformat(), hid, rec["confidence_pct"]),
                )
                inserted["calibration"] += 1
            except sqlite3.IntegrityError as e:
                # PK collision — already inserted in a prior run today.
                print(f"[parse] skip {hid}: {e}", file=sys.stderr)
                inserted["skipped"] += 1

        if untried:
            try:
                c.execute(
                    """
                    INSERT INTO untried_hypotheses
                      (id, registered_at, registered_by, idea, rationale,
                       estimated_effort_hr, estimated_impact_usd,
                       estimated_p_correct, expected_value, status)
                    VALUES (?, ?, 'claude_weekly', ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        untried["id"],
                        now.isoformat(),
                        untried["idea"],
                        untried["rationale"],
                        untried["estimated_effort_hr"],
                        untried["estimated_impact_usd"],
                        untried["estimated_p_correct"],
                        untried["expected_value"],
                    ),
                )
                inserted["untried"] += 1
            except sqlite3.IntegrityError as e:
                print(f"[parse] skip untried {untried['id']}: {e}", file=sys.stderr)
                inserted["skipped"] += 1

        c.commit()
    finally:
        c.close()
    return inserted


# ============================================================================
# Main entry point
# ============================================================================

def parse_and_persist(response_text: str, *, now: datetime | None = None) -> dict:
    """Parse Claude's full strategy_review response and write to DB.

    Returns dict with counts: {hypotheses, untried, calibration, skipped}.
    Safe to call from strategy_review.py main() — won't raise on parse failures.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    messages = split_messages(response_text)
    recs: list[dict] = []
    untried: dict | None = None

    for msg in messages:
        rec = parse_recommendation(msg)
        if rec:
            recs.append(rec)
            continue
        if untried is None:
            cand = parse_untried(msg)
            if cand:
                untried = cand

    return persist(recs, untried, now)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: parse_review_to_db.py <response_or_review_md_path>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    text = path.read_text()
    # If we were given a review_*.md (which has prompt+response), strip to just the response.
    if "## RESPONSE" in text:
        text = text.split("## RESPONSE", 1)[1]

    result = parse_and_persist(text)
    print(f"Inserted: hypotheses={result['hypotheses']} "
          f"untried={result['untried']} "
          f"calibration={result['calibration']} "
          f"skipped={result['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
