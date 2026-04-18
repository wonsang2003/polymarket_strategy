from __future__ import annotations

import html
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from polymarket_strat.application.service import StrategyApplicationService
from polymarket_strat.config import PortfolioState, TradingConstraints


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.2f}"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def _titleize(name: str) -> str:
    return name.replace("_", " ").title()


def _json_block(payload: Any) -> str:
    return _escape(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _tone_from_bool(value: bool) -> str:
    return "good" if value else "bad"


def _tone_from_return(value: float) -> str:
    if value > 0:
        return "good"
    if value < 0:
        return "bad"
    return "neutral"


def _tone_from_edge(edge: float | None) -> str:
    if edge is None:
        return "neutral"
    if abs(edge) >= 0.1:
        return "good"
    if abs(edge) >= 0.05:
        return "warn"
    return "neutral"


def _pill(label: str, tone: str = "neutral") -> str:
    return f'<span class="pill pill-{tone}">{_escape(label)}</span>'


def _metric_card(label: str, value: str, note: str = "", tone: str = "neutral") -> str:
    note_html = f'<div class="metric-note">{_escape(note)}</div>' if note else ""
    return f"""
    <article class="metric-card metric-card-{tone}">
      <div class="metric-label">{_escape(label)}</div>
      <div class="metric-value">{value}</div>
      {note_html}
    </article>
    """


def _sparkline(values: list[float], *, stroke: str, fill: str, gradient_id: str) -> str:
    if not values:
        return '<svg class="sparkline" viewBox="0 0 320 120" preserveAspectRatio="none"></svg>'
    low = min(values)
    high = max(values)
    span = max(high - low, 1e-9)
    points = []
    for index, value in enumerate(values):
        x = index / max(len(values) - 1, 1) * 320
        y = 100 - ((value - low) / span) * 80
        points.append(f"{x:.2f},{y:.2f}")
    polyline = " ".join(points)
    area = f"0,100 {polyline} 320,100"
    return f"""
    <svg class="sparkline" viewBox="0 0 320 120" preserveAspectRatio="none">
      <defs>
        <linearGradient id="{_escape(gradient_id)}" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="{fill}" stop-opacity="0.52"/>
          <stop offset="100%" stop-color="{fill}" stop-opacity="0.04"/>
        </linearGradient>
      </defs>
      <polygon points="{area}" fill="url(#{_escape(gradient_id)})"></polygon>
      <polyline points="{polyline}" fill="none" stroke="{stroke}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></polyline>
    </svg>
    """


def _equity_curve(returns: list[float]) -> list[float]:
    equity = 1.0
    curve = [equity]
    for value in returns:
        equity *= 1.0 + value
        curve.append(equity)
    return curve


def _render_key_value_grid(payload: dict[str, Any]) -> str:
    if not payload:
        return '<div class="empty-state">No structured metadata available.</div>'
    cells = []
    for key, value in payload.items():
        if isinstance(value, float):
            rendered = _fmt_num(value)
        else:
            rendered = _escape(value)
        cells.append(
            f"""
            <div class="kv-card">
              <span>{_escape(key.replace('_', ' ').title())}</span>
              <strong>{rendered}</strong>
            </div>
            """
        )
    return "".join(cells)


def _render_signal_cards(signals: list[dict[str, Any]], *, strategy_slug: str) -> str:
    cards = []
    for index, signal in enumerate(signals):
        edge = signal.get("metadata", {}).get("edge")
        tone = _tone_from_edge(float(edge)) if isinstance(edge, (int, float)) else "neutral"
        cards.append(
            f"""
            <article class="signal-card">
              <div class="signal-top">
                <div>
                  <div class="signal-question">{_escape(signal.get("question", ""))}</div>
                  <div class="signal-subtitle">{_escape(signal.get("market", ""))} · {_escape(signal.get("category", ""))}</div>
                </div>
                {_pill(signal.get("side", "Signal"), tone)}
              </div>
              <div class="signal-grid">
                <div><span>Score</span><strong>{_fmt_num(float(signal.get("signal_score", 0.0)))}</strong></div>
                <div><span>Price</span><strong>{_fmt_num(float(signal.get("reference_price", 0.0)))}</strong></div>
                <div><span>Edge</span><strong>{_fmt_pct(float(edge)) if isinstance(edge, (int, float)) else "n/a"}</strong></div>
              </div>
              <details class="detail-box">
                <summary>Signal Context</summary>
                <pre>{_json_block(signal.get("metadata", {}))}</pre>
              </details>
            </article>
            """
        )
    if not cards:
        return f'<div class="empty-state" id="{strategy_slug}-signal-empty">No current signals.</div>'
    return "".join(cards)


def _render_trade_cards(trade_plan: list[dict[str, Any]], *, strategy_slug: str) -> tuple[str, str]:
    cards = []
    panels = []
    if not trade_plan:
        empty = '<div class="empty-state">No trade plan available.</div>'
        return empty, empty

    for index, plan in enumerate(trade_plan):
        plan_id = f"{strategy_slug}-decision-{index}"
        executable = bool(plan.get("executable"))
        tone = _tone_from_bool(executable)
        metadata = plan.get("metadata", {})
        risk_metrics = plan.get("risk_metrics", {})
        edge = metadata.get("edge")
        outcome_tone = _tone_from_edge(float(edge)) if isinstance(edge, (int, float)) else tone
        rationale_items = "".join(f"<li>{_escape(reason)}</li>" for reason in plan.get("rationale", []))
        metadata_grid = _render_key_value_grid(metadata)
        risk_grid = _render_key_value_grid(risk_metrics)
        search_blob = " ".join(
            [
                str(plan.get("question", "")),
                str(plan.get("market", "")),
                str(plan.get("category", "")),
                " ".join(str(item) for item in plan.get("rationale", [])),
                json.dumps(metadata, sort_keys=True),
            ]
        ).lower()

        cards.append(
            f"""
            <article class="decision-card" data-filter-state="{"executable" if executable else "blocked"}" data-search-text="{_escape(search_blob)}">
              <button class="decision-button {'is-active' if index == 0 else ''}" type="button" data-decision-target="{plan_id}">
                <div class="decision-top">
                  <div>
                    <div class="decision-question">{_escape(plan.get("question", ""))}</div>
                    <div class="decision-subtitle">{_escape(plan.get("market", ""))} · {_escape(plan.get("category", ""))}</div>
                  </div>
                  <div class="decision-pills">
                    {_pill("Executable" if executable else "Blocked", tone)}
                    {_pill(plan.get("side", ""), outcome_tone)}
                  </div>
                </div>
                <div class="decision-stats">
                  <div><span>Target</span><strong>{_fmt_money(float(plan.get("target_notional", 0.0)))}</strong></div>
                  <div><span>Risk</span><strong>{_fmt_pct(float(plan.get("risk_score", 0.0)))}</strong></div>
                  <div><span>EV</span><strong>{_fmt_pct(float(plan.get("expected_value", 0.0)))}</strong></div>
                  <div><span>Spread</span><strong>{_fmt_num(float(plan.get("spread", 0.0)))}</strong></div>
                </div>
              </button>
            </article>
            """
        )

        panels.append(
            f"""
            <article class="decision-detail {'is-active' if index == 0 else ''}" id="{plan_id}">
              <div class="decision-detail-header">
                <div>
                  <div class="eyebrow">Decision Lab</div>
                  <h3>{_escape(plan.get("question", ""))}</h3>
                  <p>{_escape(plan.get("market", ""))} · {_escape(plan.get("outcome", ""))} · {_escape(plan.get("category", ""))}</p>
                </div>
                <div class="decision-pills">
                  {_pill("Executable" if executable else "Blocked", tone)}
                  {_pill(plan.get("side", ""), outcome_tone)}
                </div>
              </div>
              <div class="metric-strip">
                {_metric_card("Reference Price", _fmt_num(float(plan.get("reference_price", 0.0))), tone="neutral")}
                {_metric_card("Best Ask", _fmt_num(float(plan.get("best_ask", 0.0))), tone="neutral")}
                {_metric_card("Best Bid", _fmt_num(float(plan.get("best_bid", 0.0))), tone="neutral")}
                {_metric_card("Top Ask Size", _fmt_num(float(plan.get("top_ask_size", 0.0))), tone="neutral")}
                {_metric_card("Top Bid Size", _fmt_num(float(plan.get("top_bid_size", 0.0))), tone="neutral")}
                {_metric_card("Signal Score", _fmt_num(float(plan.get("signal_score", 0.0))), tone=outcome_tone)}
              </div>
              <div class="detail-layout">
                <section class="detail-panel">
                  <div class="panel-title">Decision Reasoning</div>
                  <ol class="reasoning-list">{rationale_items or '<li>No rationale recorded.</li>'}</ol>
                </section>
                <section class="detail-panel">
                  <div class="panel-title">Trade Metadata</div>
                  <div class="kv-grid">{metadata_grid}</div>
                </section>
                <section class="detail-panel">
                  <div class="panel-title">Risk Breakdown</div>
                  <div class="kv-grid">{risk_grid}</div>
                </section>
                <section class="detail-panel">
                  <div class="panel-title">Raw Payload</div>
                  <details class="detail-box" open>
                    <summary>Inspect full trade plan object</summary>
                    <pre>{_json_block(plan)}</pre>
                  </details>
                </section>
              </div>
            </article>
            """
        )

    return "".join(cards), "".join(panels)


def _render_trade_rows(trades: list[dict[str, Any]], *, strategy_slug: str) -> str:
    rows = []
    for index, trade in enumerate(trades):
        pnl = float(trade.get("pnl", 0.0))
        return_pct = float(trade.get("return_pct", 0.0))
        tone = _tone_from_return(pnl)
        metadata = trade.get("metadata", {})
        search_blob = " ".join(
            [
                str(trade.get("market", "")),
                str(trade.get("side", "")),
                json.dumps(metadata, sort_keys=True),
            ]
        ).lower()
        rows.append(
            f"""
            <tr data-search-text="{_escape(search_blob)}">
              <td>{index + 1}</td>
              <td>{_escape(trade.get("market", ""))}</td>
              <td>{_escape(trade.get("side", ""))}</td>
              <td data-sort="{float(trade.get('entry_price', 0.0)):.8f}">{_fmt_num(float(trade.get("entry_price", 0.0)))}</td>
              <td data-sort="{float(trade.get('exit_price', 0.0)):.8f}">{_fmt_num(float(trade.get("exit_price", 0.0)))}</td>
              <td class="text-{tone}" data-sort="{return_pct:.8f}">{_fmt_pct(return_pct)}</td>
              <td class="text-{tone}" data-sort="{pnl:.8f}">{_fmt_num(pnl)}</td>
              <td>
                <details class="table-details">
                  <summary>Inspect</summary>
                  <pre>{_json_block(metadata)}</pre>
                </details>
              </td>
            </tr>
            """
        )
    if not rows:
        return f'<tr><td colspan="8" class="muted-cell" id="{strategy_slug}-trades-empty">No trades available.</td></tr>'
    return "".join(rows)


def _render_strategy_tabs(analyses: list[dict[str, Any]], backtests_by_name: dict[str, dict[str, Any]]) -> str:
    buttons = []
    for index, analysis in enumerate(analyses):
        backtest = backtests_by_name[analysis["strategy_name"]]
        name = _titleize(analysis["strategy_name"])
        tone = _tone_from_bool(bool(backtest["passed"]))
        buttons.append(
            f"""
            <button class="strategy-tab {'is-active' if index == 0 else ''}" type="button" data-tab-target="{_slug(analysis['strategy_name'])}">
              <span class="strategy-tab-name">{_escape(name)}</span>
              <span class="strategy-tab-stats">
                {_pill("Pass" if backtest["passed"] else "Fail", tone)}
                <span>{_fmt_pct(float(backtest["expected_value"]))} EV</span>
              </span>
            </button>
            """
        )
    return "".join(buttons)


def _render_strategy_panel(analysis: dict[str, Any], backtest: dict[str, Any], *, index: int) -> str:
    strategy_slug = _slug(analysis["strategy_name"])
    trade_returns = [float(trade["return_pct"]) for trade in backtest["trades"]]
    curve = _equity_curve(trade_returns)
    signal_scores = [float(signal["signal_score"]) for signal in analysis["signals"]]
    strongest_signal = max(signal_scores, default=0.0)
    executable_count = sum(1 for plan in analysis["trade_plan"] if plan["executable"])
    blocked_count = len(analysis["trade_plan"]) - executable_count
    status_tone = _tone_from_bool(bool(backtest["passed"]))
    diagnostics_json = _json_block(backtest["diagnostics"])
    analysis_diagnostics_json = _json_block(analysis["diagnostics"])
    trade_cards, detail_panels = _render_trade_cards(analysis["trade_plan"], strategy_slug=strategy_slug)
    signal_cards = _render_signal_cards(analysis["signals"], strategy_slug=strategy_slug)
    diagnostics_grid = _render_key_value_grid(
        {
            "Trade Count": backtest["trade_count"],
            "Mean Return": _fmt_pct(float(backtest["mean_return"])),
            "Median Return": _fmt_pct(float(backtest["median_return"])),
            "Win Rate": _fmt_pct(float(backtest["win_rate"])),
            "Max Drawdown": _fmt_pct(float(backtest["max_drawdown"])),
            "Calibration Error": _fmt_pct(backtest["calibration_error"]) if backtest["calibration_error"] is not None else "n/a",
            "Executable Ideas": executable_count,
            "Blocked Ideas": blocked_count,
        }
    )

    return f"""
    <section class="strategy-panel {'is-active' if index == 0 else ''}" data-tab-panel="{strategy_slug}">
      <div class="strategy-hero">
        <div class="strategy-copy">
          <div class="eyebrow">{_escape(_titleize(analysis["strategy_name"]))}</div>
          <h2>{_escape(_titleize(analysis["strategy_name"]))}</h2>
          <p>Interactive research dossier for this strategy, including live decision rationale, signal evidence, historical outcome review, and raw diagnostics for auditability.</p>
        </div>
        <div class="strategy-status-cluster">
          {_pill("PASS" if backtest["passed"] else "FAIL", status_tone)}
          {_pill(f"{len(analysis['trade_plan'])} ideas", "neutral")}
          {_pill(f"{executable_count} executable", "good" if executable_count else "neutral")}
        </div>
      </div>

      <div class="metrics-grid">
        {_metric_card("Backtest EV", _fmt_pct(float(backtest["expected_value"])), note="Expected value across historical trades", tone=status_tone)}
        {_metric_card("Win Rate", _fmt_pct(float(backtest["win_rate"])), note="Historical hit rate", tone="good" if float(backtest["win_rate"]) >= 0.5 else "bad")}
        {_metric_card("Max Drawdown", _fmt_pct(float(backtest["max_drawdown"])), note="Worst observed equity pullback", tone="bad" if float(backtest["max_drawdown"]) > 0.2 else "good")}
        {_metric_card("Trade Count", _fmt_num(int(backtest["trade_count"])), note="Historical sample size")}
        {_metric_card("Strongest Signal", _fmt_num(strongest_signal), note=f"{len(signal_scores)} current signals")}
        {_metric_card("Calibration Error", _fmt_pct(backtest["calibration_error"]) if backtest["calibration_error"] is not None else "n/a", note="Lower is better")}
      </div>

      <div class="visual-grid">
        <article class="visual-card">
          <div class="visual-card-top">
            <div>
              <div class="visual-title">Equity Arc</div>
              <strong>{_escape(_titleize(analysis["strategy_name"]))}</strong>
            </div>
            {_pill("Drawdown Focus", "bad" if float(backtest["max_drawdown"]) > 0.2 else "good")}
          </div>
          {_sparkline(curve or [1.0], stroke="#ff7d61" if not backtest["passed"] else "#3ddc97", fill="#ffbca7" if not backtest["passed"] else "#8bffd1", gradient_id=f'{strategy_slug}-equity')}
        </article>
        <article class="visual-card">
          <div class="visual-card-top">
            <div>
              <div class="visual-title">Signal Pressure</div>
              <strong>Current signal strength</strong>
            </div>
            {_pill(f"{len(analysis['signals'])} signals", "neutral")}
          </div>
          {_sparkline(signal_scores or [0.0], stroke="#3fb8ff", fill="#86d6ff", gradient_id=f'{strategy_slug}-signal')}
        </article>
        <article class="visual-card">
          <div class="visual-card-top">
            <div>
              <div class="visual-title">Return Beats</div>
              <strong>Per-trade outcome cadence</strong>
            </div>
            {_pill("PnL Rhythm", "neutral")}
          </div>
          {_sparkline(trade_returns or [0.0], stroke="#f4c76f", fill="#ffe3a9", gradient_id=f'{strategy_slug}-returns')}
        </article>
      </div>

      <section class="insight-grid">
        <article class="insight-card">
          <div class="section-heading">
            <div>
              <div class="eyebrow">Decision Theater</div>
              <h3>Why the strategy wants to buy or stay out</h3>
            </div>
            <div class="toolbar">
              <button class="filter-chip is-active" type="button" data-filter-panel="{strategy_slug}" data-filter-value="all">All</button>
              <button class="filter-chip" type="button" data-filter-panel="{strategy_slug}" data-filter-value="executable">Executable</button>
              <button class="filter-chip" type="button" data-filter-panel="{strategy_slug}" data-filter-value="blocked">Blocked</button>
            </div>
          </div>
          <div class="search-shell">
            <input class="search-input" type="search" placeholder="Search rationale, market, metadata" data-search-panel="{strategy_slug}" />
          </div>
          <div class="decision-grid" data-decision-grid="{strategy_slug}">
            <div class="decision-list">{trade_cards}</div>
            <div class="decision-detail-stack" data-decision-details="{strategy_slug}">{detail_panels}</div>
          </div>
        </article>

        <article class="insight-card">
          <div class="section-heading">
            <div>
              <div class="eyebrow">Signal Stream</div>
              <h3>What the model is seeing right now</h3>
            </div>
          </div>
          <div class="signal-stack">{signal_cards}</div>
        </article>
      </section>

      <section class="insight-grid">
        <article class="insight-card">
          <div class="section-heading">
            <div>
              <div class="eyebrow">Backtest Tape</div>
              <h3>Historical trade log</h3>
            </div>
            <div class="toolbar toolbar-note">Click column headers to sort.</div>
          </div>
          <div class="search-shell">
            <input class="search-input" type="search" placeholder="Search historical trades" data-search-table="{strategy_slug}-trades-table" />
          </div>
          <div class="table-shell">
            <table class="sortable-table" id="{strategy_slug}-trades-table">
              <thead>
                <tr>
                  <th data-sort-type="number">#</th>
                  <th data-sort-type="text">Market</th>
                  <th data-sort-type="text">Side</th>
                  <th data-sort-type="number">Entry</th>
                  <th data-sort-type="number">Exit</th>
                  <th data-sort-type="number">Return</th>
                  <th data-sort-type="number">PnL</th>
                  <th data-sort-type="text">Metadata</th>
                </tr>
              </thead>
              <tbody>
                {_render_trade_rows(backtest["trades"], strategy_slug=strategy_slug)}
              </tbody>
            </table>
          </div>
        </article>

        <article class="insight-card">
          <div class="section-heading">
            <div>
              <div class="eyebrow">Diagnostics Vault</div>
              <h3>Audit trail and model internals</h3>
            </div>
          </div>
          <div class="diagnostics-grid">{diagnostics_grid}</div>
          <details class="detail-box">
            <summary>Backtest Diagnostics</summary>
            <pre>{diagnostics_json}</pre>
          </details>
          <details class="detail-box">
            <summary>Analysis Diagnostics</summary>
            <pre>{analysis_diagnostics_json}</pre>
          </details>
        </article>
      </section>
    </section>
    """


def build_strategy_report(*, use_sample: bool, state_path: str) -> str:
    constraints = TradingConstraints()
    portfolio_state = PortfolioState.load(state_path, constraints)
    service = StrategyApplicationService(use_sample=use_sample)
    strategies = service.available_strategies()

    analyses = []
    for name in strategies:
        projected = portfolio_state.clone()
        analysis = service.analyze(name, constraints=constraints, portfolio_state=projected)
        analyses.append(service.describe_analysis(analysis))

    backtests = [asdict(result) for result in service.backtest("all")]
    backtests_by_name = {item["strategy_name"]: item for item in backtests}
    real_status = service.real_data_status()

    total_trades = sum(item["trade_count"] for item in backtests)
    passed_count = sum(1 for item in backtests if item["passed"])
    executable_count = sum(sum(1 for plan in analysis["trade_plan"] if plan["executable"]) for analysis in analyses)
    avg_ev = sum(float(item["expected_value"]) for item in backtests) / max(len(backtests), 1)
    best_strategy = max(backtests, key=lambda item: float(item["expected_value"]), default=None)
    worst_drawdown = max((float(item["max_drawdown"]) for item in backtests), default=0.0)
    strategy_tabs = _render_strategy_tabs(analyses, backtests_by_name)
    strategy_panels = "".join(
        _render_strategy_panel(analysis, backtests_by_name[analysis["strategy_name"]], index=index)
        for index, analysis in enumerate(analyses)
    )

    html_report = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1"/>
      <title>Polymarket Strategy Intelligence</title>
      <style>
        :root {{
          --bg-0: #050b16;
          --bg-1: #0a1324;
          --bg-2: rgba(11, 23, 42, 0.88);
          --panel: rgba(14, 29, 52, 0.78);
          --panel-strong: rgba(16, 33, 60, 0.94);
          --line: rgba(255, 255, 255, 0.08);
          --line-strong: rgba(255, 255, 255, 0.14);
          --text: #f5f7fb;
          --muted: #8ea2c6;
          --muted-strong: #bed0ec;
          --good: #35d98d;
          --bad: #ff7d61;
          --warn: #f0c366;
          --accent: #52c8ff;
          --accent-soft: rgba(82, 200, 255, 0.18);
          --gold: #ffd07e;
          --shadow: 0 34px 80px rgba(0, 0, 0, 0.34);
          --radius-xl: 30px;
          --radius-lg: 24px;
          --radius-md: 18px;
          --radius-sm: 14px;
        }}
        * {{ box-sizing: border-box; }}
        html {{ scroll-behavior: smooth; }}
        body {{
          margin: 0;
          font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
          background:
            radial-gradient(circle at 14% 16%, rgba(82, 200, 255, 0.20), transparent 24%),
            radial-gradient(circle at 86% 10%, rgba(255, 208, 126, 0.16), transparent 20%),
            radial-gradient(circle at 74% 74%, rgba(53, 217, 141, 0.12), transparent 22%),
            linear-gradient(180deg, #040914 0%, #081121 36%, #050b16 100%);
          color: var(--text);
          min-height: 100vh;
        }}
        body::before {{
          content: "";
          position: fixed;
          inset: 0;
          background-image:
            linear-gradient(rgba(255, 255, 255, 0.02) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
          background-size: 32px 32px;
          mask-image: radial-gradient(circle at center, black 32%, transparent 85%);
          pointer-events: none;
        }}
        .shell {{
          width: min(1560px, calc(100% - 40px));
          margin: 0 auto;
          padding: 26px 0 90px;
          position: relative;
          z-index: 1;
        }}
        .hero {{
          position: relative;
          overflow: hidden;
          background:
            linear-gradient(140deg, rgba(12, 24, 45, 0.96), rgba(8, 17, 31, 0.86)),
            radial-gradient(circle at top right, rgba(82, 200, 255, 0.22), transparent 30%);
          border: 1px solid var(--line);
          border-radius: 34px;
          padding: 34px;
          box-shadow: var(--shadow);
        }}
        .hero::after {{
          content: "";
          position: absolute;
          inset: auto -5% -18% auto;
          width: 420px;
          height: 420px;
          border-radius: 50%;
          background: radial-gradient(circle, rgba(82, 200, 255, 0.20), transparent 66%);
          filter: blur(4px);
          pointer-events: none;
        }}
        .eyebrow {{
          font-size: 12px;
          letter-spacing: 0.18em;
          text-transform: uppercase;
          color: var(--gold);
          margin-bottom: 12px;
        }}
        h1, h2, h3, p {{
          margin: 0;
        }}
        h1 {{
          font-size: clamp(42px, 7vw, 82px);
          line-height: 0.92;
          letter-spacing: -0.05em;
          max-width: 11ch;
        }}
        .hero-copy {{
          display: flex;
          justify-content: space-between;
          gap: 28px;
          align-items: end;
        }}
        .hero-lead {{
          max-width: 78ch;
        }}
        .hero-lead p {{
          margin-top: 18px;
          color: var(--muted);
          font-size: 16px;
          line-height: 1.7;
        }}
        .hero-badges {{
          display: flex;
          gap: 12px;
          flex-wrap: wrap;
          justify-content: end;
          max-width: 420px;
        }}
        .summary-shell {{
          display: grid;
          grid-template-columns: 1.25fr 0.85fr;
          gap: 18px;
          margin-top: 28px;
        }}
        .panel, .metric-card, .insight-card, .strategy-tab, .signal-card, .visual-card, .detail-panel, .decision-button, .kv-card, .data-chip {{
          background: var(--panel);
          border: 1px solid var(--line);
          backdrop-filter: blur(16px);
        }}
        .panel {{
          border-radius: var(--radius-xl);
          padding: 24px;
        }}
        .section-heading {{
          display: flex;
          justify-content: space-between;
          gap: 20px;
          align-items: end;
          margin-bottom: 16px;
        }}
        .section-heading h3 {{
          font-size: 28px;
          letter-spacing: -0.03em;
        }}
        .toolbar {{
          display: flex;
          gap: 10px;
          align-items: center;
          flex-wrap: wrap;
          color: var(--muted);
          font-size: 13px;
        }}
        .toolbar-note {{
          padding-bottom: 6px;
        }}
        .summary-grid, .metrics-grid, .metric-strip {{
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 14px;
        }}
        .metric-card {{
          border-radius: 20px;
          padding: 18px;
          min-height: 120px;
          position: relative;
          overflow: hidden;
        }}
        .metric-card::before {{
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(150deg, rgba(255, 255, 255, 0.06), transparent 58%);
          pointer-events: none;
        }}
        .metric-card-good {{
          box-shadow: inset 0 0 0 1px rgba(53, 217, 141, 0.14);
        }}
        .metric-card-bad {{
          box-shadow: inset 0 0 0 1px rgba(255, 125, 97, 0.16);
        }}
        .metric-label {{
          color: var(--muted);
          font-size: 12px;
          text-transform: uppercase;
          letter-spacing: 0.12em;
        }}
        .metric-value {{
          font-size: 34px;
          font-weight: 700;
          margin-top: 12px;
          letter-spacing: -0.04em;
        }}
        .metric-note {{
          margin-top: 12px;
          color: var(--muted);
          font-size: 13px;
          line-height: 1.45;
        }}
        .data-grid {{
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 12px;
        }}
        .data-chip {{
          border-radius: 18px;
          padding: 16px;
        }}
        .data-chip strong {{
          display: block;
          font-size: 22px;
          margin-bottom: 6px;
        }}
        .data-chip span {{
          color: var(--muted);
          font-size: 13px;
        }}
        .strategy-tabs {{
          position: sticky;
          top: 18px;
          z-index: 3;
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 14px;
          margin-top: 22px;
          margin-bottom: 18px;
        }}
        .strategy-tab {{
          border-radius: 20px;
          padding: 18px 20px;
          color: var(--text);
          cursor: pointer;
          transition: transform 180ms ease, border-color 180ms ease, background 180ms ease, box-shadow 180ms ease;
          text-align: left;
          box-shadow: 0 18px 40px rgba(0, 0, 0, 0.16);
        }}
        .strategy-tab:hover {{
          transform: translateY(-2px);
          border-color: var(--line-strong);
        }}
        .strategy-tab.is-active {{
          background: linear-gradient(135deg, rgba(18, 38, 70, 0.98), rgba(11, 24, 44, 0.98));
          border-color: rgba(82, 200, 255, 0.24);
          box-shadow: 0 24px 56px rgba(0, 0, 0, 0.22), inset 0 0 0 1px rgba(82, 200, 255, 0.16);
        }}
        .strategy-tab-name {{
          display: block;
          font-size: 20px;
          font-weight: 700;
          letter-spacing: -0.03em;
        }}
        .strategy-tab-stats {{
          margin-top: 10px;
          display: flex;
          justify-content: space-between;
          gap: 12px;
          align-items: center;
          color: var(--muted-strong);
          font-size: 13px;
        }}
        .panel-stack {{
          display: grid;
          gap: 24px;
        }}
        .strategy-panel {{
          display: none;
          gap: 22px;
          background: rgba(8, 18, 33, 0.52);
          border: 1px solid var(--line);
          border-radius: 32px;
          padding: 26px;
          box-shadow: var(--shadow);
        }}
        .strategy-panel.is-active {{
          display: grid;
        }}
        .strategy-hero {{
          display: flex;
          justify-content: space-between;
          gap: 24px;
          align-items: start;
        }}
        .strategy-copy h2 {{
          font-size: 40px;
          letter-spacing: -0.04em;
        }}
        .strategy-copy p {{
          margin-top: 12px;
          color: var(--muted);
          max-width: 78ch;
          line-height: 1.7;
        }}
        .strategy-status-cluster, .decision-pills, .hero-badges {{
          display: flex;
          gap: 10px;
          flex-wrap: wrap;
        }}
        .pill {{
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 10px 14px;
          border-radius: 999px;
          font-size: 12px;
          font-weight: 700;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          border: 1px solid transparent;
          white-space: nowrap;
        }}
        .pill-good {{
          background: rgba(53, 217, 141, 0.14);
          color: #b1ffd8;
          border-color: rgba(53, 217, 141, 0.24);
        }}
        .pill-bad {{
          background: rgba(255, 125, 97, 0.14);
          color: #ffd4ca;
          border-color: rgba(255, 125, 97, 0.25);
        }}
        .pill-warn {{
          background: rgba(240, 195, 102, 0.14);
          color: #ffe4ac;
          border-color: rgba(240, 195, 102, 0.24);
        }}
        .pill-neutral {{
          background: rgba(255, 255, 255, 0.06);
          color: var(--text);
          border-color: rgba(255, 255, 255, 0.10);
        }}
        .visual-grid {{
          display: grid;
          grid-template-columns: 1.2fr 1fr 1fr;
          gap: 16px;
        }}
        .visual-card {{
          border-radius: 22px;
          padding: 18px;
          min-height: 220px;
        }}
        .visual-card-top {{
          display: flex;
          justify-content: space-between;
          gap: 16px;
          align-items: start;
          margin-bottom: 12px;
        }}
        .visual-card-top strong {{
          font-size: 18px;
          letter-spacing: -0.02em;
        }}
        .visual-title {{
          font-size: 12px;
          text-transform: uppercase;
          letter-spacing: 0.14em;
          color: var(--muted);
          margin-bottom: 8px;
        }}
        .sparkline {{
          width: 100%;
          height: 152px;
        }}
        .insight-grid {{
          display: grid;
          grid-template-columns: 1.35fr 0.95fr;
          gap: 18px;
        }}
        .insight-card {{
          border-radius: 28px;
          padding: 20px;
        }}
        .search-shell {{
          margin-bottom: 14px;
        }}
        .search-input {{
          width: 100%;
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 16px;
          background: rgba(255, 255, 255, 0.04);
          color: var(--text);
          padding: 14px 16px;
          font: inherit;
          outline: none;
          transition: border-color 160ms ease, box-shadow 160ms ease, background 160ms ease;
        }}
        .search-input:focus {{
          border-color: rgba(82, 200, 255, 0.36);
          box-shadow: 0 0 0 4px rgba(82, 200, 255, 0.10);
          background: rgba(82, 200, 255, 0.04);
        }}
        .filter-chip {{
          border: 1px solid rgba(255, 255, 255, 0.08);
          background: rgba(255, 255, 255, 0.04);
          color: var(--muted-strong);
          padding: 10px 14px;
          border-radius: 999px;
          cursor: pointer;
          font: inherit;
          transition: all 160ms ease;
        }}
        .filter-chip.is-active {{
          background: linear-gradient(135deg, rgba(82, 200, 255, 0.18), rgba(82, 200, 255, 0.08));
          color: var(--text);
          border-color: rgba(82, 200, 255, 0.24);
          box-shadow: inset 0 0 0 1px rgba(82, 200, 255, 0.16);
        }}
        .decision-grid {{
          display: grid;
          grid-template-columns: 0.82fr 1.18fr;
          gap: 16px;
          min-height: 620px;
        }}
        .decision-list, .decision-detail-stack, .signal-stack {{
          display: grid;
          gap: 12px;
          align-content: start;
        }}
        .decision-list {{
          max-height: 780px;
          overflow: auto;
          padding-right: 6px;
        }}
        .decision-card[hidden] {{
          display: none;
        }}
        .decision-button {{
          width: 100%;
          border-radius: 22px;
          padding: 18px;
          color: var(--text);
          text-align: left;
          cursor: pointer;
          transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
        }}
        .decision-button:hover {{
          transform: translateY(-2px);
          border-color: var(--line-strong);
        }}
        .decision-button.is-active {{
          background: linear-gradient(135deg, rgba(17, 34, 62, 0.98), rgba(12, 25, 46, 0.98));
          border-color: rgba(82, 200, 255, 0.24);
          box-shadow: 0 20px 40px rgba(0, 0, 0, 0.22);
        }}
        .decision-top {{
          display: flex;
          justify-content: space-between;
          gap: 16px;
          align-items: start;
        }}
        .decision-question, .signal-question {{
          font-size: 20px;
          font-weight: 700;
          letter-spacing: -0.03em;
          line-height: 1.2;
        }}
        .decision-subtitle, .signal-subtitle {{
          margin-top: 8px;
          color: var(--muted);
          font-size: 13px;
        }}
        .decision-stats, .signal-grid, .kv-grid, .diagnostics-grid {{
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 12px;
          margin-top: 16px;
        }}
        .decision-stats span, .signal-grid span, .kv-card span {{
          display: block;
          font-size: 12px;
          color: var(--muted);
          margin-bottom: 4px;
        }}
        .decision-stats strong, .signal-grid strong, .kv-card strong {{
          font-size: 17px;
          letter-spacing: -0.02em;
        }}
        .decision-detail {{
          display: none;
        }}
        .decision-detail.is-active {{
          display: grid;
          gap: 16px;
        }}
        .decision-detail-header {{
          display: flex;
          justify-content: space-between;
          gap: 18px;
          align-items: start;
        }}
        .decision-detail-header h3 {{
          font-size: 34px;
          letter-spacing: -0.04em;
        }}
        .decision-detail-header p {{
          margin-top: 8px;
          color: var(--muted);
        }}
        .metric-strip {{
          grid-template-columns: repeat(6, minmax(0, 1fr));
        }}
        .metric-strip .metric-card {{
          min-height: 104px;
        }}
        .detail-layout {{
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 16px;
        }}
        .detail-panel {{
          border-radius: 22px;
          padding: 18px;
          background: var(--panel-strong);
        }}
        .panel-title {{
          font-size: 13px;
          color: var(--gold);
          text-transform: uppercase;
          letter-spacing: 0.12em;
          margin-bottom: 14px;
        }}
        .reasoning-list {{
          margin: 0;
          padding-left: 18px;
          color: var(--muted-strong);
          line-height: 1.7;
        }}
        .kv-card {{
          border-radius: 16px;
          padding: 14px;
          min-height: 88px;
        }}
        .detail-box {{
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 18px;
          background: rgba(255, 255, 255, 0.03);
          overflow: hidden;
        }}
        .detail-box summary, .table-details summary {{
          cursor: pointer;
          padding: 14px 16px;
          color: var(--muted-strong);
          font-weight: 600;
          list-style: none;
        }}
        .detail-box summary::-webkit-details-marker, .table-details summary::-webkit-details-marker {{
          display: none;
        }}
        pre {{
          margin: 0;
          padding: 0 16px 16px;
          overflow: auto;
          color: #cfe2ff;
          font-family: "SFMono-Regular", "Menlo", monospace;
          font-size: 12px;
          line-height: 1.6;
          white-space: pre-wrap;
          word-break: break-word;
        }}
        .signal-card {{
          border-radius: 22px;
          padding: 18px;
          background: var(--panel-strong);
        }}
        .signal-top {{
          display: flex;
          justify-content: space-between;
          gap: 16px;
          align-items: start;
        }}
        .table-shell {{
          overflow: auto;
          border-radius: 22px;
          border: 1px solid var(--line);
          background: rgba(5, 12, 24, 0.74);
        }}
        table {{
          width: 100%;
          border-collapse: collapse;
          min-width: 780px;
        }}
        th, td {{
          padding: 14px 16px;
          text-align: left;
          border-bottom: 1px solid rgba(255, 255, 255, 0.06);
          vertical-align: top;
          font-size: 14px;
        }}
        th {{
          position: sticky;
          top: 0;
          background: rgba(10, 18, 32, 0.94);
          color: var(--muted);
          text-transform: uppercase;
          font-size: 12px;
          letter-spacing: 0.08em;
          cursor: pointer;
        }}
        tbody tr {{
          transition: background 120ms ease;
        }}
        tbody tr:hover {{
          background: rgba(255, 255, 255, 0.03);
        }}
        .table-details {{
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 14px;
          background: rgba(255, 255, 255, 0.03);
          min-width: 170px;
        }}
        .table-details pre {{
          padding-top: 0;
        }}
        .text-good {{ color: #9bffcf; }}
        .text-bad {{ color: #ffc0b1; }}
        .text-neutral {{ color: var(--text); }}
        .muted-cell, .empty-state {{
          color: var(--muted);
          text-align: center;
          padding: 28px;
        }}
        [hidden] {{
          display: none !important;
        }}
        @media (max-width: 1240px) {{
          .summary-shell, .insight-grid, .decision-grid, .visual-grid {{
            grid-template-columns: 1fr;
          }}
          .metric-strip {{
            grid-template-columns: repeat(3, minmax(0, 1fr));
          }}
        }}
        @media (max-width: 920px) {{
          .shell {{
            width: min(100% - 24px, 1560px);
          }}
          .hero, .strategy-panel {{
            padding: 20px;
            border-radius: 24px;
          }}
          .strategy-tabs, .summary-grid, .metrics-grid, .detail-layout, .metric-strip, .decision-stats, .signal-grid, .data-grid, .diagnostics-grid, .kv-grid {{
            grid-template-columns: 1fr;
          }}
          .hero-copy, .strategy-hero, .decision-top, .decision-detail-header, .section-heading {{
            flex-direction: column;
            align-items: start;
          }}
          h1 {{
            font-size: 46px;
          }}
          .decision-list {{
            max-height: none;
            overflow: visible;
            padding-right: 0;
          }}
        }}
      </style>
    </head>
    <body>
      <main class="shell">
        <section class="hero">
          <div class="hero-copy">
            <div class="hero-lead">
              <div class="eyebrow">Polymarket Strategy Intelligence</div>
              <h1>Interactive Strategy Command Deck</h1>
              <p>This report is designed as an investigation surface, not a static export. You can switch strategies instantly, inspect the reason behind every buy or block, sort historical trades, search diagnostics, and compare how expected value, drawdown, calibration, and execution quality interact before you trust real capital.</p>
            </div>
            <div class="hero-badges">
              {_pill("Interactive", "good")}
              {_pill("Decision Reasoning", "warn")}
              {_pill("Backtest Forensics", "neutral")}
            </div>
          </div>

          <div class="summary-shell">
            <section class="panel">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Portfolio Snapshot</div>
                  <h3>Cross-strategy operating view</h3>
                </div>
              </div>
              <div class="summary-grid">
                {_metric_card("Strategies", _fmt_num(len(strategies)), note="Currently wired into the domain")}
                {_metric_card("Backtested Trades", _fmt_num(total_trades), note="Across all strategies")}
                {_metric_card("Passing Strategies", _fmt_num(passed_count), note=f"{len(strategies) - passed_count} failing right now", tone="good" if passed_count else "bad")}
                {_metric_card("Executable Ideas", _fmt_num(executable_count), note="Current analysis output")}
                {_metric_card("Average EV", _fmt_pct(avg_ev), note="Cross-strategy average", tone="good" if avg_ev > 0 else "bad")}
                {_metric_card("Cash", _fmt_money(float(portfolio_state.cash)), note="From persisted portfolio state")}
                {_metric_card("Best Strategy", _escape(_titleize(best_strategy['strategy_name'])) if best_strategy else "n/a", note=_fmt_pct(float(best_strategy["expected_value"])) if best_strategy else "", tone="good" if best_strategy and float(best_strategy["expected_value"]) > 0 else "neutral")}
                {_metric_card("Worst Drawdown", _fmt_pct(worst_drawdown), note="Across all backtests", tone="bad" if worst_drawdown > 0.2 else "good")}
                {_metric_card("Mode", "Sample" if use_sample else "Real", note="Current report data mode", tone="neutral")}
              </div>
            </section>

            <section class="panel">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Research Data</div>
                  <h3>Data readiness surface</h3>
                </div>
              </div>
              <div class="data-grid">
                <div class="data-chip"><strong>{_fmt_num(real_status["real_mispricing_rows"])}</strong><span>Real polling rows processed</span></div>
                <div class="data-chip"><strong>{_fmt_num(len(real_status["raw_files"]))}</strong><span>Raw research files</span></div>
                <div class="data-chip"><strong>{_fmt_num(len(real_status["processed_files"]))}</strong><span>Processed datasets</span></div>
                <div class="data-chip"><strong>{"Yes" if real_status["market_metadata_available"] else "No"}</strong><span>Archived market metadata ready</span></div>
              </div>
              <details class="detail-box" style="margin-top: 16px;">
                <summary>Inspect real-data status</summary>
                <pre>{_json_block(real_status)}</pre>
              </details>
            </section>
          </div>
        </section>

        <section class="strategy-tabs" id="strategy-tabs">
          {strategy_tabs}
        </section>

        <section class="panel-stack">
          {strategy_panels}
        </section>
      </main>

      <script>
        (() => {{
          const tabs = Array.from(document.querySelectorAll('[data-tab-target]'));
          const panels = Array.from(document.querySelectorAll('[data-tab-panel]'));

          function activateTab(target) {{
            tabs.forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tabTarget === target));
            panels.forEach((panel) => panel.classList.toggle('is-active', panel.dataset.tabPanel === target));
          }}

          tabs.forEach((tab) => {{
            tab.addEventListener('click', () => activateTab(tab.dataset.tabTarget));
          }});

          document.querySelectorAll('[data-filter-panel]').forEach((button) => {{
            button.addEventListener('click', () => {{
              const panel = button.dataset.filterPanel;
              const value = button.dataset.filterValue;
              document.querySelectorAll(`[data-filter-panel="${{panel}}"]`).forEach((chip) => {{
                chip.classList.toggle('is-active', chip === button);
              }});
              document.querySelectorAll(`[data-decision-grid="${{panel}}"] .decision-card`).forEach((card) => {{
                const visible = value === 'all' || card.dataset.filterState === value;
                card.hidden = !visible;
              }});
            }});
          }});

          document.querySelectorAll('[data-decision-target]').forEach((button) => {{
            button.addEventListener('click', () => {{
              const target = button.dataset.decisionTarget;
              const scope = button.closest('[data-decision-grid]');
              if (!scope) return;
              scope.querySelectorAll('[data-decision-target]').forEach((item) => item.classList.remove('is-active'));
              scope.querySelectorAll('.decision-detail').forEach((panel) => {{
                panel.classList.toggle('is-active', panel.id === target);
              }});
              button.classList.add('is-active');
            }});
          }});

          function applySearch(input, nodes) {{
            const query = input.value.trim().toLowerCase();
            nodes.forEach((node) => {{
              const haystack = (node.dataset.searchText || node.textContent || '').toLowerCase();
              node.hidden = query && !haystack.includes(query);
            }});
          }}

          document.querySelectorAll('[data-search-panel]').forEach((input) => {{
            const panel = input.dataset.searchPanel;
            const nodes = Array.from(document.querySelectorAll(`[data-decision-grid="${{panel}}"] .decision-card`));
            input.addEventListener('input', () => applySearch(input, nodes));
          }});

          document.querySelectorAll('[data-search-table]').forEach((input) => {{
            const table = document.getElementById(input.dataset.searchTable);
            if (!table) return;
            const rows = Array.from(table.querySelectorAll('tbody tr'));
            input.addEventListener('input', () => applySearch(input, rows));
          }});

          document.querySelectorAll('.sortable-table').forEach((table) => {{
            const headers = Array.from(table.querySelectorAll('th'));
            headers.forEach((header, index) => {{
              let ascending = true;
              header.addEventListener('click', () => {{
                const tbody = table.querySelector('tbody');
                const rows = Array.from(tbody.querySelectorAll('tr'));
                rows.sort((a, b) => {{
                  const cellA = a.children[index];
                  const cellB = b.children[index];
                  const rawA = cellA?.dataset.sort ?? cellA?.textContent ?? '';
                  const rawB = cellB?.dataset.sort ?? cellB?.textContent ?? '';
                  const type = header.dataset.sortType;
                  if (type === 'number') {{
                    return ascending ? Number(rawA) - Number(rawB) : Number(rawB) - Number(rawA);
                  }}
                  return ascending ? String(rawA).localeCompare(String(rawB)) : String(rawB).localeCompare(String(rawA));
                }});
                rows.forEach((row) => tbody.appendChild(row));
                headers.forEach((item) => item.removeAttribute('data-sort-order'));
                header.setAttribute('data-sort-order', ascending ? 'asc' : 'desc');
                ascending = !ascending;
              }});
            }});
          }});

          if (tabs.length > 0) {{
            activateTab(tabs[0].dataset.tabTarget);
          }}
        }})();
      </script>
    </body>
    </html>
    """
    return html_report


def write_strategy_report(output_path: str | Path, *, use_sample: bool, state_path: str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_strategy_report(use_sample=use_sample, state_path=state_path))
    return path
