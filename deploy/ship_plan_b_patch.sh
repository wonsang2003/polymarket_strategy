#!/usr/bin/env bash
# One-shot ship script for the Apr 26 2026 Plan B + narrow-bracket NO cap patch.
#
# What it does (in order):
#   1. Verify the three patched files exist and the new constants are present.
#   2. Run the new test suite locally as a final gate.
#   3. Stage + commit ONLY the three files (no opportunistic churn).
#   4. Push to origin/main.
#   5. Invoke deploy/upload_to_ec2.sh (code-only, DB skipped — same default).
#   6. Run pytest on EC2 to confirm the patch landed remotely.
#   7. Print the gate-rejects keys to watch in the next autotrade cycle.
#
# Usage:
#   bash deploy/ship_plan_b_patch.sh           # full ship: commit + push + deploy
#   bash deploy/ship_plan_b_patch.sh --dry-run # show what would happen, do nothing
#   bash deploy/ship_plan_b_patch.sh --no-push # local commit only (no push, no deploy)
#
# Prereqs (all already in place from prior work — sanity-checked at runtime):
#   - $REPO_LOCAL points at the working tree (default: $HOME/Downloads/polymarket_strat)
#   - $EC2_KEY exists with 0400 perms
#   - git is configured with credentials that can push to origin
#   - tests/test_plan_b_cap.py exists (created by the patch)

set -euo pipefail

REPO_LOCAL="${REPO_LOCAL:-$HOME/Downloads/polymarket_strat}"
EC2_HOST="${EC2_HOST:-54.180.64.168}"
EC2_USER="${EC2_USER:-ubuntu}"
EC2_KEY="${EC2_KEY:-$HOME/.ssh/polymarket-seoul.pem}"
REPO_REMOTE="${REPO_REMOTE:-/home/ubuntu/polymarket}"

DRY_RUN=0
NO_PUSH=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --no-push) NO_PUSH=1 ;;
        --help|-h) head -22 "$0"; exit 0 ;;
        *) echo "unknown arg: $arg"; exit 1 ;;
    esac
done

run() {
    if (( DRY_RUN )); then
        echo "  [dry-run] $*"
    else
        eval "$@"
    fi
}

cd "$REPO_LOCAL"

# ---- 1) verify patch files present
echo "===== 1) Verify patched files ====="
PATCH_FILES=(
    "polymarket_strat/domain/weather/strategy.py"
    "tests/test_plan_b_cap.py"
    ".claude/CLAUDE.md"
)
for f in "${PATCH_FILES[@]}"; do
    [[ -f "$f" ]] || { echo "  MISSING: $f"; exit 1; }
    echo "  ok: $f"
done

# Sanity check: new constants are in place.
grep -q "_PLAN_B_HIGH_P_CAP: float = 0.80" polymarket_strat/domain/weather/strategy.py \
    || { echo "  MISSING constant _PLAN_B_HIGH_P_CAP=0.80 — patch didn't land"; exit 1; }
grep -q "_PLAN_B_HIGH_P_EDGE_TRIGGER: float = 0.15" polymarket_strat/domain/weather/strategy.py \
    || { echo "  MISSING constant _PLAN_B_HIGH_P_EDGE_TRIGGER=0.15"; exit 1; }
grep -q "_NARROW_BRACKET_WIDTH_F: float = 2.0" polymarket_strat/domain/weather/strategy.py \
    || { echo "  MISSING constant _NARROW_BRACKET_WIDTH_F=2.0"; exit 1; }
grep -q "_NARROW_BRACKET_NO_MAX_P: float = 0.75" polymarket_strat/domain/weather/strategy.py \
    || { echo "  MISSING constant _NARROW_BRACKET_NO_MAX_P=0.75"; exit 1; }
grep -q "narrow_bracket_no_cap" polymarket_strat/domain/weather/strategy.py \
    || { echo "  MISSING gate counter narrow_bracket_no_cap"; exit 1; }
echo "  all constants + gate counter present"

# ---- 2) run pinning tests locally
echo
echo "===== 2) Local pytest ====="
if (( DRY_RUN )); then
    echo "  [dry-run] would run: python -m pytest tests/test_plan_b_cap.py -v"
else
    python -m pytest tests/test_plan_b_cap.py -v --tb=short
fi

# ---- 3) commit
echo
echo "===== 3) Stage + commit ====="
# Show what will be committed.
git diff --stat -- "${PATCH_FILES[@]}" || true
echo

