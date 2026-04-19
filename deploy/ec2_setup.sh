#!/usr/bin/env bash
# Polymarket EC2-side setup script.
# Runs on EC2 after rsync + DB copy. Idempotent — safe to re-run.
#
# Steps:
#   1. Verify file layout & venv
#   2. Verify DB (integrity, row counts)
#   3. Create logs/ directories
#   4. Smoke test: polymarket-strat doctor
#   5. Install cron jobs
#   6. Install systemd service for Streamlit dashboard
#   7. Print Tailscale auth instructions (interactive — user must run sudo tailscale up)
#
# Usage (on EC2):
#   bash deploy/ec2_setup.sh
#
set -euo pipefail

REPO_ROOT="/home/ubuntu/polymarket"
VENV_PY="$REPO_ROOT/venv/bin/python"
VENV_BIN="$REPO_ROOT/venv/bin"

cd "$REPO_ROOT"

step() { echo; echo "===== $* ====="; }

# ---- 1. layout
step "1) Repo layout check"
for f in .env polymarket_strat/main.py polymarket_strat/pyproject.toml \
         tools/dashboard/app.py scripts/backup_db.py \
         deploy/cron_jobs.crontab deploy/polymarket-dashboard.service; do
    if [[ -f "$REPO_ROOT/$f" ]]; then
        echo "  OK  $f"
    else
        echo "  MISSING  $f" >&2; exit 1
    fi
done

# venv
if [[ ! -x "$VENV_PY" ]]; then
    echo "venv missing — recreate via: python3 -m venv venv && source venv/bin/activate && pip install -e ./polymarket_strat" >&2
    exit 1
fi

# CLI
if ! "$VENV_BIN/polymarket-strat" --help >/dev/null 2>&1; then
    echo "polymarket-strat CLI broken" >&2; exit 1
fi
echo "  OK  polymarket-strat CLI"

# ---- 2. DB
step "2) DB sanity check"
if [[ ! -f "$REPO_ROOT/data/weather/weather.db" ]]; then
    echo "data/weather/weather.db missing — scp it from Mac first" >&2; exit 1
fi

"$VENV_PY" - <<'PY'
import sqlite3, os
db = "/home/ubuntu/polymarket/data/weather/weather.db"
conn = sqlite3.connect(db)
print(f"  size: {os.path.getsize(db)/1024/1024:.2f} MB")
print(f"  integrity: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
for tbl in ('forecasts', 'observations', 'forecast_errors',
            'error_distributions', 'trade_history'):
    n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl:24s} {n:>10,}")
conn.close()
PY

# ---- 3. logs / runtime dirs
step "3) Create runtime directories"
mkdir -p "$REPO_ROOT/logs"
mkdir -p "$REPO_ROOT/runtime"
mkdir -p "$REPO_ROOT/reports"
mkdir -p "$REPO_ROOT/data/weather/backups"
mkdir -p "$REPO_ROOT/tools/dashboard/logs"
mkdir -p "$REPO_ROOT/tools/lag_monitor/logs"
echo "  OK"

# ---- 4. smoke test
step "4) Smoke test (polymarket-strat doctor)"
"$VENV_BIN/polymarket-strat" doctor || {
    echo "doctor failed — proceed at your own risk" >&2
}

# ---- 5. cron
step "5) Install cron jobs"
# preserve any unrelated existing entries; replace the polymarket block
TMPCRON=$(mktemp)
crontab -l 2>/dev/null | grep -v "polymarket" > "$TMPCRON" || true
cat "$REPO_ROOT/deploy/cron_jobs.crontab" >> "$TMPCRON"
crontab "$TMPCRON"
rm "$TMPCRON"
echo "  Installed:"
crontab -l | grep -E "(polymarket|TZ|SHELL|PATH)" | sed 's/^/    /'

# ---- 6. systemd dashboard service
step "6) Install systemd dashboard service"
sudo cp "$REPO_ROOT/deploy/polymarket-dashboard.service" /etc/systemd/system/polymarket-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable polymarket-dashboard.service
sudo systemctl restart polymarket-dashboard.service
sleep 3
sudo systemctl status polymarket-dashboard.service --no-pager | head -15

echo
echo "  Verifying dashboard responds on :8501 ..."
if curl -sf -o /dev/null --max-time 8 http://127.0.0.1:8501/_stcore/health; then
    echo "  OK  dashboard healthy"
else
    echo "  WARN dashboard not responding yet — check: journalctl -u polymarket-dashboard -e --no-pager"
fi

# ---- 7. Tailscale info
step "7) Tailscale (manual step — interactive)"
if command -v tailscale >/dev/null 2>&1; then
    if ! sudo tailscale status >/dev/null 2>&1; then
        echo "  Tailscale installed but not joined to a tailnet."
        echo "  Run this and click the printed URL on your phone/Mac:"
        echo "      sudo tailscale up --ssh --hostname=polymarket-paper"
        echo
        echo "  Once joined, find the tailnet IP:"
        echo "      tailscale ip -4"
        echo
        echo "  Then phone -> http://<tailnet-ip>:8501"
    else
        echo "  Tailscale already up."
        echo "  IP: $(tailscale ip -4 2>/dev/null | head -1)"
    fi
else
    echo "  Tailscale not installed — skip"
fi

step "DONE"
echo "Next: lock down EC2 security group (close 0.0.0.0/0 :22 once Tailscale SSH works, keep :8501 closed always)"
