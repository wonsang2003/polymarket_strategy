#!/usr/bin/env bash
set -e
cd /home/ubuntu/polymarket
echo "--- import sanity ---"
venv/bin/python -c "
from polymarket_strat.domain.weather.tail_strategy import (
    analyze_tail_brackets, evaluate_tail_bracket,
    NO_SIDE_MAX_P_MODEL, MAX_POSITION_FRACTION, KELLY_FRACTION,
)
from polymarket_strat.domain.weather.nowcast import (
    BracketNowcast, classify_bracket, adaptive_margin_f,
)
print('imports OK')
print(f'NO_SIDE_MAX_P_MODEL={NO_SIDE_MAX_P_MODEL}')
print(f'MAX_POSITION_FRACTION={MAX_POSITION_FRACTION}')
print(f'KELLY_FRACTION={KELLY_FRACTION}')
print(f'adaptive_margin_f(3.0)={adaptive_margin_f(3.0):.2f}')
print(f'BracketNowcast values: {[b.value for b in BracketNowcast]}')
"
echo
echo "--- run_autotrade dry analysis (no execution) ---"
venv/bin/python -c "
from polymarket_strat.main import _run_tail_strategy_pass
from polymarket_strat.config import TradingConstraints, PortfolioState
from polymarket_strat.domain.models import StrategyAnalysis
constraints = TradingConstraints()
ps = PortfolioState.default(constraints)
mock_analysis = StrategyAnalysis(strategy_name='weather_bracket', signals=[], trade_plan=[], diagnostics={})
plans, diag = _run_tail_strategy_pass(
    mainstream_analysis=mock_analysis,
    constraints=constraints,
    portfolio_state=ps,
)
print(f'tail plans: {len(plans)}')
print(f'tail diagnostics keys: {list(diag.keys())}')
"
