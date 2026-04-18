#!/usr/bin/env zsh
# Install and load the polymarket-dashboard launchd agent.
#
# Fills in the plist template with:
#   - absolute path to this repo (auto-detected)
#   - your tailnet IP (either auto-detected or --ip override)
#   - path to streamlit binary (from `which streamlit`)
#
# Then copies it to ~/Library/LaunchAgents/ and loads it. The dashboard will
# auto-start at login and auto-restart on crash.
#
# Usage:
#     ./deploy/install_dashboard_agent.sh                     # auto-detect
#     ./deploy/install_dashboard_agent.sh --ip 100.64.1.23    # explicit IP
#     ./deploy/install_dashboard_agent.sh --uninstall         # remove + unload
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.wonsang.polymarket-dashboard"
TEMPLATE="${REPO_ROOT}/deploy/com.wonsang.polymarket-dashboard.plist.template"
AGENT_DIR="${HOME}/Library/LaunchAgents"
AGENT_PATH="${AGENT_DIR}/${LABEL}.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -f "${AGENT_PATH}" ]]; then
        launchctl unload "${AGENT_PATH}" || true
        rm -f "${AGENT_PATH}"
        echo "[install_dashboard_agent] uninstalled ${LABEL}"
    else
        echo "[install_dashboard_agent] nothing to uninstall at ${AGENT_PATH}"
    fi
    exit 0
fi

# -- resolve tailnet IP ---------------------------------------------------
TAILNET_IP=""
if [[ "${1:-}" == "--ip" && -n "${2:-}" ]]; then
    TAILNET_IP="$2"
else
    # Auto-detect from `tailscale ip -4` (most reliable) or `ifconfig`
    if command -v tailscale >/dev/null 2>&1; then
        TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    fi
    if [[ -z "${TAILNET_IP}" ]]; then
        TAILNET_IP="$(ifconfig | awk '/inet 100\./ {print $2; exit}' || true)"
    fi
fi

if [[ -z "${TAILNET_IP}" ]]; then
    echo "[install_dashboard_agent] could not detect tailnet IP automatically."
    echo "Install Tailscale and log in first, or pass --ip 100.x.y.z explicitly."
    exit 2
fi

# -- resolve streamlit binary --------------------------------------------
STREAMLIT_BIN="$(command -v streamlit || true)"
if [[ -z "${STREAMLIT_BIN}" ]]; then
    echo "[install_dashboard_agent] streamlit not found on PATH."
    echo "Run: pip install streamlit pandas"
    exit 2
fi

# -- ensure log dir exists -----------------------------------------------
mkdir -p "${REPO_ROOT}/tools/dashboard/logs"
mkdir -p "${AGENT_DIR}"

# -- fill in template ----------------------------------------------------
sed \
    -e "s|\${REPO_ROOT}|${REPO_ROOT}|g" \
    -e "s|\${TAILNET_IP}|${TAILNET_IP}|g" \
    -e "s|\${STREAMLIT_BIN}|${STREAMLIT_BIN}|g" \
    "${TEMPLATE}" > "${AGENT_PATH}"

echo "[install_dashboard_agent] wrote ${AGENT_PATH}"
echo "  repo:       ${REPO_ROOT}"
echo "  tailnet IP: ${TAILNET_IP}"
echo "  streamlit:  ${STREAMLIT_BIN}"

# -- reload ---------------------------------------------------------------
launchctl unload "${AGENT_PATH}" 2>/dev/null || true
launchctl load "${AGENT_PATH}"

echo ""
echo "[install_dashboard_agent] loaded. Dashboard should be up in ~5 sec at:"
echo "  http://${TAILNET_IP}:8501"
echo ""
echo "Logs:"
echo "  ${REPO_ROOT}/tools/dashboard/logs/dashboard.stdout"
echo "  ${REPO_ROOT}/tools/dashboard/logs/dashboard.stderr"
echo ""
echo "Check status: launchctl list | grep ${LABEL}"
echo "Uninstall:    ./deploy/install_dashboard_agent.sh --uninstall"
