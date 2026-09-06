from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from quant.simulator.admission import (
    GeoblockSnapshot,
    MemoryAdmissionStore,
    UnifiedAdmissionService,
)
from quant.simulator.combo.adapters import OfficialComboRestAdapter
from quant.simulator.combo.models import (
    ComboDirection,
    ComboQuote,
    ComboRequest,
    ComboRfqState,
    ExecutionStatus,
    OfficialRfqSnapshot,
    RequestedSize,
    SizeUnit,
    decimal_to_e6,
    e6_to_decimal,
)
from quant.simulator.combo.quoter_ws import (
    InMemoryQuoterCheckpoint,
    OfficialQuoterCommandSession,
    OfficialQuoterWsAdapter,
    parse_quoter_event,
)
from quant.simulator.combo.state_machine import ComboRfqMachine
from quant.simulator.combo.service import ComboQuoterService, ComboRfqService

ROOT = Path(__file__).resolve().parents[3]
OPENAPI = ROOT / "docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/raw/api-spec/combos-rfq-openapi.yaml"
ASYNCAPI = ROOT / "docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/raw/asyncapi-rfq.json"
NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class Headers:
    def __init__(self, value: str) -> None:
        self.value = value

    def headers(self, **_: Any) -> dict[str, str]:
        return {"X-Test-Auth": self.value}


def _request(direction: ComboDirection = ComboDirection.BUY) -> ComboRequest:
    return ComboRequest(
        rfq_id="rfq-1",
        leg_position_ids=("leg-1", "leg-2"),
        yes_position_id="combo-yes",
        no_position_id="combo-no",
        direction=direction,
        requested_size=RequestedSize(
            SizeUnit.NOTIONAL if direction is ComboDirection.BUY else SizeUnit.SHARES,
            1_000_000,
        ),
        created_at=NOW,
        submission_deadline=NOW + timedelta(milliseconds=400),
    )


def _quote() -> ComboQuote:
    return ComboQuote(
        quote_id="quote-1",
        rfq_id="rfq-1",
        price_e6=450_000,
        size_e6=2_000_000,
        expires_at=NOW + timedelta(seconds=10),
    )


def test_local_openapi_and_asyncapi_contracts_cover_required_protocol() -> None:
    openapi = yaml.safe_load(OPENAPI.read_text())
    asyncapi = json.loads(ASYNCAPI.read_text())

    assert "/v1/rfq/combo-markets" in openapi["paths"]
    assert "/v1/maker/quotes" in openapi["paths"]
    assert "/v1/maker/quotes/cancel" in openapi["paths"]
    assert "/v1/maker/confirmations" in openapi["paths"]
    description = asyncapi["info"]["description"]
    assert "six-decimal fixed-point" in description
    schemas = asyncapi["components"]["schemas"]
    assert schemas["RfqExecutionUpdate"]["properties"]["status"]["enum"] == [
        "MATCHED",
        "MINED",
        "RETRYING",
        "CONFIRMED",
        "FAILED",
    ]
    assert "submission_deadline" in schemas["RfqRequest"]["required"]
    assert "confirm_by" in schemas["RfqConfirmationRequest"]["required"]


def test_e6_is_exact_and_buy_sell_units_are_not_interchangeable() -> None:
    assert decimal_to_e6(Decimal("1.234567")) == 1_234_567
    assert e6_to_decimal("1234567") == Decimal("1.234567")
    with pytest.raises(ValueError, match="more than six"):
        decimal_to_e6("1.0000001")
    with pytest.raises(ValueError, match="BUY RFQ must use notional"):
        ComboRequest(
            **{
                **_request().__dict__,
                "requested_size": RequestedSize(SizeUnit.SHARES, 1_000_000),
            }
        )
    with pytest.raises(ValueError, match="SELL RFQ must use shares"):
        ComboRequest(
            **{
                **_request(ComboDirection.SELL).__dict__,
                "requested_size": RequestedSize(SizeUnit.NOTIONAL, 1_000_000),
            }
        )


