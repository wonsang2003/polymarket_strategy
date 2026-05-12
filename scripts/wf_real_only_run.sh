#!/bin/bash
# Walk-forward backtest with D-90 real-forecasts-only training window.
# Patches backtest.py to add --train-window-days flag, runs, restores.
set -euo pipefail

cd /home/ubuntu/polymarket

echo "=== 1. Backup backtest.py ==="
cp tools/walk_forward/backtest.py tools/walk_forward/backtest.py.bak

echo "=== 2. Patch: add --train-window-days flag + filter ==="
python3 <<'PY'
import re
p = "tools/walk_forward/backtest.py"
src = open(p).read()

# 1) Add argument parser line
old = '    parser.add_argument("--lead-hours", type=int, default=24, help="Forecast lead to use (default 24)")'
new = old + '\n    parser.add_argument("--train-window-days", type=int, default=None, help="Restrict training to last N days of errors (e.g. 90 for real-forecasts-only).")'
assert old in src, "lead-hours arg not found"
src = src.replace(old, new)

# 2) Plumb to walk_forward_city_model: add to function signature
old = "def walk_forward_city_model("
src = src.replace(old, old)  # noop, just verifying

# 3) Replace the train filter
old_filter = "        train_rows = [(e, r) for (e, dd, r) in errors if dd < d]"
new_filter = """        train_window_days = locals().get("train_window_days", None)
        if train_window_days is not None:
            cutoff_d = d - timedelta(days=train_window_days)
            train_rows = [(e, r) for (e, dd, r) in errors if cutoff_d <= dd < d]
        else:
            train_rows = [(e, r) for (e, dd, r) in errors if dd < d]"""
assert old_filter in src, "train filter not found"
src = src.replace(old_filter, new_filter)

# 4) Add train_window_days to walk_forward_city_model signature + pass-through
# Function signature: find the params block
sig_old = """    min_train: int = 30,"""
sig_new = """    min_train: int = 30,
    train_window_days: int | None = None,"""
src = src.replace(sig_old, sig_new, 1)

# 5) Pass through in main()
call_old = "                    min_train=args.min_train,"
call_new = """                    min_train=args.min_train,
                    train_window_days=args.train_window_days,"""
src = src.replace(call_old, call_new)

open(p, "w").write(src)
print("Patched OK")
PY

echo "=== 3. Run walk-forward: 24h lead, 90d window, all cities/models ==="
mkdir -p reports
/home/ubuntu/polymarket/venv/bin/python tools/walk_forward/backtest.py \
    --all-cities --all-models \
    --lead-hours 24 \
    --train-window-days 90 \
    --min-train 20 \
    --start 2026-02-15 --end 2026-04-25 \
    --csv tools/walk_forward/last_run_24h_real90.csv 2>&1 | tail -40

echo "=== 4. Restore original backtest.py ==="
mv tools/walk_forward/backtest.py.bak tools/walk_forward/backtest.py

echo "=== 5. CSV stats ==="
ls -la tools/walk_forward/last_run_24h_real90.csv
echo "Row count:"
wc -l tools/walk_forward/last_run_24h_real90.csv

echo "DONE"
