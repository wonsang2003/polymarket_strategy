#!/usr/bin/env bash
cd /home/ubuntu/polymarket
nohup venv/bin/python scripts/tail_ece_audit.py > /tmp/tail_ece.log 2>&1 &
echo "PID=$!"
