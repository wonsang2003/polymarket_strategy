"""LiveRunner + settle cycle + process lock.

These tests exercise the composition layer in ``polymarket_strat.live.runner``
without touching the real strategy, the real CLOB, or Polymarket Gamma. The
runner takes every dependency as a constructor arg, so the tests stand up fake
strategies, coordinators, and databases and assert that:

* kill switches (env flag + halt file) short-circuit the cycle
* duplicate-token guard only fires in live mode
* per-cycle total-notional budget caps extra fills
* ``executable=False`` plans are skipped only in live mode
* each runner-level reject still writes a row (so analytics sees the surfaced
  opportunity)
* per-fill + cycle-summary Telegram messages go out but never crash the cycle
* ``run_settle_cycle`` sums realized P&L using actual-fill math
* ``process_lock`` is exclusive — a second acquire raises fast
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from polymarket_strat.config import TradingConstraints
from polymarket_strat.domain.models import StrategyAnalysis, TradePlan
from polymarket_strat.live.executor import (
    FillResult,
    LiveCoordinator,
    LiveExecutorConfig,
    REASON_SHADOW,
    REASON_SUBMIT_ERROR,
)
from polymarket_strat.live.persistence import LiveDatabase
from polymarket_strat.live.runner import (
    DEFAULT_LOCK_FILE,
    REASON_RUNNER_BUDGET_EXHAUSTED,
    REASON_RUNNER_DUPLICATE,
    REASON_RUNNER_EMERGENCY_STOP,
    REASON_RUNNER_HALT_FILE,
    REASON_RUNNER_NOT_EXECUTABLE,
    CycleReport,
    LiveRunner,
    PlanOutcome,
    SettleReport,
    _expected_pnl,
    _settle_pnl,
    process_lock,
    run_settle_cycle,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCoordinator:
    """Mimics LiveCoordinator — returns canned FillResults by token_id.

    We don't subclass the real coordinator because its constructor wants a
    live HTTP client; the runner only ever touches ``.config.mode`` and
    ``.execute_buy()`` so a duck-typed stand-in is enough.
    """
    mode: str = "shadow"
    fills: dict[str, FillResult] = field(default_factory=dict)
    default_filled: bool = True
    calls: list[tuple[str, float]] = field(default_factory=list)

    @property
    def config(self) -> LiveExecutorConfig:
        return LiveExecutorConfig(mode=self.mode)

    def execute_buy(self, *, token_id: str, notional: float) -> FillResult:
        self.calls.append((token_id, notional))
        if token_id in self.fills:
            canned = self.fills[token_id]
            # respect the fill but update notional fields from the call
            return FillResult(
                mode=self.mode,
                token_id=token_id,
                notional_requested=notional,
                filled=canned.filled,
                reason=canned.reason,
                fill_ts=time.time(),
                actual_vwap=canned.actual_vwap,
                actual_cost_usd=canned.actual_cost_usd,
                actual_shares=canned.actual_shares,
                slippage_usd=canned.slippage_usd,
                quoted_best_ask=canned.quoted_best_ask,
                quoted_best_bid=canned.quoted_best_bid,
            )
        # default path — a clean fill at 0.50
        if self.default_filled:
            return FillResult(
                mode=self.mode,
                token_id=token_id,
                notional_requested=notional,
                filled=True,
                reason="" if self.mode == "live" else REASON_SHADOW,
                fill_ts=time.time(),
                actual_vwap=0.50,
                actual_cost_usd=notional,
                actual_shares=notional / 0.50,
                quoted_best_ask=0.50,
                quoted_best_bid=0.49,
            )
        return FillResult(
            mode=self.mode,
            token_id=token_id,
            notional_requested=notional,
            filled=False,
            reason=REASON_SUBMIT_ERROR,
            fill_ts=time.time(),
        )


@dataclass
class _FakeStrategy:
    """Mimics WeatherBracketStrategy — returns a preset StrategyAnalysis."""
    analysis: StrategyAnalysis
    calls: list[dict[str, Any]] = field(default_factory=list)

    def analyze(self, *, constraints: TradingConstraints, portfolio_state) -> StrategyAnalysis:
        self.calls.append({
            "constraints": constraints,
            "open_positions": set(portfolio_state.open_positions),
            "cash": portfolio_state.cash,
        })
        return self.analysis


def _make_plan(
    *,
    token_id: str = "tok-1",
    city: str = "seoul",
    target_date: str = "2026-04-22",
    target_notional: float = 5.0,
    reference_price: float = 0.50,
    executable: bool = True,
    model_prob: float = 0.70,
    edge: float = 0.10,
    regime: str = "STABLE_HIGH",
) -> TradePlan:
    return TradePlan(
        strategy_name="weather_bracket",
        market="0xmarket",
        question=f"{city} high temp?",
        category="weather",
        outcome="YES",
        token_id=token_id,
        side="YES",
        signal_score=1.5,
        target_notional=target_notional,
        reference_price=reference_price,
        best_ask=0.50,
        best_bid=0.49,
        spread=0.01,
        top_ask_size=100.0,
        top_bid_size=100.0,
        risk_score=0.1,
        expected_value=0.5,
        executable=executable,
        rationale=["test plan"],
        metadata={
            "city": city,
            "target_date": target_date,
            "bracket_lower_f": 69.8,
            "bracket_upper_f": 10000.0,
            "model_prob": model_prob,
            "edge_after_fees": edge,
            "regime": regime,
        },
    )


def _make_analysis(plans: list[TradePlan]) -> StrategyAnalysis:
    return StrategyAnalysis(
        strategy_name="weather_bracket",
        signals=[],
        trade_plan=plans,
        diagnostics={
            "contracts_found": len(plans),
            "signals_generated": len(plans),
            "plans_generated": len(plans),
        },
    )


@pytest.fixture
def live_db(tmp_path: Path) -> LiveDatabase:
    db = LiveDatabase(tmp_path / "live.db")
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def test_cycle_short_circuits_on_emergency_stop(live_db: LiveDatabase, monkeypatch):
    monkeypatch.setenv("EMERGENCY_STOP", "1")
    coord = _FakeCoordinator(mode="live")
    strat = _FakeStrategy(_make_analysis([_make_plan()]))
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    report = runner.run_cycle()
    assert report.skipped == REASON_RUNNER_EMERGENCY_STOP
    assert report.outcomes == ()
    assert strat.calls == []  # strategy never called
    assert coord.calls == []


def test_cycle_short_circuits_on_halt_file(live_db: LiveDatabase, tmp_path: Path):
    halt = tmp_path / "halt"
    halt.write_text("stop")
    coord = _FakeCoordinator(mode="live")
    strat = _FakeStrategy(_make_analysis([_make_plan()]))
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        halt_file=halt,
    )
    report = runner.run_cycle()
    assert report.skipped == REASON_RUNNER_HALT_FILE
    assert strat.calls == []


def test_cycle_runs_when_emergency_stop_not_set(live_db: LiveDatabase, monkeypatch):
    monkeypatch.delenv("EMERGENCY_STOP", raising=False)
    coord = _FakeCoordinator(mode="shadow")
    plan = _make_plan()
    strat = _FakeStrategy(_make_analysis([plan]))
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        halt_file=Path("/tmp/does-not-exist-xyz-123"),
    )
    report = runner.run_cycle()
    assert report.skipped == ""
    assert len(strat.calls) == 1


# ---------------------------------------------------------------------------
# Happy path — shadow + live
# ---------------------------------------------------------------------------


def test_shadow_cycle_fills_every_plan_and_writes_rows(live_db: LiveDatabase):
    plans = [_make_plan(token_id=f"tok-{i}") for i in range(3)]
    strat = _FakeStrategy(_make_analysis(plans))
    coord = _FakeCoordinator(mode="shadow")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    report = runner.run_cycle()
    assert report.filled_count == 3
    assert len(report.outcomes) == 3
    # All rows persisted
    recent = live_db.get_recent_attempts(limit=10)
    assert len(recent) == 3
    # Shadow rows still get a row_id
    for outcome in report.outcomes:
        assert outcome.row_id is not None
    # The fake coordinator was called for every plan
    assert len(coord.calls) == 3


def test_live_cycle_fills_plans_within_budget(live_db: LiveDatabase):
    plans = [_make_plan(token_id=f"tok-{i}", target_notional=5.0) for i in range(3)]
    strat = _FakeStrategy(_make_analysis(plans))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        max_live_total_open_usd=50.0,
    )
    report = runner.run_cycle()
    assert report.filled_count == 3
    assert all(o.filled for o in report.outcomes)


# ---------------------------------------------------------------------------
# Duplicate guard
# ---------------------------------------------------------------------------


def test_live_cycle_rejects_already_open_token(live_db: LiveDatabase):
    # Seed an already-open live fill for tok-dup.
    live_db.save_attempt(
        city="seoul",
        target_date=date(2026, 4, 22),
        fill_row={
            "mode": "live",
            "token_id": "tok-dup",
            "filled": 1,
            "reason": "",
            "notional_requested": 5.0,
            "quoted_best_ask": 0.50,
            "quoted_best_bid": 0.49,
            "quoted_spread": 0.01,
            "book_age_s": 1.0,
            "limit_price": 0.51,
            "shares_target": 10.0,
            "expected_vwap": 0.50,
            "expected_cost_usd": 5.0,
            "expected_slippage_per_share": 0.0,
            "depth_usd_within_limit": 50.0,
            "depth_shares_within_limit": 100.0,
            "order_id": "0xabc",
            "actual_vwap": 0.50,
            "actual_cost_usd": 5.0,
            "actual_shares": 10.0,
            "slippage_usd": 0.0,
            "error_message": "",
        },
    )

    plan = _make_plan(token_id="tok-dup")
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        max_live_total_open_usd=200.0,
    )
    report = runner.run_cycle()
    assert report.outcomes[0].filled is False
    assert report.outcomes[0].reason == REASON_RUNNER_DUPLICATE
    # Coordinator was NOT called — the runner short-circuited before submit.
    assert coord.calls == []
    # But a rejected row was still persisted for analytics.
    assert report.outcomes[0].row_id is not None


def test_shadow_cycle_ignores_duplicate_guard(live_db: LiveDatabase):
    # Same token already present, but shadow mode should still re-log.
    live_db.save_attempt(
        city="seoul",
        target_date=date(2026, 4, 22),
        fill_row={
            "mode": "live", "token_id": "tok-dup", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.0, "quoted_best_bid": 0.0,
            "quoted_spread": 0.0, "book_age_s": 0.0, "limit_price": 0.0,
            "shares_target": 0.0, "expected_vwap": 0.0, "expected_cost_usd": 0.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 0.0,
            "depth_shares_within_limit": 0.0, "order_id": "0xabc",
            "actual_vwap": 0.5, "actual_cost_usd": 5.0, "actual_shares": 10.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )
    plan = _make_plan(token_id="tok-dup")
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="shadow")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    report = runner.run_cycle()
    # Shadow ignores the guard — coord was called.
    assert coord.calls == [("tok-dup", 5.0)]
    assert report.filled_count == 1


# ---------------------------------------------------------------------------
# Executable flag
# ---------------------------------------------------------------------------


def test_live_cycle_rejects_non_executable_plan(live_db: LiveDatabase):
    plan = _make_plan(executable=False)
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    report = runner.run_cycle()
    assert report.outcomes[0].reason == REASON_RUNNER_NOT_EXECUTABLE
    assert coord.calls == []


def test_shadow_cycle_still_submits_non_executable_plan(live_db: LiveDatabase):
    plan = _make_plan(executable=False)
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="shadow")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    report = runner.run_cycle()
    # Shadow stress-tests the gates — coord is still called.
    assert len(coord.calls) == 1


# ---------------------------------------------------------------------------
# Budget cap
# ---------------------------------------------------------------------------


def test_live_cycle_stops_at_budget(live_db: LiveDatabase):
    plans = [
        _make_plan(token_id="tok-1", target_notional=30.0),
        _make_plan(token_id="tok-2", target_notional=30.0),
        _make_plan(token_id="tok-3", target_notional=30.0),
    ]
    strat = _FakeStrategy(_make_analysis(plans))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        max_live_total_open_usd=50.0,  # only one $30 fill fits; second exhausts
    )
    report = runner.run_cycle()
    reasons = [o.reason for o in report.outcomes]
    # The first plan fills (30 < 50). The second plan (30 > 20 remaining) is
    # rejected as budget_exhausted. Same for the third.
    assert report.outcomes[0].filled is True
    assert reasons[1] == REASON_RUNNER_BUDGET_EXHAUSTED
    assert reasons[2] == REASON_RUNNER_BUDGET_EXHAUSTED
    assert len(coord.calls) == 1


def test_live_cycle_starts_with_budget_net_of_already_open(live_db: LiveDatabase):
    # Pre-seed $40 of already-open live notional.
    live_db.save_attempt(
        city="seoul",
        target_date=date(2026, 4, 21),
        fill_row={
            "mode": "live", "token_id": "tok-existing", "filled": 1, "reason": "",
            "notional_requested": 40.0, "quoted_best_ask": 0.0, "quoted_best_bid": 0.0,
            "quoted_spread": 0.0, "book_age_s": 0.0, "limit_price": 0.0,
            "shares_target": 0.0, "expected_vwap": 0.0, "expected_cost_usd": 0.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 0.0,
            "depth_shares_within_limit": 0.0, "order_id": "0xabc",
            "actual_vwap": 0.5, "actual_cost_usd": 40.0, "actual_shares": 80.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )

    plans = [_make_plan(token_id="tok-new", target_notional=30.0)]
    strat = _FakeStrategy(_make_analysis(plans))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        max_live_total_open_usd=50.0,  # $50 cap − $40 already open = $10 remaining
    )
    report = runner.run_cycle()
    assert report.outcomes[0].reason == REASON_RUNNER_BUDGET_EXHAUSTED


# ---------------------------------------------------------------------------
# Portfolio state wiring — strategy sees open tokens for its own dedup.
# ---------------------------------------------------------------------------


def test_strategy_receives_open_tokens_from_live_db(live_db: LiveDatabase):
    live_db.save_attempt(
        city="seoul",
        target_date=date(2026, 4, 21),
        fill_row={
            "mode": "live", "token_id": "tok-existing", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.0, "quoted_best_bid": 0.0,
            "quoted_spread": 0.0, "book_age_s": 0.0, "limit_price": 0.0,
            "shares_target": 0.0, "expected_vwap": 0.0, "expected_cost_usd": 0.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 0.0,
            "depth_shares_within_limit": 0.0, "order_id": "0xabc",
            "actual_vwap": 0.5, "actual_cost_usd": 5.0, "actual_shares": 10.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )
    strat = _FakeStrategy(_make_analysis([]))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    runner.run_cycle()
    assert strat.calls[0]["open_positions"] == {"tok-existing"}
    # cash should be bankroll minus the $5 already open
    assert strat.calls[0]["cash"] == pytest.approx(995.0)


# ---------------------------------------------------------------------------
# Telegram wiring
# ---------------------------------------------------------------------------


def test_live_fill_sends_telegram_alert(live_db: LiveDatabase):
    sent: list[str] = []
    plan = _make_plan()
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        telegram_send=lambda msg: sent.append(msg),
    )
    runner.run_cycle()
    # Expect a per-fill alert + a cycle summary.
    assert any("LIVE FILL" in m for m in sent)
    assert any("RUNNER CYCLE" in m for m in sent)


def test_shadow_cycle_emits_no_summary_when_no_fills(live_db: LiveDatabase):
    plan = _make_plan(executable=False)  # shadow still fills, actually
    strat = _FakeStrategy(_make_analysis([]))  # empty plan list -> no fills
    coord = _FakeCoordinator(mode="shadow")
    sent: list[str] = []
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        telegram_send=lambda msg: sent.append(msg),
    )
    runner.run_cycle()
    assert sent == []  # chatter suppression


def test_telegram_send_failure_does_not_crash_cycle(live_db: LiveDatabase):
    def _bad_send(msg: str) -> None:
        raise RuntimeError("telegram down")

    plan = _make_plan()
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
        telegram_send=_bad_send,
    )
    # Should not raise even though telegram_send does.
    report = runner.run_cycle()
    assert report.filled_count == 1


# ---------------------------------------------------------------------------
# Persistence — runner rejects still persist rows
# ---------------------------------------------------------------------------


def test_runner_rejects_persist_rejected_rows(live_db: LiveDatabase):
    plan = _make_plan(executable=False)
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="live")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    runner.run_cycle()
    fill_rate = live_db.fill_rate(mode="live")
    assert fill_rate["n"] == 1
    assert fill_rate["filled"] == 0
    assert fill_rate["rejections"].get(REASON_RUNNER_NOT_EXECUTABLE) == 1


# ---------------------------------------------------------------------------
# Cycle report serialization
# ---------------------------------------------------------------------------


def test_cycle_report_to_dict_round_trip(live_db: LiveDatabase):
    plan = _make_plan()
    strat = _FakeStrategy(_make_analysis([plan]))
    coord = _FakeCoordinator(mode="shadow")
    runner = LiveRunner(
        strategy_factory=lambda: strat,
        live_db=live_db,
        coordinator=coord,
        constraints=TradingConstraints(bankroll=1000.0),
    )
    report = runner.run_cycle()
    payload = report.to_dict()
    assert payload["mode"] == "shadow"
    assert payload["filled_count"] == 1
    assert len(payload["outcomes"]) == 1
    assert payload["outcomes"][0]["token_id"] == plan.token_id
    assert payload["duration_s"] >= 0.0


# ---------------------------------------------------------------------------
# _settle_pnl & run_settle_cycle
# ---------------------------------------------------------------------------


def test_settle_pnl_win_uses_shares_and_entry_price():
    # 10 shares at 0.50 entry → win pays 10 * 0.50 * 0.98 = 4.90
    assert _settle_pnl(outcome=1, shares=10.0, actual_cost_usd=5.0, entry_price=0.50) == pytest.approx(4.90)


def test_settle_pnl_loss_returns_negative_actual_cost():
    assert _settle_pnl(outcome=0, shares=10.0, actual_cost_usd=5.0, entry_price=0.50) == -5.0


def test_settle_pnl_win_never_negative_on_entry_over_one():
    # Defensive: if entry_price bug pushes over 1.0, payout clamps at 0.
    assert _settle_pnl(outcome=1, shares=10.0, actual_cost_usd=5.0, entry_price=1.5) == 0.0


def test_run_settle_cycle_resolves_wins_and_losses(live_db: LiveDatabase):
    # Two open live fills, one will resolve YES, one NO.
    win_id = live_db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row={
            "mode": "live", "token_id": "tok-win", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.50, "quoted_best_bid": 0.49,
            "quoted_spread": 0.01, "book_age_s": 1.0, "limit_price": 0.51,
            "shares_target": 10.0, "expected_vwap": 0.50, "expected_cost_usd": 5.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 50.0,
            "depth_shares_within_limit": 100.0, "order_id": "0xabc",
            "actual_vwap": 0.50, "actual_cost_usd": 5.0, "actual_shares": 10.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )
    loss_id = live_db.save_attempt(
        city="nyc", target_date=date(2026, 4, 22),
        fill_row={
            "mode": "live", "token_id": "tok-loss", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.60, "quoted_best_bid": 0.59,
            "quoted_spread": 0.01, "book_age_s": 1.0, "limit_price": 0.61,
            "shares_target": 8.33, "expected_vwap": 0.60, "expected_cost_usd": 5.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 50.0,
            "depth_shares_within_limit": 100.0, "order_id": "0xdef",
            "actual_vwap": 0.60, "actual_cost_usd": 5.0, "actual_shares": 8.33,
            "slippage_usd": 0.0, "error_message": "",
        },
    )

    def _resolve(pos: dict[str, Any]) -> tuple[int, float] | None:
        if pos["token_id"] == "tok-win":
            return 1, 80.0
        if pos["token_id"] == "tok-loss":
            return 0, 50.0
        return None

    report = run_settle_cycle(live_db=live_db, resolve_outcome=_resolve, mode="live")
    assert len(report.settled) == 2
    assert report.unresolved == ()

    # Total pnl = win (10 * 0.50 * 0.98 = 4.90) − loss (5.00) = −0.10
    assert report.total_pnl == pytest.approx(-0.10)

    # DB rows also updated.
    rows = live_db.get_recent_attempts(limit=10)
    settled = {r["id"]: r for r in rows}
    assert settled[win_id]["outcome"] == 1
    assert settled[win_id]["pnl"] == pytest.approx(4.90)
    assert settled[loss_id]["outcome"] == 0
    assert settled[loss_id]["pnl"] == pytest.approx(-5.0)


def test_run_settle_cycle_leaves_unresolved_alone(live_db: LiveDatabase):
    rid = live_db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row={
            "mode": "live", "token_id": "tok-pending", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.50, "quoted_best_bid": 0.49,
            "quoted_spread": 0.01, "book_age_s": 1.0, "limit_price": 0.51,
            "shares_target": 10.0, "expected_vwap": 0.50, "expected_cost_usd": 5.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 50.0,
            "depth_shares_within_limit": 100.0, "order_id": "0xabc",
            "actual_vwap": 0.50, "actual_cost_usd": 5.0, "actual_shares": 10.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )
    report = run_settle_cycle(
        live_db=live_db,
        resolve_outcome=lambda pos: None,  # always unresolved
        mode="live",
    )
    assert report.settled == ()
    assert len(report.unresolved) == 1
    rows = live_db.get_recent_attempts()
    assert rows[0]["outcome"] is None  # unchanged


def test_run_settle_cycle_filters_by_mode(live_db: LiveDatabase):
    live_db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row={
            "mode": "live", "token_id": "tok-live", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.50, "quoted_best_bid": 0.49,
            "quoted_spread": 0.01, "book_age_s": 1.0, "limit_price": 0.51,
            "shares_target": 10.0, "expected_vwap": 0.50, "expected_cost_usd": 5.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 50.0,
            "depth_shares_within_limit": 100.0, "order_id": "x",
            "actual_vwap": 0.50, "actual_cost_usd": 5.0, "actual_shares": 10.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )
    live_db.save_attempt(
        city="seoul", target_date=date(2026, 4, 22),
        fill_row={
            "mode": "shadow", "token_id": "tok-shadow", "filled": 1, "reason": "",
            "notional_requested": 5.0, "quoted_best_ask": 0.50, "quoted_best_bid": 0.49,
            "quoted_spread": 0.01, "book_age_s": 1.0, "limit_price": 0.51,
            "shares_target": 10.0, "expected_vwap": 0.50, "expected_cost_usd": 5.0,
            "expected_slippage_per_share": 0.0, "depth_usd_within_limit": 50.0,
            "depth_shares_within_limit": 100.0, "order_id": "y",
            "actual_vwap": 0.50, "actual_cost_usd": 5.0, "actual_shares": 10.0,
            "slippage_usd": 0.0, "error_message": "",
        },
    )

    seen_tokens: list[str] = []

    def _resolve(pos: dict[str, Any]) -> tuple[int, float] | None:
        seen_tokens.append(pos["token_id"])
        return 1, 80.0

    report = run_settle_cycle(live_db=live_db, resolve_outcome=_resolve, mode="live")
    assert seen_tokens == ["tok-live"]
    assert len(report.settled) == 1


# ---------------------------------------------------------------------------
# _expected_pnl helper
# ---------------------------------------------------------------------------


def test_expected_pnl_matches_yes_only_ev():
    # $5 at 0.5 with P=0.7 →
    # win/$ = 0.98 * 0.5 / 0.5 = 0.98
    # EV/$ = 0.7 * 0.98 − 0.3 = 0.386
    # EV = 1.93
    ev = _expected_pnl(model_prob=0.70, entry_price=0.50, notional=5.0)
    assert ev == pytest.approx(1.93)


def test_expected_pnl_zero_when_notional_or_price_missing():
    assert _expected_pnl(model_prob=0.70, entry_price=0.0, notional=5.0) == 0.0
    assert _expected_pnl(model_prob=0.70, entry_price=0.50, notional=0.0) == 0.0


# ---------------------------------------------------------------------------
# process_lock — exclusive access
# ---------------------------------------------------------------------------


def test_process_lock_allows_single_holder(tmp_path: Path):
    lock_path = tmp_path / "runner.lock"
    with process_lock(lock_path):
        assert lock_path.exists()
    # Releasing on exit: second acquire should succeed.
    with process_lock(lock_path):
        pass


def test_process_lock_rejects_second_holder(tmp_path: Path):
    """Second acquire from a separate thread should fail fast.

    fcntl.flock is per-file-descriptor — a second thread opening a fresh
    fd and asking for LOCK_EX|LOCK_NB while the first holder is still active
    gets BlockingIOError, which we re-raise as RuntimeError.
    """
    lock_path = tmp_path / "runner.lock"
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def _first():
        try:
            with process_lock(lock_path):
                acquired.set()
                release.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_first)
    t1.start()
    assert acquired.wait(timeout=5.0), "first holder never acquired"

    try:
        with pytest.raises(RuntimeError, match="already running"):
            with process_lock(lock_path):
                pytest.fail("second holder should not have acquired")
    finally:
        release.set()
        t1.join(timeout=5.0)

    assert not errors
