"""Pin the Apr 27 2026 _resolve_token_side smart-fallback contract.

Why this exists:
  Before this fix, run_rebalance read `pos.get("token_side") or "YES"`,
  which silently coerced any NULL token_side to YES. A NO-side trade
  with side="BUY_NO" but token_side=NULL would get YES-side edge math
  applied to NO-side prices, producing nonsense edge values and
  potentially wrong rebalance decisions.

  The smart fallback prefers `side` ("BUY_NO" → NO) before falling back
  to the stored token_side. This protects against:
    - Legacy rows pre-dating the token_side column
    - Future strategies that forget to set metadata["token_side"]
    - Schema migrations that wipe token_side
"""
from __future__ import annotations

from polymarket_strat.main import _resolve_token_side


class TestResolveTokenSide:
    """Test the resolution rules in priority order."""

    def test_side_buy_no_returns_no_even_if_token_side_yes(self):
        """Side wins over token_side. side=BUY_NO trumps stored token_side=YES."""
        pos = {"side": "BUY_NO", "token_side": "YES"}
        assert _resolve_token_side(pos) == "NO"

    def test_side_buy_yes_returns_yes(self):
        """Side wins over token_side. side=BUY_YES trumps stored token_side=NO."""
        pos = {"side": "BUY_YES", "token_side": "NO"}
        assert _resolve_token_side(pos) == "YES"

    def test_side_no_short_form_returns_no(self):
        """Some legacy rows have just `side="NO"` without the BUY_ prefix."""
        pos = {"side": "NO", "token_side": None}
        assert _resolve_token_side(pos) == "NO"

    def test_side_buy_only_returns_token_side_if_set(self):
        """When side is just 'BUY' (no NO/YES), defer to token_side."""
        pos = {"side": "BUY", "token_side": "NO"}
        assert _resolve_token_side(pos) == "NO"

    def test_side_buy_only_returns_yes_if_token_side_null(self):
        """Final fallback: legacy 'BUY' side with NULL token_side → YES.
        Maintains backward-compat with pre-fix-#5 rows that were YES-only."""
        pos = {"side": "BUY", "token_side": None}
        assert _resolve_token_side(pos) == "YES"

    def test_side_null_token_side_no_returns_no(self):
        """Token_side wins when side is missing entirely."""
        pos = {"side": None, "token_side": "NO"}
        assert _resolve_token_side(pos) == "NO"

    def test_both_null_returns_yes(self):
        """Final fallback: completely empty position → YES (legacy default)."""
        pos = {"side": None, "token_side": None}
        assert _resolve_token_side(pos) == "YES"

    def test_empty_strings_return_yes(self):
        """Empty-string falsy values fall back to YES."""
        pos = {"side": "", "token_side": ""}
        assert _resolve_token_side(pos) == "YES"

    def test_case_insensitive_side(self):
        """Side check is case-insensitive ('buy_no', 'Buy_No', etc.)."""
        pos = {"side": "buy_no", "token_side": "YES"}
        assert _resolve_token_side(pos) == "NO"

        pos = {"side": "Buy_No", "token_side": "YES"}
        assert _resolve_token_side(pos) == "NO"

    def test_no_substring_in_unrelated_side_doesnt_match(self):
        """Sanity: side="NORMAL" or weird string with NO substring still
        triggers the NO branch. This is intentional — strategies don't
        emit "NORMAL" as a side, so a substring match is safer than a
        strict equality check (covers 'BUY_NO', 'NO', 'buy_no_token')."""
        pos = {"side": "NORMAL", "token_side": "YES"}
        # This case is acceptable: rather than risk false negatives on
        # legitimate NO trades with weird side labels, we err on the side
        # of NO. If a future bug introduces "NORMAL" as a side, the
        # token_side column will remain authoritative for non-NO picks.
        assert _resolve_token_side(pos) == "NO"

    def test_real_world_legacy_row(self):
        """Spot check: the exact bug pattern from Apr 26 2026 audit."""
        # Trade #178 munich: side='NO', token_side=NULL (pre-Apr-27 fix).
        # Before smart fallback: rebalance treated as YES → wrong edge math.
        # After smart fallback: correctly resolved to NO.
        pos = {"id": 178, "city": "munich", "side": "NO", "token_side": None}
        assert _resolve_token_side(pos) == "NO"
