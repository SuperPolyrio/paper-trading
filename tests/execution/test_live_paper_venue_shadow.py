import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.paper.live_shadow_service import (
    LivePaperShadowService,
    LiveShadowStats,
    _load_worker_build_id,
    _validate_run_args,
    _venue_authority_action,
    parse_args,
)
from quant.paper.professional_execution import PaperRiskContext
from quant.paper.taker_execution import OrderIntent
from quant.simulator.oms import OmsAdmissionStatus
from quant.simulator.paper_worker_scheduler import DeterministicPaperIntentScheduler

NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


class _RejectingShadow:
    def __init__(self) -> None:
        self.requests = []

    def evaluate(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            agreement=False,
            gateway_queued=True,
            reason="signer:throttled",
        )


class _PulseShadow:
    def pulse(self, *, now_ts_ns):
        assert now_ts_ns > 0
        return SimpleNamespace(
            heartbeat_auto_cancels=2,
            reservation_release_events=1,
        )


class _RestartShadow:
    def reconcile_stale_reservations(self):
        return 3


def _intent() -> OrderIntent:
    return OrderIntent(
        strategy_id="strategy",
        market_id="market",
        condition_id="condition",
        asset_id="asset",
        side="BUY",
        order_type="FOK",
        limit_price=Decimal("0.5"),
        size=Decimal(1),
        post_only=False,
        decision_ts=NOW,
        client_order_id="client-one",
    )


def test_live_paper_shadow_records_disagreement_but_returns_no_enforcement_signal() -> (
    None
):
    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.venue_admission_shadow = _RejectingShadow()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        result = await service._observe_venue_admission(7, _intent(), NOW)

        assert result is not None
        assert result.agreement is False
        assert service.stats.venue_shadow_evaluations == 1
        assert service.stats.venue_shadow_disagreements == 1
        assert service.stats.venue_shadow_queued == 1
        assert service.stats.venue_shadow_failures == 0
        assert service.venue_admission_shadow.requests[0].paper_admitted is True

    asyncio.run(exercise())


def test_live_paper_shadow_failure_is_observed_and_not_raised() -> None:
    class _FailingShadow:
        def evaluate(self, _request):
            raise RuntimeError("shadow unavailable")

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.venue_admission_shadow = _FailingShadow()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        result = await service._observe_venue_admission(8, _intent(), NOW)

        assert result is None
        assert service.stats.venue_shadow_evaluations == 0
        assert service.stats.venue_shadow_failures == 1
        assert "shadow unavailable" in service.stats.last_venue_shadow_reason

    asyncio.run(exercise())


def test_live_service_pulse_tracks_dead_man_release_evidence() -> None:
    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.venue_admission_shadow = _PulseShadow()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        result = await service._advance_venue_shadow()

        assert result is None
        assert service.stats.venue_shadow_heartbeat_auto_cancels == 2
        assert service.stats.venue_shadow_reservation_releases == 1
        assert service.stats.venue_shadow_failures == 0

    asyncio.run(exercise())


def test_dead_man_release_is_separately_opt_in() -> None:
    default = parse_args(["run"])
    shadow_only = parse_args(
        [
            "run",
            "--venue-admission-shadow",
            "--venue-heartbeat-timeout-seconds",
            "5",
        ]
    )
    enforced = parse_args(
        [
            "run",
            "--venue-admission-shadow",
            "--venue-heartbeat-timeout-seconds",
            "5",
            "--venue-heartbeat-enforce-release",
        ]
    )

    assert default.venue_heartbeat_enforce_release is False
    assert shadow_only.venue_heartbeat_enforce_release is False
    assert enforced.venue_heartbeat_enforce_release is True


def test_lifecycle_scheduler_shadow_is_separately_opt_in() -> None:
    assert parse_args(["run"]).lifecycle_scheduler_shadow is False
    assert (
        parse_args(["run", "--lifecycle-scheduler-shadow"]).lifecycle_scheduler_shadow
        is True
    )


def test_persistent_db_connections_are_separately_opt_in() -> None:
    assert parse_args(["run"]).persistent_db_connections is False
    assert (
        parse_args(["run", "--persistent-db-connections"]).persistent_db_connections
        is True
    )


