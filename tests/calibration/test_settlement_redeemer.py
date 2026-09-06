from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.calibration.settlement_redeemer import (
    CONDITIONAL_TOKENS,
    NEG_RISK_ADAPTER,
    RedeemCandidate,
    RelayerCredentialsMissing,
    SafeSettlementRedeemer,
    SettlementPreflightError,
    SettlementRedeemError,
)


def _candidate(*, negative_risk: bool = False) -> RedeemCandidate:
    return RedeemCandidate(
        title="Test market",
        outcome="Yes",
        condition_id="0x" + "11" * 32,
        asset_id="123",
        size=Decimal("8"),
        negative_risk=negative_risk,
        redeemable=True,
        current_value=Decimal("8"),
        current_price=Decimal("1"),
    )


def _redeemer(environ=None) -> SafeSettlementRedeemer:
    instance = object.__new__(SafeSettlementRedeemer)
    instance.environ = environ or {}
    return instance


def test_select_candidate_prefers_smallest_standard_position(monkeypatch) -> None:
    redeemer = _redeemer()
    monkeypatch.setattr(
        redeemer,
        "list_redeemable_positions",
        lambda: [
            _candidate(negative_risk=True),
            RedeemCandidate(
                title="Large",
                outcome="No",
                condition_id="0x" + "22" * 32,
                asset_id="456",
                size=Decimal("19"),
                negative_risk=False,
                redeemable=True,
                current_value=Decimal("19"),
                current_price=Decimal("1"),
            ),
            RedeemCandidate(
                title="Small",
                outcome="No",
                condition_id="0x" + "33" * 32,
                asset_id="789",
                size=Decimal("7"),
                negative_risk=False,
                redeemable=True,
                current_value=Decimal("7"),
                current_price=Decimal("1"),
            ),
        ],
    )

    selected = redeemer.select_candidate(max_size=Decimal("20"))

    assert selected.title == "Small"
    assert selected.asset_id == "789"


def test_select_candidate_rejects_zero_value_losing_position(monkeypatch) -> None:
    redeemer = _redeemer()
    losing = RedeemCandidate(
        title="Resolved loser",
        outcome="Yes",
        condition_id="0x" + "44" * 32,
        asset_id="999",
        size=Decimal("7"),
        negative_risk=True,
        redeemable=True,
        current_value=Decimal("0"),
        current_price=Decimal("0"),
    )
    monkeypatch.setattr(redeemer, "list_redeemable_positions", lambda: [losing])

    with pytest.raises(SettlementPreflightError, match="no redeemable position"):
        redeemer.select_candidate(
            asset_id=losing.asset_id,
            allow_negative_risk=True,
        )


def test_standard_redeem_uses_ctf_and_binary_index_sets() -> None:
    redeemer = _redeemer()

    target, calldata = redeemer._redeem_call(
        _candidate(),
        negative_risk_amounts=None,
    )

    assert target == CONDITIONAL_TOKENS
    assert calldata.startswith("0x01b7037c")


def test_negative_risk_requires_exact_amounts() -> None:
    redeemer = _redeemer()

    with pytest.raises(SettlementPreflightError, match="exact"):
        redeemer._redeem_call(_candidate(negative_risk=True), negative_risk_amounts=None)

    target, calldata = redeemer._redeem_call(
        _candidate(negative_risk=True),
        negative_risk_amounts=(8_000_000, 0),
    )
    assert target == NEG_RISK_ADAPTER
    assert calldata.startswith("0x")


def test_auth_mode_never_reuses_clob_credentials() -> None:
    redeemer = _redeemer(
        {
            "POLY_QUANT_PROBE_API_KEY": "clob-key",
            "POLY_QUANT_PROBE_API_SECRET": "clob-secret",
            "POLY_QUANT_PROBE_API_PASSPHRASE": "clob-passphrase",
        }
    )

    assert redeemer._auth_mode() == "MISSING"
    with pytest.raises(RelayerCredentialsMissing):
        redeemer._auth_headers("POST", "/submit", {}, "MISSING")


def test_relayer_api_key_mode_uses_only_documented_headers() -> None:
    redeemer = _redeemer(
        {
            "POLYMARKET_RELAYER_API_KEY": "relay-key",
            "POLYMARKET_RELAYER_API_KEY_ADDRESS": "0xabc",
        }
    )

    assert redeemer._auth_mode() == "RELAYER_API_KEY"
    assert redeemer._auth_headers("POST", "/submit", {}, "RELAYER_API_KEY") == {
        "RELAYER_API_KEY": "relay-key",
        "RELAYER_API_KEY_ADDRESS": "0xabc",
    }


def test_relayer_key_owner_must_match_signer() -> None:
    redeemer = _redeemer(
        {
            "POLYMARKET_RELAYER_API_KEY": "relay-key",
            "POLYMARKET_RELAYER_API_KEY_ADDRESS": "0x0000000000000000000000000000000000000001",
        }
    )
    redeemer.signer_address = "0x0000000000000000000000000000000000000002"

    with pytest.raises(SettlementPreflightError, match="owner"):
        redeemer._require_relayer_key_owner()


def test_submit_refuses_changed_safe_nonce(monkeypatch) -> None:
    redeemer = _redeemer()
    redeemer._safe_nonce = lambda: 9
    preflight = SimpleNamespace(
        auth_mode="RELAYER_API_KEY",
        safe_nonce=8,
    )

    with pytest.raises(SettlementPreflightError, match="nonce changed"):
        redeemer.submit_and_wait(preflight, metadata="test")


def test_submit_rejection_is_structured_and_audits_unchanged_chain() -> None:
    class Response:
        status_code = 400
        is_success = False
        text = '{"message":"precheck rejected"}'

        @staticmethod
        def json():
            return {"message": "precheck rejected"}

    class Client:
        @staticmethod
        def post(*_args, **_kwargs):
            return Response()

        @staticmethod
        def get(*_args, **_kwargs):
            return SimpleNamespace(
                is_success=True,
                json=lambda: [
                    {
                        "transactionID": "tx-1",
                        "state": "STATE_FAILED",
                        "nonce": "8",
                        "metadata": "test metadata",
                        "errorMsg": "PRECHECK_SKIPPED: zero position balance",
                    }
                ],
            )

    redeemer = _redeemer()
    redeemer.client = Client()
    redeemer.relayer_url = "https://relayer.example"
    redeemer._safe_nonce = lambda: 8
    redeemer._write_route = lambda: {"blocked": False}
    redeemer._build_safe_request = lambda *_args, **_kwargs: {"safe": "request"}
    redeemer._auth_headers = lambda *_args, **_kwargs: {"auth": "redacted"}
    redeemer._token_balance = lambda _asset_id: Decimal("8")
    redeemer._pusd_balance = lambda: Decimal("132")
    preflight = SimpleNamespace(
        auth_mode="RELAYER_API_KEY",
        safe_nonce=8,
        candidate=_candidate(),
        token_balance=Decimal("8"),
        pusd_balance=Decimal("132"),
    )

    with pytest.raises(SettlementRedeemError, match="zero position balance") as raised:
        redeemer.submit_and_wait(preflight, metadata="test metadata")

    evidence = raised.value.evidence
    assert evidence["http_status"] == 400
    assert evidence["relayer_transaction"]["transaction_id"] == "tx-1"
    assert evidence["relayer_transaction"]["state"] == "STATE_FAILED"
    assert evidence["chain_after_rejection"]["safe_nonce_unchanged"] is True
