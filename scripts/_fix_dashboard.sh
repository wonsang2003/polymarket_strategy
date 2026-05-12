#!/usr/bin/env bash
set -e
cd /home/ubuntu/polymarket

echo "--- 1. Install streamlit-autorefresh ---"
venv/bin/pip install --quiet streamlit-autorefresh
venv/bin/pip show streamlit-autorefresh 2>&1 | head -2

echo
echo "--- 2. Verify dashboard app imports cleanly ---"
venv/bin/python -c "
from streamlit_autorefresh import st_autorefresh
print('st_autorefresh import OK')
"

echo
echo "--- 3. Restart streamlit dashboard process ---"
# Kill existing streamlit
pkill -f 'streamlit run.*tools/dashboard/app.py' 2>/dev/null || true
sleep 2

# Verify killed
if pgrep -f 'streamlit run.*tools/dashboard/app.py' > /dev/null; then
    echo "WARN: streamlit still running, force kill"
    pkill -9 -f 'streamlit run.*tools/dashboard/app.py' || true
    sleep 1
fi

# Restart
mkdir -p tools/dashboard/logs
nohup venv/bin/streamlit run tools/dashboard/app.py \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    --server.headless true \
    --server.fileWatcherType none \
    --browser.gatherUsageStats false \
    > tools/dashboard/logs/dashboard.stdout \
    2> tools/dashboard/logs/dashboard.stderr &
NEW_PID=$!
echo "started streamlit PID=$NEW_PID"
sleep 4

echo
echo "--- 4. Verify dashboard responding ---"
if pgrep -f 'streamlit run.*tools/dashboard/app.py' > /dev/null; then
    echo "OK: streamlit process running"
else
    echo "FAIL: streamlit not running"
    tail -30 tools/dashboard/logs/dashboard.stderr
    exit 1
fi

# HTTP check
HTTP_CODE=$(curl -sS -o /dev/null -w '%{http_code}' http://localhost:8501/_stcore/health 2>&1 || echo "ERR")
echo "health check HTTP $HTTP_CODE"

echo
echo "--- 5. Verify auto-refresh import ---"
grep -c 'st_autorefresh' tools/dashboard/app.py
echo "should be >= 2"
