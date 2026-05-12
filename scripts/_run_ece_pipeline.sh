#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/polymarket

echo "--- run compute_per_city_ece against todays honest_ece_report ---"
venv/bin/python scripts/compute_per_city_ece.py 2>&1 | tail -35

echo
echo "--- reload crontab from new file ---"
crontab deploy/cron_jobs.crontab
echo done. New crontab:
crontab -l | grep -E "ece|reliability" | head
