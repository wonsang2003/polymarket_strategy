import unittest
from pathlib import Path

from polymarket_strat.application.service import StrategyApplicationService
from polymarket_strat.backtest import run_backtests
from polymarket_strat.config import PortfolioState, TradingConstraints
from polymarket_strat.infrastructure.real_data import load_real_data_status, load_real_mispricing_backtest_rows
from polymarket_strat.main import run_execute
from polymarket_strat.presentation.reporting import write_strategy_report
from polymarket_strat.sample_data import SamplePolymarketClient, build_sample_mispricing_markets, build_sample_whales
from polymarket_strat.strategy import MispricingProbabilityStrategy, WhaleTrackerStrategy


class StrategyTests(unittest.TestCase):
    def test_whale_strategy_sample_analysis(self) -> None:
        client = SamplePolymarketClient()
        strategy = WhaleTrackerStrategy(client)

        whales = strategy.rank_whales()
        self.assertGreaterEqual(len(whales), 2)
        self.assertGreater(whales[0].realized_pnl, 0)

        signals = strategy.generate_copy_signals(whales)
        self.assertTrue(signals)
        self.assertEqual(signals[0].market, "m10")

        analysis = strategy.analyze(constraints=TradingConstraints(), portfolio_state=PortfolioState.default(TradingConstraints()))
        self.assertTrue(analysis.trade_plan)
        self.assertTrue(analysis.trade_plan[0].executable)
        self.assertGreater(analysis.trade_plan[0].target_notional, 0)
        self.assertLess(analysis.trade_plan[0].risk_score, 1)

    def test_mispricing_strategy_creates_edges(self) -> None:
        strategy = MispricingProbabilityStrategy(markets=build_sample_mispricing_markets())
        analysis = strategy.analyze(constraints=TradingConstraints(), portfolio_state=PortfolioState.default(TradingConstraints()))
        self.assertTrue(analysis.signals)
        self.assertTrue(any(plan.executable for plan in analysis.trade_plan))
        self.assertTrue(all(plan.metadata["edge"] >= 0.05 or plan.metadata["edge"] <= -0.05 for plan in analysis.trade_plan))

    def test_drawdown_brake_blocks_new_trades(self) -> None:
        client = SamplePolymarketClient()
        strategy = WhaleTrackerStrategy(client)
        whales = strategy.rank_whales()
        constraints = TradingConstraints()
        stressed_state = PortfolioState(
            cash=50.0,
            current_equity=800.0,
            peak_equity=1000.0,
            open_positions={},
            category_exposure={},
            category_position_counts={},
        )

        analysis = strategy.analyze(constraints=constraints, portfolio_state=stressed_state)
        self.assertTrue(analysis.trade_plan)
        self.assertFalse(any(item.executable for item in analysis.trade_plan))

    def test_execute_updates_state_once(self) -> None:
        state_path = Path("runtime/test_execute_state.json")
        if state_path.exists():
            state_path.unlink()

        run_execute("whale_following", use_sample=True, mode="paper", state_path=str(state_path), confirm_live=False)

        state = PortfolioState.load(state_path, TradingConstraints())
        self.assertEqual(state.open_positions["m10"], 50.0)
        self.assertIn("m11", state.open_positions)
        self.assertGreater(state.open_positions["m11"], 0)

    def test_backtest_runs_for_all_strategies(self) -> None:
        results = run_backtests("all", use_sample=True)
        self.assertGreaterEqual(len(results), 2)
        by_name = {item["strategy_name"]: item for item in results}
        self.assertIn("whale_following", by_name)
        self.assertIn("mispricing", by_name)
        self.assertGreater(by_name["whale_following"]["trade_count"], 0)
        self.assertGreater(by_name["mispricing"]["trade_count"], 0)

    def test_application_service_lists_strategies(self) -> None:
        service = StrategyApplicationService(use_sample=True)
        self.assertEqual(set(service.available_strategies()), {"whale_following", "mispricing", "weather_bracket"})

    def test_real_data_loader_detects_downloaded_files(self) -> None:
        status = load_real_data_status()
        self.assertIn("data/raw/polymarket_sk_2022_market.json", status["raw_files"])
        self.assertGreaterEqual(status["real_mispricing_rows"], 0)

    def test_real_mispricing_rows_load_if_present(self) -> None:
        rows = load_real_mispricing_backtest_rows()
        self.assertIsInstance(rows, list)

    def test_html_report_generation(self) -> None:
        report_path = Path("reports/test_strategy_report.html")
        if report_path.exists():
            report_path.unlink()
        write_strategy_report(report_path, use_sample=True, state_path="runtime/test_execute_state.json")
        html_text = report_path.read_text()
        self.assertIn("Polymarket Strategy Intelligence", html_text)
        self.assertIn("Whale Following", html_text)
        self.assertIn("Mispricing", html_text)
        self.assertIn("Interactive Strategy Command Deck", html_text)
        self.assertIn("data-tab-target", html_text)
        self.assertIn("Decision Theater", html_text)


if __name__ == "__main__":
    unittest.main()
