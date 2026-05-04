"""Initialize hypothesis tracker DB tables.

Creates 4 new tables in weather.db (idempotent — safe to re-run):
  - strategy_hypotheses : DAG of proposed/shipped/evaluated changes
  - lessons_learned     : distilled invariants from past hypotheses
  - claude_calibration  : Claude's own confidence accuracy tracking
  - counterfactual_runs : monthly param-sweep replay results

Run once on EC2:
    /home/ubuntu/polymarket/venv/bin/python scripts/init_hypothesis_db.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


SCHEMA = """
-- ────────────────────────────────────────────────────────────────────
-- Hypothesis log (append-only DAG of strategy decisions)
-- ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategy_hypotheses (
    id                  TEXT PRIMARY KEY,           -- e.g. "H_2026_05_06_01"
    proposed_at         TIMESTAMP NOT NULL,
    proposed_by         TEXT NOT NULL,              -- "claude_weekly" | "claude_daily" | "user"
    parent_hypothesis_id TEXT,                      -- DAG link
    motivating_signal   TEXT NOT NULL,              -- "amsterdam −$163 in week-2 review"
    hypothesis          TEXT NOT NULL,              -- "tighten amsterdam edge_floor to 0.10"
    proposed_change     TEXT NOT NULL,              -- file:line + diff snippet
    expected_effect     TEXT NOT NULL,              -- "+$50/wk amsterdam realized"
    confidence_pct      INTEGER NOT NULL,           -- 0-100
    risk_level          TEXT NOT NULL,              -- 'low' | 'medium' | 'high'
    status              TEXT NOT NULL DEFAULT 'proposed',  -- 'proposed' | 'rejected' | 'shipped' | 'reverted'
    user_decision       TEXT,                       -- 'YES' | 'NO' | 'MODIFY'
    user_decision_at    TIMESTAMP,
    shipped_at          TIMESTAMP,
    ship_commit_sha     TEXT,
    reverted_at         TIMESTAMP,
    revert_reason       TEXT,
    evaluation_at       TIMESTAMP,
    actual_effect       TEXT,
    measured_pnl_delta  REAL,
    verdict             TEXT,                       -- 'win' | 'partial' | 'lose' | 'inconclusive'
    archived            INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hyp_status ON strategy_hypotheses(status);
CREATE INDEX IF NOT EXISTS idx_hyp_proposed_at ON strategy_hypotheses(proposed_at);
CREATE INDEX IF NOT EXISTS idx_hyp_parent ON strategy_hypotheses(parent_hypothesis_id);


-- ────────────────────────────────────────────────────────────────────
-- Lessons learned (distilled wisdom from past hypotheses)
-- ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lessons_learned (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    distilled_at        TIMESTAMP NOT NULL,
    period              TEXT NOT NULL,              -- "2026-Q2-W3" or "2026-05"
    lesson              TEXT NOT NULL,              -- "Wide one-sided brackets at low NO ask = full notional loss factory"
    evidence_hypothesis_ids TEXT,                   -- JSON array of supporting hypothesis IDs
    confidence          TEXT NOT NULL,              -- 'high' | 'medium' | 'low'
    contradicted_at     TIMESTAMP,                  -- if later disproven
    contradicted_by_id  TEXT                        -- which hypothesis disproved it
);
CREATE INDEX IF NOT EXISTS idx_lesson_period ON lessons_learned(period);


-- ────────────────────────────────────────────────────────────────────
-- Claude calibration (track Claude's own confidence accuracy)
-- ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS claude_calibration (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    review_at           TIMESTAMP NOT NULL,
    review_type         TEXT NOT NULL,              -- 'daily' | 'weekly' | 'monthly'
    hypothesis_id       TEXT NOT NULL,
    claude_confidence_pct INTEGER NOT NULL,         -- e.g. 65
    actual_outcome      TEXT,                       -- filled at evaluation: 'win' | 'lose' | 'partial'
    was_correct         INTEGER,                    -- 1 if outcome matches confidence direction
    FOREIGN KEY (hypothesis_id) REFERENCES strategy_hypotheses(id)
);


-- ────────────────────────────────────────────────────────────────────
-- Counterfactual runs (monthly param-sweep what-ifs)
-- ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS counterfactual_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TIMESTAMP NOT NULL,
    period_start        TEXT NOT NULL,
    period_end          TEXT NOT NULL,
    scenario            TEXT NOT NULL,              -- "no_city_calibration" | "no_catastrophic_flip" | etc.
    realized_pnl        REAL NOT NULL,              -- actual P&L during period
    counterfactual_pnl  REAL NOT NULL,              -- estimated P&L if scenario applied
    delta               REAL NOT NULL,              -- counterfactual - realized
    notes               TEXT
);


-- ────────────────────────────────────────────────────────────────────
-- Untried hypotheses register (parking lot for ideas)
-- ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS untried_hypotheses (
    id                  TEXT PRIMARY KEY,           -- "U_2026_05_07_01"
    registered_at       TIMESTAMP NOT NULL,
    registered_by       TEXT NOT NULL,
    idea                TEXT NOT NULL,
    rationale           TEXT NOT NULL,
    estimated_effort_hr REAL,
    estimated_impact_usd REAL,
    estimated_p_correct REAL,                       -- probability Claude thinks it'd be correct
    expected_value      REAL,                       -- impact * p_correct
    status              TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'promoted' | 'archived'
    promoted_to_hypothesis_id TEXT,                 -- if elevated to active hypothesis
    promoted_at         TIMESTAMP,
    archived_at         TIMESTAMP,
    archive_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_untried_status ON untried_hypotheses(status);
"""


def main():
    if not DB.exists():
        print(f"DB not found: {DB}")
        return
    c = sqlite3.connect(str(DB))
    c.executescript(SCHEMA)
    c.commit()
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('strategy_hypotheses', 'lessons_learned', "
        "'claude_calibration', 'counterfactual_runs', 'untried_hypotheses') "
        "ORDER BY name"
    ).fetchall()
    print("Created tables:")
    for r in rows:
        cnt = c.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
        print(f"  {r[0]:<28} rows={cnt}")
    c.close()


if __name__ == "__main__":
    main()