def test_deadlines_come_from_server_and_timeout_requires_status_query() -> None:
    machine = ComboRfqMachine(_request())
    machine, _ = machine.open_competition(event_id="open", event_ts=NOW)
    machine, _ = machine.submit_quote(
        _quote(), event_id="quote", event_ts=NOW + timedelta(milliseconds=399)
    )
    machine, _ = machine.accept_quote(
        event_id="accept", event_ts=NOW + timedelta(seconds=9, milliseconds=999)
    )
    machine, timeout = machine.local_timeout(
        event_id="timeout", event_ts=NOW + timedelta(seconds=10)
    )

    assert machine.state is ComboRfqState.RECONCILING
    assert machine.needs_reconciliation
    assert timeout.payload["retry_policy"] == "QUERY_STATUS_DO_NOT_RESUBMIT"
    machine, _ = machine.apply_execution(
        ExecutionStatus.CONFIRMED,
        event_id="late-terminal",
        event_ts=NOW + timedelta(seconds=11),
        tx_hash="0xtx",
    )
    assert machine.state is ComboRfqState.CONFIRMED


def test_expired_submission_and_last_look_fail_closed() -> None:
    machine = ComboRfqMachine(_request())
    machine, _ = machine.open_competition(event_id="open", event_ts=NOW)
    machine, transition = machine.submit_quote(
        _quote(),
        event_id="late-quote",
        event_ts=NOW + timedelta(milliseconds=401),
    )
    assert machine.state is ComboRfqState.FAILED
    assert transition.payload["reason"] == "quote_submitted_after_submission_deadline"

    machine = ComboRfqMachine(_request())
    machine, _ = machine.open_competition(event_id="open-2", event_ts=NOW)
    machine, _ = machine.submit_quote(_quote(), event_id="quote-2", event_ts=NOW)
    machine, _ = machine.accept_quote(event_id="accept-2", event_ts=NOW)
    machine, _ = machine.request_last_look(
        confirm_by=NOW + timedelta(seconds=1),
        event_id="last-look",
        event_ts=NOW,
    )
    machine, transition = machine.confirm_last_look(
        confirm=True,
        event_id="late-confirm",
        event_ts=NOW + timedelta(seconds=1, milliseconds=1),
    )
    assert machine.state is ComboRfqState.FAILED
    assert transition.payload["reason"] == "last_look_confirm_by_elapsed"


def test_quoter_events_are_deterministically_deduplicated() -> None:
    raw = {
        "type": "execution_update",
        "payload": {"rfq_id": "rfq-1", "quote_id": "q-1", "status": "MINED"},
    }
    first = parse_quoter_event(raw, received_at=NOW)
    second = parse_quoter_event(raw, received_at=NOW + timedelta(seconds=1))
    checkpoint = InMemoryQuoterCheckpoint()

    assert first.event_id == second.event_id
    assert checkpoint.record(first)
    assert not checkpoint.record(second)


def test_public_catalog_maps_position_ids_by_outcome_and_paginates() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "markets": [
                        {
                            "id": "market-1",
                            "condition_id": "condition-1",
                            "position_ids": ["yes-1", "no-1"],
                            "outcomes": ["Yes", "No"],
                            "outcome_prices": ["0.6", "0.4"],
                            "slug": "slug-1",
                            "title": "Title 1",
                            "volume": 10,
                            "tags": ["sports"],
                        }
                    ],
                    "next_cursor": "cursor-2",
                }
            ),
            FakeResponse({"markets": [], "next_cursor": None}),
        ]
    )
    adapter = OfficialComboRestAdapter(session=session)

    markets = adapter.all_combo_markets()

    assert len(markets) == 1
    assert markets[0].yes_position_id == "yes-1"
    assert markets[0].no_position_id == "no-1"
    assert session.calls[1]["params"]["cursor"] == "cursor-2"


