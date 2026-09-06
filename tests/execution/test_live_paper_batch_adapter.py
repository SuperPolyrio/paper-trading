import asyncio
from types import SimpleNamespace

from quant.paper.live_shadow_service import (
    LivePaperShadowService,
    LiveShadowStats,
    parse_args,
)
from quant.paper.live_shadow_store import SCHEMA_STATEMENTS
from quant.simulator.run_artifact_store import SIMULATOR_ARTIFACT_SCHEMA


class _BatchStore:
    def __init__(self) -> None:
        self.admissions = []
        self.completed = []
        self.executions = []

    def record_batch_admission(self, intent_id, **kwargs):
        self.admissions.append((intent_id, kwargs))
        return True

    def complete(self, intent_id, result):
        self.completed.append((intent_id, result))

    def record_batch_execution(self, intent_id, result):
        self.executions.append((intent_id, result))
        return True


def _service(store) -> LivePaperShadowService:
    service = object.__new__(LivePaperShadowService)
    service.store = store
    service.lifecycle_scheduler_shadow = None
    service.stats = LiveShadowStats(worker_id="worker")
    service.db_operation_timeout_seconds = 1.0
    return service


def test_batch_adapter_updates_child_evidence_without_replacing_execution() -> None:
    async def exercise() -> None:
        store = _BatchStore()
        service = _service(store)
        result = SimpleNamespace(audit_key="audit:one")

        await service._record_batch_admission(
            7,
            paper_admitted=True,
            venue_shadow=SimpleNamespace(agreement=True),
        )
        await service._complete_intent(7, result)

        assert store.admissions[0][0] == 7
        assert store.completed == [(7, result)]
        assert store.executions == [(7, result)]
        assert service.stats.batch_evidence_updates == 2
        assert service.stats.batch_evidence_failures == 0

    asyncio.run(exercise())


def test_batch_evidence_failure_does_not_fail_completed_paper_order() -> None:
    class _FailingBatchStore(_BatchStore):
        def record_batch_execution(self, intent_id, result):
            del intent_id, result
            raise RuntimeError("batch evidence unavailable")

    async def exercise() -> None:
        store = _FailingBatchStore()
        service = _service(store)
        result = SimpleNamespace(audit_key="audit:one")

        await service._complete_intent(8, result)

        assert store.completed == [(8, result)]
        assert service.stats.batch_evidence_failures == 1
        assert "batch evidence unavailable" in str(
            service.stats.last_batch_evidence_error
        )

    asyncio.run(exercise())


def test_order_result_is_durable_before_account_side_effects() -> None:
    observed: list[str] = []

    class _Store(_BatchStore):
        def complete(self, intent_id, result):
            observed.append("commit")
            super().complete(intent_id, result)

    async def exercise() -> None:
        service = _service(_Store())
        result = SimpleNamespace(audit_key="audit:durable-first")

        async def apply(_intent_id, _result, *, finalize_oms):
            assert finalize_oms is True
            observed.append("side_effects")

        service._apply_committed_result_side_effects = apply
        await service._complete_intent(9, result, finalize_oms=True)

        assert observed == ["commit", "side_effects"]

    asyncio.run(exercise())


def test_failed_order_result_commit_has_no_account_side_effects() -> None:
    observed: list[str] = []

    class _Store(_BatchStore):
        def complete(self, intent_id, result):
            del intent_id, result
            observed.append("commit_failed")
            raise RuntimeError("durable result unavailable")

    async def exercise() -> None:
        service = _service(_Store())

        async def apply(_intent_id, _result, *, finalize_oms):
            del finalize_oms
            observed.append("side_effects")

        service._apply_committed_result_side_effects = apply
        try:
            await service._complete_intent(
                10,
                SimpleNamespace(audit_key="audit:must-not-apply"),
            )
        except RuntimeError as exc:
            assert "durable result unavailable" in str(exc)
        else:
            raise AssertionError("expected durable commit failure")

        assert observed == ["commit_failed"]

    asyncio.run(exercise())


def test_committed_result_survives_retryable_account_failure() -> None:
    store = _BatchStore()

    async def exercise() -> None:
        service = _service(store)

        async def apply(_intent_id, _result, *, finalize_oms):
            del finalize_oms
            raise RuntimeError("ledger temporarily unavailable")

        service._apply_committed_result_side_effects = apply
        result = SimpleNamespace(audit_key="audit:retry-side-effects")
        await service._complete_intent(11, result)

        assert store.completed == [(11, result)]
        assert service.stats.accounting_reconciliation_failures == 1
        assert "ledger temporarily unavailable" in str(
            service.stats.last_accounting_reconciliation_error
        )

    asyncio.run(exercise())


def test_batch_cli_and_schema_have_durable_parent_child_identity() -> None:
    args = parse_args(
        [
            "submit-batch",
            "--batch-id",
            "batch:one",
            "--config-hash",
            "config:one",
            "--input",
            "orders.json",
        ]
    )
    live_schema = "\n".join(SCHEMA_STATEMENTS)
    simulator_schema = "\n".join(SIMULATOR_ARTIFACT_SCHEMA)

    assert args.batch_id == "batch:one"
    assert "ADD COLUMN IF NOT EXISTS batch_id TEXT" in live_schema
    assert "paper_order_batch_children" in simulator_schema
    assert "intent_id BIGINT" in simulator_schema
