#!/usr/bin/env bash
# EC2-side recovery: restore trade_history from Apr 22 backup, clear fuse_hidden
# debris, bounce the dashboard. Idempotent. Exits 0 on success.

set -u  # NOT -e on purpose — we want diagnostic lines even if pkill has no match
REPO=/home/ubuntu/polymarket
DB=$REPO/data/weather/weather.db
BACKUP=$REPO/data/weather/backups/weather_20260422_133001.db
TS=$(date +%Y%m%d_%H%M%S)

echo "===== 1) Stop dashboard (release DB handle) ====="
pkill -f "streamlit run" 2>/dev/null && echo "  streamlit stopped" || echo "  streamlit not running"
sleep 2

echo
echo "===== 2) Pause cron (prevent hourly autotrade racing restore) ====="
crontab -l > /tmp/cron.bak.$TS 2>/dev/null || true
crontab -r 2>/dev/null || true
echo "  cron paused (backup at /tmp/cron.bak.$TS)"

echo
echo "===== 3) Snapshot current DB before overwrite ====="
if [ -f "$DB" ]; then
    cp "$DB" "$REPO/data/weather/weather.pre_recovery_${TS}.db"
    echo "  snapshot: weather.pre_recovery_${TS}.db"
    CUR_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM trade_history;" 2>/dev/null || echo "?")
    echo "  current trade_history rows: $CUR_ROWS"
fi

echo
echo "===== 4) Verify backup ====="
if [ ! -f "$BACKUP" ]; then
    echo "  MISSING BACKUP: $BACKUP"
    echo "  ABORT"
    exit 1
fi
BACKUP_ROWS=$(sqlite3 "$BACKUP" "SELECT COUNT(*) FROM trade_history;")
BACKUP_OPEN=$(sqlite3 "$BACKUP" "SELECT COUNT(*) FROM trade_history WHERE outcome IS NULL;")
BACKUP_SETTLED=$(sqlite3 "$BACKUP" "SELECT COUNT(*) FROM trade_history WHERE outcome IS NOT NULL;")
echo "  backup rows: $BACKUP_ROWS total / $BACKUP_OPEN open / $BACKUP_SETTLED settled"

echo
echo "===== 5) Restore backup over weather.db ====="
cp "$BACKUP" "$DB"
chown ubuntu:ubuntu "$DB" 2>/dev/null || true
echo "  restored"

echo
echo "===== 6) Clear stale WAL/SHM + fuse_hidden debris ====="
rm -f "$REPO/data/weather/weather.db-wal" "$REPO/data/weather/weather.db-shm"
find "$REPO/data/weather" -maxdepth 1 -name '.fuse_hidden*' -print -delete 2>/dev/null || true
echo "  cleared"

echo
echo "===== 7) Verify restored DB ====="
sqlite3 "$DB" "SELECT COUNT(*) total, SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) open, SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) settled FROM trade_history;" | awk -F '|' '{printf "  total=%s open=%s settled=%s\n", $1, $2, $3}'
INTEG=$(sqlite3 "$DB" "PRAGMA integrity_check;")
echo "  integrity: $INTEG"

echo
echo "===== 8) Restart dashboard ====="
cd $REPO
mkdir -p tools/dashboard/logs
nohup $REPO/venv/bin/streamlit run tools/dashboard/app.py \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    --server.headless true \
    --server.fileWatcherType none \
    --browser.gatherUsageStats false \
    >> tools/dashboard/logs/dashboard.stdout \
    2>> tools/dashboard/logs/dashboard.stderr &
disown
sleep 4
if curl -sf -o /dev/null --max-time 6 http://127.0.0.1:8501/_stcore/health; then
    echo "  dashboard healthy on :8501"
else
    echo "  WARN dashboard not responding — tail tools/dashboard/logs/dashboard.stderr"
fi

echo
echo "===== 9) Restore cron ====="
crontab /tmp/cron.bak.$TS 2>/dev/null && echo "  cron restored" || echo "  WARN cron not restored — run: crontab /tmp/cron.bak.$TS"
crontab -l 2>/dev/null | grep -E "polymarket|autotrade|calibrate|backup" | head -6 | sed 's/^/    /'

echo
echo "===== RECOVERY DONE ====="
exit 0