def test_http_200_business_failed_is_not_normalized_as_success() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "rfq_id": "rfq-1",
                    "status": "FAILED",
                    "error": {"code": "NO_QUOTES", "message": "no executable quote"},
                }
            )
        ]
    )
    adapter = OfficialComboRestAdapter(
        session=session,
        account_headers=Headers("account"),
        builder_headers=Headers("builder"),
    )

    result = adapter.create_builder_rfq(_request())

    assert result.state is ComboRfqState.FAILED
    assert result.error_code == "NO_QUOTES"
    assert session.calls[0]["headers"]["X-Test-Auth"] == "builder"


def test_quoter_command_session_sends_quote_cancel_and_last_look_contracts() -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def send(self, value: str) -> None:
            self.messages.append(json.loads(value))

    async def scenario() -> list[dict[str, Any]]:
        websocket = WebSocket()
        session = OfficialQuoterCommandSession(websocket)
        await session.submit_quote(_request(), _quote(), now=NOW)
        await session.cancel_quote(
            rfq_id="rfq-1",
            quote_id="quote-1",
            signer_address="0xsigner",
            maker_address="0xmaker",
        )
        await session.respond_last_look(
            rfq_id="rfq-1",
            quote_id="quote-1",
            confirm_by=NOW + timedelta(seconds=1),
            confirm=True,
            now=NOW,
        )
        return websocket.messages

    messages = asyncio.run(scenario())

    assert [message["type"] for message in messages] == [
        "RFQ_QUOTE",
        "RFQ_QUOTE_CANCEL",
        "RFQ_CONFIRMATION_RESPONSE",
    ]
    assert messages[0]["price_e6"] == "450000"
    assert messages[2]["decision"] == "CONFIRM"


def test_quoter_command_session_rejects_expired_server_deadlines() -> None:
    class WebSocket:
        async def send(self, value: str) -> None:
            raise AssertionError("expired command must not be sent")

    async def scenario() -> None:
        session = OfficialQuoterCommandSession(WebSocket())
        with pytest.raises(ValueError, match="submission_deadline"):
            await session.submit_quote(
                _request(),
                _quote(),
                now=NOW + timedelta(milliseconds=401),
            )
        with pytest.raises(ValueError, match="confirm_by"):
            await session.respond_last_look(
                rfq_id="rfq-1",
                quote_id="quote-1",
                confirm_by=NOW + timedelta(seconds=1),
                confirm=True,
                now=NOW + timedelta(seconds=2),
            )

    asyncio.run(scenario())


def test_quoter_ws_reconnects_after_disconnect_and_authenticates_again() -> None:
    class Auth:
        def auth_payload(self) -> dict[str, str]:
            return {"type": "AUTH", "token": "test-only"}

    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.closed = False

        async def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        def __aiter__(self) -> WebSocket:
            return self

        async def __anext__(self) -> str:
            if len(self.sent) == 1:
                return json.dumps(
                    {
                        "type": "execution_update",
                        "payload": {
                            "rfq_id": "rfq-1",
                            "quote_id": "quote-1",
                            "status": "MATCHED",
                        },
                    }
                )
            raise StopAsyncIteration

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> tuple[int, WebSocket, str]:
        attempts = 0
        socket = WebSocket()

        async def connect(*_: Any, **__: Any) -> WebSocket:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("controlled disconnect")
            return socket

        adapter = OfficialQuoterWsAdapter(
            auth_provider=Auth(),
            checkpoint=InMemoryQuoterCheckpoint(),
            connect_factory=connect,
            reconnect_min_seconds=0,
            reconnect_max_seconds=0,
        )
        stream = adapter.events()
        event = await anext(stream)
        await stream.aclose()
        return attempts, socket, event.execution_status or ""

    attempts, socket, status = asyncio.run(scenario())
    assert attempts == 2
    assert socket.sent == [{"type": "AUTH", "token": "test-only"}]
    assert socket.closed
    assert status == "MATCHED"