def test_operations_admission_is_shadow_or_enforced_and_validated() -> None:
    default = parse_args(["run"])
    shadow = parse_args(["run", "--operations-admission-shadow"])
    enforce = parse_args(
        [
            "run",
            "--operations-admission-enforce",
            "--operations-status-max-age-seconds",
            "30",
            "--operations-yellow-max-notional",
            "5",
        ]
    )

    assert default.operations_admission_shadow is False
    assert default.operations_admission_enforce is False
    assert shadow.operations_admission_shadow is True
    assert enforce.operations_admission_enforce is True
    _validate_run_args(enforce)

    invalid = parse_args(["run", "--operations-status-max-age-seconds", "0"])
    with pytest.raises(ValueError, match="operations status max age"):
        _validate_run_args(invalid)


def test_worker_build_identity_requires_a_sha256_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "build.json"
    build_id = "a" * 64
    manifest.write_text(json.dumps({"build_id": build_id}), encoding="utf-8")

    assert _load_worker_build_id(manifest) == build_id
    assert LiveShadowStats(worker_id="worker", build_id=build_id).as_dict()[
        "build_id"
    ] == build_id

    manifest.write_text(json.dumps({"build_id": "not-a-hash"}), encoding="utf-8")
    assert _load_worker_build_id(manifest) is None
    assert _load_worker_build_id(tmp_path / "missing.json") is None


def test_intent_claim_uses_independent_db_lane() -> None:
    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service._intent_claim_task = None
        service._claimed_intents = deque()
        service.history = {"asset": [object()]}
        service.pending = {}
        service.stats = LiveShadowStats(worker_id="worker")
        service.worker_id = "worker"
        service._last_intent_poll = 0.0
        service._connected_sources = lambda: {"primary"}
        claimed_row = object()
        service.store = SimpleNamespace(claim=lambda **_kwargs: [claimed_row])
        calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def db_call(func, /, *args, **kwargs):
            calls.append((func, args, kwargs))
            return [claimed_row]

        service._claim_db_call = db_call

        def forbidden_intent_db_call(*_args, **_kwargs):
            raise AssertionError("claim must not use the serial execution DB lane")

        service._intent_db_call = forbidden_intent_db_call
        service._schedule_intent_claim()
        assert service._intent_claim_task is not None
        await service._intent_claim_task

        assert len(calls) == 1
        assert calls[0][2] == {"worker_id": "worker", "limit": 50}

        service._harvest_intent_claim()
        assert service._intent_claim_task is None
        assert list(service._claimed_intents) == [claimed_row]

    asyncio.run(exercise())


def test_lifecycle_scheduler_authority_is_separately_opt_in() -> None:
    assert parse_args(["run"]).lifecycle_scheduler_enforce_order is False
    assert (
        parse_args(
            [
                "run",
                "--lifecycle-scheduler-shadow",
                "--lifecycle-scheduler-enforce-order",
            ]
        ).lifecycle_scheduler_enforce_order
        is True
    )


def test_lifecycle_scheduler_authority_requires_shadow_comparison() -> None:
    args = parse_args(["run", "--lifecycle-scheduler-enforce-order"])

    with pytest.raises(ValueError, match="requires lifecycle scheduler shadow"):
        _validate_run_args(args)


def test_durable_liquidity_overlay_requires_scheduler_authority_and_version() -> None:
    shadow_without_scheduler = parse_args(
        [
            "run",
            "--durable-liquidity-overlay-shadow",
            "--liquidity-overlay-version",
            "test-overlay",
        ]
    )
    with pytest.raises(ValueError, match="requires deterministic scheduler"):
        _validate_run_args(shadow_without_scheduler)

    enforce_without_version = parse_args(
        [
            "run",
            "--lifecycle-scheduler-shadow",
            "--lifecycle-scheduler-enforce-order",
            "--durable-liquidity-overlay-enforce",
        ]
    )
    with pytest.raises(ValueError, match="requires a stable version"):
        _validate_run_args(enforce_without_version)

    valid = parse_args(
        [
            "run",
            "--lifecycle-scheduler-shadow",
            "--lifecycle-scheduler-enforce-order",
            "--durable-liquidity-overlay-enforce",
            "--liquidity-overlay-version",
            "test-overlay",
        ]
    )
    _validate_run_args(valid)


