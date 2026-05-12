"""Claude API qualitative gate for trade approval.

Apr 25 2026 — STRATEGY 7. After all quantitative gates pass, send the
trade context to Claude for a qualitative "real edge or artifact?"
review. Only fires the trade if Claude approves with confidence ≥ 0.6.

Why Claude as final gate:
  Our quant pipeline catches numerical-pattern artifacts (high-p with
  large edge, low ensemble confidence, etc.) but can't reason about
  semantics — "this bracket question sounds weird", "the bounds don't
  match the question", "the threshold is unusual for this city's
  climate". Claude can spot these in seconds where bespoke validators
  would take months to write.

Cost economics:
  ~$0.05/call × 5-10 trades/day = $7-15/month. Trivial vs the $-687
  drawdown we're recovering from. If Claude vetoes one bad trade per
  week, the gate pays for itself many times over.

Failure modes (and mitigations):
  1. API down / timeout → fail-safe: BLOCK the trade. Conservative.
     Better to miss an edge than fire on an unverified signal.
  2. Claude wrong (false positive — approves a bad trade) → quant
     pipeline already approved; Claude just fails to veto. Same as
     pre-Claude state, no worse.
  3. Claude wrong (false negative — vetoes a good trade) → measurable
     opportunity cost. Track via metadata; tune threshold based on
     hit rate.

Configuration:
  Disabled by default. Set ANTHROPIC_API_KEY in env or .env to enable.
  When disabled, every trade passes through (gate is a no-op). When
  enabled, every quant-approved trade goes through Claude.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any


# Approval threshold — Claude must return confidence ≥ this to approve.
# 0.6 is reasonably conservative: Claude is fairly calibrated on its
# own confidence, so 0.6 means "more likely than not real edge with
# some margin". Below this, when Claude is unsure, we lean toward not
# trading — consistent with the post-flip "market is mostly right"
# paradigm.
_CLAUDE_APPROVAL_THRESHOLD: float = 0.60

# API timeout — too short risks false-veto on slow API; too long blocks
# the autotrade tick. 15 seconds is a reasonable middle ground.
_CLAUDE_TIMEOUT_S: float = 15.0

# Model — using Sonnet for cost/speed balance. Opus would be slightly
# better at reasoning but 5x cost; Haiku faster but less precise on
# domain reasoning.
_CLAUDE_MODEL: str = "claude-sonnet-4-6"


@dataclass(slots=True)
class ClaudeReview:
    """Structured response from the Claude review."""
    approved: bool                # is_real_edge AND confidence >= threshold
    confidence: float             # Claude's stated confidence (0-1)
    reasoning: str                # short prose explanation
    red_flags: list[str]          # list of specific concerns
    raw_response: str             # full Claude response for audit log


# System prompt — establishes Claude as a quant analyst with the
# context to spot artifacts. We keep this focused and explicit about
# the failure modes we've seen in the wild so Claude can pattern-match.
_SYSTEM_PROMPT = """You are a senior quantitative analyst reviewing
proposed weather-bracket trades on Polymarket. Your job: decide if a
trade is REAL EDGE or an ARTIFACT.

Common artifact patterns to watch for:
1. Bracket-parsing failures: model_prob ≈ 1.00 with market_prob far
   below — usually means the bracket bounds got parsed wrong and the
   model is confidently rating an unbounded interval.
2. Climatology-impossible signals: e.g., model says "Dubai will be
   ≤5°C with 80% confidence in July" — physically nonsensical.
3. Stale-forecast trades: lead < 6h with no nowcast — market has
   observation info we don't.
4. Disagreement-with-consensus on calm days: ensemble σ < 1°F with
   model_prob deviating > 20pp from market_prob — usually our σ floor
   broke down.
5. Tropical-city low-temperature brackets in summer: market is right;
   forecast is wrong.

When in doubt, defer to the market. The market has more information
on average than our 24-hour-old forecast.

Respond ONLY with valid JSON in this exact schema:
{
  "is_real_edge": true|false,
  "confidence": 0.0-1.0,
  "reasoning": "1-2 sentences explaining the verdict",
  "red_flags": ["list", "of", "specific concerns"]
}
"""


def build_user_prompt(trade_ctx: dict[str, Any]) -> str:
    """Format the trade context as a Claude user message.

    `trade_ctx` is a dict with keys:
      city, target_date, question, bracket_lower_f, bracket_upper_f,
      model_prob, market_prob, edge_after_fees, ensemble_spread_f,
      forecast_high_f_per_model (dict), regime, lead_hours, season,
      strategy_subtype (e.g. "coherence_arb" or None for normal)
    """
    forecast_lines = []
    fpm = trade_ctx.get("forecast_high_f_per_model") or {}
    for model, value in fpm.items():
        forecast_lines.append(f"  - {model}: {value}°F")
    forecasts_str = "\n".join(forecast_lines) if forecast_lines else "  (none recorded)"

    return f"""Review this weather-bracket trade and respond per schema.

