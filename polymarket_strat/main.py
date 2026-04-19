from __future__ import annotations

import argparse
import importlib.util
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from polymarket_strat.application.service import StrategyApplicationService
from polymarket_strat.config import AccountConfig, PortfolioState, TradingConstraints, load_env_file
from polymarket_strat.execution import LiveExecutor, PaperExecutor, order_to_dict
from polymarket_strat.presentation.reporting import write_strategy_report


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _load_portfolio_state(state_path: str, constraints: TradingConstraints) -> PortfolioState:
    return PortfolioState.load(state_path, constraints)


def run_analyze(strategy_name: str, *, use_sample: bool, state_path: str) -> None:
    constraints = TradingConstraints()
    portfolio_state = _load_portfolio_state(state_path, constraints)
    projected_state = portfolio_state.clone()
    service = StrategyApplicationService(use_sample=use_sample)
    analysis = service.analyze(strategy_name, constraints=constraints, portfolio_state=projected_state)
    _print_json(
        {
            "strategy": strategy_name,
            "risk_config": asdict(constraints),
            "portfolio_state": asdict(portfolio_state),
            "projected_portfolio_state": asdict(projected_state),
            **service.describe_analysis(analysis),
        }
    )


def run_execute(strategy_name: str, *, use_sample: bool, mode: str, state_path: str, confirm_live: bool) -> None:
    constraints = TradingConstraints()
    portfolio_state = _load_portfolio_state(state_path, constraints)
    projected_state = portfolio_state.clone()
    service = StrategyApplicationService(use_sample=use_sample)
    analysis = service.analyze(strategy_name, constraints=constraints, portfolio_state=projected_state)
    executable_orders = [item for item in analysis.trade_plan if item.executable]

    if mode == "paper":
        executor = PaperExecutor()
    else:
        if not confirm_live:
            raise ValueError("Live execution requires --confirm-live so you do not place real orders accidentally.")
        account = AccountConfig.from_env()
        executor = LiveExecutor(account)

    # Lazy-import DB only for weather strategy (avoids sqlite dep for other strategies)
    weather_db = None
    already_open_token_ids: set[str] = set()
    if strategy_name == "weather_bracket":
        from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
        weather_db = WeatherDatabase()
        # Prevent re-entering the same token that is already an open position
        for pos in weather_db.get_open_positions():
            if pos.get("token_id"):
                already_open_token_ids.add(pos["token_id"])

    results = []
    for item in executable_orders:
        if item.token_id in already_open_token_ids:
            continue
        order = executor.execute_market_buy(
            market=item.market,
            outcome=item.outcome,
            token_id=item.token_id,
            amount=item.target_notional,
            reference_price=item.best_ask,
        )
        order_dict = order_to_dict(order)
        results.append(order_dict)
        portfolio_state.cash = max(portfolio_state.cash - item.target_notional, 0.0)
        portfolio_state.open_positions[item.market] = portfolio_state.open_positions.get(item.market, 0.0) + item.target_notional
        portfolio_state.category_exposure[item.category] = portfolio_state.category_exposure.get(item.category, 0.0) + item.target_notional
        portfolio_state.category_position_counts[item.category] = portfolio_state.category_position_counts.get(item.category, 0) + 1

        # Persist weather bracket trades for paper-trading P&L tracking
        if weather_db is not None:
            meta = item.metadata
            raw_date = meta.get("target_date")
            try:
                tdate = date.fromisoformat(raw_date) if raw_date else date.today()
            except (ValueError, TypeError):
                tdate = date.today()
            trade_id = weather_db.save_trade(
                city=meta.get("city", ""),
                target_date=tdate,
                bracket_lower_f=float(meta.get("bracket_lower_f", 0.0)),
                bracket_upper_f=float(meta.get("bracket_upper_f", 0.0)),
                model_prob=float(meta.get("model_prob", 0.0)),
                market_prob=float(item.reference_price),
                edge=float(item.expected_value),
                kelly_fraction=float(meta.get("kelly_fraction", 0.0)),
                notional=float(item.target_notional),
                entry_price=float(item.best_ask),
                side=item.side,
                mode=mode,
                market_id=item.market,
                token_id=item.token_id,
                question=item.question,
                regime=meta.get("regime"),
            )
            order_dict["trade_id"] = trade_id

    if weather_db is not None:
        weather_db.close()

    portfolio_state.save(state_path)
    _print_json(
        {
            "strategy": strategy_name,
            "mode": mode,
            "orders": results,
            "count": len(results),
            "state_path": state_path,
            "portfolio_state": asdict(portfolio_state),
        }
    )


