#!/usr/bin/env bash
# Mac-side one-shot deploy: rsync repo + scp DB + run remote setup.
# Idempotent — safe to re-run after code changes (skips DB copy unless --refresh-db).
#
# Usage:
#   bash deploy/upload_to_ec2.sh
#   bash deploy/upload_to_ec2.sh --refresh-db    # force re-copy weather.db
#   bash deploy/upload_to_ec2.sh --skip-setup    # only push code, don't re-run setup
#
set -euo pipefail

# ---- config
EC2_HOST="${EC2_HOST:-54.180.64.168}"
EC2_USER="${EC2_USER:-ubuntu}"
EC2_KEY="${EC2_KEY:-$HOME/.ssh/polymarket-seoul.pem}"
REPO_LOCAL="${REPO_LOCAL:-$HOME/Downloads/polymarket_strat}"
REPO_REMOTE="${REPO_REMOTE:-/home/ubuntu/polymarket}"

REFRESH_DB=0
SKIP_SETUP=0
for arg in "$@"; do
    case "$arg" in
        --refresh-db) REFRESH_DB=1 ;;
        --skip-setup) SKIP_SETUP=1 ;;
        --help|-h) head -10 "$0"; exit 0 ;;
        *) echo "unknown arg: $arg"; exit 1 ;;
    esac
done

# ---- pre-flight
echo "===== 0) Pre-flight ====="
[[ -f "$EC2_KEY" ]] || { echo "missing key: $EC2_KEY"; exit 1; }
[[ $(stat -f "%Lp" "$EC2_KEY" 2>/dev/null || stat -c "%a" "$EC2_KEY") == "400" ]] || \
    { echo "fixing key perms"; chmod 400 "$EC2_KEY"; }
[[ -d "$REPO_LOCAL" ]] || { echo "missing repo: $REPO_LOCAL"; exit 1; }
[[ -f "$REPO_LOCAL/data/weather/weather.db" ]] || \
    { echo "missing DB: $REPO_LOCAL/data/weather/weather.db"; exit 1; }

# Connectivity
ssh -i "$EC2_KEY" -o ConnectTimeout=10 -o BatchMode=yes \
    "$EC2_USER@$EC2_HOST" "echo ok" >/dev/null || \
    { echo "EC2 unreachable"; exit 1; }
echo "  EC2 reachable"

cd "$REPO_LOCAL"

# ---- 1) stop local processes that may be writing to DB (Mac side)
echo
echo "===== 1) Stop Mac-side writers (best-effort) ====="
# Lag monitor runs under plain `nohup caffeinate -i python3 …` (not launchd —
# macOS Login Items approval flow blocks new user LaunchAgents without a
# signed/notarized bundle, and for 30 days of paper trading that ceremony
# isn't worth it). Kill it here; step 6 below respawns it under a fresh
# caffeinate so the power-management assertion is re-armed post-deploy.
pkill -f "lag_monitor/monitor.py" 2>/dev/null \
    && echo "  stopped lag_monitor (will be restarted in step 6)" \
    || echo "  lag_monitor not running"
# Dashboard DOES run under launchd (it was approved in an earlier session, so
# it survives reboots). Kickstart bounces it so any held DB read-connection
# drops cleanly before we overwrite the file in step 4.
launchctl kickstart -k "gui/$(id -u)/com.wonsang.polymarket-dashboard" 2>/dev/null \
    && echo "  bounced dashboard agent" || echo "  dashboard agent not loaded"

# ---- 2) WAL checkpoint
echo
echo "===== 2) Checkpoint Mac DB ====="
python3 - <<'PY'
import sqlite3, os
db = "data/weather/weather.db"
conn = sqlite3.connect(db)
r = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
print(f"  checkpoint: {r}")
print(f"  integrity:  {ic}")
print(f"  size:       {os.path.getsize(db)/1024/1024:.2f} MB")
for tbl in ('forecasts','observations','forecast_errors','error_distributions','trade_history'):
    n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl:24s} {n:>10,}")
conn.close()
PY

# ---- 3) rsync code
echo
echo "===== 3) Sync code to EC2 ====="
rsync -az --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'venv' \
  --exclude '.venv' \
  --exclude 'data/weather/*.db*' \
  --exclude 'data/weather/weather_pretrim_*' \
  --exclude 'data/weather/backups' \
  --exclude 'reports/*.html' \
  --exclude 'tools/dashboard/logs' \
  --exclude 'tools/lag_monitor/logs' \
  --exclude 'logs' \
  --exclude 'runtime' \
  --exclude '.DS_Store' \
  --exclude '.pytest_cache' \
  --exclude 'polymarket_strat.egg-info' \
  -e "ssh -i $EC2_KEY" \
  ./ "$EC2_USER@$EC2_HOST:$REPO_REMOTE/"
echo "  code synced"

