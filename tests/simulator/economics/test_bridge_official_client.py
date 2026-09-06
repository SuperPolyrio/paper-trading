from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from quant.simulator.admission import MemoryAdmissionStore, UnifiedAdmissionService
from quant.simulator.economics.bridge_client import (
    BridgeCommandService,
    PolymarketBridgeClient,
)
from quant.simulator.economics.bridge_cli import build_parser, run

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/raw/api-spec/bridge-openapi.yaml"


class Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class Session:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return Response(self.payloads.pop(0))


def test_bridge_openapi_contract_contains_all_user_commands() -> None:
    spec = yaml.safe_load(SPEC.read_text())
    assert set(spec["paths"]) >= {
        "/supported-assets",
        "/quote",
        "/deposit",
        "/withdraw",
        "/status/{address}",
    }


def test_address_commands_are_dry_run_without_explicit_execute() -> None:
    session = Session([])
    service = BridgeCommandService(
        client=PolymarketBridgeClient(session=session),
        admission=UnifiedAdmissionService(store=MemoryAdmissionStore()),
    )

    result = service.withdrawal_address(
        account_id="account-1",
        strategy_id="strategy-1",
        wallet_address="0xwallet",
        to_chain_id="1",
        to_token_address="0xtoken",
        recipient_address="0xrecipient",
    )

    assert result["status"] == "DRY_RUN"
    assert session.calls == []


def test_bridge_status_walks_cursor_and_recovery_never_credits_without_evidence() -> None:
    session = Session(
        [
            {"transactions": [{"txHash": "0xone"}], "nextCursor": "next"},
            {"transactions": [{"txHash": "0xtwo"}], "nextCursor": None},
            {"transactions": [], "nextCursor": None},
        ]
    )
    service = BridgeCommandService(
        client=PolymarketBridgeClient(session=session),
        admission=UnifiedAdmissionService(store=MemoryAdmissionStore()),
    )

    rows = service.status(
        account_id="account-1",
        strategy_id="strategy-1",
        bridge_address="bridge-address",
    )
    missing = service.recovery(
        account_id="account-1",
        strategy_id="strategy-1",
        bridge_address="bridge-address",
        transaction_hash="0xmissing",
    )

    assert {row["txHash"] for row in rows} == {"0xone", "0xtwo"}
    assert session.calls[1]["params"]["cursor"] == "next"
    assert missing["status"] == "NO_OFFICIAL_EVIDENCE"
    assert "DO_NOT_CREDIT_CASH" in missing["action"]


def test_bridge_cli_exposes_all_commands_and_defaults_mutations_to_dry_run() -> None:
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert set(commands) == {
        "supported-assets",
        "quote",
        "deposit-address",
        "withdrawal-address",
        "status",
        "recovery",
    }
    session = Session([])
    service = BridgeCommandService(
        client=PolymarketBridgeClient(session=session),
        admission=UnifiedAdmissionService(store=MemoryAdmissionStore()),
    )
    result = run(
        service,
        [
            "--account-id",
            "account-1",
            "deposit-address",
            "--wallet-address",
            "0xwallet",
        ],
    )
    assert result["status"] == "DRY_RUN"
    assert session.calls == []