def test_late_or_backward_nonterminal_status_cannot_regress_state() -> None:
    machine = ComboRfqMachine(_request())
    machine, _ = machine.apply_execution(
        ExecutionStatus.MATCHED,
        event_id="matched",
        event_ts=NOW + timedelta(seconds=2),
    )
    machine, _ = machine.apply_execution(
        ExecutionStatus.MINED,
        event_id="mined",
        event_ts=NOW + timedelta(seconds=3),
    )
    with pytest.raises(ValueError, match="invalid RFQ execution transition"):
        machine.apply_execution(
            ExecutionStatus.MATCHED,
            event_id="backward",
            event_ts=NOW + timedelta(seconds=4),
        )
    with pytest.raises(ValueError, match="late non-terminal"):
        machine.apply_execution(
            ExecutionStatus.RETRYING,
            event_id="late",
            event_ts=NOW + timedelta(seconds=1),
        )


def test_reconciliation_skips_missed_events_and_ignores_stale_regression() -> None:
    class Store:
        def apply_transition(
            self, machine: ComboRfqMachine, transition: Any, *, source: str
        ) -> ComboRfqMachine:
            assert source == "OFFICIAL_RECONCILIATION"
            return machine

    service = ComboRfqService(
        adapter=None,  # type: ignore[arg-type]
        store=Store(),  # type: ignore[arg-type]
        admission=None,  # type: ignore[arg-type]
    )
    machine = ComboRfqMachine(_request())
    mined = OfficialRfqSnapshot.from_api(
        {"rfq_id": "rfq-1", "status": "MINED", "tx_hash": "0xpending"}
    )
    machine = service.reconcile_snapshot(
        machine,
        mined,
        event_id="mined-after-disconnect",
        event_ts=NOW + timedelta(seconds=3),
    )
    assert machine.state is ComboRfqState.MINED

    stale_matched = OfficialRfqSnapshot.from_api(
        {"rfq_id": "rfq-1", "status": "MATCHED"}
    )
    unchanged = service.reconcile_snapshot(
        machine,
        stale_matched,
        event_id="stale-matched",
        event_ts=NOW + timedelta(seconds=2),
    )
    assert unchanged is machine


def test_quoter_commands_all_pass_through_unified_admission() -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def send(self, value: str) -> None:
            self.messages.append(json.loads(value))

    class Geo:
        def snapshot(self, *, now: datetime | None = None) -> GeoblockSnapshot:
            observed = now or NOW
            return GeoblockSnapshot(
                blocked=False,
                country="US",
                region="WY",
                detected_ip="203.0.113.10",
                observed_at=observed,
                expires_at=observed + timedelta(minutes=1),
                raw_payload_hash="a" * 64,
            )

    async def scenario() -> tuple[list[dict[str, Any]], MemoryAdmissionStore]:
        websocket = WebSocket()
        store = MemoryAdmissionStore()
        service = ComboQuoterService(
            session=OfficialQuoterCommandSession(websocket),
            admission=UnifiedAdmissionService(store=store, geoblock_provider=Geo()),
        )
        await service.submit_quote(
            _request(),
            _quote(),
            account_id="account-1",
            strategy_id="strategy-1",
            observed_at=NOW,
        )
        await service.cancel_quote(
            rfq_id="rfq-1",
            quote_id="quote-1",
            signer_address="0xsigner",
            maker_address="0xmaker",
            account_id="account-1",
            strategy_id="strategy-1",
            observed_at=NOW,
        )
        await service.respond_last_look(
            _request(),
            quote_id="quote-1",
            confirm_by=NOW + timedelta(seconds=1),
            confirm=False,
            account_id="account-1",
            strategy_id="strategy-1",
            observed_at=NOW,
        )
        return websocket.messages, store

    messages, store = asyncio.run(scenario())
    assert len(messages) == 3
    assert len(store.decisions) == 3
    assert all(decision.allowed for decision in store.decisions.values())