# ---- 4) copy DB
echo
echo "===== 4) Copy DB to EC2 ====="
NEED_COPY=$REFRESH_DB
if [[ $NEED_COPY -eq 0 ]]; then
    REMOTE_SIZE=$(ssh -i "$EC2_KEY" "$EC2_USER@$EC2_HOST" \
        "stat -c%s $REPO_REMOTE/data/weather/weather.db 2>/dev/null || echo 0")
    LOCAL_SIZE=$(stat -f%z data/weather/weather.db 2>/dev/null || stat -c%s data/weather/weather.db)
    if [[ "$REMOTE_SIZE" == "$LOCAL_SIZE" && "$REMOTE_SIZE" != "0" ]]; then
        echo "  DB size matches ($LOCAL_SIZE bytes) — skipping. Use --refresh-db to force."
    else
        NEED_COPY=1
    fi
fi

if [[ $NEED_COPY -eq 1 ]]; then
    # Clear any stale .tmp from a previous aborted deploy before scp starts —
    # otherwise scp errors can leave an old file in place that our rename then
    # promotes to be the main DB, silently deploying old bytes.
    ssh -i "$EC2_KEY" "$EC2_USER@$EC2_HOST" \
        "mkdir -p $REPO_REMOTE/data/weather && \
         rm -f $REPO_REMOTE/data/weather/weather.db.tmp"
    scp -i "$EC2_KEY" -p \
        data/weather/weather.db \
        "$EC2_USER@$EC2_HOST:$REPO_REMOTE/data/weather/weather.db.tmp"
    # Atomic rename + nuke stale WAL/SHM from the prior DB. Without the rm,
    # SQLite on EC2 opens the new main file next to old sidecar journals,
    # tries to replay them, and reports "database disk image is malformed".
    # The checkpoint on the Mac side (step 2) already flushed WAL into the
    # main file, so the new DB is self-contained and the sidecars can go.
    ssh -i "$EC2_KEY" "$EC2_USER@$EC2_HOST" \
        "mv $REPO_REMOTE/data/weather/weather.db.tmp $REPO_REMOTE/data/weather/weather.db && \
         rm -f $REPO_REMOTE/data/weather/weather.db-wal $REPO_REMOTE/data/weather/weather.db-shm"
    # Hash verify: if Mac and EC2 don't match, fail loud instead of silently
    # handing a corrupt DB to ec2_setup.sh.
    LOCAL_HASH=$(shasum -a 256 data/weather/weather.db 2>/dev/null | awk '{print $1}' || \
                 sha256sum data/weather/weather.db | awk '{print $1}')
    REMOTE_HASH=$(ssh -i "$EC2_KEY" "$EC2_USER@$EC2_HOST" \
        "sha256sum $REPO_REMOTE/data/weather/weather.db" | awk '{print $1}')
    if [[ "$LOCAL_HASH" != "$REMOTE_HASH" ]]; then
        echo "  DB HASH MISMATCH after copy:"
        echo "    mac: $LOCAL_HASH"
        echo "    ec2: $REMOTE_HASH"
        echo "  refusing to continue — run deploy again with --refresh-db"
        exit 1
    fi
    echo "  DB copied (stale WAL/SHM cleared, hash verified)"
fi

# ---- 5) run EC2 setup
if [[ $SKIP_SETUP -eq 0 ]]; then
    echo
    echo "===== 5) Run EC2 setup script ====="
    chmod +x deploy/ec2_setup.sh
    ssh -i "$EC2_KEY" -t "$EC2_USER@$EC2_HOST" \
        "chmod +x $REPO_REMOTE/deploy/ec2_setup.sh && bash $REPO_REMOTE/deploy/ec2_setup.sh"
fi

# ---- 6) Restart local lag monitor under caffeinate
# caffeinate -i must wrap python DIRECTLY. Wrapping the start_lag_monitor.sh
# helper instead would be useless — that script backgrounds python via nohup
# then exits, so caffeinate's direct child would exit immediately and the
# PreventUserIdleSystemSleep assertion would drop, letting the Mac sleep and
# stall the poller. Spawning python as caffeinate's direct child keeps the
# assertion asserted for the lifetime of the monitor.
echo
echo "===== 6) Restart lag monitor (nohup + caffeinate) ====="
pkill -f "tools/lag_monitor/monitor.py" 2>/dev/null && sleep 1 || true
mkdir -p tools/lag_monitor/logs
nohup /usr/bin/caffeinate -i python3 tools/lag_monitor/monitor.py \
    --cities seoul,tokyo,nyc,london,la \
    >> tools/lag_monitor/logs/monitor.stdout \
    2>> tools/lag_monitor/logs/monitor.stderr &
MONITOR_PID=$!
disown
sleep 2
if kill -0 "$MONITOR_PID" 2>/dev/null; then
    echo "  lag monitor running as PID $MONITOR_PID (caffeinate-wrapped)"
else
    echo "  WARN: lag monitor failed to start — check tools/lag_monitor/logs/monitor.stderr"
fi

echo
echo "===== ALL DONE ====="
echo "Manual remaining steps (one-time):"
echo "  1. SSH to EC2:    ssh -i $EC2_KEY $EC2_USER@$EC2_HOST"
echo "  2. Tailscale:     sudo tailscale up --ssh --hostname=polymarket-paper"
echo "  3. Phone test:    http://<tailnet-ip>:8501"
echo "  4. Tighten SG:    AWS console -> SG -> remove SSH 0.0.0.0/0 once Tailscale SSH works"
