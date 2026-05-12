#!/usr/bin/env bash
# Reproduce the polymarket-strat vs python -m divergence, if it still exists.
# Run both back-to-back; check for pycache, file-path divergence, module identity.
set -eu
REPO=/home/ubuntu/polymarket
cd "$REPO"

echo "===== A) Where does each path load main.py from? ====="
"$REPO/venv/bin/python3" - <<'PY'
import importlib.util, sys
spec = importlib.util.find_spec("polymarket_strat.main")
print(f"  import spec origin: {spec.origin}")
import polymarket_strat.main as m
print(f"  module __file__:    {m.__file__}")
print(f"  module __cached__:  {m.__cached__}")
# Show mtime of each
import os
for p in [m.__file__, m.__cached__]:
    if p and os.path.exists(p):
        print(f"    mtime({p}) = {os.path.getmtime(p):.0f}")
PY

echo
echo "===== B) pycache state in editable source dir ====="
find "$REPO/polymarket_strat" -name "__pycache__" -type d 2>/dev/null | while read d; do
  echo "  $d"
  ls -la "$d" 2>/dev/null | grep "main\." | head -3
done

echo
echo "===== C) Entry-point script content ====="
cat "$REPO/venv/bin/polymarket-strat"

echo
echo "===== D) Compare file hashes via both loaders ====="
"$REPO/venv/bin/python3" - <<'PY'
import hashlib, polymarket_strat.main as m
with open(m.__file__, "rb") as f:
    h = hashlib.sha256(f.read()).hexdigest()
print(f"  import-path main.py sha256: {h}")
print(f"  _resolve_via_polymarket id: {id(m._resolve_via_polymarket)}")
import inspect
src = inspect.getsource(m._resolve_via_polymarket)
# Show just the "gate" decision lines
for line in src.splitlines():
    if "0.99" in line or "closed" in line or "accepting" in line:
        print(f"    {line.rstrip()}")
PY

echo
echo "===== E) Run both back-to-back and diff the output ====="
# Count current open positions
OPEN_BEFORE=$(sqlite3 "$REPO/data/weather/weather.db" "SELECT COUNT(*) FROM trade_history WHERE outcome IS NULL")
echo "  open positions before: $OPEN_BEFORE"

echo
echo "  --- Run 1: polymarket-strat settle --auto"
OUT1=$("$REPO/venv/bin/polymarket-strat" settle --auto 2>&1)
echo "$OUT1" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f'    settled_count={d[\"settled_count\"]} total_pnl={d[\"total_pnl\"]} errors={len(d[\"errors\"])}')"
OPEN_MID=$(sqlite3 "$REPO/data/weather/weather.db" "SELECT COUNT(*) FROM trade_history WHERE outcome IS NULL")
echo "    open after run 1: $OPEN_MID"

echo
echo "  --- Run 2: python -m polymarket_strat.main settle --auto"
OUT2=$("$REPO/venv/bin/python" -m polymarket_strat.main settle --auto 2>&1)
echo "$OUT2" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f'    settled_count={d[\"settled_count\"]} total_pnl={d[\"total_pnl\"]} errors={len(d[\"errors\"])}')"
OPEN_AFTER=$(sqlite3 "$REPO/data/weather/weather.db" "SELECT COUNT(*) FROM trade_history WHERE outcome IS NULL")
echo "    open after run 2: $OPEN_AFTER"

echo
echo "===== F) Full output comparison (first 300 chars each) ====="
echo "  polymarket-strat:"
echo "$OUT1" | head -c 400
echo
echo "  python -m:"
echo "$OUT2" | head -c 400
echo