def test_durable_own_order_oms_requires_account_and_scheduler_for_enforcement() -> None:
    without_account = parse_args(["run", "--own-order-oms-shadow"])
    with pytest.raises(ValueError, match="requires a shared paper account id"):
        _validate_run_args(without_account)

    enforce_without_scheduler = parse_args(
        [
            "run",
            "--own-order-oms-enforce",
            "--paper-account-id",
            "paper-account",
        ]
    )
    with pytest.raises(ValueError, match="requires deterministic scheduler"):
        _validate_run_args(enforce_without_scheduler)

    valid = parse_args(
        [
            "run",
            "--lifecycle-scheduler-shadow",
            "--lifecycle-scheduler-enforce-order",
            "--own-order-oms-enforce",
            "--paper-account-id",
            "paper-account",
        ]
    )
    _validate_run_args(valid)


def test_own_order_oms_shadow_records_rejection_without_raising() -> None:
    class _OmsGate:
        def evaluate(self, intent_id, intent):
            assert intent_id == 17
            assert intent.client_order_id == "client-one"
            return SimpleNamespace(
                accepted=False,
                admission=SimpleNamespace(
                    status=OmsAdmissionStatus.REJECTED_SELF_TRADE,
                    reason="incoming_would_cross_own_working_order",
                ),
            )

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.own_order_oms_gate = _OmsGate()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        decision = await service._observe_own_order_oms(17, _intent())

        assert decision is not None
        assert decision.accepted is False
        assert service.stats.own_order_oms_evaluations == 1
        assert service.stats.own_order_oms_rejections == 1
        assert service.stats.own_order_oms_failures == 0

    asyncio.run(exercise())


def test_fill_finality_shadow_is_separately_opt_in() -> None:
    assert parse_args(["run"]).fill_finality_shadow is False
    assert parse_args(["run", "--fill-finality-shadow"]).fill_finality_shadow is True


def test_fill_finality_shadow_records_fragments_without_changing_result() -> None:
    class _Finality:
        def record_result(self, result, *, paper_intent_id):
            assert result.audit_key == "audit:fill"
            assert paper_intent_id == 77
            return (
                SimpleNamespace(fragment=SimpleNamespace(trade_id="trade:0")),
                SimpleNamespace(fragment=SimpleNamespace(trade_id="trade:1")),
            )

    async def exercise() -> None:
        result = SimpleNamespace(
            audit_key="audit:fill",
            fills=(SimpleNamespace(), SimpleNamespace()),
        )
        service = object.__new__(LivePaperShadowService)
        service.fill_finality_shadow = _Finality()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        service.fill_finality_auto_reconcile = False
        await service._observe_fill_finality(77, result)

        assert service.stats.fill_finality_shadow_matches == 2
        assert service.stats.fill_finality_shadow_failures == 0
        assert service.stats.last_fill_finality_trade_ids == ["trade:0", "trade:1"]
        assert result.audit_key == "audit:fill"

    asyncio.run(exercise())


def test_fill_finality_shadow_failure_is_observed_and_not_raised() -> None:
    class _Finality:
        def record_result(self, _result, *, paper_intent_id):
            assert paper_intent_id == 78
            raise RuntimeError("finality database unavailable")

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.fill_finality_shadow = _Finality()
        service.fill_finality_auto_reconcile = False
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        await service._observe_fill_finality(
            78, SimpleNamespace(audit_key="audit:fill", fills=(SimpleNamespace(),))
        )

        assert service.stats.fill_finality_shadow_matches == 0
        assert service.stats.fill_finality_shadow_failures == 1
        assert "finality database unavailable" in str(
            service.stats.last_fill_finality_error
        )

    asyncio.run(exercise())


def test_venue_authority_defers_queue_and_rejects_terminal_denial() -> None:
    assert _venue_authority_action(None) == "DEFER"
    assert (
        _venue_authority_action(
            SimpleNamespace(
                gateway_terminally_denied=False,
                gateway_queued=True,
                gateway_accepted_now=False,
            )
        )
        == "DEFER"
    )
    assert (
        _venue_authority_action(
            SimpleNamespace(
                gateway_terminally_denied=True,
                gateway_queued=False,
                gateway_accepted_now=False,
            )
        )
        == "REJECT"
    )
    assert (
        _venue_authority_action(
            SimpleNamespace(
                gateway_terminally_denied=False,
                gateway_queued=False,
                gateway_accepted_now=True,
            )
        )
        == "ADMIT"
    )


def test_venue_authority_requires_scheduler() -> None:
    args = parse_args(["run", "--venue-admission-enforce"])

    with pytest.raises(ValueError, match="requires deterministic scheduler"):
        _validate_run_args(args)