def run_positions() -> None:
    """Show all open (unsettled) paper/live weather trades."""
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
    db = WeatherDatabase()
    positions = db.get_open_positions()
    db.close()
    _print_json({"open_positions": positions, "count": len(positions)})


def _pnl(*, outcome: int, notional: float, entry_price: float, fee: float = 0.02) -> float:
    """Compute P&L for a binary bracket trade.

    Polymarket charges the 2% fee on WINNINGS ONLY (profit above notional),
    not on the full payout. See `.claude/claude.md` §4.1 for the derivation.
        WIN:  pnl = n_shares * (1 - entry_price) * (1 - fee)
        LOSS: pnl = -notional
    """
    n_shares = notional / entry_price if entry_price > 0 else 0
    return round(n_shares * (1 - entry_price) * (1 - fee) if outcome == 1 else -notional, 4)


def _settle_from_iem(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Determine trade outcome from actual IEM weather observation.

    Falls back to this when Polymarket API doesn't have the market
    (resolved markets get removed from the API).

    Returns dict with outcome/pnl/observed_high_f, or None if no observation.
    """
    from polymarket_strat.domain.weather.models import CITY_REGISTRY
    from polymarket_strat.infrastructure.weather.station_client import StationObservationClient

    city = pos.get("city", "")
    station = CITY_REGISTRY.get(city)
    if not station:
        return None

    try:
        target_date = date.fromisoformat(pos["target_date"])
    except (ValueError, TypeError, KeyError):
        return None

    obs_list = StationObservationClient().fetch_daily_highs(
        station, start=target_date, end=target_date
    )
    if not obs_list:
        return None

    observed_high_f = obs_list[0].observed_high_f
    lower_f = float(pos.get("bracket_lower_f", -999))
    upper_f = float(pos.get("bracket_upper_f", 999))
    outcome = 1 if lower_f <= observed_high_f < upper_f else 0

    return {
        "outcome": outcome,
        "observed_high_f": round(observed_high_f, 2),
        "pnl": _pnl(
            outcome=outcome,
            notional=float(pos.get("notional", 0)),
            entry_price=float(pos.get("entry_price") or pos.get("market_prob") or 0),
        ),
    }


def run_settle(trade_id: int | None, *, auto: bool) -> None:
    """Settle weather trades.

    Resolution priority:
      1. Polymarket API outcomePrices (fast, works for active/recent markets)
      2. IEM weather observation (reliable fallback for resolved/deleted markets)

    --auto settles all trades whose target_date has passed.
    --trade-id N settles one specific trade.
    """
    from polymarket_strat.api import PolymarketPublicClient
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase

    db = WeatherDatabase()
    client = PolymarketPublicClient()
    settled = []
    errors = []

    positions = db.get_open_positions()
    if trade_id is not None:
        positions = [p for p in positions if p["id"] == trade_id]
        if not positions:
            db.close()
            _print_json({"error": f"No open trade with id={trade_id}"})
            return

    today = date.today()
    for pos in positions:
        target_date_str = pos.get("target_date", "")
        try:
            target_date = date.fromisoformat(target_date_str)
        except (ValueError, TypeError):
            continue

        if auto and target_date >= today:
            continue

        outcome: int | None = None
        observed_high_f: float | None = None
        source = "unknown"

        # --- Strategy 1: Polymarket API ---
        mkt_id = pos.get("market_id") or pos.get("token_id")
        if mkt_id:
            try:
                mkt = client.get_market(mkt_id)
                raw_prices = mkt.get("outcomePrices") or []
                if isinstance(raw_prices, str):
                    import json as _json
                    try:
                        raw_prices = _json.loads(raw_prices)
                    except Exception:
                        raw_prices = []
                if raw_prices:
                    p = float(raw_prices[0])
                    if p >= 0.99:
                        outcome, source = 1, "polymarket_api"
                    elif p <= 0.01:
                        outcome, source = 0, "polymarket_api"
            except Exception:
                pass  # fall through to IEM

        # --- Strategy 2: IEM observation ---
        if outcome is None:
            iem = _settle_from_iem(pos)
            if iem:
                outcome = iem["outcome"]
                observed_high_f = iem["observed_high_f"]
                source = "iem_observation"

        if outcome is None:
            errors.append({
                "id": pos["id"],
                "city": pos.get("city"),
                "target_date": target_date_str,
                "error": "market unresolved and no IEM observation yet — try again tomorrow",
            })
            continue

        trade_pnl = _pnl(
            outcome=outcome,
            notional=float(pos.get("notional", 0)),
            entry_price=float(pos.get("entry_price") or pos.get("market_prob") or 0),
        )
        db.settle_trade(pos["id"], outcome=outcome, pnl=trade_pnl)
        record: dict[str, Any] = {
            "id": pos["id"],
            "city": pos.get("city"),
            "target_date": target_date_str,
            "question": pos.get("question", ""),
            "outcome": "YES" if outcome == 1 else "NO",
            "pnl": trade_pnl,
            "source": source,
        }
        if observed_high_f is not None:
            record["observed_high_f"] = observed_high_f
        settled.append(record)

    db.close()
    total_pnl = sum(s["pnl"] for s in settled)
    _print_json({
        "settled": settled,
        "settled_count": len(settled),
        "total_pnl": round(total_pnl, 4),
        "errors": errors,
    })


def run_autotrade(
    *,
    mode: str = "paper",
    confirm_live: bool = False,
    state_path: str = "runtime/portfolio_state.json",
    env_file: str = ".env",
    max_open: int = 30,
    drawdown_brake_pct: float | None = None,
) -> dict[str, Any]:
    """Full autotrade cycle: settle → safety check → analyze → execute → notify.

    Returns a JSON-serializable summary of the cycle.
    """
    import sys

    from polymarket_strat.config import TelegramConfig
    from polymarket_strat.execution import LiveExecutor, PaperExecutor, order_to_dict
    from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
    from polymarket_strat.notifications.telegram import TelegramNotifier

    load_env_file(env_file)
    print(f"[autotrade] Starting cycle (mode={mode})...", file=sys.stderr)

    db = WeatherDatabase()
    constraints = TradingConstraints()
    # Resolve daily-drawdown brake: CLI override → constraints default.
    # constraints.max_daily_drawdown is 14% (Apr 19 2026), sized to
    # accommodate ~2.8 full-loss positions under the 5% per-position cap.
    if drawdown_brake_pct is None:
        drawdown_brake_pct = constraints.max_daily_drawdown
    cycle: dict[str, Any] = {"mode": mode, "drawdown_brake_pct": drawdown_brake_pct}

    # ------------------------------------------------------------------
    # Step 1: Settle expired trades
    # ------------------------------------------------------------------
    print("[autotrade] Step 1: Settling expired trades...", file=sys.stderr)
    open_before = db.get_open_positions()
    settled: list[dict[str, Any]] = []
    today = date.today()

    for pos in open_before:
        try:
            target_date = date.fromisoformat(pos.get("target_date", ""))
        except (ValueError, TypeError):
            continue
        if target_date >= today:
            continue  # not yet expired

        iem = _settle_from_iem(pos)
        if iem is None:
            continue
        trade_pnl = iem["pnl"]
        db.settle_trade(pos["id"], outcome=iem["outcome"], pnl=trade_pnl)
        settled.append({
            "id": pos["id"],
            "city": pos.get("city"),
            "question": pos.get("question", "")[:50],
            "outcome": "YES" if iem["outcome"] == 1 else "NO",
            "pnl": trade_pnl,
            "observed_high_f": iem.get("observed_high_f"),
        })

    settled_pnl = sum(s["pnl"] for s in settled)
    cycle["settled_count"] = len(settled)
    cycle["settled_pnl"] = round(settled_pnl, 4)
    cycle["settled"] = settled
    print(f"[autotrade]   Settled {len(settled)} trades, session P&L: ${settled_pnl:+,.2f}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Step 2: Safety gates
    # ------------------------------------------------------------------
    open_now = db.get_open_positions()
    cycle["open_positions"] = len(open_now)

    if len(open_now) >= max_open:
        cycle["skipped"] = f"max_open_positions ({max_open})"
        print(f"[autotrade] BRAKE: {len(open_now)} open positions >= {max_open}. Skipping execution.", file=sys.stderr)
        _autotrade_notify(cycle, env_file)
        db.close()
        _print_json(cycle)
        return cycle

    # Daily drawdown check: sum of all P&L settled today
    all_trades = db.get_trades(limit=500)
    today_str = today.isoformat()
    today_pnl = sum(
        float(t.get("pnl") or 0)
        for t in all_trades
        if t.get("settled_at") and str(t["settled_at"])[:10] == today_str
    )
    brake_threshold = -drawdown_brake_pct * constraints.bankroll
    if today_pnl < brake_threshold:
        cycle["skipped"] = f"daily_drawdown (today P&L ${today_pnl:+,.2f} < ${brake_threshold:,.2f})"
        print(f"[autotrade] BRAKE: daily P&L ${today_pnl:+,.2f} < ${brake_threshold:,.2f}. Skipping execution.", file=sys.stderr)
        _autotrade_notify(cycle, env_file)
        db.close()
        _print_json(cycle)
        return cycle

    # ------------------------------------------------------------------
    # Step 3: Analyze weather brackets
    # ------------------------------------------------------------------
    print("[autotrade] Step 3: Analyzing weather brackets...", file=sys.stderr)
    portfolio_state = _load_portfolio_state(state_path, constraints)
    service = StrategyApplicationService(use_sample=False)
    analysis = service.analyze("weather_bracket", constraints=constraints, portfolio_state=portfolio_state)
    executable = [p for p in analysis.trade_plan if p.executable]
    print(f"[autotrade]   {len(analysis.signals)} signals, {len(executable)} executable trades.", file=sys.stderr)
    # Propagate strategy-layer telemetry so Telegram/CloudWatch can see WHY a
    # cycle produced zero executable trades (per-gate rejection histogram,
    # no-forecast / no-dists contract counts, HRRR long-lead drops). Without
    # this the Lambda cycle is a black box — only the end-state is visible.
    cycle["diagnostics"] = analysis.diagnostics or {}

    # ------------------------------------------------------------------
    # Step 4: Execute trades
    # ------------------------------------------------------------------
    if not executable:
        cycle["new_trade_count"] = 0
        cycle["skipped"] = cycle.get("skipped") or "no_executable_signals"
    else:
        print(f"[autotrade] Step 4: Executing {len(executable)} trades ({mode})...", file=sys.stderr)
        if mode == "live":
            if not confirm_live:
                cycle["skipped"] = "live_mode_requires_--confirm-live"
                _autotrade_notify(cycle, env_file)
                db.close()
                _print_json(cycle)
                return cycle
            executor = LiveExecutor(AccountConfig.from_env())
        else:
            executor = PaperExecutor()

        # Prevent re-booking the same token_id on every cron tick.
        # Mirrors the guard in run_execute (CLAUDE.md §10 bug fix #7).
        already_open_token_ids: set[str] = {
            pos["token_id"] for pos in db.get_open_positions() if pos.get("token_id")
        }

        executed: list[dict[str, Any]] = []
        skipped_duplicates = 0
        for item in executable:
            if item.token_id and item.token_id in already_open_token_ids:
                skipped_duplicates += 1
                continue
            order = executor.execute_market_buy(
                market=item.market,
                outcome=item.outcome,
                token_id=item.token_id,
                amount=item.target_notional,
                reference_price=item.best_ask,
            )
            order_dict = order_to_dict(order)
            # Mark as open so later items in the same cycle aren't re-booked
            if item.token_id:
                already_open_token_ids.add(item.token_id)

            # Persist to weather DB
            meta = item.metadata
            raw_date = meta.get("target_date")
            try:
                tdate = date.fromisoformat(raw_date) if raw_date else today
            except (ValueError, TypeError):
                tdate = today

            db.save_trade(
                city=meta.get("city", ""),
                target_date=tdate,
                bracket_lower_f=float(meta.get("bracket_lower_f", 0)),
                bracket_upper_f=float(meta.get("bracket_upper_f", 0)),
                model_prob=float(meta.get("model_prob", 0)),
                market_prob=float(item.reference_price),
                edge=float(item.expected_value),
                kelly_fraction=float(meta.get("kelly_fraction", 0)),
                notional=float(item.target_notional),
                entry_price=float(item.best_ask),
                side=item.side,
                mode=mode,
                market_id=item.market,
                token_id=item.token_id,
                question=item.question,
                regime=meta.get("regime"),
            )

            # Update portfolio state
            portfolio_state.cash = max(portfolio_state.cash - item.target_notional, 0)
            portfolio_state.open_positions[item.market] = (
                portfolio_state.open_positions.get(item.market, 0) + item.target_notional
            )
            order_dict["city"] = meta.get("city")
            order_dict["outcome"] = item.outcome
            executed.append(order_dict)

        portfolio_state.save(state_path)
        cycle["new_trade_count"] = len(executed)
        cycle["new_trades"] = executed
        cycle["skipped_duplicates"] = skipped_duplicates

    # Cumulative P&L
    all_settled = [t for t in db.get_trades(limit=5000) if t.get("pnl") is not None]
    cycle["cumulative_pnl"] = round(sum(float(t["pnl"]) for t in all_settled), 4)
    cycle["open_positions"] = len(db.get_open_positions())

    db.close()

    # ------------------------------------------------------------------
    # Step 5: Telegram notification
    # ------------------------------------------------------------------
    _autotrade_notify(cycle, env_file)

    print(f"[autotrade] Cycle complete. {cycle.get('new_trade_count', 0)} new trades, cumulative P&L: ${cycle.get('cumulative_pnl', 0):+,.2f}", file=sys.stderr)
    _print_json(cycle)
    return cycle


def _autotrade_notify(cycle: dict[str, Any], env_file: str) -> None:
    """Send Telegram summary. Silently skip if no Telegram config."""
    try:
        from polymarket_strat.config import TelegramConfig
        from polymarket_strat.notifications.telegram import TelegramNotifier

        load_env_file(env_file)
        config = TelegramConfig.from_env()
        notifier = TelegramNotifier(config)

        # Settlement report
        if cycle.get("settled"):
            notifier.send_settlement_report(
                settled=cycle["settled"],
                total_pnl=cycle.get("settled_pnl", 0),
            )

        # New trade notification
        if cycle.get("new_trades"):
            notifier.send_trade_executed(trades=cycle["new_trades"])

        # Consolidated summary
        notifier.send_autotrade_summary(cycle=cycle)
    except Exception as exc:
        import sys
        print(f"[autotrade] Telegram notification failed: {exc}", file=sys.stderr)


def run_backtest(strategy_name: str, *, use_sample: bool) -> None:
    service = StrategyApplicationService(use_sample=use_sample)
    _print_json({"backtests": [asdict(result) for result in service.backtest(strategy_name)]})


def run_doctor(env_file: str, state_path: str) -> None:
    constraints = TradingConstraints()
    state = _load_portfolio_state(state_path, constraints)
    env_path = Path(env_file)
    py_clob_client_installed = importlib.util.find_spec("py_clob_client") is not None
    env_text = env_path.read_text() if env_path.exists() else ""
    env_values: dict[str, str] = {}
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env_values[key.strip()] = value.strip()
    private_key_value = env_values.get("POLYMARKET_PRIVATE_KEY", "")
    funder_value = env_values.get("POLYMARKET_FUNDER", "")
    payload = {
        "checks": {
            "env_file_exists": env_path.exists(),
            "state_file_exists": Path(state_path).exists(),
            "py_clob_client_installed": py_clob_client_installed,
            "has_private_key": bool(private_key_value) and "YOUR_PRIVATE_KEY" not in private_key_value,
            "has_funder": bool(funder_value) and "YOUR_FUNDER_ADDRESS" not in funder_value,
        },
        "available_strategies": StrategyApplicationService(use_sample=True).available_strategies(),
        "real_data_status": StrategyApplicationService(use_sample=True).real_data_status(),
        "risk_config": asdict(constraints),
        "portfolio_state": asdict(state),
    }
    _print_json(payload)


def run_report(*, use_sample: bool, state_path: str, output_path: str) -> None:
    path = write_strategy_report(output_path, use_sample=use_sample, state_path=state_path)
    _print_json({"report_path": str(path), "use_sample": use_sample, "state_path": state_path})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket multi-strategy trading toolkit.")
    parser.add_argument("--env-file", default=".env", help="Path to local environment file.")
    parser.add_argument("--state-file", default="runtime/portfolio_state.json", help="Path to persisted portfolio state.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze a strategy and build its trade plan.")
    analyze.add_argument("--strategy", choices=["whale_following", "mispricing", "weather_bracket"], default="whale_following")
    analyze.add_argument("--sample", action="store_true", help="Use bundled sample data.")

    execute = subparsers.add_parser("execute", help="Execute the currently executable trade plan.")
    execute.add_argument("--strategy", choices=["whale_following", "mispricing", "weather_bracket"], default="whale_following")
    execute.add_argument("--sample", action="store_true", help="Use bundled sample data.")
    execute.add_argument("--mode", choices=["paper", "live"], default="paper")
    execute.add_argument("--confirm-live", action="store_true")

    backtest = subparsers.add_parser("backtest", help="Backtest one strategy or all strategies.")
    backtest.add_argument("--strategy", choices=["whale_following", "mispricing", "weather_bracket", "all"], default="all")
    backtest.add_argument("--sample", action="store_true", help="Use bundled sample data.")

    doctor = subparsers.add_parser("doctor", help="Check local readiness for strategy execution.")
    doctor.add_argument("--sample", action="store_true", help="Use bundled sample configuration.")

    report = subparsers.add_parser("report", help="Generate a visual HTML report for analyses and backtests.")
    report.add_argument("--sample", action="store_true", help="Use bundled sample data.")
    report.add_argument("--output", default="reports/strategy_report.html", help="Where to write the HTML report.")

    monitor = subparsers.add_parser("monitor", help="Poll ELITE whales and send Telegram alerts for large trades.")
    monitor.add_argument("--min-size", type=float, default=1000.0, help="Minimum trade notional ($) to trigger alert.")
    monitor.add_argument("--monitor-state", default="runtime/whale_monitor_state.json", help="Path to monitor state file.")

    insider = subparsers.add_parser(
        "insider",
        help="Scan political/economic markets for insider-anomaly signals (volume spikes, new wallets, coordinated buys, price impact).",
    )
    insider.add_argument(
        "--min-score",
        type=float,
        default=0.15,
        help="Minimum combined suspicion score (0–1+) to trigger a Telegram alert.",
    )
    insider.add_argument(
        "--insider-state",
        default="runtime/insider_monitor_state.json",
        help="Path to insider monitor state file.",
    )

    wcal = subparsers.add_parser("weather-calibrate", help="Run offline calibration for weather error distributions.")
    wcal.add_argument(
        "--cities",
        default="all",
        help="Comma-separated city keys (no spaces), e.g. london,hong_kong,chicago — or 'all'.",
    )
    wcal.add_argument("--lookback-days", type=int, default=365, help="Days of history to calibrate from.")

    subparsers.add_parser("positions", help="Show all open (unsettled) paper/live weather trades.")

    settle = subparsers.add_parser("settle", help="Settle resolved weather trades and compute P&L.")
    settle.add_argument("--trade-id", type=int, default=None, help="Settle a specific trade by ID.")
    settle.add_argument("--auto", action="store_true", help="Auto-settle all open positions whose target date has passed.")

    auto = subparsers.add_parser("autotrade", help="Full automated cycle: settle → analyze → execute → notify.")
    auto.add_argument("--mode", choices=["paper", "live"], default="paper", help="Trading mode.")
    auto.add_argument("--confirm-live", action="store_true", help="Required safety flag for live execution.")
    auto.add_argument("--max-open", type=int, default=30, help="Max open positions before skipping execution.")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    load_env_file(Path(args.env_file))

    if args.command == "analyze":
        run_analyze(args.strategy, use_sample=args.sample, state_path=args.state_file)
        return
    if args.command == "execute":
        run_execute(
            args.strategy,
            use_sample=args.sample,
            mode=args.mode,
            state_path=args.state_file,
            confirm_live=args.confirm_live,
        )
        return
    if args.command == "backtest":
        run_backtest(args.strategy, use_sample=args.sample)
        return
    if args.command == "doctor":
        run_doctor(args.env_file, args.state_file)
        return
    if args.command == "report":
        run_report(use_sample=args.sample, state_path=args.state_file, output_path=args.output)
        return
    if args.command == "monitor":
        from polymarket_strat.monitor import run_monitor

        result = run_monitor(
            min_size=args.min_size,
            state_path=args.monitor_state,
            env_file=args.env_file,
        )
        _print_json(result)
        return
    if args.command == "weather-calibrate":
        from polymarket_strat.application.service import StrategyApplicationService

        svc = StrategyApplicationService(use_sample=False)
        strategy = svc.create_strategy("weather_bracket")
        cities = None if args.cities == "all" else args.cities.split(",")
        result = strategy.calibrate(cities=cities, lookback_days=args.lookback_days)
        _print_json(result)
        return
    if args.command == "insider":
        from polymarket_strat.monitor import run_insider_monitor

        result = run_insider_monitor(
            min_score=args.min_score,
            state_path=args.insider_state,
            env_file=args.env_file,
        )
        _print_json(result)
        return
    if args.command == "positions":
        run_positions()
        return
    if args.command == "settle":
        run_settle(args.trade_id, auto=args.auto)
        return
    if args.command == "autotrade":
        run_autotrade(
            mode=args.mode,
            confirm_live=args.confirm_live,
            state_path=args.state_file,
            env_file=args.env_file,
            max_open=args.max_open,
        )
        return
    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
