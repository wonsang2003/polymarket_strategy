#!/usr/bin/env bash
# Post-deploy verifier for the Plan B + narrow-bracket NO cap patch.
#
# Confirms the patch is actually live on EC2 in five layers. EC2 does NOT
# have .git/ (upload_to_ec2.sh rsyncs with --exclude '.git'), so the truth
# of "did the patch land" is in file content + mtime, not git log.
#
# Layers (fail-fast on each):
#   0. Connectivity: probe Tailscale 100.120.171.1 first, fall back to
#      public IP, error out if neither is reachable.
#   1. File mtime: strategy.py modified within the last 6 hours.
#   2. Source: the four new constants + new gate counter exist in strategy.py.
#   3. Tests: tests/test_plan_b_cap.py exists and passes on EC2.
#   4. Imports: importing strategy reads the new constant values at runtime.
#   5. Behavior: scan the last 6h of trade_history for any fresh row that
#      should have been blocked by the new gates. Any post-deploy hit ⇒
#      the patch isn't actually firing.
#
# Usage:
#   bash deploy/verify_plan_b_patch.sh                  # auto-pick host
#   EC2_HOST=100.120.171.1 bash deploy/verify_plan_b_patch.sh   # force Tailscale
#   EC2_HOST=54.180.64.168 bash deploy/verify_plan_b_patch.sh   # force public
#
# Exit code:  0 = all green,  non-zero = at least one layer failed.

set -uo pipefail

EC2_USER="${EC2_USER:-ubuntu}"
EC2_KEY="${EC2_KEY:-$HOME/.ssh/polymarket-seoul.pem}"
REPO_REMOTE="${REPO_REMOTE:-/home/ubuntu/polymarket}"
TAILSCALE_IP="${TAILSCALE_IP:-100.120.171.1}"
PUBLIC_IP="${PUBLIC_IP:-54.180.64.168}"

[[ -f "$EC2_KEY" ]] || { echo "missing key: $EC2_KEY"; exit 1; }

FAILS=0
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*"; FAILS=$((FAILS + 1)); }

# ---- 0) connectivity probe — pick the live host
echo "===== 0) Connectivity probe ====="
ssh_try() {
    local host="$1"
    ssh -i "$EC2_KEY" -o ConnectTimeout=8 -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        "$EC2_USER@$host" "echo OK" 2>/dev/null
}

EC2_HOST=""
if [[ -n "${EC2_HOST_OVERRIDE:-}" ]]; then
    EC2_HOST="$EC2_HOST_OVERRIDE"
    echo "  using override: $EC2_HOST"
elif [[ -n "${EC2_HOST:-}" && "$EC2_HOST" != "$TAILSCALE_IP" && "$EC2_HOST" != "$PUBLIC_IP" ]]; then
    # Custom host already set, trust it.
    :
else
    # Auto-probe Tailscale first (more reliable per CLAUDE.md), then public.
    if [[ "$(ssh_try "$TAILSCALE_IP")" == "OK" ]]; then
        EC2_HOST="$TAILSCALE_IP"
        ok "Tailscale ($TAILSCALE_IP) reachable"
    elif [[ "$(ssh_try "$PUBLIC_IP")" == "OK" ]]; then
        EC2_HOST="$PUBLIC_IP"
        ok "Public IP ($PUBLIC_IP) reachable (Tailscale was not)"
    else
        fail "neither Tailscale ($TAILSCALE_IP) nor public ($PUBLIC_IP) reachable"
        echo
        echo "Diagnostics:"
        echo "  - Is Tailscale up on your Mac? Check the menu-bar icon."
        echo "  - Is the EC2 box stopped? Log into AWS console."
        echo "  - SSH key path: $EC2_KEY (perms must be 0400)"
        exit 1
    fi
fi
echo "  using EC2_HOST=$EC2_HOST"

ssh_exec() {
    ssh -i "$EC2_KEY" -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
        "$EC2_USER@$EC2_HOST" "$@"
}

# Quick sanity: REPO_REMOTE exists.
ssh_exec "[[ -d $REPO_REMOTE ]]" || {
    fail "REPO_REMOTE=$REPO_REMOTE does not exist on EC2"
    exit 1
}

# ---- 1) file mtime — proves rsync overwrote strategy.py recently
echo
echo "===== 1) Strategy.py mtime ====="
MTIME_OUT=$(ssh_exec "stat -c '%Y %y' $REPO_REMOTE/polymarket_strat/domain/weather/strategy.py")
echo "  $MTIME_OUT"
MTIME_EPOCH=$(echo "$MTIME_OUT" | awk '{print $1}')
NOW_EPOCH=$(ssh_exec "date +%s")
AGE_SEC=$(( NOW_EPOCH - MTIME_EPOCH ))
AGE_MIN=$(( AGE_SEC / 60 ))
if (( AGE_SEC < 21600 )); then  # < 6 hours
    ok "modified $AGE_MIN min ago — recent enough"
else
    fail "modified $AGE_MIN min ago — older than 6h, did rsync skip the file?"
fi

# ---- 2) source constants present
echo
echo "===== 2) Source constants on EC2 ====="
CHECKS=(
    "_PLAN_B_HIGH_P_CAP: float = 0.80"
    "_PLAN_B_HIGH_P_EDGE_TRIGGER: float = 0.15"
    "_NARROW_BRACKET_WIDTH_F: float = 2.0"
    "_NARROW_BRACKET_NO_MAX_P: float = 0.75"
    "narrow_bracket_no_cap"
)
for needle in "${CHECKS[@]}"; do
    # Use fgrep-style fixed-string match to avoid regex meta on the colons.
    if ssh_exec "grep -F -q '$needle' $REPO_REMOTE/polymarket_strat/domain/weather/strategy.py"; then
        ok "$needle"
    else
        fail "MISSING: $needle"
    fi
