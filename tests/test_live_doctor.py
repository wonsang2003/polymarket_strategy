"""Pre-flight checks — wallet + network + API reachability.

Each check is tested against a fake DoctorDeps subclass that records calls
and returns canned responses. No network, no eth-account, no sockets open.
The goal is to pin the decision logic so CLI changes or RPC-response shape
drift doesn't silently bypass a real failure (e.g., an empty allowance
returning passed=True).
"""
from __future__ import annotations

from typing import Any

import pytest

from polymarket_strat.live.doctor import (
    CTF_EXCHANGE,
    NEG_RISK_EXCHANGE,
    SEV_ERROR,
    SEV_INFO,
    SEV_WARN,
    USDC_ADDRESS,
    CheckResult,
    DoctorDeps,
    check_clob_orderbook,
    check_ctf_allowance,
    check_env_vars,
    check_eoa_derivation,
    check_gamma_reachable,
    check_polygon_rpc,
    check_telegram,
    check_usdc_balance,
    run_doctor,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeHttpClient:
    def __init__(self, markets: list[dict] | None = None, orderbook: dict | None = None,
                 markets_exc: Exception | None = None, book_exc: Exception | None = None):
        self._markets = markets if markets is not None else []
        self._orderbook = orderbook if orderbook is not None else {}
        self._markets_exc = markets_exc
        self._book_exc = book_exc
        self.calls: list[tuple[str, Any]] = []

    def get_markets(self, *, limit: int = 50, **kwargs) -> list[dict]:
        self.calls.append(("get_markets", {"limit": limit}))
        if self._markets_exc:
            raise self._markets_exc
        return self._markets[:limit]

    def get_orderbook(self, token_id: str) -> dict:
        self.calls.append(("get_orderbook", {"token_id": token_id}))
        if self._book_exc:
            raise self._book_exc
        return self._orderbook


class _FakeDeps(DoctorDeps):
    """DoctorDeps subclass that records everything and returns canned data."""

    def __init__(
        self,
        *,
        http_client: _FakeHttpClient | None = None,
        eoa: str = "0xEoAADDrESS00000000000000000000000000ABCD",
        rpc_responses: dict[tuple[str, str], dict] | None = None,
        rpc_exc: Exception | None = None,
        telegram_response: dict | None = None,
        telegram_exc: Exception | None = None,
        derive_exc: Exception | None = None,
    ):
        super().__init__(http_client=http_client or _FakeHttpClient())
        self._eoa = eoa
        self._rpc_responses = rpc_responses or {}
        self._rpc_exc = rpc_exc
        self._telegram_response = telegram_response if telegram_response is not None else {"ok": True}
        self._telegram_exc = telegram_exc
        self._derive_exc = derive_exc
        self.rpc_calls: list[tuple[str, list]] = []
        self.telegram_calls: list[tuple[str, str, str]] = []

    def derive_eoa(self, private_key: str) -> str:
        if self._derive_exc:
            raise self._derive_exc
        return self._eoa

    def call_rpc(self, rpc_url: str, method: str, params: list) -> dict:
        self.rpc_calls.append((method, params))
        if self._rpc_exc:
            raise self._rpc_exc
        # Key: (method, data-hex-prefix) for eth_call; ("eth_blockNumber", "") for block.
        if method == "eth_blockNumber":
            return self._rpc_responses.get(("eth_blockNumber", ""), {"result": "0x4a817c800"})
        if method == "eth_call":
            call = params[0] if params else {}
            data = str(call.get("data") or "")
            key = ("eth_call", data[:10])  # selector match (0x + 8 hex)
            return self._rpc_responses.get(key, {"result": "0x"})
        return {"result": "0x"}

    def telegram_send(self, bot_token: str, chat_id: str, text: str) -> dict:
        self.telegram_calls.append((bot_token, chat_id, text))
        if self._telegram_exc:
            raise self._telegram_exc
        return self._telegram_response


VALID_PK = "0x" + "a" * 64
VALID_FUNDER = "0x" + "b" * 40


def _valid_env() -> dict[str, str]:
    return {"POLYMARKET_PRIVATE_KEY": VALID_PK, "POLYMARKET_FUNDER": VALID_FUNDER}


def _hex_word(value: int) -> dict[str, str]:
    """Build an RPC response whose result encodes ``value`` as a 32-byte word."""
    return {"result": "0x" + format(value, "064x")}


# ===========================================================================
# check_env_vars
# ===========================================================================


def test_env_vars_passes_with_valid_keys():
    r = check_env_vars(env=_valid_env())
    assert r.passed
    assert r.severity == SEV_INFO
    assert r.data["funder"] == VALID_FUNDER


def test_env_vars_rejects_missing_private_key():
    r = check_env_vars(env={"POLYMARKET_FUNDER": VALID_FUNDER})
    assert not r.passed
    assert r.severity == SEV_ERROR
    assert "POLYMARKET_PRIVATE_KEY" in r.detail


def test_env_vars_rejects_missing_funder():
    r = check_env_vars(env={"POLYMARKET_PRIVATE_KEY": VALID_PK})
    assert not r.passed
    assert "FUNDER" in r.detail


def test_env_vars_rejects_placeholder():
    env = {
        "POLYMARKET_PRIVATE_KEY": "0xYOUR_PRIVATE_KEY" + "0" * 48,
        "POLYMARKET_FUNDER": VALID_FUNDER,
    }
    r = check_env_vars(env=env)
    assert not r.passed
    assert "laceholder" in r.detail  # matches "placeholder"/"Placeholder"


def test_env_vars_rejects_malformed_private_key():
    r = check_env_vars(env={
        "POLYMARKET_PRIVATE_KEY": "abc123",  # no 0x, wrong length
        "POLYMARKET_FUNDER": VALID_FUNDER,
    })
    assert not r.passed
    assert "PRIVATE_KEY" in r.detail


def test_env_vars_rejects_malformed_funder():
    r = check_env_vars(env={
        "POLYMARKET_PRIVATE_KEY": VALID_PK,
        "POLYMARKET_FUNDER": "0x1234",  # wrong length
    })
    assert not r.passed
    assert "FUNDER" in r.detail


# ===========================================================================
# check_polygon_rpc
# ===========================================================================


def test_polygon_rpc_passes_when_block_returned():
    deps = _FakeDeps(rpc_responses={("eth_blockNumber", ""): _hex_word(80_000_000)})
    r = check_polygon_rpc(deps, "https://polygon-rpc.com")
    assert r.passed
    assert r.data["block"] == 80_000_000


def test_polygon_rpc_fails_on_exception():
    deps = _FakeDeps(rpc_exc=RuntimeError("connection refused"))
    r = check_polygon_rpc(deps, "https://polygon-rpc.com")
    assert not r.passed
    assert "connection refused" in r.detail


def test_polygon_rpc_fails_on_zero_block():
    deps = _FakeDeps(rpc_responses={("eth_blockNumber", ""): {"result": "0x0"}})
    r = check_polygon_rpc(deps, "https://polygon-rpc.com")
    assert not r.passed


def test_polygon_rpc_fails_on_error_field():
    deps = _FakeDeps(rpc_responses={("eth_blockNumber", ""): {"error": {"message": "rate limited"}}})
    r = check_polygon_rpc(deps, "https://polygon-rpc.com")
    assert not r.passed
    assert "rate limited" in r.detail


# ===========================================================================
# check_eoa_derivation
# ===========================================================================


def test_eoa_matches_funder():
    # fake deps derive to VALID_FUNDER verbatim
    deps = _FakeDeps(eoa=VALID_FUNDER)
    r = check_eoa_derivation(deps, VALID_PK, VALID_FUNDER)
    assert r.passed
    assert r.severity == SEV_INFO
    assert r.data["matches_funder"] is True


def test_eoa_case_insensitive_match():
    deps = _FakeDeps(eoa=VALID_FUNDER.upper())
    r = check_eoa_derivation(deps, VALID_PK, VALID_FUNDER.lower())
    assert r.passed
    assert r.data["matches_funder"] is True


def test_eoa_mismatch_warns_but_passes():
    # A different address still derives successfully — this is valid for
    # proxy/Safe funder setups (signature_type=1 or 2).
    deps = _FakeDeps(eoa="0x" + "c" * 40)
    r = check_eoa_derivation(deps, VALID_PK, VALID_FUNDER)
    assert r.passed
    assert r.severity == SEV_WARN
    assert r.data["matches_funder"] is False


def test_eoa_derivation_error_fails():
    deps = _FakeDeps(derive_exc=ValueError("bad key"))
    r = check_eoa_derivation(deps, "notakey", VALID_FUNDER)
    assert not r.passed
    assert "bad key" in r.detail


# ===========================================================================
# check_usdc_balance
# ===========================================================================


def test_usdc_balance_passes_when_sufficient():
    # 100 USDC = 100 * 10^6
    deps = _FakeDeps(rpc_responses={("eth_call", "0x70a08231"): _hex_word(100_000_000)})
    r = check_usdc_balance(deps, "rpc", VALID_FUNDER, min_usdc=10.0)
    assert r.passed
    assert r.data["balance_usdc"] == pytest.approx(100.0)


def test_usdc_balance_fails_below_min():
    # 5 USDC, need 10
    deps = _FakeDeps(rpc_responses={("eth_call", "0x70a08231"): _hex_word(5_000_000)})
    r = check_usdc_balance(deps, "rpc", VALID_FUNDER, min_usdc=10.0)
    assert not r.passed
    assert "$5.00" in r.detail
    assert r.data["balance_usdc"] == pytest.approx(5.0)


def test_usdc_balance_fails_on_rpc_error():
    deps = _FakeDeps(rpc_exc=RuntimeError("node down"))
    r = check_usdc_balance(deps, "rpc", VALID_FUNDER)
    assert not r.passed
    assert "node down" in r.detail


# ===========================================================================
# check_ctf_allowance
# ===========================================================================


def _approvals_rpc(
    *,
    legacy_usdc: int = 0,
    legacy_ctf: int = 0,
    neg_usdc: int = 0,
    neg_ctf: int = 0,
    neg_adapter_usdc: int = 0,
) -> dict:
    # check_ctf_allowance makes 5 RPC calls in order; our fake matches by
    # (method, data[:10]) but both USDC calls use the same allowance selector.
    # To tell them apart we need a richer fake — but here we leverage that
    # both USDC allowance calls share a selector (0xdd62ed3e) and we can use
    # a stateful side effect via a dict-that-counts. Simplify: use a custom
    # rpc_responses map keyed by full data string so each call gets the right
    # answer.
    # Actually, easier: subclass _FakeDeps and override call_rpc with a
    # counter — see _SequencedDeps below.
    raise NotImplementedError("use _SequencedDeps")


class _SequencedDeps(DoctorDeps):
    """RPC responses returned in the exact order they're requested."""

    def __init__(self, responses: list[dict]):
        super().__init__()
        self._queue = list(responses)
        self.rpc_calls: list[tuple[str, list]] = []

    def derive_eoa(self, pk: str) -> str:
        return VALID_FUNDER

    def call_rpc(self, rpc_url: str, method: str, params: list) -> dict:
        self.rpc_calls.append((method, params))
        if not self._queue:
            return {"result": "0x"}
        return self._queue.pop(0)

    def telegram_send(self, *a, **k) -> dict:
        return {"ok": True}


def test_ctf_allowance_passes_when_both_fully_approved():
    # Order in check_ctf_allowance:
    #   legacy USDC allowance, legacy CTF approval, neg USDC allowance,
    #   neg CTF approval, neg adapter USDC allowance.
    deps = _SequencedDeps([
        _hex_word(10**30),   # legacy USDC allowance
        _hex_word(1),        # legacy CTF isApprovedForAll
        _hex_word(10**30),   # neg USDC allowance
        _hex_word(1),        # neg CTF isApprovedForAll
        _hex_word(10**30),   # neg adapter USDC allowance
    ])
    r = check_ctf_allowance(deps, "rpc", VALID_FUNDER)
    assert r.passed
    assert r.severity == SEV_INFO
    assert r.data["legacy_ctf_approved"] is True
    assert r.data["neg_ctf_approved"] is True


def test_ctf_allowance_warns_when_only_legacy_approved():
    deps = _SequencedDeps([
        _hex_word(10**30),   # legacy USDC
        _hex_word(1),        # legacy CTF
        _hex_word(0),        # neg USDC — zero
        _hex_word(0),        # neg CTF — not approved
        _hex_word(0),        # neg adapter USDC
    ])
    r = check_ctf_allowance(deps, "rpc", VALID_FUNDER)
    assert r.passed
    assert r.severity == SEV_WARN
    assert "NegRisk" in r.detail


def test_ctf_allowance_warns_when_only_negrisk_approved():
    deps = _SequencedDeps([
        _hex_word(0),        # legacy USDC
        _hex_word(0),        # legacy CTF
        _hex_word(10**30),   # neg USDC
        _hex_word(1),        # neg CTF
        _hex_word(10**30),   # neg adapter USDC
    ])
    r = check_ctf_allowance(deps, "rpc", VALID_FUNDER)
    assert r.passed
    assert r.severity == SEV_WARN
    assert "legacy" in r.detail.lower()


def test_ctf_allowance_fails_when_neither_approved():
    deps = _SequencedDeps([_hex_word(0)] * 5)
    r = check_ctf_allowance(deps, "rpc", VALID_FUNDER)
    assert not r.passed
    assert r.severity == SEV_ERROR
    assert "setAllowance" in r.detail


def test_ctf_allowance_rejects_when_usdc_allowance_below_threshold():
    # 0.5 USDC allowance = 500_000 wei — below the 1e6 threshold.
    deps = _SequencedDeps([
        _hex_word(500_000),
        _hex_word(1),
        _hex_word(500_000),
        _hex_word(1),
        _hex_word(500_000),
    ])
    r = check_ctf_allowance(deps, "rpc", VALID_FUNDER)
    assert not r.passed  # neither "fully approved" under the threshold


def test_ctf_allowance_fails_on_rpc_error():
    class _Boom(DoctorDeps):
        def derive_eoa(self, pk): return VALID_FUNDER
        def call_rpc(self, *a, **k): raise RuntimeError("rpc boom")
        def telegram_send(self, *a, **k): return {"ok": True}
    deps = _Boom()
    r = check_ctf_allowance(deps, "rpc", VALID_FUNDER)
    assert not r.passed
    assert "rpc boom" in r.detail


# ===========================================================================
# check_gamma_reachable
# ===========================================================================


def test_gamma_passes_with_valid_response():
    http = _FakeHttpClient(markets=[{"question": "Will X happen?", "slug": "will-x"}])
    deps = _FakeDeps(http_client=http)
    r = check_gamma_reachable(deps)
    assert r.passed
    assert "Will X happen?" in r.detail


def test_gamma_warns_on_empty_list():
    deps = _FakeDeps(http_client=_FakeHttpClient(markets=[]))
    r = check_gamma_reachable(deps)
    assert not r.passed
    assert r.severity == SEV_WARN


def test_gamma_fails_on_exception():
    deps = _FakeDeps(http_client=_FakeHttpClient(markets_exc=RuntimeError("503 bad gateway")))
    r = check_gamma_reachable(deps)
    assert not r.passed
    assert "503" in r.detail


# ===========================================================================
# check_clob_orderbook
# ===========================================================================


def _valid_book(token_id: str = "tok-1") -> dict:
    import time
    return {
        "asset_id": token_id,
        "timestamp": str(time.time()),
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.50", "size": "100"}],
    }


def test_clob_orderbook_passes_end_to_end():
    http = _FakeHttpClient(
        markets=[{"clobTokenIds": '["0xtoken-a", "0xtoken-b"]'}],
        orderbook=_valid_book("0xtoken-a"),
    )
    deps = _FakeDeps(http_client=http)
    r = check_clob_orderbook(deps)
    assert r.passed
    assert r.data["token_id"] == "0xtoken-a"
    assert r.data["levels_ask"] == 1


def test_clob_orderbook_accepts_token_id_param():
    http = _FakeHttpClient(orderbook=_valid_book("explicit-tok"))
    deps = _FakeDeps(http_client=http)
    r = check_clob_orderbook(deps, token_id="explicit-tok")
    # No Gamma call needed when token_id provided.
    assert r.passed
    assert all(name != "get_markets" for name, _ in http.calls)


def test_clob_orderbook_warns_when_no_markets_have_tokens():
    http = _FakeHttpClient(markets=[{"question": "no tokens"}])
    deps = _FakeDeps(http_client=http)
    r = check_clob_orderbook(deps)
    assert not r.passed
    assert r.severity == SEV_WARN


def test_clob_orderbook_warns_when_book_empty():
    empty_book = {"asset_id": "tok", "timestamp": "1745280000", "bids": [], "asks": []}
    http = _FakeHttpClient(orderbook=empty_book)
    deps = _FakeDeps(http_client=http)
    r = check_clob_orderbook(deps, token_id="tok")
    assert r.passed
    assert r.severity == SEV_WARN


def test_clob_orderbook_fails_on_fetch_error():
    http = _FakeHttpClient(book_exc=RuntimeError("timeout"))
    deps = _FakeDeps(http_client=http)
    r = check_clob_orderbook(deps, token_id="tok")
    assert not r.passed
    assert "timeout" in r.detail


# ===========================================================================
# check_telegram
# ===========================================================================


def test_telegram_skipped_when_creds_missing():
    deps = _FakeDeps()
    r = check_telegram(deps, env={})
    assert r.passed
    assert r.severity == SEV_INFO
    assert "skipping" in r.detail.lower()
    assert deps.telegram_calls == []


def test_telegram_passes_when_send_succeeds():
    deps = _FakeDeps(telegram_response={"ok": True, "result": {"message_id": 42}})
    r = check_telegram(deps, env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"})
    assert r.passed
    assert deps.telegram_calls == [("t", "c", "[doctor] Live pipeline doctor OK ✅")]


def test_telegram_warns_on_exception():
    deps = _FakeDeps(telegram_exc=RuntimeError("401 bad bot token"))
    r = check_telegram(deps, env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"})
    assert not r.passed
    assert r.severity == SEV_WARN


def test_telegram_warns_on_non_ok_response():
    deps = _FakeDeps(telegram_response={"ok": False, "description": "Forbidden"})
    r = check_telegram(deps, env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"})
    assert not r.passed
    assert r.severity == SEV_WARN


# ===========================================================================
# run_doctor — composition + short-circuit
# ===========================================================================


def _all_passing_deps() -> _FakeDeps:
    """Build a deps instance that makes every check pass."""
    http = _FakeHttpClient(
        markets=[{"question": "q", "clobTokenIds": '["tok"]'}],
        orderbook=_valid_book("tok"),
    )
    # Need sequenced RPC for eth_blockNumber + balance + 5 allowance calls.
    # Override call_rpc with a selector-aware routing + a queue for allowance.
    deps = _FakeDeps(
        http_client=http,
        eoa=VALID_FUNDER,
        rpc_responses={
            ("eth_blockNumber", ""): _hex_word(80_000_000),
        },
    )
    # balance + 5 allowances: route by selector so isApprovedForAll returns
    # exactly 1 (the ABI-encoded True) while balance/allowance return big
    # numbers that comfortably clear thresholds.
    original = deps.call_rpc

    def routed(rpc_url: str, method: str, params: list):
        if method == "eth_blockNumber":
            return original(rpc_url, method, params)
        if method == "eth_call":
            call = params[0] if params else {}
            data = str(call.get("data") or "")
            if data.startswith("0xe985e9c5"):  # isApprovedForAll(bool)
                return _hex_word(1)
            return _hex_word(10**30)  # balance + allowances
        return {"result": "0x"}
    deps.call_rpc = routed  # type: ignore[assignment]
    return deps


def test_run_doctor_full_pass():
    report = run_doctor(deps=_all_passing_deps(), env=_valid_env(), skip={"telegram"})
    assert report.overall_passed
    names = [r.name for r in report.results]
    assert "env_vars" in names
    assert "polygon_rpc" in names
    assert "eoa_derivation" in names
    assert "usdc_balance" in names
    assert "ctf_allowance" in names
    assert "gamma_api" in names
    assert "clob_orderbook" in names


def test_run_doctor_short_circuits_on_env_failure():
    # If env_vars fails, none of the wallet checks can run.
    report = run_doctor(deps=_all_passing_deps(), env={}, skip={"telegram"})
    assert not report.overall_passed
    names = [r.name for r in report.results]
    assert names == ["env_vars"]  # short-circuited


def test_run_doctor_honors_skip():
    report = run_doctor(
        deps=_all_passing_deps(),
        env=_valid_env(),
        skip={"telegram", "clob_orderbook", "gamma_api"},
    )
    names = [r.name for r in report.results]
    assert "clob_orderbook" not in names
    assert "gamma_api" not in names
    assert "telegram" not in names


def test_run_doctor_overall_failure_when_usdc_insufficient():
    deps = _all_passing_deps()
    # Override: balance call returns 1 USDC, below the 10 default.
    original = deps.call_rpc
    balance_selector = "0x70a08231"

    def low_balance(rpc_url: str, method: str, params: list):
        if method == "eth_call":
            call = params[0] if params else {}
            data = str(call.get("data") or "")
            if data.startswith(balance_selector) and (call.get("to") or "").lower() == USDC_ADDRESS.lower():
                return _hex_word(1_000_000)  # 1 USDC
            return _hex_word(10**30)
        return original(rpc_url, method, params)
    deps.call_rpc = low_balance  # type: ignore[assignment]

    report = run_doctor(deps=deps, env=_valid_env(), skip={"telegram"})
    assert not report.overall_passed
    assert any(r.name == "usdc_balance" and not r.passed for r in report.results)


def test_run_doctor_report_serializes_to_json():
    report = run_doctor(deps=_all_passing_deps(), env=_valid_env(), skip={"telegram"})
    payload = report.to_dict()
    assert payload["overall_passed"] is True
    assert isinstance(payload["results"], list)
    for r in payload["results"]:
        assert {"name", "passed", "severity", "detail", "data"} <= set(r.keys())


def test_doctor_report_required_failures_excludes_warnings():
    # Build a report manually with one error fail + one warn fail.
    from polymarket_strat.live.doctor import DoctorReport
    rpt = DoctorReport(
        results=(
            CheckResult("a", False, SEV_ERROR, "err"),
            CheckResult("b", False, SEV_WARN, "warn"),
            CheckResult("c", True, SEV_INFO, "ok"),
        ),
        overall_passed=False,
    )
    assert [r.name for r in rpt.required_failures] == ["a"]
    assert [r.name for r in rpt.warnings] == ["b"]
