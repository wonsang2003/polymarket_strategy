#!/usr/bin/env bash
set -e
cd /home/ubuntu/polymarket
venv/bin/python - <<'PY'
from polymarket_strat.config import TradingConstraints, PortfolioState
tc = TradingConstraints()
print(f"max_daily_drawdown:    {tc.max_daily_drawdown}  (was 0.14, was triggered after $-{0.14*tc.bankroll:.0f} daily P&L)")
print(f"drawdown_soft_limit:   {tc.drawdown_soft_limit}")
print(f"drawdown_hard_limit:   {tc.drawdown_hard_limit}")
print()
print(f"With bankroll = ${tc.bankroll}:")
print(f"  brake_threshold:    -{tc.max_daily_drawdown * tc.bankroll:.2f}  (today P&L must be < this to skip cycle)")
print()
# Simulate a stressed portfolio state (was 20% drawdown — previously blocked)
ps = PortfolioState(cash=50.0, current_equity=800.0, peak_equity=1000.0,
                     open_positions={}, category_exposure={}, category_position_counts={})
print(f"Simulated stressed state: equity={ps.current_equity}, peak={ps.peak_equity}, drawdown={ps.drawdown:.2%}")
print(f"  drawdown {ps.drawdown:.4f} >= hard_limit {tc.drawdown_hard_limit}? "
      f"{ps.drawdown >= tc.drawdown_hard_limit}  (False = no block)")
print(f"  drawdown {ps.drawdown:.4f} >= soft_limit {tc.drawdown_soft_limit}? "
      f"{ps.drawdown >= tc.drawdown_soft_limit}  (False = no size shrinkage)")
PY
