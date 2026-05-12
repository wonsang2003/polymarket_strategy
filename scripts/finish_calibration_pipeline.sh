#!/usr/bin/env bash
# One-shot finalizer after multi-lead calibration.
#
# Does everything needed to close out the April 19 calibration work:
#   1. Backs up the DB (timestamped, kept in data/weather/backups/)
#   2. Wipes stale HRRR/NAM rows from error_distributions
#   3. Prints the clean distribution breakdown
#   4. Runs walk-forward backtest at 24h (baseline) and 48h (tomorrow contracts)
#   5. Prints diff summary so skill-score impact is obvious
#
# Usage:
#     ./scripts/finish_calibration_pipeline.sh
#
# Runs in ~3-10 minutes depending on cpu. Safe to re-run (backup on every run).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DB="data/weather/weather.db"
BACKUP_DIR="data/weather/backups"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_PATH="$BACKUP_DIR/weather_${TIMESTAMP}.db"

WF_24H_CSV="tools/walk_forward/last_run_24h.csv"
WF_48H_CSV="tools/walk_forward/last_run_48h.csv"
WF_START="${WF_START:-2026-01-15}"
WF_END="${WF_END:-2026-04-10}"

echo "============================================================"
echo "[1/5] Backing up DB → $BACKUP_PATH"
echo "============================================================"
mkdir -p "$BACKUP_DIR"
# Use SQLite online backup (safe vs. writers).
sqlite3 "$DB" ".backup '$BACKUP_PATH'"
ls -lh "$BACKUP_PATH"

echo
echo "============================================================"
echo "[2/5] Pre-cleanup distribution breakdown"
echo "============================================================"
sqlite3 "$DB" <<'SQL'
.mode column
.headers on
SELECT model, regime, lead_hours, COUNT(*) AS n
FROM error_distributions
GROUP BY model, regime, lead_hours
ORDER BY lead_hours, model, regime;
SELECT lead_hours, COUNT(*) AS total FROM error_distributions GROUP BY lead_hours;
SQL

echo
echo "============================================================"
echo "[3/5] Deleting stale HRRR/NAM rows (inference borrows GFS)"
echo "============================================================"
BEFORE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM error_distributions;")
sqlite3 "$DB" "DELETE FROM error_distributions WHERE model IN ('hrrr','nam');"
AFTER=$(sqlite3 "$DB" "SELECT COUNT(*) FROM error_distributions;")
echo "Deleted $((BEFORE - AFTER)) rows.  ${BEFORE} → ${AFTER}"

echo
echo "--- post-cleanup breakdown ---"
sqlite3 "$DB" <<'SQL'
.mode column
.headers on
SELECT lead_hours, COUNT(*) AS total FROM error_distributions GROUP BY lead_hours;
SQL

echo
echo "============================================================"
echo "[4/5] Walk-forward backtest — 24h baseline"
echo "  range: $WF_START .. $WF_END"
echo "  csv:   $WF_24H_CSV"
echo "============================================================"
python tools/walk_forward/backtest.py \
    --all-cities --all-models \
    --start "$WF_START" --end "$WF_END" \
    --lead-hours 24 \
    --csv "$WF_24H_CSV" \
  | tee tools/walk_forward/last_run_24h.log

echo
echo "============================================================"
echo "[5/5] Walk-forward backtest — 48h tomorrow contracts"
echo "  range: $WF_START .. $WF_END"
echo "  csv:   $WF_48H_CSV"
echo "============================================================"
python tools/walk_forward/backtest.py \
    --all-cities --all-models \
    --start "$WF_START" --end "$WF_END" \
    --lead-hours 48 \
    --csv "$WF_48H_CSV" \
  | tee tools/walk_forward/last_run_48h.log

echo
echo "============================================================"
echo "DONE — diff summary"
echo "============================================================"

python - <<'PYEOF'
import csv, statistics, sys
from pathlib import Path

def load(path):
    p = Path(path)
    if not p.exists():
        print(f"  (missing: {path})")
        return None
    with p.open() as f:
        return list(csv.DictReader(f))

def col(rows, name):
    if not rows: return []
    vals = []
    for r in rows:
        v = r.get(name)
        if v in (None, "", "nan"): continue
        try: vals.append(float(v))
        except ValueError: pass
    return vals

def summary(label, path):
    rows = load(path)
    if not rows: return
    brier = col(rows, "brier") or col(rows, "brier_score")
    logl  = col(rows, "log_loss") or col(rows, "logloss")
    bss   = col(rows, "brier_skill") or col(rows, "skill_score") or col(rows, "brier_skill_score")
    n     = len(rows)
    print(f"  {label}: n={n}")
    if brier: print(f"    brier        median={statistics.median(brier):.4f}  mean={statistics.mean(brier):.4f}")
    if logl:  print(f"    log-loss     median={statistics.median(logl):.4f}  mean={statistics.mean(logl):.4f}")
    if bss:   print(f"    brier skill  median={statistics.median(bss):+.4f}  mean={statistics.mean(bss):+.4f}")
    if not (brier or logl or bss):
        print(f"    (no recognized metric columns in CSV)  columns: {list(rows[0].keys())}")

summary("24h", "tools/walk_forward/last_run_24h.csv")
summary("48h", "tools/walk_forward/last_run_48h.csv")
PYEOF

echo
echo "Done. Final artifacts:"
echo "  DB backup:         $BACKUP_PATH"
echo "  24h walk-forward:  tools/walk_forward/last_run_24h.csv  (+ .log)"
echo "  48h walk-forward:  tools/walk_forward/last_run_48h.csv  (+ .log)"
echo
echo "Dashboard picks up last_run_24h.csv automatically.  If you want the 48h"
echo "view to land too, symlink:  ln -sf last_run_48h.csv tools/walk_forward/last_run.csv"
