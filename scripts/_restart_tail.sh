#!/usr/bin/env bash
kill 60444 2>/dev/null
sleep 2
cd /home/ubuntu/polymarket
nohup venv/bin/python -u scripts/tail_ece_audit.py > /tmp/tail_ece2.log 2>&1 &
echo "PID=$!"
