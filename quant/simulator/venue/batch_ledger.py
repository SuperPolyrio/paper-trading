"""Durable non-atomic batch execution and paper-ledger finalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from quant.paper.taker_execution import PaperExecutionResult

from .batch_order_model import PaperOrderBatch, PaperOrderBatchResult
from .gateway import VenueGateway


class BatchArtifactSink(Protocol):
    def persist_order_batch(
        self,
        batch: PaperOrderBatch,
        result: PaperOrderBatchResult,
        *,
        strategy_id: str,
        config_hash: str,
    ) -> None: ...

    def batch_ledger_eligible_command_ids(
        self,
        *,
        batch_id: str,
        command_ids: tuple[str, ...],
    ) -> frozenset[str]: ...

    def mark_batch_child_execution(
        self,
        *,
        batch_id: str,
        command_id: str,
        execution_status: str,
        audit_key: str,
        ledger_status: str,
        result_payload: Mapping[str, Any],
    ) -> None: ...


class PaperExecutionLedgerSink(Protocol):
    def append(self, result: PaperExecutionResult) -> None: ...


@dataclass(frozen=True)
class DurableBatchSubmission:
    batch: PaperOrderBatch
    result: PaperOrderBatchResult


@dataclass(frozen=True)
class BatchChildExecution:
    command_id: str
    result: PaperExecutionResult


@dataclass(frozen=True)
class BatchLedgerFinalizeResult:
    batch_id: str
    supplied_count: int
    eligible_count: int
    applied_count: int
    no_effect_count: int
    skipped_count: int


class DurableBatchExecutor:
    """Submit one gateway batch and atomically persist its child decisions."""

    def __init__(
        self,
        *,
        gateway: VenueGateway,
        artifact_sink: BatchArtifactSink,
        strategy_id: str,
        config_hash: str,
    ) -> None:
        if not str(strategy_id).strip() or not str(config_hash).strip():
            raise ValueError("strategy_id and config_hash are required")
        self.gateway = gateway
        self.artifact_sink = artifact_sink
        self.strategy_id = str(strategy_id)
        self.config_hash = str(config_hash)

    def submit(
        self,
        batch: PaperOrderBatch,
        *,
        now_ts_ns: int | None = None,
    ) -> DurableBatchSubmission:
        now = batch.submitted_ts_ns if now_ts_ns is None else int(now_ts_ns)
        arrived = batch.with_arrival(now)
        result = self.gateway.submit_batch(arrived, now_ts_ns=now)
        self.artifact_sink.persist_order_batch(
            arrived,
            result,
            strategy_id=self.strategy_id,
            config_hash=self.config_hash,
        )
        return DurableBatchSubmission(batch=arrived, result=result)


class BatchLedgerFinalizer:
    """Apply only admitted, terminal fill results to the idempotent paper ledger."""

    def __init__(
        self,
        *,
        artifact_sink: BatchArtifactSink,
        ledger_sink: PaperExecutionLedgerSink,
    ) -> None:
        self.artifact_sink = artifact_sink
        self.ledger_sink = ledger_sink

    def finalize(
        self,
        *,
        batch_id: str,
        children: tuple[BatchChildExecution, ...],
    ) -> BatchLedgerFinalizeResult:
        if not str(batch_id).strip():
            raise ValueError("batch_id is required")
        command_ids = tuple(str(item.command_id) for item in children)
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("batch child execution command_id values must be unique")
        eligible = self.artifact_sink.batch_ledger_eligible_command_ids(
            batch_id=str(batch_id),
            command_ids=command_ids,
        )
        applied = 0
        no_effect = 0
        for child in children:
            if child.command_id not in eligible:
                continue
            result = child.result
            payload = {
                "execution_status": result.status,
                "audit_key": result.audit_key,
                "filled_size": str(result.filled_size),
                "filled_notional": str(result.filled_notional),
                "total_fee": str(result.total_fee),
            }
            successful_fill = result.status in {"FILLED", "PARTIAL"} and bool(
                result.fills
            )
            if successful_fill:
                try:
                    self.ledger_sink.append(result)
                except Exception:
                    self.artifact_sink.mark_batch_child_execution(
                        batch_id=str(batch_id),
                        command_id=child.command_id,
                        execution_status=result.status,
                        audit_key=result.audit_key,
                        ledger_status="FAILED",
                        result_payload=payload,
                    )
                    raise
                ledger_status = "APPLIED"
                applied += 1
            else:
                ledger_status = "NO_EFFECT"
                no_effect += 1
            self.artifact_sink.mark_batch_child_execution(
                batch_id=str(batch_id),
                command_id=child.command_id,
                execution_status=result.status,
                audit_key=result.audit_key,
                ledger_status=ledger_status,
                result_payload=payload,
            )
        return BatchLedgerFinalizeResult(
            batch_id=str(batch_id),
            supplied_count=len(children),
            eligible_count=len(eligible),
            applied_count=applied,
            no_effect_count=no_effect,
            skipped_count=len(children) - len(eligible),
        )
