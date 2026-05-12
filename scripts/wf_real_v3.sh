#!/bin/bash
# v3: Copy backtest.py to a new filename, patch the new copy, run, never touch original.
set -euo pipefail
cd /home/ubuntu/polymarket

BACKTEST_NEW=tools/walk_forward/backtest_real90.py

echo "=== 1. Copy backtest.py -> backtest_real90.py ==="
cp tools/walk_forward/backtest.py "$BACKTEST_NEW"

echo "=== 2. Patch the new copy ==="
python3 <<PY
p = "$BACKTEST_NEW"
src = open(p).read()

# 1) New flag
old1 = '    parser.add_argument("--lead-hours", type=int, default=24, help="Forecast lead to use (default 24)")'
new1 = old1 + '\n    parser.add_argument("--train-window-days", type=int, default=None, help="Restrict training to last N days.")'
assert src.count(old1) == 1, f"old1 count={src.count(old1)}"
src = src.replace(old1, new1)

# 2) Function signature
old2 = "    min_train: int = 30,"
new2 = "    min_train: int = 30,\n    train_window_days: int | None = None,"
assert src.count(old2) == 1, f"old2 count={src.count(old2)}"
src = src.replace(old2, new2)

# 3) Train filter (must use precise context to ensure single match)
old3 = """        # Training set: all errors with obs_date strictly before d
        train_rows = [(e, r) for (e, dd, r) in errors if dd < d]"""
new3 = """        # Training set: errors with obs_date strictly before d (window-bounded)
        if train_window_days is not None:
            cutoff_d = d - timedelta(days=train_window_days)
            train_rows = [(e, r) for (e, dd, r) in errors if cutoff_d <= dd < d]
        else:
            train_rows = [(e, r) for (e, dd, r) in errors if dd < d]"""
assert src.count(old3) == 1, f"old3 count={src.count(old3)}"
src = src.replace(old3, new3)

# 4) Pass-through
old4 = "                    min_train=args.min_train,"
new4 = "                    min_train=args.min_train,\n                    train_window_days=args.train_window_days,"
assert src.count(old4) == 1, f"old4 count={src.count(old4)}"
src = src.replace(old4, new4)

open(p, "w").write(src)
print("Patched 4/4 OK")
PY

echo "=== 3. Verify syntax ==="
python3 -c "import ast; ast.parse(open('$BACKTEST_NEW').read()); print('SYNTAX OK')"

echo "=== 4. Run walk-forward ==="
/home/ubuntu/polymarket/venv/bin/python "$BACKTEST_NEW" \
    --all-cities --all-models \
    --lead-hours 24 \
    --train-window-days 90 \
    --min-train 20 \
    --start 2026-02-15 --end 2026-04-25 \
    --csv tools/walk_forward/last_run_24h_real90.csv 2>&1 | tail -50

echo "=== 5. CSV stats ==="
ls -la tools/walk_forward/last_run_24h_real90.csv
wc -l tools/walk_forward/last_run_24h_real90.csv

echo "=== 6. Cleanup new copy ==="
rm "$BACKTEST_NEW"

echo "DONE"
