"""Pre-flight checks for live trading.

Runs eight independent checks and reports their status. Required for any
live run — ``runner.py`` refuses to flip to ``mode=live`` unless every
*required* check returns ``passed=True``. Optional checks (Telegram) report
but don't block.

The checks
----------
1. ``env_vars``           — POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER set and well-formed.
2. ``polygon_rpc``        — Polygon RPC reachable (``eth_blockNumber`` succeeds).
3. ``eoa_derivation``     — Private key derives to an EOA on the same chain.
4. ``usdc_balance``       — Funder wallet has ≥ ``min_usdc`` USDC.e on Polygon.
5. ``ctf_allowance``      — Funder has approved the CTF Exchange to move both
                            USDC (ERC-20 allowance ≥ large) and CTF shares
                            (ERC-1155 isApprovedForAll). Checks both the legacy
                            CTF Exchange and NegRisk CTF Exchange; needs at
                            least one of them fully approved or warns.
6. ``gamma_api``          — ``GET /markets?limit=1`` on Gamma returns 200 + JSON.
7. ``clob_orderbook``     — Fetch any token's orderbook; verifies parse path.
8. ``telegram``           — Optional. Sends a one-line "doctor OK" message if
                            TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set.

Dependency injection
--------------------
Every network or cryptographic side effect is abstracted behind ``DoctorDeps``.
Real runs build ``DefaultDoctorDeps`` (urllib + eth_account). Tests build a
fake subclass that returns canned values — no mocking libraries required.

CLI
---
Run directly::

    python -m polymarket_strat.live.doctor                # all checks
    python -m polymarket_strat.live.doctor --skip telegram
    python -m polymarket_strat.live.doctor --json         # machine-readable

Exit code: 0 if every *required* check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.request import Request, urlopen

from polymarket_strat.api import PolymarketPublicClient


# ---------------------------------------------------------------------------
# On-chain constants (Polygon mainnet — Polymarket's only deployment).
# ---------------------------------------------------------------------------
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # USDC.e (bridged)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"    # Conditional Tokens
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"   # legacy CTF Exchange
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # NegRisk CTF Exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"   # NegRisk Adapter

POLYGON_RPC_DEFAULT = "https://polygon-rpc.com"
USDC_DECIMALS = 6

# 4-byte function selectors (keccak256 of signature, truncated).
SEL_BALANCE_OF = "70a08231"
SEL_ALLOWANCE = "dd62ed3e"
SEL_IS_APPROVED_FOR_ALL = "e985e9c5"

# Severity used in CheckResult.
SEV_ERROR = "error"   # blocks live trading
SEV_WARN = "warn"     # degraded but non-blocking
SEV_INFO = "info"     # purely informational


# ---------------------------------------------------------------------------
# Result container.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    severity: str = SEV_ERROR
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dependency surface — real impl + test fakes subclass this.
# ---------------------------------------------------------------------------
@dataclass
class DoctorDeps:
    """Injectable dependencies for the checks.

    Subclass and override any method in tests. ``DefaultDoctorDeps`` wires
    the real urllib / eth_account implementations.
    """

    http_client: PolymarketPublicClient = field(default_factory=PolymarketPublicClient)

    # --- Cryptography ----------------------------------------------------
    def derive_eoa(self, private_key: str) -> str:
        """Return the 0x-checksum address for ``private_key``."""
        raise NotImplementedError

    # --- JSON-RPC --------------------------------------------------------
    def call_rpc(self, rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        """POST a JSON-RPC request and return the parsed response."""
        raise NotImplementedError

    # --- Telegram --------------------------------------------------------
    def telegram_send(self, bot_token: str, chat_id: str, text: str) -> dict[str, Any]:
        """Send a Telegram message. Returns the parsed API response."""
        raise NotImplementedError


class DefaultDoctorDeps(DoctorDeps):
    """Production deps: urllib for HTTP, eth_account for key derivation."""

    def derive_eoa(self, private_key: str) -> str:
        try:
            from eth_account import Account  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "eoa_derivation requires `eth-account`. `pip install eth-account`."
            ) from exc
        acct = Account.from_key(private_key)
        return str(acct.address)

    def call_rpc(self, rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urlopen(request, timeout=15, context=ctx) as response:
            return json.loads(response.read().decode("utf-8"))

    def telegram_send(self, bot_token: str, chat_id: str, text: str) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
        request = Request(url, data=payload, headers={"Content-Type": "application/json"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urlopen(request, timeout=15, context=ctx) as response:
            return json.loads(response.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# ABI encoding helpers — trivial enough to not pull in eth-abi.
# ---------------------------------------------------------------------------
def _strip0x(s: str) -> str:
    return s[2:] if s.startswith("0x") or s.startswith("0X") else s


def _pad_address(addr: str) -> str:
    """Pad a 20-byte hex address to 32 bytes (64 hex chars), lowercased."""
    clean = _strip0x(addr).lower()
    if len(clean) != 40:
        raise ValueError(f"expected a 20-byte address, got len={len(clean)}: {addr!r}")
    return clean.rjust(64, "0")


def _encode_balance_of(owner: str) -> str:
    return "0x" + SEL_BALANCE_OF + _pad_address(owner)


def _encode_allowance(owner: str, spender: str) -> str:
    return "0x" + SEL_ALLOWANCE + _pad_address(owner) + _pad_address(spender)


def _encode_is_approved_for_all(owner: str, operator: str) -> str:
    return "0x" + SEL_IS_APPROVED_FOR_ALL + _pad_address(owner) + _pad_address(operator)


def _hex_to_int(hex_str: str) -> int:
    """Parse a 0x-prefixed or bare hex string into int. Empty → 0."""
    if not hex_str:
        return 0
    return int(_strip0x(hex_str) or "0", 16)


def _eth_call(
    deps: DoctorDeps,
    rpc_url: str,
    to: str,
    data: str,
) -> str:
    """Run ``eth_call`` and return the raw hex result. Raises on RPC error."""
    resp = deps.call_rpc(rpc_url, "eth_call", [{"to": to, "data": data}, "latest"])
    if "error" in resp:
        err = resp["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"eth_call failed: {msg}")
    return str(resp.get("result", "0x"))


# ---------------------------------------------------------------------------
# Individual checks — each a pure function over DoctorDeps + env.
# ---------------------------------------------------------------------------
def check_env_vars(env: dict[str, str] | None = None) -> CheckResult:
    """POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER set and well-formed."""
    source = env if env is not None else os.environ
    pk = (source.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    funder = (source.get("POLYMARKET_FUNDER") or "").strip()

    if not pk:
        return CheckResult("env_vars", False, SEV_ERROR, "POLYMARKET_PRIVATE_KEY is empty")
    if not funder:
        return CheckResult("env_vars", False, SEV_ERROR, "POLYMARKET_FUNDER is empty")
    if "YOUR_PRIVATE_KEY" in pk or "YOUR_FUNDER_ADDRESS" in funder:
        return CheckResult(
            "env_vars", False, SEV_ERROR,
            "Placeholder values still in .env — replace before enabling live",
        )
    if not pk.startswith("0x") or len(pk) != 66:
        return CheckResult(
            "env_vars", False, SEV_ERROR,
            f"POLYMARKET_PRIVATE_KEY must be 0x + 64 hex chars (got len={len(pk)})",
        )
    if not funder.startswith("0x") or len(funder) != 42:
        return CheckResult(
            "env_vars", False, SEV_ERROR,
            f"POLYMARKET_FUNDER must be 0x + 40 hex chars (got len={len(funder)})",
        )
    return CheckResult(
        "env_vars", True, SEV_INFO,
        f"Private key + funder {funder[:6]}…{funder[-4:]} present",
        data={"funder": funder},
    )


def check_polygon_rpc(deps: DoctorDeps, rpc_url: str) -> CheckResult:
    """``eth_blockNumber`` returns a recent block."""
    try:
        resp = deps.call_rpc(rpc_url, "eth_blockNumber", [])
    except Exception as exc:  # noqa: BLE001 — any failure is fatal for this check
        return CheckResult("polygon_rpc", False, SEV_ERROR, f"RPC unreachable: {exc}")

    if "error" in resp:
        return CheckResult("polygon_rpc", False, SEV_ERROR, f"RPC error: {resp['error']}")

    block = _hex_to_int(resp.get("result", "0x0"))
    if block <= 0:
        return CheckResult("polygon_rpc", False, SEV_ERROR, f"implausible block number {block}")
    return CheckResult(
        "polygon_rpc", True, SEV_INFO,
        f"Block {block:,} on {rpc_url}",
        data={"block": block, "rpc_url": rpc_url},
    )


def check_eoa_derivation(
    deps: DoctorDeps,
    private_key: str,
    expected_funder: str,
) -> CheckResult:
    """Private key derives to some EOA; we report whether it == funder.

    Polymarket supports funder != EOA (signature_type=2 uses a Safe/proxy
    where the EOA signs but a different address holds funds), so a mismatch
    is a WARN, not an ERROR. The LiveCoordinator is still safe — the CLOB
    client derives the same EOA from the same PK.
    """
    try:
        eoa = deps.derive_eoa(private_key)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("eoa_derivation", False, SEV_ERROR, f"derive failed: {exc}")

    # The Account.from_key output is checksummed; normalize both ends.
    eoa_norm = eoa.lower()
    funder_norm = (expected_funder or "").lower()

    if not funder_norm:
        return CheckResult(
            "eoa_derivation", True, SEV_WARN,
            f"EOA derived → {eoa}. No funder to compare against.",
            data={"eoa": eoa},
        )
    if eoa_norm == funder_norm:
        return CheckResult(
            "eoa_derivation", True, SEV_INFO,
            f"EOA matches funder ({eoa[:6]}…{eoa[-4:]})",
            data={"eoa": eoa, "matches_funder": True},
        )
    return CheckResult(
        "eoa_derivation", True, SEV_WARN,
        f"EOA {eoa[:6]}…{eoa[-4:]} ≠ funder {expected_funder[:6]}…{expected_funder[-4:]} — "
        "OK for proxy/Safe wallets (signature_type=1/2), verify POLYMARKET_SIGNATURE_TYPE is set",
        data={"eoa": eoa, "matches_funder": False},
    )


def check_usdc_balance(
    deps: DoctorDeps,
    rpc_url: str,
    funder: str,
    *,
    min_usdc: float = 10.0,
) -> CheckResult:
    """Funder has ≥ ``min_usdc`` USDC.e on Polygon."""
    try:
        raw = _eth_call(deps, rpc_url, USDC_ADDRESS, _encode_balance_of(funder))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("usdc_balance", False, SEV_ERROR, f"balanceOf call failed: {exc}")

    balance_wei = _hex_to_int(raw)
    balance_usdc = balance_wei / 10**USDC_DECIMALS

    if balance_usdc < min_usdc:
        return CheckResult(
            "usdc_balance", False, SEV_ERROR,
            f"Funder has ${balance_usdc:,.2f} USDC, need ≥ ${min_usdc:,.2f}. "
            f"Deposit USDC.e on Polygon to {funder}.",
            data={"balance_usdc": balance_usdc, "required_usdc": min_usdc},
        )
    return CheckResult(
        "usdc_balance", True, SEV_INFO,
        f"Funder balance: ${balance_usdc:,.2f} USDC.e",
        data={"balance_usdc": balance_usdc},
    )


def check_ctf_allowance(
    deps: DoctorDeps,
    rpc_url: str,
    funder: str,
) -> CheckResult:
    """Check USDC allowance + CTF isApprovedForAll for both exchanges.

    Polymarket requires two approvals per exchange:
      * USDC.allowance(funder, exchange) > 0  (for BUYs)
      * CTF.isApprovedForAll(funder, exchange) == true  (for SELLs / settlement)

    Either the legacy CTF Exchange or the NegRisk Exchange must be fully
    approved; both approved is ideal. We WARN if only one is set (some
    markets aren't tradeable) and ERROR if neither is set.
    """
    try:
        legacy_usdc = _hex_to_int(
            _eth_call(deps, rpc_url, USDC_ADDRESS, _encode_allowance(funder, CTF_EXCHANGE))
        )
        legacy_ctf = _hex_to_int(
            _eth_call(deps, rpc_url, CTF_ADDRESS, _encode_is_approved_for_all(funder, CTF_EXCHANGE))
        )
        neg_usdc = _hex_to_int(
            _eth_call(deps, rpc_url, USDC_ADDRESS, _encode_allowance(funder, NEG_RISK_EXCHANGE))
        )
        neg_ctf = _hex_to_int(
            _eth_call(deps, rpc_url, CTF_ADDRESS, _encode_is_approved_for_all(funder, NEG_RISK_EXCHANGE))
        )
        neg_adapter_usdc = _hex_to_int(
            _eth_call(deps, rpc_url, USDC_ADDRESS, _encode_allowance(funder, NEG_RISK_ADAPTER))
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("ctf_allowance", False, SEV_ERROR, f"allowance call failed: {exc}")

    # A "fully approved" exchange has both USDC allowance > 0 AND isApprovedForAll == true.
    # The allowance threshold of 1 USDC.e (1e6 wei) is a lower bound — most Polymarket
    # setup guides call setAllowance(exchange, 2^256-1), which this will happily accept.
    legacy_ok = legacy_usdc >= 1_000_000 and legacy_ctf == 1
    neg_ok = neg_usdc >= 1_000_000 and neg_ctf == 1 and neg_adapter_usdc >= 1_000_000

    data = {
        "legacy_usdc_allowance": legacy_usdc,
        "legacy_ctf_approved": legacy_ctf == 1,
        "neg_usdc_allowance": neg_usdc,
        "neg_ctf_approved": neg_ctf == 1,
        "neg_adapter_usdc_allowance": neg_adapter_usdc,
    }

    if legacy_ok and neg_ok:
        return CheckResult(
            "ctf_allowance", True, SEV_INFO,
            "Both legacy CTF Exchange + NegRisk Exchange fully approved",
            data=data,
        )
    if legacy_ok:
        return CheckResult(
            "ctf_allowance", True, SEV_WARN,
            "Legacy CTF Exchange approved; NegRisk NOT approved — NegRisk markets will reject. "
            "Run Polymarket's enableTrading() against the NegRisk contracts to unlock them.",
            data=data,
        )
    if neg_ok:
        return CheckResult(
            "ctf_allowance", True, SEV_WARN,
            "NegRisk Exchange approved; legacy CTF Exchange NOT approved — legacy markets will reject.",
            data=data,
        )
    return CheckResult(
        "ctf_allowance", False, SEV_ERROR,
        "Neither exchange approved. Run Polymarket's setAllowance + setApprovalForAll "
        "flow before enabling live — see https://docs.polymarket.com/#allowances",
        data=data,
    )


def check_gamma_reachable(deps: DoctorDeps) -> CheckResult:
    """``GET /markets?limit=1`` returns a non-empty list."""
    try:
        markets = deps.http_client.get_markets(limit=1)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("gamma_api", False, SEV_ERROR, f"Gamma unreachable: {exc}")

    if not isinstance(markets, list):
        return CheckResult(
            "gamma_api", False, SEV_ERROR,
            f"unexpected response type: {type(markets).__name__}",
        )
    if not markets:
        return CheckResult(
            "gamma_api", False, SEV_WARN,
            "Gamma returned an empty markets list — possible API degradation",
        )
    sample = markets[0]
    sample_q = str(sample.get("question") or sample.get("slug") or "?")[:60]
    return CheckResult(
        "gamma_api", True, SEV_INFO,
        f"Gamma OK. Sample market: {sample_q}",
        data={"n_markets": len(markets)},
    )


def check_clob_orderbook(deps: DoctorDeps, token_id: str | None = None) -> CheckResult:
    """Fetch an orderbook end-to-end. If ``token_id`` is None, look one up via Gamma.

    This catches two things the prior checks miss: (a) the CLOB host is
    reachable from wherever we're running (separate from Gamma — they
    live on different domains), and (b) our orderbook parse path survives
    the real response schema.
    """
    from polymarket_strat.live.orderbook import parse_orderbook

    resolved_token = token_id
    if resolved_token is None:
        # Pick any active, unclosed market and grab its first clobTokenId.
        try:
            markets = deps.http_client.get_markets(limit=20)
        except Exception as exc:  # noqa: BLE001
            return CheckResult("clob_orderbook", False, SEV_ERROR, f"gamma fetch failed: {exc}")
        for m in markets:
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    tokens = None
            if tokens and isinstance(tokens, list):
                resolved_token = str(tokens[0])
                break
        if not resolved_token:
            return CheckResult(
                "clob_orderbook", False, SEV_WARN,
                "no clobTokenIds in first 20 markets — can't validate orderbook parse",
            )

    try:
        raw = deps.http_client.get_orderbook(resolved_token)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("clob_orderbook", False, SEV_ERROR, f"CLOB fetch failed: {exc}")

    try:
        book = parse_orderbook(raw, token_id=resolved_token)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("clob_orderbook", False, SEV_ERROR, f"parse failed: {exc}")

    if book.best_ask is None and book.best_bid is None:
        return CheckResult(
            "clob_orderbook", True, SEV_WARN,
            f"Book reachable but empty for {resolved_token[:10]}… — try a different token",
            data={"token_id": resolved_token, "age_s": book.age_seconds()},
        )
    return CheckResult(
        "clob_orderbook", True, SEV_INFO,
        f"Book OK for {resolved_token[:10]}…: "
        f"ask={book.best_ask.price if book.best_ask else 'n/a'}, "
        f"bid={book.best_bid.price if book.best_bid else 'n/a'}, "
        f"age={book.age_seconds():.1f}s",
        data={
            "token_id": resolved_token,
            "age_s": book.age_seconds(),
            "levels_ask": len(book.asks),
            "levels_bid": len(book.bids),
        },
    )


def check_telegram(deps: DoctorDeps, env: dict[str, str] | None = None) -> CheckResult:
    """Optional: send a one-line test message if Telegram creds are set."""
    source = env if env is not None else os.environ
    bot = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (source.get("TELEGRAM_CHAT_ID") or "").strip()

    if not bot or not chat:
        return CheckResult(
            "telegram", True, SEV_INFO,
            "Telegram creds not set — skipping (optional check)",
        )
    try:
        resp = deps.telegram_send(bot, chat, "[doctor] Live pipeline doctor OK ✅")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "telegram", False, SEV_WARN,
            f"send failed: {exc} — live will still run, alerts will be silent",
        )

    if not isinstance(resp, dict) or not resp.get("ok"):
        return CheckResult(
            "telegram", False, SEV_WARN,
            f"Telegram API returned non-ok response: {resp}",
        )
    return CheckResult("telegram", True, SEV_INFO, "Test message delivered")


# ---------------------------------------------------------------------------
# Composition.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DoctorReport:
    results: tuple[CheckResult, ...]
    overall_passed: bool  # True iff every required (error-severity) check passed

    @property
    def required_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.passed and r.severity == SEV_ERROR)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.passed and r.severity == SEV_WARN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_passed": self.overall_passed,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "severity": r.severity,
                    "detail": r.detail,
                    "data": r.data,
                }
                for r in self.results
            ],
        }


def run_doctor(
    *,
    deps: DoctorDeps | None = None,
    env: dict[str, str] | None = None,
    rpc_url: str | None = None,
    min_usdc: float = 10.0,
    skip: set[str] | None = None,
) -> DoctorReport:
    """Run every check. ``skip`` is a set of check names to omit."""
    deps = deps or DefaultDoctorDeps()
    env_map = env if env is not None else dict(os.environ)
    rpc = rpc_url or env_map.get("POLYGON_RPC_URL", POLYGON_RPC_DEFAULT)
    skip = skip or set()

    results: list[CheckResult] = []

    # 1. env_vars — gates the rest of the wallet checks.
    if "env_vars" not in skip:
        r = check_env_vars(env=env_map)
        results.append(r)
        if not r.passed:
            return _finalize(results)
        funder = str(r.data.get("funder") or "")
    else:
        funder = (env_map.get("POLYMARKET_FUNDER") or "").strip()

    # 2. polygon_rpc — gates the on-chain checks.
    if "polygon_rpc" not in skip:
        rpc_result = check_polygon_rpc(deps, rpc)
        results.append(rpc_result)
        rpc_ok = rpc_result.passed
    else:
        rpc_ok = True  # trust the user

    # 3. eoa_derivation — cheap, runs even without RPC.
    if "eoa_derivation" not in skip:
        pk = (env_map.get("POLYMARKET_PRIVATE_KEY") or "").strip()
        results.append(check_eoa_derivation(deps, pk, funder))

    # 4. usdc_balance — needs RPC + funder.
    if "usdc_balance" not in skip and rpc_ok and funder:
        results.append(check_usdc_balance(deps, rpc, funder, min_usdc=min_usdc))

    # 5. ctf_allowance — needs RPC + funder.
    if "ctf_allowance" not in skip and rpc_ok and funder:
        results.append(check_ctf_allowance(deps, rpc, funder))

    # 6. gamma_api.
    if "gamma_api" not in skip:
        results.append(check_gamma_reachable(deps))

    # 7. clob_orderbook.
    if "clob_orderbook" not in skip:
        results.append(check_clob_orderbook(deps))

    # 8. telegram — optional.
    if "telegram" not in skip:
        results.append(check_telegram(deps, env=env_map))

    return _finalize(results)


def _finalize(results: list[CheckResult]) -> DoctorReport:
    overall = all(r.passed for r in results if r.severity == SEV_ERROR)
    return DoctorReport(results=tuple(results), overall_passed=overall)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
_STATUS_ICON = {
    (True, SEV_INFO): "✓",
    (True, SEV_WARN): "⚠",
    (True, SEV_ERROR): "✓",  # shouldn't happen — passed+error is nonsensical
    (False, SEV_INFO): "✗",
    (False, SEV_WARN): "⚠",
    (False, SEV_ERROR): "✗",
}


def _render_human(report: DoctorReport) -> str:
    lines = [
        "=" * 72,
        " LIVE DOCTOR — pre-flight checks",
        "=" * 72,
    ]
    for r in report.results:
        icon = _STATUS_ICON.get((r.passed, r.severity), "?")
        lines.append(f"  {icon}  [{r.severity:5s}] {r.name:18s}  {r.detail}")
    lines.append("-" * 72)
    if report.overall_passed:
        warns = len(report.warnings)
        suffix = f" ({warns} warning{'s' if warns != 1 else ''})" if warns else ""
        lines.append(f"  RESULT: PASS{suffix} — live trading unblocked.")
    else:
        fails = [r.name for r in report.required_failures]
        lines.append(f"  RESULT: FAIL — {len(fails)} required check(s) failed: {', '.join(fails)}")
        lines.append("  Fix the failures above before enabling mode=live.")
    lines.append("=" * 72)
    return "\n".join(lines)


def _parse_skip(raw: list[str] | None) -> set[str]:
    if not raw:
        return set()
    # Support both `--skip a --skip b` and `--skip a,b`.
    out: set[str] = set()
    for entry in raw:
        for token in entry.split(","):
            token = token.strip()
            if token:
                out.add(token)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="polymarket_strat.live.doctor",
        description="Pre-flight checks for live trading.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        help="Check name(s) to skip. Repeat or comma-separate. "
             "Names: env_vars, polygon_rpc, eoa_derivation, usdc_balance, "
             "ctf_allowance, gamma_api, clob_orderbook, telegram.",
    )
    parser.add_argument(
        "--rpc-url",
        default=None,
        help="Polygon RPC URL (defaults to $POLYGON_RPC_URL or polygon-rpc.com).",
    )
    parser.add_argument(
        "--min-usdc",
        type=float,
        default=10.0,
        help="Minimum USDC balance required on the funder (default: $10).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report instead of text.")
    args = parser.parse_args(argv)

    report = run_doctor(
        rpc_url=args.rpc_url,
        min_usdc=args.min_usdc,
        skip=_parse_skip(args.skip),
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_render_human(report))

    return 0 if report.overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