def test_automatic_finality_requires_provisional_recording() -> None:
    args = parse_args(["run", "--fill-finality-auto-reconcile"])

    with pytest.raises(ValueError, match="requires fill finality shadow"):
        _validate_run_args(args)


def test_lifecycle_scheduler_authority_failure_is_fail_closed() -> None:
    class _FailingAuthority:
        def order_due(self, _items, *, arrival_ts):
            del arrival_ts
            raise RuntimeError("authority unavailable")

    async def exercise() -> None:
        intent = _intent()
        pending = SimpleNamespace(row=SimpleNamespace(intent_id=9, intent=intent))
        service = object.__new__(LivePaperShadowService)
        service.lifecycle_scheduler_enforce_order = True
        service.paper_intent_scheduler = _FailingAuthority()
        service.engine = SimpleNamespace(
            config=SimpleNamespace(latency=SimpleNamespace(arrival_ts=lambda _ts: NOW))
        )
        service.pending = {9: pending}
        service.stats = LiveShadowStats(worker_id="worker")

        await service._execute_due()

        assert list(service.pending) == [9]
        assert service.stats.lifecycle_scheduler_authority_failures == 1
        assert "authority unavailable" in str(
            service.stats.last_lifecycle_scheduler_error
        )

    asyncio.run(exercise())


def test_scheduler_artifact_failure_is_fail_closed_before_execution() -> None:
    class _FailingArtifactWorker:
        def replay_events(self, **_kwargs):
            raise RuntimeError("artifact database unavailable")

    async def exercise() -> None:
        intent = _intent()
        pending = SimpleNamespace(row=SimpleNamespace(intent_id=9, intent=intent))
        service = object.__new__(LivePaperShadowService)
        service.lifecycle_scheduler_enforce_order = True
        service.paper_intent_scheduler = DeterministicPaperIntentScheduler()
        service.simulator_artifact_worker = _FailingArtifactWorker()
        service.simulator_run_id = "paper-test-run"
        service.db_operation_timeout_seconds = 1.0
        service.engine = SimpleNamespace(
            config=SimpleNamespace(latency=SimpleNamespace(arrival_ts=lambda _ts: NOW))
        )
        service.pending = {9: pending}
        service.stats = LiveShadowStats(worker_id="worker")

        await service._execute_due()

        assert list(service.pending) == [9]
        assert service.stats.simulator_artifact_failures == 1
        assert service.stats.lifecycle_scheduler_authority_failures == 1
        assert "artifact database unavailable" in str(
            service.stats.last_simulator_artifact_error
        )

    asyncio.run(exercise())


def test_lifecycle_scheduler_failure_never_changes_paper_result() -> None:
    class _FailingScheduler:
        def observe(self, _audit_key):
            raise RuntimeError("scheduler shadow unavailable")

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.lifecycle_scheduler_shadow = _FailingScheduler()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        await service._observe_lifecycle_scheduler("audit:one")

        assert service.stats.lifecycle_scheduler_evaluations == 0
        assert service.stats.lifecycle_scheduler_failures == 1
        assert "scheduler shadow unavailable" in str(
            service.stats.last_lifecycle_scheduler_error
        )

    asyncio.run(exercise())


def test_live_service_accounts_for_restart_releases() -> None:
    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.venue_admission_shadow = _RestartShadow()
        service.stats = LiveShadowStats(worker_id="worker")
        service.db_operation_timeout_seconds = 1.0

        await service._reconcile_venue_shadow_restart()

        assert service.stats.venue_shadow_restart_releases == 3
        assert service.stats.venue_shadow_reservation_releases == 3
        assert service.stats.venue_shadow_failures == 0

    asyncio.run(exercise())


def test_risk_context_retries_server_statement_timeout() -> None:
    class _FlakyRiskStore:
        def __init__(self) -> None:
            self.calls = 0

        def risk_context(self, _intent):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("canceling statement due to statement timeout")
            return PaperRiskContext()

    async def exercise() -> None:
        store = _FlakyRiskStore()
        service = object.__new__(LivePaperShadowService)
        service.portfolio_store = store
        service.db_operation_timeout_seconds = 1.0

        result = await service._load_risk_context(_intent())

        assert result == PaperRiskContext()
        assert store.calls == 3

    asyncio.run(exercise())