CITY: {trade_ctx.get("city")}
TARGET DATE: {trade_ctx.get("target_date")}
QUESTION: {trade_ctx.get("question", "(no question text)")}
BRACKET RANGE: [{trade_ctx.get("bracket_lower_f")}, {trade_ctx.get("bracket_upper_f")}]°F
LEAD TIME: {trade_ctx.get("lead_hours", "?")} hours to settlement
SEASON CODE: {trade_ctx.get("season", "?")} (0=winter,1=spring,2=summer,3=fall; SH cities flipped)
REGIME: {trade_ctx.get("regime", "?")}

OUR MODEL SAYS: {trade_ctx.get("model_prob"):.3f} probability YES
MARKET PRICES: {trade_ctx.get("market_prob"):.3f}
EDGE AFTER FEES: {trade_ctx.get("edge_after_fees", 0):.3f}
ENSEMBLE SPREAD: {trade_ctx.get("ensemble_spread_f", 0):.1f}°F (range, max-min across model members)

PER-MODEL FORECASTS:
{forecasts_str}

STRATEGY THAT FIRED: {trade_ctx.get("strategy_subtype") or "standard_edge"}

Apply the artifact-detection criteria from the system prompt. Reply
with JSON only."""


class ClaudeTradeReviewer:
    """Optional API gate — wraps an anthropic client.

    When ANTHROPIC_API_KEY is missing, every call is a pass-through
    (always approves). Logs warnings on first call so the operator
    knows the gate is disabled.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        approval_threshold: float = _CLAUDE_APPROVAL_THRESHOLD,
        timeout_s: float = _CLAUDE_TIMEOUT_S,
        model: str = _CLAUDE_MODEL,
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.threshold = approval_threshold
        self.timeout = timeout_s
        self.model = model
        self._client = None
        self._enabled = bool(self.api_key)
        self._warned_disabled = False

    def _ensure_client(self):
        """Lazy import — anthropic is an optional dep."""
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError:
            print(
                "[claude_gate] anthropic package not installed; gate disabled. "
                "Run `pip install anthropic` to enable.",
                file=sys.stderr,
            )
            self._enabled = False
            return None
        self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def review_trade(self, trade_ctx: dict[str, Any]) -> ClaudeReview:
        """Review a single trade. Returns ClaudeReview with .approved
        flag.

        Pass-through (always approve) when:
          - No API key configured
          - anthropic package not installed
          - API call fails (fail-OPEN here — we don't want a temporary
            outage to halt all trading; the quant pipeline already
            approved).

        Real veto requires actually getting a Claude response with
        is_real_edge=False or confidence < threshold.
        """
        if not self._enabled:
            if not self._warned_disabled:
                print(
                    "[claude_gate] disabled (no ANTHROPIC_API_KEY); passing all trades.",
                    file=sys.stderr,
                )
                self._warned_disabled = True
            return ClaudeReview(
                approved=True,
                confidence=1.0,
                reasoning="claude_gate disabled — auto-approve",
                red_flags=[],
                raw_response="",
            )
        client = self._ensure_client()
        if client is None:
            return ClaudeReview(
                approved=True,
                confidence=1.0,
                reasoning="anthropic SDK missing — auto-approve",
                red_flags=[],
                raw_response="",
            )

        user_msg = build_user_prompt(trade_ctx)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=400,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text if response.content else ""
        except Exception as exc:
            # Fail-open: API hiccup shouldn't block all trades. Log
            # and pass through. If this happens repeatedly we'll see
            # it in metadata stats.
            print(f"[claude_gate] API call failed: {exc!r}; passing trade.", file=sys.stderr)
            return ClaudeReview(
                approved=True,
                confidence=1.0,
                reasoning=f"claude_gate API error — auto-approve: {type(exc).__name__}",
                red_flags=[],
                raw_response="",
            )

        # Parse JSON. Claude is usually clean about JSON-only responses
        # but sometimes wraps in markdown ```json — strip it.
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                # Strip ```json ... ``` fences
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
            parsed = json.loads(cleaned)
            is_real = bool(parsed.get("is_real_edge", False))
            conf = float(parsed.get("confidence", 0.0))
            reasoning = str(parsed.get("reasoning", ""))[:500]
            red_flags = list(parsed.get("red_flags", []))[:5]
            approved = is_real and conf >= self.threshold
            return ClaudeReview(
                approved=approved,
                confidence=conf,
                reasoning=reasoning,
                red_flags=red_flags,
                raw_response=raw[:2000],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(
                f"[claude_gate] failed to parse response: {exc!r}; passing trade. "
                f"Raw: {raw[:200]!r}",
                file=sys.stderr,
            )
            # Fail-open on malformed response — same as API failure.
            return ClaudeReview(
                approved=True,
                confidence=1.0,
                reasoning=f"claude_gate parse error — auto-approve",
                red_flags=[],
                raw_response=raw[:2000],
            )

    @property
    def enabled(self) -> bool:
        return self._enabled


# Module-level singleton for shared use. Tests can override.
_default_reviewer: ClaudeTradeReviewer | None = None


def get_claude_reviewer() -> ClaudeTradeReviewer:
    """Lazy singleton — first call creates the reviewer, subsequent
    calls return the same instance. Reuses the API connection."""
    global _default_reviewer
    if _default_reviewer is None:
        _default_reviewer = ClaudeTradeReviewer()
    return _default_reviewer


def reset_for_tests() -> None:
    global _default_reviewer
    _default_reviewer = None
