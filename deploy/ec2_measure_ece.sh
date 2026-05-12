#!/usr/bin/env bash
# Apr 25 2026 — Layer 1 ECE measurement on EC2.
#
# Run this ON EC2 after `bash deploy/upload_to_ec2.sh` has synced the new
# strategy.py + measure_ece.py + quantile_pricing module. Confirms:
#   1. Quantile model artifacts exist on disk (44 expected)
#   2. measure_ece.py runs end-to-end against the prod DB
#   3. Prints aggregate ECE + per-bucket table
#
# Usage (on EC2, in repo root):
#   bash deploy/ec2_measure_ece.sh
#
# Output: data/weather/ece_report.json (also tail'd to stdout).
#
set -euo pipefail

REPO_ROOT="/home/ubuntu/polymarket"
VENV_PY="$REPO_ROOT/venv/bin/python"
MODELS_DIR="$REPO_ROOT/data/weather/quantile_models"
REPORT_PATH="$REPO_ROOT/data/weather/ece_report.json"

cd "$REPO_ROOT"

step() { echo; echo "===== $* ====="; }

step "1) Verify quantile models present"
if [[ ! -d "$MODELS_DIR" ]]; then
    echo "  MISSING $MODELS_DIR — train first via scripts/train_quantile_models.py" >&2
    exit 1
fi
N_MODELS=$(ls -1 "$MODELS_DIR"/*.pkl 2>/dev/null | wc -l)
echo "  $N_MODELS quantile artifacts in $MODELS_DIR"
if [[ "$N_MODELS" -lt 1 ]]; then
    echo "  FAIL: no quantile artifacts" >&2; exit 1
fi

step "2) Verify training metrics"
METRICS="$REPO_ROOT/data/weather/quantile_training_metrics.json"
if [[ -f "$METRICS" ]]; then
    "$VENV_PY" - <<'PY'
import json, statistics
m = json.load(open("/home/ubuntu/polymarket/data/weather/quantile_training_metrics.json"))
buckets = m.get("per_bucket", [])
trained = [b for b in buckets if b.get("status") == "trained"]
skipped = [b for b in buckets if b.get("status") != "trained"]
pls = [b["holdout_pinball_loss"] for b in trained if "holdout_pinball_loss" in b]
cws = [b["conformal_widening"] for b in trained if "conformal_widening" in b]
print(f"  trained: {len(trained)}, skipped: {len(skipped)}")
if pls: print(f"  median pinball: {statistics.median(pls):.4f}")
if cws: print(f"  median conformal widening: {statistics.median(cws):.4f}°F")
PY
else
    echo "  metrics file absent (non-fatal): $METRICS"
fi

step "3) Run measure_ece.py"
"$VENV_PY" scripts/measure_ece.py

step "4) Tail report"
if [[ -f "$REPORT_PATH" ]]; then
    "$VENV_PY" - <<'PY'
import json
r = json.load(open("/home/ubuntu/polymarket/data/weather/ece_report.json"))
print(f"  n_total_holdout:           {r['n_total_holdout']}")
print(f"  aggregate_ece_raw:         {r['aggregate_ece_raw']:.4f}  ({r['aggregate_ece_raw']*100:.2f}%)")
print(f"  aggregate_ece_conformal:   {r['aggregate_ece_conformal']:.4f}  ({r['aggregate_ece_conformal']*100:.2f}%)")
print(f"  aggregate_brier_raw:       {r['aggregate_brier_raw']:.4f}")
print(f"  aggregate_brier_conformal: {r['aggregate_brier_conformal']:.4f}")
print()
print(f"  Per-bucket ECE (top 10 by n_holdout):")
top = sorted(r["per_bucket"], key=lambda b: -b["n_holdout"])[:10]
print(f"    {'city':16s}{'lead':>6s}{'n':>8s}{'ece_raw':>10s}{'ece_conf':>10s}")
for b in top:
    print(f"    {b['city']:16s}{b['lead_hours']:>6}{b['n_holdout']:>8}"
          f"{b['ece_raw']:>10.4f}{b['ece_conformal']:>10.4f}")
PY
else
    echo "  ECE report missing — measure_ece.py probably exited non-zero" >&2
    exit 1
fi

step "5) Decision guidance"
"$VENV_PY" - <<'PY'
import json
r = json.load(open("/home/ubuntu/polymarket/data/weather/ece_report.json"))
ece_conf = r["aggregate_ece_conformal"]
ece_raw = r["aggregate_ece_raw"]
brier_conf = r["aggregate_brier_conformal"]
print()
print(f"  Decision matrix:")
if ece_conf <= 0.07:
    print(f"  ✓ aggregate ECE (conformal) {ece_conf*100:.2f}% ≤ 7% target")
    print(f"  → SAFE to enable use_quantile_pricing=True in production.")
    print(f"    Action: edit polymarket_strat/config.py, flip default to True,")
    print(f"            redeploy, watch first 24h of paper trades for behavioral diff.")
elif ece_conf <= 0.10:
    print(f"  ~ aggregate ECE (conformal) {ece_conf*100:.2f}% in [7%, 10%] borderline")
    print(f"  → SOFT-LAUNCH: enable via env var only, A/B against parametric for 5-7 days.")
    print(f"    Both pipelines run, log model_prob_quantile vs model_prob_parametric, decide later.")
else:
    print(f"  ✗ aggregate ECE (conformal) {ece_conf*100:.2f}% > 10%")
    print(f"  → DO NOT ENABLE. Investigate per-bucket breakdown — likely a few cities")
    print(f"    with thin holdout dragging the mean. Consider per-(city, lead) gate.")
print()
if ece_conf < ece_raw - 0.005:
    print(f"  ✓ Conformal wrapper helps: ECE {ece_raw*100:.2f}% → {ece_conf*100:.2f}%")
elif ece_conf > ece_raw + 0.005:
    print(f"  ✗ Conformal wrapper HURTS calibration here. Use raw, or tune α.")
else:
    print(f"  ~ Conformal wrapper neutral on aggregate (raw {ece_raw*100:.2f}% / conf {ece_conf*100:.2f}%).")
    print(f"    Still recommended for tail safety; difference may be larger per-bucket.")
PY

echo
echo "===== DONE ====="
echo "Report: $REPORT_PATH"
