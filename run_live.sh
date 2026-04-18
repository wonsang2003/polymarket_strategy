#!/usr/bin/env zsh
set -euo pipefail

cd "$(dirname "$0")"
python3 -m polymarket_strat.main --env-file .env --state-file runtime/portfolio_state.json plan
python3 -m polymarket_strat.main --env-file .env --state-file runtime/portfolio_state.json execute --mode live --confirm-live
