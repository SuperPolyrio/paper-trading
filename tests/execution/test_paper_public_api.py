from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from flask import Flask

from quant.paper.public_api import (
    ApiIdentity,
    IdempotencyClaim,
    PaperApiError,
    canonical_request_hash,
    decode_cursor,
    encode_cursor,
    hash_api_token,
    issue_api_token,
    normalize_limit,
    parse_api_token,
    require_idempotency_key,
)
from quant.paper.tenant_platform import TenantPrincipal
from scripts.api.routes.paper_v1 import build_openapi_spec, create_paper_v1_blueprint


class FakeBackend:
    def __init__(self) -> None:
        tenant_id = uuid4()
        user_id = uuid4()
        self.identity = ApiIdentity(
            principal=TenantPrincipal(tenant_id=tenant_id, actor_user_id=user_id),
            api_key_id=uuid4(),
            key_prefix="ppk_test",
            scopes=frozenset(
                {"paper:read", "paper:trade", "paper:accounts:write", "paper:admin"}
            ),
        )
        self.account_id = uuid4()
        self.strategy_id = uuid4()
        self.accounts: dict[UUID, dict[str, object]] = {}
        self.orders: dict[int, dict[str, object]] = {}
        self.replays: dict[UUID, dict[str, object]] = {}
        self.scenarios: dict[UUID, dict[str, object]] = {}
        self.conditional_orders: dict[UUID, dict[str, object]] = {}
        self.idempotency: dict[tuple[str, str], dict[str, object]] = {}
        self.request_log: list[dict[str, object]] = []
        self.export_bytes = 0
        self.tenant_status = "ACTIVE"
        self.jobs: list[dict[str, object]] = []
        self.dlq: dict[UUID, dict[str, object]] = {}
        self.notices: dict[UUID, dict[str, object]] = {}
        self.incidents: dict[UUID, dict[str, object]] = {}
        self.retention: dict[str, dict[str, object]] = {}
        self.bundles: dict[UUID, tuple[dict[str, object], bytes]] = {}

    def authenticate(self, token: str) -> ApiIdentity:
        if token != "test-paper-token":
            raise PaperApiError(
                "PAPER_AUTH_INVALID", "invalid API key", status_code=401
            )
        return self.identity

    def charge_request_quota(
        self, identity: ApiIdentity, *, request_id: UUID
    ) -> object:
        assert identity == self.identity
        return SimpleNamespace(
            hard_limit=Decimal(1000),
            used_after=Decimal(1),
            window_seconds=60,
        )

    def claim_idempotency(
        self,
        identity: ApiIdentity,
        *,
        operation: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> IdempotencyClaim:
        request_hash = canonical_request_hash(payload)
        lookup = (operation, idempotency_key)
        current = self.idempotency.get(lookup)
        if current is not None:
            if current["request_hash"] != request_hash:
                raise PaperApiError(
                    "PAPER_IDEMPOTENCY_CONFLICT",
                    "key reused with another payload",
                    status_code=409,
                )
            return IdempotencyClaim(
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                lease_owner=None,
                replayed=True,
                response_status=int(current["status"]),
                response_body=current["body"],
            )
        return IdempotencyClaim(
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            lease_owner=uuid4(),
            replayed=False,
        )

    def complete_idempotency(
        self,
        identity: ApiIdentity,
        claim: IdempotencyClaim,
        *,
        status_code: int,
        response_body: dict[str, object],
    ) -> None:
        self.idempotency[(claim.operation, claim.idempotency_key)] = {
            "request_hash": claim.request_hash,
            "status": status_code,
            "body": response_body,
        }

    def fail_idempotency(self, *args: object, **kwargs: object) -> None:
        return None

    def record_request(self, identity: ApiIdentity, **record: object) -> None:
        self.request_log.append(record)

    def list_accounts(
        self, identity: ApiIdentity, **kwargs: object
    ) -> dict[str, object]:
        return {"items": list(self.accounts.values()), "next_cursor": None}

    def create_account(
        self,
        identity: ApiIdentity,
        *,
        name: str,
        initial_cash: Decimal,
        idempotency_key: str,
    ) -> dict[str, object]:
        account = {
            "account_id": self.account_id,
            "default_strategy_id": self.strategy_id,
            "name": name,
            "initial_cash": initial_cash,
            "status": "ACTIVE",
        }
        self.accounts[self.account_id] = account
        return account

    def get_account(self, identity: ApiIdentity, account_id: UUID) -> dict[str, object]:
        if account_id not in self.accounts:
            raise PaperApiError("PAPER_ACCOUNT_NOT_FOUND", "not found", status_code=404)
        return self.accounts[account_id]

    def fork_account(
        self, identity: ApiIdentity, **kwargs: object
    ) -> dict[str, object]:
        return self.create_account(
            identity,
            name=str(kwargs["name"]),
            initial_cash=Decimal(100),
            idempotency_key=str(kwargs["idempotency_key"]),
        )

    def submit_order(
        self,
        identity: ApiIdentity,
        *,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        order = {
            "intent_id": len(self.orders) + 1,
            "account_id": payload["account_id"],
            "asset_id": payload["asset_id"],
            "side": payload["side"],
            "status": "QUEUED",
        }
        self.orders[int(order["intent_id"])] = order
        return order

    def get_order(self, identity: ApiIdentity, order_id: int) -> dict[str, object]:
        if order_id not in self.orders:
            raise PaperApiError("PAPER_ORDER_NOT_FOUND", "not found", status_code=404)
        return self.orders[order_id]

    def list_orders(self, identity: ApiIdentity, **kwargs: object) -> dict[str, object]:
        return {"items": list(self.orders.values()), "next_cursor": None}

    def cancel_order(self, identity: ApiIdentity, order_id: int) -> dict[str, object]:
        order = self.get_order(identity, order_id)
        order["status"] = "CANCELED"
        return order

    def cancel_orders(
        self, identity: ApiIdentity, order_ids: list[int]
    ) -> dict[str, object]:
        canceled: list[int] = []
        missing: dict[str, str] = {}
        for order_id in order_ids:
            if order_id in self.orders:
                self.orders[order_id]["status"] = "CANCELED"
                canceled.append(order_id)
            else:
                missing[str(order_id)] = "not_found_or_not_owned"
        return {"canceled": canceled, "not_canceled": missing}

    def cancel_market_orders(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
        asset_id: str | None,
        condition_id: str | None,
    ) -> dict[str, object]:
        canceled = [
            order_id
            for order_id, order in self.orders.items()
            if (asset_id is None or order.get("asset_id") == asset_id)
            and (condition_id is None or order.get("condition_id") == condition_id)
        ]
        for order_id in canceled:
            self.orders[order_id]["status"] = "CANCELED"
        return {"account_id": account_id, "matched": len(canceled), "canceled": canceled}

    def cancel_all_orders(
        self,
        identity: ApiIdentity,
        *,
        account_id: UUID,
    ) -> dict[str, object]:
        canceled = list(self.orders)
        for order in self.orders.values():
            order["status"] = "CANCELED"
        return {"account_id": account_id, "matched": len(canceled), "canceled": canceled}

    def replace_order(
        self, identity: ApiIdentity, order_id: int, **kwargs: object
    ) -> dict[str, object]:
        order = self.get_order(identity, order_id)
        order.update(kwargs)
        return order

    def list_positions(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"items": [], "next_cursor": None}

    def list_fills(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"items": [], "next_cursor": None}

    def list_ledger(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"items": [], "next_cursor": None}

    def list_journal(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "journal_id": "journal-1",
                    "line_index": 0,
                    "account_code": "CASH",
                    "debit": "1.00",
                    "credit": "0",
                }
            ],
            "next_cursor": None,
        }

    def list_tca(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "order_id": "order-1",
                    "status": "IMMEDIATE_COMPLETE",
                    "fidelity_level": "TAKER_L2_UNCALIBRATED",
                }
            ],
            "next_cursor": None,
        }

    def get_performance(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {
            "account": self.accounts[self.account_id],
            "summary": {
                "balance": "1000",
                "equity": "1001",
                "available_cash": "999",
                "reserved_cash": "1",
                "effective_status": "ACTIVE",
            },
            "current": {"nav_complete": True},
            "history": [],
            "next_cursor": None,
            "data_quality": {"nav_complete": True},
        }

    def get_order_audit(
        self, identity: ApiIdentity, order_id: int
    ) -> dict[str, object]:
        order = self.get_order(identity, order_id)
        return {
            "order": order,
            "timeline": [
                {
                    "kind": "ORDER_EVENT",
                    "state": "CREATED",
                    "event_type": "ORDER_CREATED",
                }
            ],
            "fills": [],
            "ledger": [],
            "quality": {
                "execution_fidelity": "TAKER_L2_UNCALIBRATED",
                "data_quality": "COVERAGE_A",
            },
        }

    def export_account(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "intent_id": 1,
                "status": "COMPLETED",
                "result": {"fidelity": "TAKER_L2_UNCALIBRATED"},
            }
        ]

    def charge_export_quota(
        self, *args: object, byte_count: int, **kwargs: object
    ) -> object:
        self.export_bytes += byte_count
        return SimpleNamespace(allowed=True)

    def create_replay_session(
        self,
        identity: ApiIdentity,
        *,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        replay_id = uuid4()
        session = {
            "replay_session_id": replay_id,
            "account_id": payload["account_id"],
            "name": payload["name"],
            "status": "CREATED",
            "event_count": 2,
            "cursor_event_index": 0,
            "data_hash": "d" * 64,
        }
        self.replays[replay_id] = session
        return session

    def list_replay_sessions(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"items": list(self.replays.values()), "next_cursor": None}

    def get_replay_session(
        self, identity: ApiIdentity, replay_session_id: UUID
    ) -> dict[str, object]:
        if replay_session_id not in self.replays:
            raise PaperApiError("PAPER_REPLAY_NOT_FOUND", "not found", status_code=404)
        return self.replays[replay_session_id]

    def pause_replay_session(
        self, identity: ApiIdentity, replay_session_id: UUID
    ) -> dict[str, object]:
        session = self.get_replay_session(identity, replay_session_id)
        session["status"] = "PAUSED"
        return session

    def resume_replay_session(
        self,
        identity: ApiIdentity,
        replay_session_id: UUID,
        *,
        max_events: int,
    ) -> dict[str, object]:
        session = self.get_replay_session(identity, replay_session_id)
        session["cursor_event_index"] = min(
            int(session["event_count"]),
            int(session["cursor_event_index"]) + max_events,
        )
        session["status"] = (
            "COMPLETED"
            if session["cursor_event_index"] == session["event_count"]
            else "RUNNING"
        )
        return session

    def fork_replay_session(
        self,
        identity: ApiIdentity,
        replay_session_id: UUID,
        *,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        parent = self.get_replay_session(identity, replay_session_id)
        child_id = uuid4()
        child = {
            **parent,
            "replay_session_id": child_id,
            "parent_replay_session_id": replay_session_id,
            "name": payload["name"],
            "status": "CREATED",
            "cursor_event_index": 0,
        }
        self.replays[child_id] = child
        return child

    def list_replay_events(
        self,
        identity: ApiIdentity,
        replay_session_id: UUID,
        **kwargs: object,
    ) -> dict[str, object]:
        self.get_replay_session(identity, replay_session_id)
        return {
            "items": [
                {
                    "event_index": 0,
                    "event_type": "ORDER_EVENT",
                    "event_ts": "2026-08-01T00:00:00Z",
                }
            ],
            "next_cursor": None,
        }

    def get_replay_report(
        self, identity: ApiIdentity, replay_session_id: UUID
    ) -> dict[str, object]:
        self.get_replay_session(identity, replay_session_id)
        return {
            "performance": {"total_return": "0.01"},
            "risk": {"max_drawdown": "0"},
            "benchmark": {"excess_return": "0.01"},
            "report_hash": "a" * 64,
        }

    def create_scenario_run(
        self, identity: ApiIdentity, *, payload: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        run_id = uuid4()
        row = {
            "scenario_run_id": run_id,
            "account_id": payload["account_id"],
            "name": payload["name"],
            "scenario_type": "SCENARIO",
            "result": {"classification": "SCENARIO_NOT_EXECUTION", "writes_to_paper_ledger": False},
        }
        self.scenarios[run_id] = row
        return row

    def list_scenario_runs(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return list(self.scenarios.values())

    def get_scenario_run(
        self, identity: ApiIdentity, scenario_run_id: UUID
    ) -> dict[str, object]:
        return self.scenarios[scenario_run_id]

    def create_conditional_order(
        self, identity: ApiIdentity, *, payload: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        order_id = uuid4()
        row = {
            "conditional_order_id": order_id,
            "account_id": payload["account_id"],
            "order_type": payload["order_type"],
            "status": "ARMED",
            "child_order": payload["child_order"],
        }
        self.conditional_orders[order_id] = row
        return row

    def create_conditional_order_group(
        self, identity: ApiIdentity, *, payload: dict[str, object], idempotency_key: str
    ) -> dict[str, object]:
        group_id = uuid4()
        rows = []
        for leg in payload["orders"]:
            order_id = uuid4()
            row = {
                "conditional_order_id": order_id,
                "account_id": payload["account_id"],
                "order_type": leg["order_type"],
                "group_id": group_id,
                "group_policy": payload["group_policy"],
                "status": "ARMED",
                "child_order": leg["child_order"],
            }
            self.conditional_orders[order_id] = row
            rows.append(row)
        return {
            "group_id": group_id,
            "group_policy": payload["group_policy"],
            "orders": rows,
        }

    def list_conditional_orders(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return list(self.conditional_orders.values())

    def get_conditional_order(
        self, identity: ApiIdentity, conditional_order_id: UUID
    ) -> dict[str, object]:
        return self.conditional_orders[conditional_order_id]

    def cancel_conditional_order(
        self, identity: ApiIdentity, conditional_order_id: UUID, *, reason: str
    ) -> dict[str, object]:
        self.conditional_orders[conditional_order_id]["status"] = "CANCELLED"
        return self.conditional_orders[conditional_order_id]

    def admin_dashboard(self, identity: ApiIdentity) -> dict[str, object]:
        return {"tenant": {"status": self.tenant_status}, "active_freezes": []}

    def list_admin_jobs(self, identity: ApiIdentity, **kwargs: object) -> list[dict[str, object]]:
        return self.jobs

    def set_tenant_freeze(
        self, identity: ApiIdentity, *, frozen: bool, reason: str
    ) -> dict[str, object]:
        assert reason
        self.tenant_status = "FROZEN" if frozen else "ACTIVE"
        return {"status": self.tenant_status}

    def set_account_freeze(
        self,
        identity: ApiIdentity,
        account_id: UUID,
        *,
        frozen: bool,
        reason: str,
    ) -> dict[str, object]:
        account = self.accounts[account_id]
        account["status"] = "FROZEN" if frozen else "ACTIVE"
        return account

    def kill_account(
        self, identity: ApiIdentity, account_id: UUID, *, reason: str
    ) -> dict[str, object]:
        account = self.set_account_freeze(
            identity, account_id, frozen=True, reason=reason
        )
        return {**account, "cancel_requested": 1}

    def reconcile_account(
        self, identity: ApiIdentity, account_id: UUID, **kwargs: object
    ) -> dict[str, object]:
        job = {"job_id": uuid4(), "status": "COMPLETED", "result": {"status": "PASS"}}
        self.jobs.append(job)
        return job

    def list_dlq(self, identity: ApiIdentity, **kwargs: object) -> list[dict[str, object]]:
        return list(self.dlq.values())

    def replay_dlq_event(
        self, identity: ApiIdentity, dlq_event_id: UUID, **kwargs: object
    ) -> dict[str, object]:
        self.dlq[dlq_event_id]["status"] = "RESOLVED"
        return {"job_id": uuid4(), "status": "COMPLETED"}

    def ignore_dlq_event(
        self, identity: ApiIdentity, dlq_event_id: UUID, *, reason: str
    ) -> dict[str, object]:
        self.dlq[dlq_event_id]["status"] = "IGNORED"
        return self.dlq[dlq_event_id]

    def list_notices(self, identity: ApiIdentity) -> list[dict[str, object]]:
        return list(self.notices.values())

    def create_notice(self, identity: ApiIdentity, **kwargs: object) -> dict[str, object]:
        notice_id = uuid4()
        row = {"notice_id": notice_id, "status": "ACTIVE", **kwargs}
        self.notices[notice_id] = row
        return row

    def set_notice_status(
        self, identity: ApiIdentity, notice_id: UUID, *, status: str
    ) -> dict[str, object]:
        self.notices[notice_id]["status"] = status
        return self.notices[notice_id]

    def list_incidents(self, identity: ApiIdentity) -> list[dict[str, object]]:
        return list(self.incidents.values())

    def create_incident(self, identity: ApiIdentity, **kwargs: object) -> dict[str, object]:
        incident_id = uuid4()
        row = {"incident_id": incident_id, "status": "OPEN", "notes": [], **kwargs}
        self.incidents[incident_id] = row
        return row

    def add_incident_note(
        self, identity: ApiIdentity, incident_id: UUID, *, body: str
    ) -> dict[str, object]:
        row = {"note_id": uuid4(), "body": body}
        self.incidents[incident_id]["notes"].append(row)
        return row

    def set_incident_status(
        self, identity: ApiIdentity, incident_id: UUID, *, status: str
    ) -> dict[str, object]:
        self.incidents[incident_id]["status"] = status
        return self.incidents[incident_id]

    def list_retention_policies(self, identity: ApiIdentity) -> list[dict[str, object]]:
        return list(self.retention.values())

    def upsert_retention_policy(
        self, identity: ApiIdentity, **kwargs: object
    ) -> dict[str, object]:
        resource = str(kwargs["resource_type"]).upper()
        row = {"resource_type": resource, **kwargs}
        self.retention[resource] = row
        return row

    def run_retention(self, identity: ApiIdentity, **kwargs: object) -> dict[str, object]:
        return {"job_id": uuid4(), "status": "COMPLETED", "result": {"deleted_rows": 0}}

    def list_evidence_bundles(
        self, identity: ApiIdentity, **kwargs: object
    ) -> list[dict[str, object]]:
        return [metadata for metadata, _ in self.bundles.values()]

    def create_evidence_bundle(
        self, identity: ApiIdentity, **kwargs: object
    ) -> dict[str, object]:
        bundle_id = uuid4()
        content = b"paper-evidence-bundle"
        metadata = {
            "bundle_id": bundle_id,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "byte_count": len(content),
        }
        self.bundles[bundle_id] = (metadata, content)
        return metadata

    def get_evidence_bundle(
        self, identity: ApiIdentity, bundle_id: UUID
    ) -> tuple[dict[str, object], bytes]:
        return self.bundles[bundle_id]


@pytest.fixture()
def api() -> tuple[Flask, FakeBackend]:
    app = Flask(__name__)
    app.config["TESTING"] = True
    backend = FakeBackend()
    app.register_blueprint(create_paper_v1_blueprint(backend))
    return app, backend


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-paper-token"}


def test_api_key_token_round_trip_and_hash_does_not_store_secret() -> None:
    tenant_id = uuid4()
    key_id = uuid4()
    token = issue_api_token(tenant_id, key_id, secret="s" * 32)
    assert parse_api_token(token) == (tenant_id, key_id)
    digest = hash_api_token(token, b"p" * 32)
    assert len(digest) == 64
    assert "s" * 8 not in digest


def test_write_scopes_imply_response_read_but_not_other_mutations() -> None:
    principal = TenantPrincipal(tenant_id=uuid4(), actor_user_id=uuid4())
    trade = ApiIdentity(principal, uuid4(), "trade", frozenset({"paper:trade"}))
    trade.require_scope("paper:read")
    trade.require_scope("paper:trade")
    with pytest.raises(PaperApiError, match="paper:accounts:write"):
        trade.require_scope("paper:accounts:write")

    account_writer = ApiIdentity(
        principal,
        uuid4(),
        "account",
        frozenset({"paper:accounts:write"}),
    )
    account_writer.require_scope("paper:read")
    with pytest.raises(PaperApiError, match="paper:trade"):
        account_writer.require_scope("paper:trade")


def test_pagination_and_idempotency_contract_helpers_are_fail_closed() -> None:
    cursor = encode_cursor({"intent_id": 42})
    assert decode_cursor(cursor) == {"intent_id": 42}
    with pytest.raises(PaperApiError, match="cursor"):
        decode_cursor("not-json")
    assert normalize_limit(None) == 50
    with pytest.raises(PaperApiError, match="between"):
        normalize_limit(201)
    with pytest.raises(PaperApiError, match="Idempotency-Key"):
        require_idempotency_key("short")


def test_public_health_and_openapi_are_unauthenticated(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, _ = api
    client = app.test_client()
    health = client.get("/v1/paper/health")
    assert health.status_code == 200
    assert health.json["mode"] == "PAPER_ONLY"
    spec = client.get("/v1/paper/openapi.json")
    assert spec.status_code == 200
    assert spec.json["openapi"] == "3.1.0"
    assert (
        spec.json["paths"]["/v1/paper/orders"]["post"]["operationId"] == "createOrder"
    )


def test_auth_error_envelope_and_paper_only_headers(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, _ = api
    response = app.test_client().get("/v1/paper/accounts")
    assert response.status_code == 401
    assert response.json["error"]["code"] == "PAPER_AUTH_REQUIRED"
    assert UUID(response.json["error"]["request_id"])
    assert response.headers["X-Paper-Trading-Mode"] == "PAPER_ONLY"


def test_preflight_and_collection_pages_use_stable_contract(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    preflight = client.options("/v1/paper/orders")
    assert preflight.status_code == 204
    backend.create_account(
        backend.identity,
        name="collections",
        initial_cash=Decimal(1000),
        idempotency_key="collections",
    )
    for resource in ("positions", "fills", "ledger"):
        response = client.get(
            f"/v1/paper/accounts/{backend.account_id}/{resource}?limit=10",
            headers=auth(),
        )
        assert response.status_code == 200
        assert response.json["data"] == {"items": [], "next_cursor": None}


def test_account_manager_reads_audit_and_export_contracts(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    backend.create_account(
        backend.identity,
        name="account manager",
        initial_cash=Decimal(1000),
        idempotency_key="account-manager",
    )
    backend.orders[1] = {
        "intent_id": 1,
        "account_id": str(backend.account_id),
        "asset_id": "asset-1",
        "status": "COMPLETED",
    }

    performance = client.get(
        f"/v1/paper/accounts/{backend.account_id}/performance",
        headers=auth(),
    )
    assert performance.status_code == 200
    assert performance.json["data"]["summary"]["effective_status"] == "ACTIVE"

    journal = client.get(
        f"/v1/paper/accounts/{backend.account_id}/journal", headers=auth()
    )
    tca = client.get(
        f"/v1/paper/accounts/{backend.account_id}/tca", headers=auth()
    )
    assert journal.json["data"]["items"][0]["account_code"] == "CASH"
    assert tca.json["data"]["items"][0]["status"] == "IMMEDIATE_COMPLETE"

    audit = client.get("/v1/paper/audit/1", headers=auth())
    assert audit.status_code == 200
    assert audit.json["data"]["timeline"][0]["state"] == "CREATED"
    assert audit.json["data"]["quality"]["data_quality"] == "COVERAGE_A"

    csv_export = client.get(
        f"/v1/paper/accounts/{backend.account_id}/export"
        "?resource=orders&format=csv",
        headers=auth(),
    )
    assert csv_export.status_code == 200
    assert csv_export.headers["X-Export-Row-Count"] == "1"
    assert len(csv_export.headers["X-Content-SHA256"]) == 64
    assert b"intent_id" in csv_export.data
    assert backend.export_bytes == len(csv_export.data)

    jsonl_export = client.get(
        f"/v1/paper/accounts/{backend.account_id}/export"
        "?resource=orders&format=jsonl",
        headers=auth(),
    )
    assert json.loads(jsonl_export.data)["intent_id"] == 1

    parquet_export = client.get(
        f"/v1/paper/accounts/{backend.account_id}/export"
        "?resource=orders&format=parquet",
        headers=auth(),
    )
    assert parquet_export.status_code == 200
    assert parquet_export.mimetype == "application/vnd.apache.parquet"
    assert parquet_export.data[:4] == parquet_export.data[-4:] == b"PAR1"


def test_openapi_exposes_account_manager_read_contracts() -> None:
    paths = build_openapi_spec()["paths"]
    assert "/v1/paper/accounts/{account_id}/performance" in paths
    assert "/v1/paper/accounts/{account_id}/journal" in paths
    assert "/v1/paper/accounts/{account_id}/tca" in paths
    assert "/v1/paper/accounts/{account_id}/export" in paths
    assert paths["/v1/paper/audit/{order_id}"]["get"]["operationId"] == "getOrderAudit"


def test_replay_session_lifecycle_and_report_contract(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    payload = {
        "account_id": str(backend.account_id),
        "name": "replay-1",
        "start_ts": "2026-08-01T00:00:00Z",
        "end_ts": "2026-08-01T01:00:00Z",
        "speed": "10",
        "seed": 7,
        "strategy_version": "strategy-v1",
        "execution_model": "recorded-paper-lifecycle-v1",
        "benchmark": "CASH",
    }
    created = client.post(
        "/v1/paper/replays",
        headers={**auth(), "Idempotency-Key": "replay-create-001"},
        json=payload,
    )
    assert created.status_code == 201
    replay_id = created.json["data"]["replay_session_id"]

    listed = client.get("/v1/paper/replays", headers=auth())
    assert listed.status_code == 200
    assert listed.json["data"]["items"][0]["data_hash"] == "d" * 64

    paused = client.post(
        f"/v1/paper/replays/{replay_id}/pause",
        headers={**auth(), "Idempotency-Key": "replay-pause-001"},
    )
    assert paused.json["data"]["status"] == "PAUSED"

    resumed = client.post(
        f"/v1/paper/replays/{replay_id}/resume",
        headers={**auth(), "Idempotency-Key": "replay-resume-001"},
        json={"max_events": 1},
    )
    assert resumed.json["data"]["status"] == "RUNNING"

    forked = client.post(
        f"/v1/paper/replays/{replay_id}/fork",
        headers={**auth(), "Idempotency-Key": "replay-fork-001"},
        json={"name": "fork-1", "benchmark": "CONSERVATIVE_NAV"},
    )
    assert forked.status_code == 201
    assert forked.json["data"]["parent_replay_session_id"] == replay_id

    events = client.get(f"/v1/paper/replays/{replay_id}/events", headers=auth())
    report = client.get(f"/v1/paper/replays/{replay_id}/report", headers=auth())
    assert events.json["data"]["items"][0]["event_type"] == "ORDER_EVENT"
    assert report.json["data"]["performance"]["total_return"] == "0.01"


def test_openapi_exposes_replay_contracts() -> None:
    paths = build_openapi_spec()["paths"]
    expected = {
        "/v1/paper/replays",
        "/v1/paper/replays/{replay_session_id}",
        "/v1/paper/replays/{replay_session_id}/pause",
        "/v1/paper/replays/{replay_session_id}/resume",
        "/v1/paper/replays/{replay_session_id}/fork",
        "/v1/paper/replays/{replay_session_id}/events",
        "/v1/paper/replays/{replay_session_id}/report",
    }
    assert expected <= set(paths)


def test_openapi_exposes_complete_retail_product_contract() -> None:
    spec = build_openapi_spec()
    paths = spec["paths"]
    expected = {
        "/v1/paper/retail/session/guest",
        "/v1/paper/retail/session/challenge",
        "/v1/paper/retail/session/wallet",
        "/v1/paper/retail/session",
        "/v1/paper/retail/session/logout",
        "/v1/paper/retail/wallets",
        "/v1/paper/retail/wallets/fork",
        "/v1/paper/retail/wallets/reset",
        "/v1/paper/retail/wallets/{virtual_wallet_id}/default",
        "/v1/paper/retail/markets",
        "/v1/paper/retail/markets/{market_slug}",
        "/v1/paper/retail/watchlist",
        "/v1/paper/retail/watchlist/{market_slug}",
        "/v1/paper/retail/preferences",
        "/v1/paper/retail/predictions",
        "/v1/paper/retail/predictions/report",
        "/v1/paper/retail/risk-profile",
        "/v1/paper/retail/notifications",
        "/v1/paper/retail/notifications/{notification_id}/read",
        "/v1/paper/retail/account-truth",
        "/v1/paper/retail/pnl-attribution",
        "/v1/paper/retail/event-risk",
        "/v1/paper/retail/event-relations",
        "/v1/paper/retail/lifecycle",
        "/v1/paper/retail/maker-workbench",
        "/v1/paper/retail/portfolio",
        "/v1/paper/retail/portfolio/refresh-metrics",
        "/v1/paper/retail/portfolios/{virtual_wallet_id}",
        "/v1/paper/retail/portfolios/{virtual_wallet_id}/journal",
        "/v1/paper/retail/following",
        "/v1/paper/retail/following/{virtual_wallet_id}",
        "/v1/paper/retail/competitions",
        "/v1/paper/retail/competitions/{competition_id}/join",
        "/v1/paper/retail/competitions/{competition_id}/standings",
        "/v1/paper/retail/data-requests",
        "/v1/paper/retail/data-requests/{request_id}/download",
        "/v1/paper/retail/official-history",
        "/v1/paper/retail/leaderboard",
    }
    assert expected <= set(paths)
    assert {
        "RetailSessionCookie",
        "PaperApiKey",
    } <= set(spec["components"]["securitySchemes"])
    reset = paths["/v1/paper/retail/wallets/reset"]["post"]
    assert {item["name"] for item in reset["parameters"]} >= {
        "Idempotency-Key",
        "X-Paper-CSRF",
    }


def test_admin_control_plane_and_evidence_export_contract(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    backend.create_account(
        backend.identity,
        name="admin account",
        initial_cash=Decimal(1000),
        idempotency_key="admin-account",
    )
    headers = {**auth(), "Idempotency-Key": "admin-freeze-tenant-001"}
    frozen = client.post(
        "/v1/paper/admin/tenant/freeze", headers=headers, json={"reason": "maintenance"}
    )
    replayed = client.post(
        "/v1/paper/admin/tenant/freeze", headers=headers, json={"reason": "maintenance"}
    )
    assert frozen.status_code == replayed.status_code == 200
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert frozen.json["data"]["status"] == "FROZEN"

    account_frozen = client.post(
        f"/v1/paper/admin/accounts/{backend.account_id}/freeze",
        headers={**auth(), "Idempotency-Key": "admin-freeze-account-001"},
        json={"reason": "operator review"},
    )
    assert account_frozen.json["data"]["status"] == "FROZEN"
    reconciled = client.post(
        f"/v1/paper/admin/accounts/{backend.account_id}/reconcile",
        headers={**auth(), "Idempotency-Key": "admin-reconcile-001"},
        json={"mode": "DRY_RUN"},
    )
    assert reconciled.json["data"]["result"]["status"] == "PASS"

    notice = client.post(
        "/v1/paper/admin/notices",
        headers={**auth(), "Idempotency-Key": "admin-notice-001"},
        json={
            "title": "Maintenance",
            "message": "Paper execution maintenance",
            "starts_at": "2026-08-16T00:00:00Z",
            "ends_at": "2026-08-16T01:00:00Z",
        },
    )
    assert notice.status_code == 201

    incident = client.post(
        "/v1/paper/admin/incidents",
        headers={**auth(), "Idempotency-Key": "admin-incident-001"},
        json={
            "title": "Delayed worker",
            "summary": "Worker delay detected",
            "severity": "SEV3",
            "started_at": "2026-08-16T00:00:00Z",
        },
    )
    incident_id = incident.json["data"]["incident_id"]
    note = client.post(
        f"/v1/paper/admin/incidents/{incident_id}/notes",
        headers={**auth(), "Idempotency-Key": "admin-note-001"},
        json={"body": "Reconciliation passed"},
    )
    assert note.json["data"]["body"] == "Reconciliation passed"

    retention = client.post(
        "/v1/paper/admin/retention/API_REQUEST_LOG",
        headers={**auth(), "Idempotency-Key": "admin-retention-001"},
        json={"retention_days": 14, "legal_hold": True},
    )
    assert retention.json["data"]["legal_hold"] is True
    invalid_boolean = client.post(
        "/v1/paper/admin/retention/API_REQUEST_LOG",
        headers={**auth(), "Idempotency-Key": "admin-retention-002"},
        json={"retention_days": 14, "legal_hold": "false"},
    )
    assert invalid_boolean.status_code == 400

    bundle = client.post(
        "/v1/paper/admin/evidence-bundles",
        headers={**auth(), "Idempotency-Key": "admin-bundle-001"},
        json={"account_id": str(backend.account_id), "incident_id": incident_id},
    )
    assert bundle.status_code == 201
    bundle_id = bundle.json["data"]["bundle_id"]
    downloaded = client.get(
        f"/v1/paper/admin/evidence-bundles/{bundle_id}/download", headers=auth()
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["X-Content-SHA256"] == hashlib.sha256(
        downloaded.data
    ).hexdigest()
    assert backend.export_bytes >= len(downloaded.data)


def test_admin_dlq_replay_and_openapi_contract(api: tuple[Flask, FakeBackend]) -> None:
    app, backend = api
    dlq_event_id = uuid4()
    backend.dlq[dlq_event_id] = {
        "dlq_event_id": dlq_event_id,
        "event_type": "ACCOUNT_RECONCILE",
        "status": "PENDING",
    }
    response = app.test_client().post(
        f"/v1/paper/admin/dlq/{dlq_event_id}/replay",
        headers={**auth(), "Idempotency-Key": "admin-dlq-replay-001"},
        json={},
    )
    assert response.status_code == 200
    assert backend.dlq[dlq_event_id]["status"] == "RESOLVED"
    paths = build_openapi_spec()["paths"]
    assert "/v1/paper/admin/dashboard" in paths
    assert "/v1/paper/admin/accounts/{account_id}/kill" in paths
    assert "/v1/paper/admin/evidence-bundles/{bundle_id}/download" in paths


def test_mutations_require_and_replay_idempotency(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    missing = client.post(
        "/v1/paper/accounts",
        headers=auth(),
        json={"name": "alpha", "initial_cash": "1000"},
    )
    assert missing.status_code == 400
    assert missing.json["error"]["code"] == "PAPER_IDEMPOTENCY_REQUIRED"

    headers = {**auth(), "Idempotency-Key": "create-alpha-001"}
    first = client.post(
        "/v1/paper/accounts",
        headers=headers,
        json={"name": "alpha", "initial_cash": "1000"},
    )
    second = client.post(
        "/v1/paper/accounts",
        headers=headers,
        json={"name": "alpha", "initial_cash": "1000"},
    )
    assert first.status_code == second.status_code == 201
    assert first.json == second.json
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.headers["RateLimit-Limit"] == "1000"
    assert backend.request_log

    conflict = client.post(
        "/v1/paper/accounts",
        headers=headers,
        json={"name": "different", "initial_cash": "1000"},
    )
    assert conflict.status_code == 409
    assert conflict.json["error"]["code"] == "PAPER_IDEMPOTENCY_CONFLICT"


def test_order_validation_and_lifecycle_contract(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    backend.create_account(
        backend.identity,
        name="orders",
        initial_cash=Decimal(1000),
        idempotency_key="setup",
    )
    order = {
        "account_id": str(backend.account_id),
        "strategy_id": str(backend.strategy_id),
        "asset_id": "123456",
        "side": "BUY",
        "time_in_force": "FOK",
        "limit_price": "0.55",
        "size": "2",
    }
    created = client.post(
        "/v1/paper/orders",
        headers={**auth(), "Idempotency-Key": "order-create-001"},
        json=order,
    )
    assert created.status_code == 202
    assert created.json["data"]["status"] == "QUEUED"

    canceled = client.delete(
        "/v1/paper/orders/1",
        headers={**auth(), "Idempotency-Key": "order-cancel-001"},
    )
    assert canceled.status_code == 200
    assert canceled.json["data"]["status"] == "CANCELED"

    invalid_gtd = client.post(
        "/v1/paper/orders",
        headers={**auth(), "Idempotency-Key": "order-create-002"},
        json={**order, "time_in_force": "GTD"},
    )
    assert invalid_gtd.status_code == 400
    assert invalid_gtd.json["error"]["code"] == "PAPER_VALIDATION_ERROR"


def test_bulk_market_and_account_cancel_routes(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    backend.create_account(
        backend.identity,
        name="bulk-cancel",
        initial_cash=Decimal(1000),
        idempotency_key="setup",
    )
    order = {
        "account_id": str(backend.account_id),
        "strategy_id": str(backend.strategy_id),
        "asset_id": "asset-bulk",
        "side": "BUY",
        "time_in_force": "GTC",
        "limit_price": "0.40",
        "size": "2",
    }
    for index in range(2):
        response = client.post(
            "/v1/paper/orders",
            headers={**auth(), "Idempotency-Key": f"bulk-create-{index}"},
            json={**order, "client_order_id": f"bulk-{index}"},
        )
        assert response.status_code == 202

    bulk = client.post(
        "/v1/paper/orders/cancel",
        headers={**auth(), "Idempotency-Key": "bulk-cancel"},
        json={"order_ids": [1, 2, 999]},
    )
    assert bulk.status_code == 200
    assert bulk.json["data"]["canceled"] == [1, 2]
    assert bulk.json["data"]["not_canceled"] == {
        "999": "not_found_or_not_owned"
    }

    market = client.post(
        "/v1/paper/orders/cancel-market",
        headers={**auth(), "Idempotency-Key": "market-cancel"},
        json={"account_id": str(backend.account_id), "asset_id": "asset-bulk"},
    )
    assert market.status_code == 200
    assert market.json["data"]["matched"] == 2

    cancel_all = client.post(
        "/v1/paper/orders/cancel-all",
        headers={**auth(), "Idempotency-Key": "account-cancel-all"},
        json={"account_id": str(backend.account_id)},
    )
    assert cancel_all.status_code == 200
    assert cancel_all.json["data"]["matched"] == 2


def test_scenario_and_conditional_order_contract(
    api: tuple[Flask, FakeBackend],
) -> None:
    app, backend = api
    client = app.test_client()
    backend.create_account(
        backend.identity,
        name="research",
        initial_cash=Decimal(1000),
        idempotency_key="setup",
    )
    scenario = client.post(
        "/v1/paper/scenarios",
        headers={**auth(), "Idempotency-Key": "scenario-create-001"},
        json={
            "account_id": str(backend.account_id),
            "name": "Resolution up",
            "inputs": {"payout_by_asset": {"123456": "1"}},
        },
    )
    assert scenario.status_code == 201
    assert scenario.json["data"]["scenario_type"] == "SCENARIO"
    assert scenario.json["data"]["result"]["writes_to_paper_ledger"] is False

    conditional = client.post(
        "/v1/paper/conditional-orders",
        headers={**auth(), "Idempotency-Key": "conditional-create-001"},
        json={
            "account_id": str(backend.account_id),
            "strategy_id": str(backend.strategy_id),
            "order_type": "STOP_LIMIT",
            "trigger": {"kind": "PRICE", "operator": "LTE", "value": "0.4"},
            "child_order": {
                "asset_id": "123456",
                "side": "SELL",
                "time_in_force": "FAK",
                "limit_price": "0.39",
                "size": "2",
            },
        },
    )
    assert conditional.status_code == 201
    assert conditional.json["data"]["status"] == "ARMED"
    order_id = conditional.json["data"]["conditional_order_id"]
    cancelled = client.delete(
        f"/v1/paper/conditional-orders/{order_id}",
        headers={**auth(), "Idempotency-Key": "conditional-cancel-001"},
        json={"reason": "strategy withdrawn"},
    )
    assert cancelled.json["data"]["status"] == "CANCELLED"

    group = client.post(
        "/v1/paper/conditional-order-groups",
        headers={**auth(), "Idempotency-Key": "conditional-group-001"},
        json={
            "account_id": str(backend.account_id),
            "strategy_id": str(backend.strategy_id),
            "group_policy": "OCO",
            "orders": [
                {
                    "order_type": "OCO",
                    "trigger": {"kind": "PRICE", "operator": "LTE", "value": "0.4"},
                    "child_order": {
                        "asset_id": "123456",
                        "side": "SELL",
                        "time_in_force": "FAK",
                        "limit_price": "0.39",
                        "size": "2",
                    },
                },
                {
                    "order_type": "OCO",
                    "trigger": {"kind": "PRICE", "operator": "GTE", "value": "0.6"},
                    "child_order": {
                        "asset_id": "123456",
                        "side": "SELL",
                        "time_in_force": "FAK",
                        "limit_price": "0.59",
                        "size": "2",
                    },
                },
            ],
        },
    )
    assert group.status_code == 201
    assert group.json["data"]["group_policy"] == "OCO"
    assert len(group.json["data"]["orders"]) == 2

    paths = build_openapi_spec()["paths"]
    assert "/v1/paper/scenarios/{scenario_run_id}" in paths
    assert "/v1/paper/conditional-orders/{conditional_order_id}" in paths
    assert "/v1/paper/conditional-order-groups" in paths


def test_checked_in_openapi_and_sdks_are_paper_only() -> None:
    spec = build_openapi_spec()
    operation_ids = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert {
        "createAccount",
        "createOrder",
        "cancelOrder",
        "replaceOrder",
        "createScenarioRun",
        "createConditionalOrder",
    } <= operation_ids
    root = Path(__file__).resolve().parents[2]
    artifact = json.loads((root / "docs/api/paper-v1-openapi.json").read_text())
    assert artifact == spec
    sources = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "sdk/python/polymarket_paper/client.py",
            "sdk/typescript/src/index.ts",
        )
    ).lower()
    assert "/v1/paper" in sources
    assert "private_key" not in sources
    assert "submit_live" not in sources