done

# ---- 3) tests file present + green
echo
echo "===== 3) Pytest on EC2 ====="
if ssh_exec "test -f $REPO_REMOTE/tests/test_plan_b_cap.py"; then
    ok "tests/test_plan_b_cap.py exists"
else
    fail "tests/test_plan_b_cap.py MISSING — was rsync excluding it?"
fi

# Try venv first, fall back to system python.
PYTEST_CMD="cd $REPO_REMOTE && (venv/bin/python -m pytest tests/test_plan_b_cap.py --tb=line 2>&1 || \
                                 python3 -m pytest tests/test_plan_b_cap.py --tb=line 2>&1) | tail -10"
PYTEST_OUT=$(ssh_exec "$PYTEST_CMD" || true)
echo "$PYTEST_OUT" | sed 's/^/    /'
if echo "$PYTEST_OUT" | grep -qE "8 passed"; then
    ok "all 8 plan_b_cap tests passed remotely"
else
    fail "plan_b_cap tests did NOT all pass remotely"
fi

# ---- 4) runtime constant values
echo
echo "===== 4) Runtime values (Python import) ====="
RUNTIME_OUT=$(ssh_exec "cd $REPO_REMOTE && (venv/bin/python || python3) -c '
from polymarket_strat.domain.weather import strategy as ws
print(f\"plan_b_p_cap={ws._PLAN_B_HIGH_P_CAP}\")
print(f\"plan_b_edge_trig={ws._PLAN_B_HIGH_P_EDGE_TRIGGER}\")
print(f\"narrow_width={ws._NARROW_BRACKET_WIDTH_F}\")
print(f\"narrow_max_p={ws._NARROW_BRACKET_NO_MAX_P}\")
'" 2>&1 || true)
echo "$RUNTIME_OUT" | sed 's/^/    /'
echo "$RUNTIME_OUT" | grep -q "plan_b_p_cap=0.8"      && ok "plan_b_p_cap=0.80"      || fail "plan_b_p_cap wrong"
echo "$RUNTIME_OUT" | grep -q "plan_b_edge_trig=0.15" && ok "plan_b_edge_trig=0.15"  || fail "plan_b_edge_trig wrong"
echo "$RUNTIME_OUT" | grep -q "narrow_width=2.0"      && ok "narrow_width=2.0"       || fail "narrow_width wrong"
echo "$RUNTIME_OUT" | grep -q "narrow_max_p=0.75"     && ok "narrow_max_p=0.75"      || fail "narrow_max_p wrong"

# ---- 5) DB sanity — any post-deploy rows that should have been blocked?
echo
echo "===== 5) DB sanity — rows since strategy.py mtime ====="
DEPLOY_TS=$(ssh_exec "date -d @$MTIME_EPOCH '+%Y-%m-%d %H:%M:%S' 2>/dev/null || \
                       date -r $MTIME_EPOCH '+%Y-%m-%d %H:%M:%S'")
echo "  deploy timestamp: $DEPLOY_TS"

DB_OUT=$(ssh_exec "cd $REPO_REMOTE && (venv/bin/python || python3) <<'PY'
import sqlite3
import datetime as dt
import os

deploy_ts = '$DEPLOY_TS'
c = sqlite3.connect('data/weather/weather.db')
rows = c.execute('''
    SELECT id, city, side, token_side, model_prob, edge,
           bracket_lower_f, bracket_upper_f, created_at
    FROM trade_history
    WHERE created_at >= ?
    ORDER BY id DESC
''', (deploy_ts,)).fetchall()
print(f'rows_since_deploy={len(rows)}')

violations = []
for r in rows:
    rid, city, side, tok, p_mod, edge, lo, up, created = r
    p = float(p_mod or 0)
    e = float(edge or 0)
    width = float(up or 0) - float(lo or 0)
    if p > 0.80 and e > 0.15:
        violations.append(('PLAN_B', rid, city, p, e, width, created))
    if (tok == 'NO' or (side and 'NO' in side)) and width < 2.0 and p > 0.75:
        violations.append(('NARROW_NO', rid, city, p, e, width, created))

if violations:
    print(f'POST_DEPLOY_VIOLATIONS={len(violations)}')
    for kind, rid, city, p, e, w, ts in violations[:10]:
        print(f'  {kind} #{rid} {city} p={p:.3f} edge={e:.3f} width={w:.1f}F created={ts}')
else:
    print('POST_DEPLOY_VIOLATIONS=0')
PY
" 2>&1 || true)
echo "$DB_OUT" | sed 's/^/    /'
if echo "$DB_OUT" | grep -q "POST_DEPLOY_VIOLATIONS=0"; then
    ok "no post-deploy rows violate the new gates"
elif echo "$DB_OUT" | grep -qE "POST_DEPLOY_VIOLATIONS=[1-9]"; then
    fail "post-deploy rows match new-gate violation pattern — patch isn't firing"
else
    fail "DB query did not return expected sentinel — see output above"
fi

# ---- summary
echo
echo "===== Summary ====="
if (( FAILS == 0 )); then
    echo "  ALL GREEN — patch is live on EC2 (host=$EC2_HOST)"
    exit 0
else
    echo "  $FAILS check(s) failed — investigate above"
    exit 1
fi