run "git add ${PATCH_FILES[*]}"
COMMIT_MSG=$(cat <<'EOF'
Tighten Plan B (0.85→0.80, 0.20→0.15) + add narrow-bracket NO cap

Triggered by NO-side bleed across 22:00–00:00 KST cycles (sao_paulo
p=0.86 → -$2.21, munich p=0.77 → -$1.87, hong_kong p=0.92 → -$1.35,
all on 1°C-wide brackets exiting via rebalance "breakeven_triggered").

Two changes in polymarket_strat/domain/weather/strategy.py:

1. Plan B high-p artifact cap tightened from (p>0.85, edge>0.20) to
   (p>0.80, edge>0.15). The losing band concentrated in p∈[0.77, 0.85],
   just below the old cap.

2. New narrow-bracket NO-side cap: bracket_width_f<2.0 (≈1°C contracts)
   AND token_side=="NO" AND model_prob>0.75 → reject. Counter
   gate_rejects["narrow_bracket_no_cap"]. Constants
   _NARROW_BRACKET_WIDTH_F=2.0, _NARROW_BRACKET_NO_MAX_P=0.75.

Why narrow brackets specifically: Polymarket EU/Asia weather contracts
are 1°C wide (~1.8°F), narrower than the ±2°F synthetic brackets the
fine-bin ECE audit was run on, so honest_ece under-states real
calibration error in the contracts we actually trade. Bracket P is
hyper-sensitive to σ-estimation error when bracket width ≈ σ.
YES-side is unaffected because the failure mode is asymmetric.

Tests: tests/test_plan_b_cap.py — 8 new pinning tests covering
constant values, type contract, ordering invariants, and
US-vs-EU width sanity. All green locally; full suite 464 passed
(1 pre-existing failure is task #22, unrelated UTC date bug).

Loosening path: only after (a) NO-side isotonic regression measurably
reduces the high-P calibration gap on real settled NO trades, or
(b) ≥60 days of real-forecast data per (city, model, lead) bucket
relaxes σ. Don't loosen blindly.

Spec updated: .claude/CLAUDE.md §15.1.1.
EOF
)
if (( DRY_RUN )); then
    echo "  [dry-run] would commit with message:"
    echo "$COMMIT_MSG" | sed 's/^/    /'
else
    git commit -m "$COMMIT_MSG" || {
        echo "  nothing to commit (already on this patch?)"
    }
fi

# ---- 4) push
if (( NO_PUSH )); then
    echo
    echo "===== 4) Skipped (--no-push) ====="
    echo "Local commit done. Push manually when ready: git push origin main"
    exit 0
fi

echo
echo "===== 4) Push to origin/main ====="
run "git push origin main"

# ---- 5) deploy to EC2
echo
echo "===== 5) Deploy to EC2 ====="
if [[ ! -f "$EC2_KEY" ]]; then
    echo "  EC2 key missing: $EC2_KEY — skipping remote deploy."
    echo "  Run manually:  bash deploy/upload_to_ec2.sh"
    exit 0
fi

run "bash deploy/upload_to_ec2.sh"

# ---- 6) verify on EC2
echo
echo "===== 6) Remote pytest ====="
run "ssh -i \"$EC2_KEY\" -o ConnectTimeout=15 \"$EC2_USER@$EC2_HOST\" \\
    'cd $REPO_REMOTE && git log --oneline -1 && \
     venv/bin/python -m pytest tests/test_plan_b_cap.py -v --tb=short'"

# ---- 7) what to watch for
echo
echo "===== 7) Watch in next autotrade cycle ====="
cat <<'EOM'
After the next hourly cron tick on EC2, look at the JSON cycle output
(or `tail logs/autotrade.log`) for these gate_rejects counters:

  plan_b_high_p_artifact   (now fires at p>0.80 + edge>0.15, was 0.85/0.20)
  narrow_bracket_no_cap    (NEW — fires on NO + width<2°F + p>0.75)

Healthy rates:
  - plan_b_high_p_artifact: 6–18 per cycle (same band, slightly more vs. before)
  - narrow_bracket_no_cap:  3–10 per cycle (the band that was leaking)

If narrow_bracket_no_cap is firing 30+ per cycle the gate is too tight —
likely the entire EU/Asia 1°C contract universe. Loosen _NARROW_BRACKET_NO_MAX_P
back toward 0.80 OR raise width threshold to 1.5°F.

If neither is firing at all, the upstream model is no longer producing
high-p signals — could be a quantile pricer regression. Inspect with:
  python -c "import sqlite3; \
    print(sqlite3.connect('data/weather/weather.db').execute(\
      'SELECT model_prob, COUNT(*) FROM trade_history WHERE created_at >= date(\\'now\\', \\'-1 day\\') GROUP BY ROUND(model_prob, 1) ORDER BY 1').fetchall())"
EOM

echo
echo "===== Done ====="
