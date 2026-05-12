"""Local-Mac CLI entrypoint for live + shadow trading.

This is the ONLY module in ``polymarket_strat.live`` with network
side effects, state, and orchestration. Everything below it (executor,
slippage, orderbook, persistence) is pure or easily testable.

Subcommands
-----------
``doctor``   Run the 8 pre-flight checks; exit 0/1.
``shadow``   One analyze-and-log cycle. Never submits to CLOB. Safe without
             POLYMARKET_PRIVATE_KEY.
``live``     One analyze-and-submit cycle. Requires doctor pass, USDC balance,
             and exchange approvals. Holds an exclusive lock on
             ``/tmp/polymarket_live.lock`` so two runners can't race.
``settle``   Scan filled-unsettled positions in ``live.db``, resolve via
             Polymarket Gamma (outcomePrices) or IEM observation fallback,
             write outcome + P&L back onto the row.

Design
------
* The paper pipeline on EC2 is untouched — we import ``WeatherBracketStrategy``
  read-only for signal generation, but persist to a completely separate
  database (``data/weather/live.db``) and state file.
* Kill-switches (``EMERGENCY_STOP``, ``/tmp/polymarket_halt``) are checked
  twice: once at cycle start (skip signal gen entirely), once per-trade by
  the coordinator (abort mid-cycle).
* Per-cycle safety budget: ``max_live_total_open_usd`` caps *total* open
  live notional. When the budget is hit, remaining plans are skipped with
  reason ``budget_exhausted`` — they still get logged so analytics show
  how much signal was passed up.
* Live trade notifications go to Telegram if credentials are set; silent
  otherwise.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.config import (
    AccountConfig,
    PortfolioState,
    TelegramConfig,
    TradingConstraints,
    load_env_file,
)
from polymarket_strat.domain.models import StrategyAnalysis, TradePlan
from polymarket_strat.live.doctor import run_doctor
from polymarket_strat.live.executor import (
    FillResult,
    LiveCoordinator,
    LiveExecutorConfig,
    REASON_SHADOW,
)
from polymarket_strat.live.persistence import LiveDatabase


DEFAULT_LOCK_FILE = Path("/tmp/polymarket_live.lock")
DEFAULT_LIVE_DB_PATH = Path("data/weather/live.db")
DEFAULT_LIVE_DB_PATH_STR = str(DEFAULT_LIVE_DB_PATH)

# Reason codes unique to the runner (coordinator has its own set).
REASON_RUNNER_DUPLICATE = "duplicate_open_position"
REASON_RUNNER_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_RUNNER_NOT_EXECUTABLE = "plan_not_executable"
REASON_RUNNER_EMERGENCY_STOP = "emergency_stop_at_cycle_start"
REASON_RUNNER_HALT_FILE = "halt_file_at_cycle_start"


# ---------------------------------------------------------------------------
# Cycle report
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """One row in the cycle report — one TradePlan evaluated."""
    token_id: str
    city: str
    notional_requested: float
    filled: bool
    reason: str
    row_id: int | None
    expected_pnl: float | None


@dataclass(frozen=True, slots=True)
class CycleReport:
    mode: str
    cycle_start: float
    cycle_end: float
    analysis: StrategyAnalysis | None
    outcomes: tuple[PlanOutcome, ...]
    skipped: str = ""  # set if whole cycle was short-circuited (kill switch, etc.)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def filled_count(self) -> int:
        return sum(1 for o in self.outcomes if o.filled)

    @property
    def filled_outcomes(self) -> tuple[PlanOutcome, ...]:
        return tuple(o for o in self.outcomes if o.filled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cycle_start": self.cycle_start,
            "cycle_end": self.cycle_end,
            "duration_s": round(self.cycle_end - self.cycle_start, 3),
            "skipped": self.skipped,
            "filled_count": self.filled_count,
            "outcomes": [
                {
                    "token_id": o.token_id,
                    "city": o.city,
                    "notional_requested": o.notional_requested,
                    "filled": o.filled,
                    "reason": o.reason,
                    "row_id": o.row_id,
                    "expected_pnl": o.expected_pnl,
                }
                for o in self.outcomes
            ],
            "diagnostics": self.diagnostics,
        }


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------
@dataclass
class LiveRunner:
    """Composition root for one cycle of live or shadow execution.

    All dependencies are explicit so tests can swap fakes without mocking.
    ``strategy_factory`` is a zero-arg callable that returns a fresh
    ``WeatherBracketStrategy`` (or any Strategy-protocol impl). Building it
    inside the runner keeps the paper-pipeline DB connections scoped to the
    cycle — no long-lived sqlite handles on the Mac between cycles.
    """

    strategy_factory: Callable[[], Any]
    live_db: LiveDatabase
    coordinator: LiveCoordinator
    constraints: TradingConstraints
    telegram_send: Callable[[str], None] | None = None
    now_fn: Callable[[], float] = time.time
    emergency_stop_env: str = "EMERGENCY_STOP"
    halt_file: Path = Path("/tmp/polymarket_halt")
    max_live_total_open_usd: float = 100.0

    # -------------------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        """Generate signals and execute each trade plan.

        Shadow mode writes every attempt to ``live.db`` with ``mode=shadow``.
        Live mode runs the FOK submit for each plan — each a separate
        trip to the CLOB, each logged row-by-row so a mid-cycle crash
        still leaves the DB in a consistent state.
        """
        cycle_start = self.now_fn()

        # 1. Early kill-switch checks — skip signal gen entirely if halted.
        if os.getenv(self.emergency_stop_env, "").strip() == "1":
            return self._skipped(
                cycle_start=cycle_start,
                skipped=REASON_RUNNER_EMERGENCY_STOP,
            )
        if self.halt_file.exists():
            return self._skipped(
                cycle_start=cycle_start,
                skipped=REASON_RUNNER_HALT_FILE,
            )

        # 2. Build portfolio state from live.db open positions.
        already_open = set(self.live_db.list_open_tokens())
        current_open_notional = self.live_db.sum_open_live_notional()
        portfolio_state = _portfolio_state_from_live(
            constraints=self.constraints,
            open_tokens=already_open,
            open_notional=current_open_notional,
        )

        # 3. Signal generation — reuse paper strategy read-only.
        strategy = self.strategy_factory()
        analysis: StrategyAnalysis = strategy.analyze(
            constraints=self.constraints,
            portfolio_state=portfolio_state,
        )

        # 4. Walk the plan list, execute each.
        outcomes: list[PlanOutcome] = []
        remaining_budget = max(0.0, self.max_live_total_open_usd - current_open_notional)
        mode = self.coordinator.config.mode

        for plan in analysis.trade_plan:
            outcome = self._handle_plan(
                plan=plan,
                mode=mode,
                already_open=already_open,
                remaining_budget=remaining_budget,
            )
            outcomes.append(outcome)
            if outcome.filled:
                already_open.add(plan.token_id)
                remaining_budget -= plan.target_notional

        cycle_end = self.now_fn()

        # 5. Telegram summary — one message per cycle, plus per-fill alerts
        #    from _handle_plan. Non-blocking (caller swallows its own errors).
        self._notify_cycle(analysis=analysis, outcomes=outcomes, mode=mode)

        return CycleReport(
            mode=mode,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            analysis=analysis,
            outcomes=tuple(outcomes),
            diagnostics={
                "open_tokens_at_start": sorted(already_open),
                "open_notional_at_start": round(current_open_notional, 2),
                "max_live_total_open_usd": self.max_live_total_open_usd,
                "plans_considered": len(analysis.trade_plan),
            },
        )

    # -------------------------------------------------------------------
    def _handle_plan(
        self,
        *,
        plan: TradePlan,
        mode: str,
        already_open: set[str],
        remaining_budget: float,
    ) -> PlanOutcome:
        meta = plan.metadata or {}
        city = str(meta.get("city") or "unknown")
        target_date = _coerce_target_date(meta.get("target_date"))

        # Duplicate guard — log a rejected row so analytics still see the
        # opportunity was surfaced. Live mode only; shadow cycle can
        # re-log freely to stress-test the strategy.
        if mode == "live" and plan.token_id in already_open:
            return self._log_runner_reject(
                plan=plan,
                city=city,
                target_date=target_date,
                reason=REASON_RUNNER_DUPLICATE,
            )

        # Executable guard — the strategy sets this to False when the
        # live orderbook spread is too wide. Shadow still runs the gates
        # (orderbook re-fetched at submit time, state may have changed).
        if not plan.executable and mode == "live":
            return self._log_runner_reject(
                plan=plan,
                city=city,
                target_date=target_date,
                reason=REASON_RUNNER_NOT_EXECUTABLE,
            )

        # Budget cap — live mode only; shadow never opens real positions.
        if mode == "live" and plan.target_notional > remaining_budget:
            return self._log_runner_reject(
                plan=plan,
                city=city,
                target_date=target_date,
                reason=REASON_RUNNER_BUDGET_EXHAUSTED,
            )

        # Submit to coordinator — this is where a real live fill happens.
        fill: FillResult = self.coordinator.execute_buy(
            token_id=plan.token_id,
            notional=plan.target_notional,
        )

        expected_pnl = _expected_pnl(
            model_prob=float(meta.get("model_prob") or 0.0),
            entry_price=float(plan.reference_price or 0.0),
            notional=float(plan.target_notional),
        )

        row_id = self.live_db.save_attempt(
            city=city,
            target_date=target_date,
            question=plan.question,
            market_id=plan.market,
            bracket_lower_f=_maybe_float(meta.get("bracket_lower_f")),
            bracket_upper_f=_maybe_float(meta.get("bracket_upper_f")),
            model_prob=_maybe_float(meta.get("model_prob")),
            market_prob=_maybe_float(plan.reference_price),
            edge=_maybe_float(meta.get("edge_after_fees")),
            regime=str(meta.get("regime") or ""),
            side=plan.side,
            expected_pnl=expected_pnl,
            fill_row=fill.to_persist_row(),
        )

        if fill.filled:
            self._send_fill_alert(plan=plan, fill=fill, city=city, expected_pnl=expected_pnl)

        return PlanOutcome(
            token_id=plan.token_id,
            city=city,
            notional_requested=plan.target_notional,
            filled=fill.filled,
            reason=fill.reason or (REASON_SHADOW if mode == "shadow" else ""),
            row_id=row_id,
            expected_pnl=expected_pnl,
        )

    # -------------------------------------------------------------------
    def _log_runner_reject(
        self,
        *,
        plan: TradePlan,
        city: str,
        target_date: date | str,
        reason: str,
    ) -> PlanOutcome:
        """Log a runner-level reject (no coordinator call was made).

        The ``fill_row`` shape still needs every field the persistence
        schema expects, so we emit a zeroed FillResult and let the DB
        store ``filled=0, reason=<runner reason>``. Analytics sees these
        as "attempts we didn't even submit" vs "attempts the CLOB rejected".
        """
        meta = plan.metadata or {}
        fill = FillResult(
            mode=self.coordinator.config.mode,
            token_id=plan.token_id,
            notional_requested=plan.target_notional,
            filled=False,
            reason=reason,
            fill_ts=self.now_fn(),
        )
        expected_pnl = _expected_pnl(
            model_prob=float(meta.get("model_prob") or 0.0),
            entry_price=float(plan.reference_price or 0.0),
            notional=float(plan.target_notional),
        )
        row_id = self.live_db.save_attempt(
            city=city,
            target_date=target_date,
            question=plan.question,
            market_id=plan.market,
            bracket_lower_f=_maybe_float(meta.get("bracket_lower_f")),
            bracket_upper_f=_maybe_float(meta.get("bracket_upper_f")),
            model_prob=_maybe_float(meta.get("model_prob")),
            market_prob=_maybe_float(plan.reference_price),
            edge=_maybe_float(meta.get("edge_after_fees")),
            regime=str(meta.get("regime") or ""),
            side=plan.side,
            expected_pnl=expected_pnl,
            fill_row=fill.to_persist_row(),
        )
        return PlanOutcome(
            token_id=plan.token_id,
            city=city,
            notional_requested=plan.target_notional,
            filled=False,
            reason=reason,
            row_id=row_id,
            expected_pnl=expected_pnl,
        )

    # -------------------------------------------------------------------
    def _skipped(self, *, cycle_start: float, skipped: str) -> CycleReport:
        end = self.now_fn()
        return CycleReport(
            mode=self.coordinator.config.mode,
            cycle_start=cycle_start,
            cycle_end=end,
            analysis=None,
            outcomes=tuple(),
            skipped=skipped,
            diagnostics={},
        )

    # -------------------------------------------------------------------
    def _send_fill_alert(
        self,
        *,
        plan: TradePlan,
        fill: FillResult,
        city: str,
        expected_pnl: float,
    ) -> None:
        if not self.telegram_send:
            return
        try:
            msg = (
                f"<b>LIVE FILL</b>\n"
                f"{city.upper()} {plan.outcome}\n"
                f"Token: <code>{plan.token_id[:14]}…</code>\n"
                f"Spent: ${fill.actual_cost_usd:.2f} @ {fill.actual_vwap:.4f}\n"
                f"Shares: {fill.actual_shares:.2f}\n"
                f"Slippage: ${fill.slippage_usd:+.2f}\n"
                f"Expected EV: ${expected_pnl:+.2f}"
            )
            self.telegram_send(msg)
        except Exception as exc:  # noqa: BLE001 — never let Telegram crash the trade
            print(f"[runner] telegram_send failed: {exc}", file=sys.stderr)

    # -------------------------------------------------------------------
    def _notify_cycle(
        self,
        *,
        analysis: StrategyAnalysis,
        outcomes: list[PlanOutcome],
        mode: str,
    ) -> None:
        if not self.telegram_send:
            return
        filled = sum(1 for o in outcomes if o.filled)
        considered = len(outcomes)
        # Only send a summary in live mode, or when at least one action happened.
        # Shadow cycles chatter too much otherwise.
        if mode != "live" and filled == 0:
            return
        try:
            diag = analysis.diagnostics or {}
            summary = (
                f"<b>RUNNER CYCLE [{mode.upper()}]</b>\n"
                f"Contracts: {diag.get('contracts_found', '?')} | "
                f"Signals: {diag.get('signals_generated', '?')} | "
                f"Plans: {diag.get('plans_generated', '?')}\n"
                f"Considered: {considered} | Filled: {filled}"
            )
            self.telegram_send(summary)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] telegram summary failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SettleReport:
    settled: tuple[dict[str, Any], ...]
    unresolved: tuple[dict[str, Any], ...]
    total_pnl: float


def run_settle_cycle(
    *,
    live_db: LiveDatabase,
    resolve_outcome: Callable[[dict[str, Any]], tuple[int, float] | None],
    mode: str | None = None,
) -> SettleReport:
    """Resolve filled-unsettled positions and write back outcome + P&L.

    ``resolve_outcome(position_row)`` returns ``(outcome, observed_high_f)``
    or ``None`` if the market still isn't resolved. Isolated from persistence
    so tests can inject fakes without mocking out Polymarket Gamma and IEM.
    """
    open_rows = live_db.get_open_positions(mode=mode)
    settled: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    total_pnl = 0.0

    for pos in open_rows:
        resolved = resolve_outcome(pos)
        if resolved is None:
            unresolved.append(pos)
            continue
        outcome, _aux = resolved
        entry_price = float(pos.get("actual_vwap") or pos.get("quoted_best_ask") or 0.0)
        actual_cost = float(pos.get("actual_cost_usd") or 0.0)
        shares = float(pos.get("actual_shares") or 0.0)
        pnl = _settle_pnl(
            outcome=outcome,
            shares=shares,
            actual_cost_usd=actual_cost,
            entry_price=entry_price,
        )
        live_db.settle_attempt(row_id=int(pos["id"]), outcome=int(outcome), pnl=pnl)
        total_pnl += pnl
        settled.append({**pos, "outcome": outcome, "pnl": pnl, "entry_price": entry_price})

    return SettleReport(
        settled=tuple(settled),
        unresolved=tuple(unresolved),
        total_pnl=round(total_pnl, 4),
    )


def _settle_pnl(
    *,
    outcome: int,
    shares: float,
    actual_cost_usd: float,
    entry_price: float,
    fee_rate: float = 0.02,
) -> float:
    """Compute P&L using the actual fill (not notional request).

    Mirrors main.py::_pnl but operates on realized fills:
        WIN:  pnl = shares * (1 - entry_price) * (1 - fee)
        LOSS: pnl = -actual_cost_usd   (can't lose more than we spent)
    """
    if outcome == 1:
        return round(shares * max(0.0, 1.0 - entry_price) * (1.0 - fee_rate), 4)
    return round(-actual_cost_usd, 4)


# ---------------------------------------------------------------------------
# Helpers — small enough to keep inline with the runner.
# ---------------------------------------------------------------------------
def _portfolio_state_from_live(
    *,
    constraints: TradingConstraints,
    open_tokens: set[str],
    open_notional: float,
) -> PortfolioState:
    """Build a minimal PortfolioState that feeds the strategy's risk manager.

    We don't track realized equity here — the runner treats each cycle
    as equity-neutral and relies on ``live_db`` for the truth of what's
    open. ``open_positions`` maps each token to a placeholder notional
    so the strategy's duplicate-guard logic works, though the runner
    also enforces dedup itself in ``_handle_plan``.
    """
    cash = max(0.0, constraints.bankroll - open_notional)
    return PortfolioState(
        cash=cash,
        current_equity=constraints.bankroll,
        peak_equity=constraints.bankroll,
        open_positions={t: 0.0 for t in open_tokens},
        category_exposure={},
        category_position_counts={},
    )


def _expected_pnl(
    *,
    model_prob: float,
    entry_price: float,
    notional: float,
    fee_rate: float = 0.02,
) -> float:
    """Duplicate of main.py::_expected_pnl with the same YES-only derivation."""
    if entry_price <= 0 or notional <= 0:
        return 0.0
    win_per_dollar = (1.0 - fee_rate) * (1.0 - entry_price) / entry_price
    ev_per_dollar = model_prob * win_per_dollar - (1.0 - model_prob)
    return round(ev_per_dollar * notional, 4)


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_target_date(value: Any) -> date | str:
    """Strategy metadata stores target_date as ISO; accept either."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return date.today()


# ---------------------------------------------------------------------------
# Process lock — prevents concurrent live runs on the same Mac.
# ---------------------------------------------------------------------------
@contextmanager
def process_lock(path: Path = DEFAULT_LOCK_FILE) -> Iterator[None]:
    """Acquire an exclusive fcntl lock on ``path`` for the duration of the block.

    Raises ``RuntimeError`` if another process holds the lock. Never blocks —
    better to fail loudly than silently wait while the CLOB order window
    closes. The lock is released on process exit (kernel reclaims it)
    even if we crash mid-cycle.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Another polymarket_strat.live.runner is already running "
                f"(lock held on {path}). Wait for it to finish or remove the lock."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Real-dependency construction — isolated so tests can ignore it.
# ---------------------------------------------------------------------------
def _build_strategy_factory() -> Callable[[], Any]:
    """Returns a zero-arg callable that constructs a fresh WeatherBracketStrategy.

    Imports are deferred to avoid pulling heavy dependencies in shadow mode
    if they're missing from the local Mac env.
    """
    def _factory() -> Any:
        from pathlib import Path as _P

        from polymarket_strat.config import WeatherConfig
        from polymarket_strat.domain.weather.models import WeatherModel
        from polymarket_strat.domain.weather.strategy import WeatherBracketStrategy
        from polymarket_strat.infrastructure.weather.grib_client import GribDataClient
        from polymarket_strat.infrastructure.weather.market_scanner import WeatherMarketScanner
        from polymarket_strat.infrastructure.weather.persistence import WeatherDatabase
        from polymarket_strat.infrastructure.weather.station_client import StationObservationClient

        cfg = WeatherConfig()
        return WeatherBracketStrategy(
            grib_client=GribDataClient(cache_dir=_P(cfg.grib_cache_dir)),
            station_client=StationObservationClient(),
            market_scanner=WeatherMarketScanner(PolymarketPublicClient()),
            db=WeatherDatabase(cfg.db_path),
            min_edge=cfg.min_edge,
            fee_rate=cfg.fee_rate,
            model_weights={
                WeatherModel.GFS: cfg.gfs_weight,
                WeatherModel.ECMWF: cfg.ecmwf_weight,
                WeatherModel.HRRR: cfg.hrrr_weight,
                WeatherModel.NAM: cfg.nam_weight,
            },
        )
    return _factory


def _build_resolve_outcome() -> Callable[[dict[str, Any]], tuple[int, float] | None]:
    """Return a closure that resolves a position via Gamma → IEM fallback.

    Mirrors ``main.py::_resolve_via_polymarket`` + ``_settle_from_iem``.
    Isolated as a closure so the runner only constructs the heavy client
    objects once per settle cycle.
    """
    from polymarket_strat.api import PolymarketPublicClient as _GammaClient
    from polymarket_strat.domain.weather.models import CITY_REGISTRY
    from polymarket_strat.infrastructure.weather.station_client import (
        StationObservationClient as _Stations,
    )

    gamma = _GammaClient()
    stations = _Stations()

    def _resolve(pos: dict[str, Any]) -> tuple[int, float] | None:
        # 1. Polymarket Gamma — fast path for still-listed markets.
        mkt_id = pos.get("market_id") or pos.get("token_id")
        if mkt_id:
            try:
                mkt = gamma.get_market(str(mkt_id))
            except Exception:
                mkt = None
            if mkt:
                closed = bool(mkt.get("closed"))
                accepting_raw = mkt.get("acceptingOrders")
                accepting = True if accepting_raw is None else bool(accepting_raw)
                raw_prices = mkt.get("outcomePrices") or []
                if isinstance(raw_prices, str):
                    try:
                        raw_prices = json.loads(raw_prices)
                    except json.JSONDecodeError:
                        raw_prices = []
                if raw_prices and (closed or not accepting):
                    try:
                        p_yes = float(raw_prices[0])
                    except (TypeError, ValueError):
                        p_yes = -1.0
                    if p_yes >= 0.99:
                        return 1, p_yes
                    if p_yes <= 0.01:
                        return 0, p_yes

        # 2. IEM fallback — authoritative for weather outcomes.
        city = str(pos.get("city") or "")
        station = CITY_REGISTRY.get(city)
        target_str = str(pos.get("target_date") or "")
        try:
            target_d = date.fromisoformat(target_str)
        except (ValueError, TypeError):
            return None
        if not station:
            return None
        try:
            obs = stations.fetch_daily_highs(station, start=target_d, end=target_d)
        except Exception:
            return None
        if not obs:
            return None
        observed = obs[0].observed_high_f
        lower = float(pos.get("bracket_lower_f") or -999)
        upper = float(pos.get("bracket_upper_f") or 999)
        outcome = 1 if lower <= observed < upper else 0
        return outcome, observed

    return _resolve


def _build_telegram_send() -> Callable[[str], None] | None:
    """Build a Telegram send callback if credentials are set."""
    try:
        cfg = TelegramConfig.from_env()
    except ValueError:
        return None
    from polymarket_strat.notifications.telegram import TelegramNotifier
    notifier = TelegramNotifier(cfg)

    def _send(text: str) -> None:
        notifier.send_message(text)
    return _send


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_doctor(args: argparse.Namespace) -> int:
    skip = set(args.skip) if args.skip else set()
    report = run_doctor(rpc_url=args.rpc_url, min_usdc=args.min_usdc, skip=skip)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        from polymarket_strat.live.doctor import _render_human
        print(_render_human(report))
    return 0 if report.overall_passed else 1


def _cmd_shadow(args: argparse.Namespace) -> int:
    coord = LiveCoordinator(
        http_client=PolymarketPublicClient(),
        config=LiveExecutorConfig(mode="shadow", max_live_notional_per_trade=args.max_per_trade),
    )
    runner = LiveRunner(
        strategy_factory=_build_strategy_factory(),
        live_db=LiveDatabase(args.db or DEFAULT_LIVE_DB_PATH_STR),
        coordinator=coord,
        constraints=TradingConstraints(),
        telegram_send=_build_telegram_send(),
        max_live_total_open_usd=args.max_total_open,
    )
    report = runner.run_cycle()
    print(json.dumps(report.to_dict(), indent=2, default=str))
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    # 1. Doctor-gate.
    if not args.skip_doctor:
        report = run_doctor()
        if not report.overall_passed:
            from polymarket_strat.live.doctor import _render_human
            print(_render_human(report), file=sys.stderr)
            print(
                "\nDoctor failed — refusing to submit live orders. "
                "Pass --skip-doctor to override (NOT RECOMMENDED).",
                file=sys.stderr,
            )
            return 2

    # 2. Hold the process lock so parallel `live` runs can't race.
    try:
        lock_ctx = process_lock(DEFAULT_LOCK_FILE)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    with lock_ctx:
        account = AccountConfig.from_env()
        coord = LiveCoordinator(
            http_client=PolymarketPublicClient(),
            config=LiveExecutorConfig(
                mode="live",
                max_live_notional_per_trade=args.max_per_trade,
            ),
            account=account,
        )
        runner = LiveRunner(
            strategy_factory=_build_strategy_factory(),
            live_db=LiveDatabase(args.db or DEFAULT_LIVE_DB_PATH_STR),
            coordinator=coord,
            constraints=TradingConstraints(),
            telegram_send=_build_telegram_send(),
            max_live_total_open_usd=args.max_total_open,
        )
        report = runner.run_cycle()
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0 if report.skipped == "" else 1


def _cmd_settle(args: argparse.Namespace) -> int:
    db = LiveDatabase(args.db or DEFAULT_LIVE_DB_PATH_STR)
    resolve = _build_resolve_outcome()
    report = run_settle_cycle(live_db=db, resolve_outcome=resolve, mode=args.mode)
    print(json.dumps({
        "mode": args.mode,
        "settled_count": len(report.settled),
        "unresolved_count": len(report.unresolved),
        "total_pnl": report.total_pnl,
        "settled": [
            {
                "id": s.get("id"),
                "city": s.get("city"),
                "target_date": s.get("target_date"),
                "outcome": s.get("outcome"),
                "pnl": s.get("pnl"),
            }
            for s in report.settled
        ],
    }, indent=2, default=str))
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env_file()

    parser = argparse.ArgumentParser(
        prog="polymarket_strat.live.runner",
        description="Local Mac CLI for live + shadow Polymarket execution.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Run pre-flight checks.")
    p_doc.add_argument("--skip", action="append", help="Check names to skip (repeat or comma-sep).")
    p_doc.add_argument("--rpc-url", default=None)
    p_doc.add_argument("--min-usdc", type=float, default=10.0)
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=_cmd_doctor)

    p_sha = sub.add_parser("shadow", help="Run one shadow cycle (no real orders).")
    p_sha.add_argument("--db", default=None, help="Path to live.db (defaults to data/weather/live.db).")
    p_sha.add_argument("--max-per-trade", type=float, default=50.0)
    p_sha.add_argument("--max-total-open", type=float, default=100.0)
    p_sha.set_defaults(func=_cmd_shadow)

    p_liv = sub.add_parser("live", help="Run one live cycle (submits FOK orders to CLOB).")
    p_liv.add_argument("--db", default=None)
    p_liv.add_argument("--max-per-trade", type=float, default=10.0)
    p_liv.add_argument("--max-total-open", type=float, default=50.0)
    p_liv.add_argument("--skip-doctor", action="store_true",
                       help="Skip doctor gate (NOT RECOMMENDED).")
    p_liv.set_defaults(func=_cmd_live)

    p_set = sub.add_parser("settle", help="Settle filled-unsettled positions.")
    p_set.add_argument("--db", default=None)
    p_set.add_argument("--mode", default=None,
                       choices=(None, "live", "shadow"),
                       help="Only settle rows with this mode (omit for all).")
    p_set.set_defaults(func=_cmd_settle)

    args = parser.parse_args(argv)

    # Collapse --skip="a,b" → ["a", "b"] — argparse already stacks repeats.
    raw_skip = getattr(args, "skip", None)
    if raw_skip:
        flat: list[str] = []
        for entry in raw_skip:
            for token in entry.split(","):
                token = token.strip()
                if token:
                    flat.append(token)
        args.skip = flat

    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