def test_risk_context_does_not_retry_unknown_database_failure() -> None:
    class _BrokenRiskStore:
        calls = 0

        def risk_context(self, _intent):
            self.calls += 1
            raise RuntimeError("database unavailable")

    async def exercise() -> None:
        store = _BrokenRiskStore()
        service = object.__new__(LivePaperShadowService)
        service.portfolio_store = store
        service.db_operation_timeout_seconds = 1.0

        with pytest.raises(RuntimeError, match="database unavailable"):
            await service._load_risk_context(_intent())
        assert store.calls == 1

    asyncio.run(exercise())


def test_market_clarification_resets_books_orders_and_reservations() -> None:
    class _Store:
        def __init__(self) -> None:
            self.closed: list[int] = []

        def apply_market_clarification(self, **_kwargs):
            return {
                "condition_id": "condition-1",
                "asset_ids": ["asset-1", "asset-2"],
                "canceled_intent_ids": [11],
                "duplicate": False,
            }

        def close_maker_queue(self, intent_id, **_kwargs):
            self.closed.append(intent_id)

    class _Books:
        def __init__(self) -> None:
            self.reset: list[str] = []

        def reset_tokens(self, asset_ids, *, reason):
            assert reason == "market_clarification"
            self.reset.extend(asset_ids)
            return list(asset_ids)

    class _Portfolio:
        def __init__(self) -> None:
            self.released: list[int] = []

        def release_order_reservation(self, intent_id, *, reason):
            assert reason == "market_clarification"
            self.released.append(intent_id)

    async def exercise() -> None:
        store = _Store()
        books = _Books()
        portfolio = _Portfolio()
        lifecycle: list[tuple[str, dict]] = []
        service = object.__new__(LivePaperShadowService)
        service.store = store
        service.route_services = {"primary": books}
        service.pending = {11: object()}
        service.portfolio_store = portfolio
        service.own_order_oms_gate = None

        async def db_call(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def causal(event_type, **kwargs):
            lifecycle.append((event_type, kwargs))

        service._db_call = db_call
        service._causal_lifecycle_event = causal
        result = await service.apply_market_clarification(
            condition_id="condition-1",
            source_event_id="clarification-1",
            clarified_at=NOW,
            payload_hash="hash-1",
        )

        assert result["reset_by_source"] == {
            "primary": ["asset-1", "asset-2"]
        }
        assert books.reset == ["asset-1", "asset-2"]
        assert portfolio.released == [11]
        assert store.closed == [11]
        assert 11 not in service.pending
        assert lifecycle[0][0] == "MARKET_CLARIFICATION"

    asyncio.run(exercise())


def test_market_clarification_command_is_applied_and_acknowledged() -> None:
    class _Store:
        def __init__(self) -> None:
            self.completed: list[tuple[int, str]] = []
            self.failed: list[int] = []

        def claim_market_clarifications(self, *, worker_id, limit):
            assert worker_id == "worker-1"
            assert limit == 10
            return [
                {
                    "command_id": 7,
                    "condition_id": "condition-1",
                    "source_event_id": "clarification-1",
                    "clarified_at": NOW,
                    "payload_hash": "hash-1",
                    "payload": {"source": "official"},
                }
            ]

        def complete_market_clarification_command(
            self, command_id, *, worker_id
        ):
            self.completed.append((command_id, worker_id))
            return True

        def fail_market_clarification_command(self, command_id, **_kwargs):
            self.failed.append(command_id)
            return True

    async def exercise() -> None:
        store = _Store()
        applied: list[dict] = []
        service = object.__new__(LivePaperShadowService)
        service.store = store
        service.worker_id = "worker-1"
        service.stats = SimpleNamespace(
            market_clarification_commands=0,
            market_clarifications_applied=0,
            market_clarification_failures=0,
            last_market_clarification_error=None,
        )
        service._last_market_clarification_poll = -10.0

        async def db_call(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def apply(**kwargs):
            applied.append(kwargs)
            return {"duplicate": False}

        service._db_call = db_call
        service.apply_market_clarification = apply

        await service._advance_market_clarifications()

        assert applied[0]["condition_id"] == "condition-1"
        assert applied[0]["payload"] == {"source": "official"}
        assert store.completed == [(7, "worker-1")]
        assert store.failed == []
        assert service.stats.market_clarifications_applied == 1
        assert service.stats.market_clarification_failures == 0

    asyncio.run(exercise())
