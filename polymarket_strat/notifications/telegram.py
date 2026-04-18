from __future__ import annotations

import json
import ssl
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_strat.config import TelegramConfig

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self.config = config

    def send_message(self, text: str, *, parse_mode: str = "HTML") -> dict[str, Any]:
        url = f"{TELEGRAM_API}/bot{self.config.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }).encode()
        request = Request(url, data=payload, headers={"Content-Type": "application/json"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urlopen(request, timeout=15, context=ctx) as response:
            return json.loads(response.read().decode())

    def send_whale_alert(
        self,
        *,
        title: str,
        outcome: str,
        side: str,
        total_size: float,
        avg_price: float,
        whale_count: int,
        signal_score: float,
        whale_summaries: list[dict[str, Any]],
        market_id: str = "",
    ) -> dict[str, Any]:
        lines = [
            "<b>WHALE ALERT</b>",
            "",
            f"<b>Market:</b> {_esc(title)}",
            f"<b>Outcome:</b> {_esc(outcome)} ({side})",
            f"<b>Combined size:</b> ${total_size:,.0f}",
            f"<b>Avg entry:</b> ${avg_price:.3f}",
            f"<b>Whales buying:</b> {whale_count}",
            f"<b>Signal score:</b> {signal_score:,.1f}",
        ]
        for ws in whale_summaries[:3]:
            wallet = ws.get("wallet", "?")[:12]
            tier = ws.get("longevity_tier", "?")
            wr = ws.get("win_rate", 0)
            sharpe = ws.get("sharpe", 0)
            months = ws.get("months_active", 0)
            pnl = ws.get("total_pnl", 0)
            lines.append("")
            lines.append(
                f"  <code>{wallet}...</code> [{tier}]\n"
                f"  WR {wr:.0%} | Sharpe {sharpe:.1f} | {months}mo | ${pnl:,.0f} PnL"
            )
        text = "\n".join(lines)
        print(f"[telegram] Sending alert: {title[:50]} | {outcome}", file=sys.stderr)
        return self.send_message(text)

    def send_insider_alert(self, *, aggregated: dict[str, Any]) -> dict[str, Any]:
        """Send an insider-anomaly alert for a single (market, outcome) bucket."""
        title = aggregated.get("market_title", "Unknown market")
        outcome = aggregated.get("outcome", "?")
        score = aggregated.get("combined_score", 0.0)
        signal_types = aggregated.get("signal_types", [])
        signals = aggregated.get("signals", [])
        market_id = aggregated.get("market_id", "")

        type_labels = {
            "volume_spike": "Volume spike",
            "new_wallet": "New wallet large bet",
            "coordinated": "Coordinated buy cluster",
            "price_impact": "Probability shift",
        }
        type_icons = {
            "volume_spike": "📈",
            "new_wallet": "👤",
            "coordinated": "🔗",
            "price_impact": "💥",
        }

        lines = [
            "<b>⚠️ INSIDER ANOMALY DETECTED</b>",
            "",
            f"<b>Market:</b> {_esc(title)}",
            f"<b>Outcome:</b> {_esc(outcome)}",
            f"<b>Suspicion score:</b> {score:.3f}",
            f"<b>Signals triggered:</b> {', '.join(signal_types)}",
        ]
        if market_id:
            lines.append(f"<b>Market ID:</b> <code>{market_id[:20]}...</code>")

        for sig in signals[:4]:
            stype = sig.get("type", "?")
            sev = sig.get("severity", 0.0)
            details = sig.get("details", {})
            icon = type_icons.get(stype, "•")
            label = type_labels.get(stype, stype)
            lines.append("")
            lines.append(f"{icon} <b>{label}</b> (severity {sev:.2f})")

            if stype == "volume_spike":
                lines.append(
                    f"  Recent: ${details.get('hot_volume_usd', 0):,.0f} | "
                    f"Baseline: ${details.get('baseline_volume_usd_normalized', 0):,.0f} | "
                    f"Ratio: {details.get('spike_ratio', 0):.1f}x"
                )
            elif stype == "new_wallet":
                w = str(details.get("wallet", ""))[:14]
                lines.append(
                    f"  Wallet <code>{w}...</code> | "
                    f"${details.get('buy_notional_usd', 0):,.0f} | No prior history"
                )
            elif stype == "coordinated":
                lines.append(
                    f"  {details.get('distinct_wallets', 0)} wallets in "
                    f"{details.get('window_minutes', 0)}min | "
                    f"${details.get('total_notional_usd', 0):,.0f}"
                )
            elif stype == "price_impact":
                lines.append(
                    f"  {details.get('price_before', 0):.2%} → "
                    f"{details.get('price_after', 0):.2%} "
                    f"(+{details.get('probability_shift_pts', 0):.1f}pts) | "
                    f"${details.get('burst_notional_usd', 0):,.0f}"
                )

        text = "\n".join(lines)
        print(f"[telegram] Sending insider alert: {title[:50]} | {outcome}", file=sys.stderr)
        return self.send_message(text)

    def send_trade_executed(self, *, trades: list[dict[str, Any]]) -> dict[str, Any]:
        """Notify about newly placed weather bracket trades."""
        if not trades:
            return {}
        lines = [
            f"<b>WEATHER TRADE{'S' if len(trades) > 1 else ''} PLACED</b>",
            f"<b>{len(trades)}</b> new position{'s' if len(trades) > 1 else ''}:",
            "",
        ]
        total_notional = 0.0
        for t in trades[:8]:
            city = t.get("city", "?")
            outcome = t.get("outcome", "?")
            notional = float(t.get("amount") or t.get("notional") or 0)
            price = float(t.get("reference_price") or t.get("entry_price") or 0)
            mode = t.get("mode", "paper")
            total_notional += notional
            lines.append(
                f"  {_esc(city.upper())} {_esc(outcome)} @ ${price:.2f} "
                f"| ${notional:,.0f} [{mode}]"
            )
        lines.append(f"\n<b>Total notional:</b> ${total_notional:,.0f}")
        return self.send_message("\n".join(lines))

    def send_settlement_report(
        self,
        *,
        settled: list[dict[str, Any]],
        total_pnl: float,
    ) -> dict[str, Any]:
        """Report settled weather trades with P&L."""
        if not settled:
            return {}
        lines = [
            f"<b>SETTLEMENT REPORT</b>",
            f"<b>{len(settled)}</b> trade{'s' if len(settled) > 1 else ''} resolved:",
            "",
        ]
        for s in settled[:10]:
            city = s.get("city", "?")
            question = s.get("question", "")[:40]
            outcome_str = s.get("outcome", "?")
            pnl = float(s.get("pnl", 0))
            icon = "+" if pnl >= 0 else ""
            result = "WIN" if outcome_str == "YES" else "LOSS"
            lines.append(f"  {_esc(city.upper())} {_esc(question)} — {result} {icon}${pnl:,.2f}")
        lines.append(f"\n<b>Session P&amp;L:</b> {'+'if total_pnl >= 0 else ''}${total_pnl:,.2f}")
        return self.send_message("\n".join(lines))

    def send_autotrade_summary(self, *, cycle: dict[str, Any]) -> dict[str, Any]:
        """Consolidated autotrade cycle report."""
        settled_count = cycle.get("settled_count", 0)
        new_trades = cycle.get("new_trade_count", 0)
        open_count = cycle.get("open_positions", 0)
        settled_pnl = cycle.get("settled_pnl", 0.0)
        cumulative_pnl = cycle.get("cumulative_pnl", 0.0)
        skipped = cycle.get("skipped")
        mode = cycle.get("mode", "paper")

        lines = [f"<b>AUTOTRADE CYCLE [{mode.upper()}]</b>", ""]
        if skipped:
            lines.append(f"Skipped execution: <b>{_esc(skipped)}</b>")
        else:
            lines.append(f"New trades: <b>{new_trades}</b>")
        lines.append(f"Settled: <b>{settled_count}</b>")
        lines.append(f"Open positions: <b>{open_count}</b>")
        lines.append(f"Session P&amp;L: {'+'if settled_pnl >= 0 else ''}${settled_pnl:,.2f}")
        lines.append(f"Cumulative P&amp;L: {'+'if cumulative_pnl >= 0 else ''}${cumulative_pnl:,.2f}")
        return self.send_message("\n".join(lines))

    def send_status(self, text: str) -> dict[str, Any]:
        return self.send_message(f"<b>Monitor status:</b> {_esc(text)}")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
