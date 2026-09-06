from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from quant.paper.public_api import ApiIdentity, PaperApiError, PostgresPaperApiBackend
from quant.paper.live_shadow_service import LivePaperShadowService
from quant.paper.taker_execution import OrderIntent
from quant.paper.tenant_platform import TenantPrincipal
from quant.simulator.admission import (
    GeoblockSnapshot,
    MemoryAdmissionStore,
    UnifiedAdmissionService,
)
from quant.simulator.multileg.execution_plan import (
    AtomicityPolicy,
    ExecutionLeg,
    MultiLegExecutionPlan,
)
from quant.simulator.multileg.plan_store import DurableMultiLegPlan
from quant.simulator.multileg.venue_adapter import (
    DurableMultiLegCoordinator,
    VenueCapabilities,
)


NOW = datetime.now(timezone.utc)


def _geo(*, blocked: bool, country: str) -> GeoblockSnapshot:
    return GeoblockSnapshot(
        blocked=blocked,
        country=country,
        region="",
        detected_ip="203.0.113.20",
        observed_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        raw_payload_hash=hashlib.sha256(country.encode()).hexdigest(),
    )


class _Provider:
    def __init__(self, snapshot: GeoblockSnapshot) -> None:
        self.value = snapshot

    def snapshot(self, *, now=None) -> GeoblockSnapshot:
        return self.value


class _Cursor:
    def __init__(self, position: Decimal) -> None:
        self.position = position
        self.query = ""

    def execute(self, query, _params=None) -> None:
        self.query = str(query)

    def fetchone(self):
        if "paper_account_registry" in self.query:
            return {
                "ledger_strategy_id": "ledger-1",
                "status": "ACTIVE",
                "strategy_id": uuid4(),
                "strategy_status": "ACTIVE",
            }
        if "count(*)" in self.query:
            return {"count": 0}
        if "paper_positions" in self.query:
            return {"quantity": self.position}
        raise AssertionError(self.query)


class _Connection:
    def commit(self) -> None:
        return None


class _TenantStore:
    def __init__(self, position: Decimal) -> None:
        self.position = position
        self.bound_intent_id: int | None = None

    @contextmanager
    def _transaction(self, *_args, **_kwargs):
        yield _Connection(), _Cursor(self.position), None

    def check_gauge_quota(self, *_args, **_kwargs) -> None:
        return None

    def require_quota(self, *_args, **_kwargs) -> None:
        return None

    def bind_intent_ownership(self, *_args, **kwargs) -> None:
        self.bound_intent_id = int(kwargs["intent_id"])


class _LiveStore:
    def __init__(self) -> None:
        self.calls = 0
        self.last_submission: dict[str, object] = {}

    def submit(self, **kwargs) -> int:
        self.calls += 1
        self.last_submission = dict(kwargs)
        return 123


def _api_backend(position: Decimal, snapshot: GeoblockSnapshot):
    backend = object.__new__(PostgresPaperApiBackend)
    backend.tenant_store = _TenantStore(position)
    backend.live_store = _LiveStore()
    backend.retail_service = SimpleNamespace(
        evaluate_order_risk=lambda *_args, **_kwargs: {
            "allowed": True,
            "reason_codes": [],
        },
        record_order_risk_decision=lambda *_args, **_kwargs: None,
    )
    backend._retail_schema_ready = True
    backend.unified_admission_service = UnifiedAdmissionService(
        store=MemoryAdmissionStore(), geoblock_provider=_Provider(snapshot)
    )
    backend.unified_admission_shadow = True
    backend.unified_admission_enforce = True
    backend.get_order = lambda _identity, intent_id: {"intent_id": intent_id}
    return backend


def _identity() -> ApiIdentity:
    tenant_id = uuid4()
    user_id = uuid4()
    return ApiIdentity(
        principal=TenantPrincipal(tenant_id=tenant_id, actor_user_id=user_id),
        api_key_id=uuid4(),
        key_prefix="ppk_test",
        scopes=frozenset({"paper:trade"}),
    )


def _order_payload(*, side: str, size: str) -> dict[str, object]:
    return {
        "account_id": str(uuid4()),
        "strategy_id": str(uuid4()),
        "asset_id": "asset-1",
        "side": side,
        "time_in_force": "FOK",
        "limit_price": "0.5",
        "size": size,
        "amount_unit": "SHARES",
    }


def test_public_api_close_only_allows_bounded_sell() -> None:
    backend = _api_backend(Decimal("3"), _geo(blocked=True, country="BR"))

    result = backend.submit_order(
        _identity(),
        payload=_order_payload(side="SELL", size="1"),
        idempotency_key="bounded-sell",
    )

    assert result == {"intent_id": 123}
    assert backend.live_store.calls == 1
    assert backend.live_store.last_submission["initial_status"] == "PENDING_OWNERSHIP"
    assert backend.tenant_store.bound_intent_id == 123


def test_public_api_close_only_rejects_reverse_open_before_enqueue() -> None:
    backend = _api_backend(Decimal("1"), _geo(blocked=True, country="BR"))

    with pytest.raises(PaperApiError) as caught:
        backend.submit_order(
            _identity(),
            payload=_order_payload(side="SELL", size="2"),
            idempotency_key="reverse-sell",
        )

    assert caught.value.code == "PAPER_ELIGIBILITY_REJECTED"
    assert backend.live_store.calls == 0


