#!/usr/bin/env zsh
# Start the Polymarket ↔ weather-forecast lag monitor in the background.
#
# Records every forecast change and every price change to a JSONL event log,
# which `tools/lag_monitor/analyze.py` joins to measure how long it takes
# Polymarket to reprice after a model update.
#
# Usage:
#     ./scripts/start_lag_monitor.sh                    # all default cities
#     ./scripts/start_lag_monitor.sh seoul,nyc,london   # custom city list
#     ./scripts/start_lag_monitor.sh --all-cities       # every CITY_REGISTRY entry
#
# Stop:
#     pkill -f "tools/lag_monitor/monitor.py"
#
# Inspect:
#     tail -f tools/lag_monitor/logs/monitor.stdout
#     python tools/lag_monitor/analyze.py
#
set -euo pipefail

cd "$(dirname "$0")/.."

LOG_DIR="tools/lag_monitor/logs"
mkdir -p "${LOG_DIR}"

STDOUT="${LOG_DIR}/monitor.stdout"
STDERR="${LOG_DIR}/monitor.stderr"

# Guard against double-start — if a process matching the monitor script is
# already running, bail out loudly.
if pgrep -f "tools/lag_monitor/monitor.py" > /dev/null; then
    echo "[start_lag_monitor] already running — PIDs:"
    pgrep -lf "tools/lag_monitor/monitor.py"
    echo "Stop with: pkill -f tools/lag_monitor/monitor.py"
    exit 1
fi

ARGS=()
if [[ $# -eq 0 ]]; then
    # Default: Asian + European + US markets we actively trade
    ARGS+=(--cities "seoul,tokyo,nyc,london,la")
elif [[ "$1" == "--all-cities" ]]; then
    ARGS+=(--all-cities)
else
    ARGS+=(--cities "$1")
fi

echo "[start_lag_monitor] launching with ARGS=${ARGS[*]}"
echo "[start_lag_monitor] stdout → ${STDOUT}"
echo "[start_lag_monitor] stderr → ${STDERR}"

nohup python3 tools/lag_monitor/monitor.py "${ARGS[@]}" \
    >> "${STDOUT}" 2>> "${STDERR}" &

PID=$!
echo "[start_lag_monitor] started PID=${PID}"
echo "[start_lag_monitor] analyze anytime: python tools/lag_monitor/analyze.py"
