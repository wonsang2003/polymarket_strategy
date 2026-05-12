#!/bin/bash
# v2: idempotent — restore from git first, then patch + run.
set -euo pipefail
cd /home/ubuntu/polymarket

echo "=== 1. Skip git restore (file pre-restored via scp) ==="

echo "=== 2. Apply patch (one-shot) ==="
python3 <<'PY'
p = "tools/walk_forward/backtest.py"
src = open(p).read()

# 1) Argument flag
old1 = '    parser.add_argument("--lead-hours", type=int, default=24, help="Forecast lead to use (default 24)")'
new1 = old1 + '\n    parser.add_argument("--train-window-days", type=int, default=None, help="Restrict training to last N days (e.g. 90 for real-forecasts-only).")'
assert old1 in src and src.count(old1) == 1
src = src.replace(old1, new1)

# 2) Function signature: add train_window_days param
old2 = "    min_train: int = 30,"
new2 = "    min_train: int = 30,\n    train_window_days: int | None = None,"
assert old2 in src and src.count(old2) == 1
src = src.replace(old2, new2)

# 3) Train filter: replace single line with conditional
old3 = "        train_rows = [(e, r) for (e, dd, r) in errors if dd < d]"
new3 = """        if train_window_days is not None:
            cutoff_d = d - timedelta(days=train_window_days)
            train_rows = [(e, r) for (e, dd, r) in errors if cutoff_d <= dd < d]
        else:
            train_rows = [(e, r) for (e, dd, r) in errors if dd < d]"""
assert old3 in src and src.count(old3) == 1
src = src.replace(old3, new3)

# 4) Pass-through in main()
old4 = "                    min_train=args.min_train,"
new4 = "                    min_train=args.min_train,\n                    train_window_days=args.train_window_days,"
assert old4 in src and src.count(old4) == 1
src = src.replace(old4, new4)

open(p, "w").write(src)
print("Patched OK (4/4)")
PY

echo "=== 3. Verify patch ==="
python3 -c "import ast; ast.parse(open('tools/walk_forward/backtest.py').read()); print('SYNTAX OK')"

echo "=== 4. Run walk-forward ==="
mkdir -p reports
/home/ubuntu/polymarket/venv/bin/python tools/walk_forward/backtest.py \
    --all-cities --all-models \
    --lead-hours 24 \
    --train-window-days 90 \
    --min-train 20 \
    --start 2026-02-15 --end 2026-04-25 \
    --csv tools/walk_forward/last_run_24h_real90.csv 2>&1 | tail -50

echo "=== 5. Skip restore (will scp clean file separately if needed) ==="

echo "=== 6. CSV stats ==="
ls -la tools/walk_forward/last_run_24h_real90.csv
wc -l tools/walk_forward/last_run_24h_real90.csv

echo "DONE"