def test_public_api_cancel_remains_allowed_in_full_block_and_is_audited() -> None:
    store = MemoryAdmissionStore()
    backend = _api_backend(Decimal("0"), _geo(blocked=True, country="IR"))
    backend.unified_admission_service = UnifiedAdmissionService(
        store=store,
        geoblock_provider=_Provider(_geo(blocked=True, country="IR")),
    )

    backend._record_cancel_admissions(
        _identity(),
        (
            {
                "intent_id": 7,
                "account_id": "account-1",
                "product_strategy_id": "strategy-1",
                "asset_id": "asset-1",
                "condition_id": "condition-1",
                "market_id": "market-1",
            },
        ),
        action="test_cancel",
    )

    decision = next(iter(store.decisions.values()))
    assert decision.allowed is True
    assert decision.jurisdiction_mode.value == "NOT_APPLICABLE"


class _PositionCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _query, _params=None) -> None:
        return None

    def fetchone(self):
        return {"quantity": Decimal("1")}


class _PositionConnection:
    def cursor(self):
        return _PositionCursor()


@contextmanager
def _position_connection_factory(**_kwargs):
    yield _PositionConnection()


def test_public_api_close_only_replace_cannot_reverse_position() -> None:
    backend = _api_backend(Decimal("1"), _geo(blocked=True, country="BR"))
    backend.connection_factory = _position_connection_factory

    with pytest.raises(PaperApiError) as caught:
        backend._require_replace_admission(
            _identity(),
            order={
                "intent_id": 8,
                "account_id": "account-1",
                "product_strategy_id": "strategy-1",
                "strategy_id": "ledger-1",
                "asset_id": "asset-1",
                "condition_id": "condition-1",
                "market_id": "market-1",
                "side": "SELL",
                "amount_unit": "SHARES",
            },
            limit_price=Decimal("0.5"),
            size=Decimal("2"),
        )

    assert caught.value.code == "PAPER_ELIGIBILITY_REJECTED"


class _PlanStore:
    def __init__(self, plan: DurableMultiLegPlan) -> None:
        self.value = plan

    def plan(self, _plan_id: str) -> DurableMultiLegPlan:
        return self.value


class _AtomicAdapter:
    capabilities = VenueCapabilities(
        venue_name="paper",
        supports_independent_clob_legs=True,
        supports_atomic_combo=True,
        supports_rfq=True,
    )

    def __init__(self) -> None:
        self.calls = 0

    def execute_atomic(self, _plan):
        self.calls += 1
        return ()


def test_combo_coordinator_cannot_be_constructed_without_admission() -> None:
    with pytest.raises(ValueError, match="unified admission service is required"):
        DurableMultiLegCoordinator(
            store=_PlanStore(None),
            adapter=_AtomicAdapter(),
        )


def test_combo_is_denied_before_venue_adapter_on_full_block() -> None:
    plan = DurableMultiLegPlan(
        plan=MultiLegExecutionPlan(
            plan_id="plan-1",
            policy=AtomicityPolicy.VENUE_ATOMIC,
            legs=(
                ExecutionLeg("yes", "asset-yes", "BUY", Decimal(1), Decimal(1)),
                ExecutionLeg("no", "asset-no", "BUY", Decimal(1), Decimal(1)),
            ),
            hedge_timeout=timedelta(seconds=1),
            created_at=NOW,
        ),
        strategy_id="strategy-1",
        account_id="account-1",
        venue_kind="COMBO",
        state="PLANNED",
        residual_exposure=Decimal(0),
        hedge_covered_exposure=Decimal(0),
        hedge_deadline=None,
    )
    adapter = _AtomicAdapter()
    coordinator = DurableMultiLegCoordinator(
        store=_PlanStore(plan),
        adapter=adapter,
        admission_service=UnifiedAdmissionService(
            store=MemoryAdmissionStore(),
            geoblock_provider=_Provider(_geo(blocked=True, country="IR")),
        ),
    )

    with pytest.raises(ValueError, match="unified admission"):
        coordinator.execute("plan-1", event_prefix="test", event_ts=NOW)

    assert adapter.calls == 0


class _PortfolioStore:
    def portfolio_snapshot(self, _strategy_id: str, _asset_id: str):
        return SimpleNamespace(position_size=Decimal("1"))


def _worker(snapshot: GeoblockSnapshot) -> LivePaperShadowService:
    worker = object.__new__(LivePaperShadowService)
    worker.unified_admission_service = UnifiedAdmissionService(
        store=MemoryAdmissionStore(),
        geoblock_provider=_Provider(snapshot),
    )
    worker.portfolio_store = _PortfolioStore()
    worker.degradation_account_id = "account-1"
    worker.db_operation_timeout_seconds = 1.0
    return worker


def _intent(size: str) -> OrderIntent:
    return OrderIntent(
        strategy_id="strategy-1",
        market_id="market-1",
        condition_id="condition-1",
        asset_id="asset-1",
        side="SELL",
        order_type="FOK",
        limit_price=Decimal("0.5"),
        size=Decimal(size),
        post_only=False,
        decision_ts=NOW,
        client_order_id=f"client-{size}",
    )


def test_worker_close_only_uses_portfolio_to_prevent_reverse_open() -> None:
    worker = _worker(_geo(blocked=True, country="BR"))

    bounded = asyncio.run(worker._observe_unified_admission(1, _intent("1")))
    reversed_order = asyncio.run(worker._observe_unified_admission(2, _intent("2")))

    assert bounded.allowed is True
    assert reversed_order.allowed is False
    assert reversed_order.reason_codes == (
        "close_only_reverse_or_nonreducing_order_denied",
    )
