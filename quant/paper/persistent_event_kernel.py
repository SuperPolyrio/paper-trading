"""Durable causal inbox and hash-chain state for the live paper worker."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from quant.simulator.kernel.event_priority import priority_for

KERNEL_MODEL_VERSION = "paper-live-persistent-causal-kernel-v1"

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_global_event_kernel_state (
        partition_key TEXT PRIMARY KEY,
        last_applied_sequence BIGINT NOT NULL DEFAULT 0,
        last_journal_hash TEXT NOT NULL DEFAULT '',
        accepted_event_count BIGINT NOT NULL DEFAULT 0,
        failed_event_count BIGINT NOT NULL DEFAULT 0,
        queue_counts_initialized BOOLEAN NOT NULL DEFAULT FALSE,
        last_event_ts_ns BIGINT,
        last_priority INTEGER,
        last_source_sequence BIGINT,
        last_tiebreaker TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (last_applied_sequence >= 0),
        CHECK (accepted_event_count >= last_applied_sequence),
        CHECK (failed_event_count >= 0),
        CHECK (failed_event_count <= accepted_event_count - last_applied_sequence)
    )
    """,
    """
    ALTER TABLE quant.paper_global_event_kernel_state
    ADD COLUMN IF NOT EXISTS accepted_event_count BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE quant.paper_global_event_kernel_state
    ADD COLUMN IF NOT EXISTS failed_event_count BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE quant.paper_global_event_kernel_state
    ADD COLUMN IF NOT EXISTS queue_counts_initialized BOOLEAN NOT NULL DEFAULT FALSE
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_global_event_kernel_events (
        partition_key TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_ts_ns BIGINT NOT NULL,
        receive_ts_ns BIGINT,
        priority INTEGER NOT NULL,
        source_sequence BIGINT NOT NULL,
        deterministic_tiebreaker TEXT NOT NULL,
        aggregate_key TEXT NOT NULL,
        intent_id BIGINT NOT NULL DEFAULT 0,
        asset_id TEXT,
        event_ts TIMESTAMPTZ NOT NULL,
        record_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        replay_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        source_event_id TEXT,
        model_version TEXT NOT NULL,
        event_fingerprint TEXT NOT NULL,
        processing_state TEXT NOT NULL DEFAULT 'PENDING',
        worker_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        accepted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        claimed_at TIMESTAMPTZ,
        applied_at TIMESTAMPTZ,
        applied_sequence BIGINT,
        journal_hash TEXT,
        late_event BOOLEAN NOT NULL DEFAULT FALSE,
        last_error TEXT,
        PRIMARY KEY (partition_key,event_id),
        UNIQUE (partition_key,applied_sequence),
        CHECK (event_ts_ns >= 0),
        CHECK (source_sequence >= 0),
        CHECK (attempts >= 0),
        CHECK (processing_state IN ('PENDING','PROCESSING','APPLIED','FAILED'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_global_event_kernel_unapplied_idx
    ON quant.paper_global_event_kernel_events (
        partition_key,event_ts_ns,priority,
        source_sequence,deterministic_tiebreaker
    )
    INCLUDE (processing_state)
    WHERE processing_state IN ('PENDING','PROCESSING','FAILED')
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_global_event_kernel_applied_idx
    ON quant.paper_global_event_kernel_events (partition_key,applied_sequence)
    WHERE applied_sequence IS NOT NULL
    """,
)


@dataclass(frozen=True)
class DurableCausalEvent:
    event_id: str
    event_type: str
    event_ts_ns: int
    receive_ts_ns: int | None
    priority: int
    source_sequence: int
    deterministic_tiebreaker: str
    aggregate_key: str
    intent_id: int
    asset_id: str | None
    event_ts: datetime
    record_payload: dict[str, Any]
    replay_payload: dict[str, Any]
    source_event_id: str | None
    model_version: str
    event_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        event_type: str,
        event_ts: datetime,
        source_sequence: int,
        aggregate_key: str,
        intent_id: int = 0,
        asset_id: str | None = None,
        record_payload: Mapping[str, Any] | None = None,
        replay_payload: Mapping[str, Any] | None = None,
        receive_ts: datetime | None = None,
        source_event_id: str | None = None,
        event_namespace: str,
        model_version: str = KERNEL_MODEL_VERSION,
    ) -> DurableCausalEvent:
        normalized_type = str(event_type).strip().upper() or "UNKNOWN"
        normalized_ts = _utc(event_ts)
        normalized_receive = _utc(receive_ts) if receive_ts is not None else None
        normalized_record = _json_mapping(record_payload)
        normalized_replay = _json_mapping(replay_payload)
        fingerprint_payload = {
            "namespace": str(event_namespace),
            "event_type": normalized_type,
            "event_ts": normalized_ts.isoformat(),
            "source_sequence": int(source_sequence),
            "aggregate_key": str(aggregate_key),
            "intent_id": int(intent_id),
            "asset_id": str(asset_id) if asset_id is not None else None,
            "record_payload": normalized_record,
            "replay_payload": normalized_replay,
            "source_event_id": source_event_id,
            "model_version": str(model_version),
        }
        event_fingerprint = _sha256(fingerprint_payload)
        return cls(
            event_id=f"pgev-{event_fingerprint}",
            event_type=normalized_type,
            event_ts_ns=_datetime_to_ns(normalized_ts),
            receive_ts_ns=(
                _datetime_to_ns(normalized_receive)
                if normalized_receive is not None
                else None
            ),
            priority=priority_for(normalized_type),
            source_sequence=int(source_sequence),
            deterministic_tiebreaker=event_fingerprint,
            aggregate_key=str(aggregate_key),
            intent_id=int(intent_id),
            asset_id=str(asset_id) if asset_id is not None else None,
            event_ts=normalized_ts,
            record_payload=normalized_record,
            replay_payload=normalized_replay,
            source_event_id=(
                str(source_event_id) if source_event_id is not None else None
            ),
            model_version=str(model_version),
            event_fingerprint=event_fingerprint,
        )

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        return (
            self.event_ts_ns,
            self.priority,
            self.source_sequence,
            self.deterministic_tiebreaker,
        )

    def causal_record(self, *, sequence: int) -> dict[str, Any]:
        return {
            "sequence": int(sequence),
            "event_type": self.event_type,
            "intent_id": self.intent_id,
            "asset_id": self.asset_id,
            "event_ts": self.event_ts.isoformat(),
            "payload": dict(self.record_payload),
        }


@dataclass(frozen=True)
class KernelAppendResult:
    event: DurableCausalEvent
    processing_state: str
    applied_sequence: int | None
    journal_hash: str | None
    inserted: bool

    @property
    def should_apply(self) -> bool:
        return self.processing_state != "APPLIED"


@dataclass(frozen=True)
class AppliedKernelEvent:
    event: DurableCausalEvent
    sequence: int
    journal_hash: str
    late_event: bool


@dataclass(frozen=True)
class KernelState:
    partition_key: str
    last_applied_sequence: int
    last_journal_hash: str
    pending_events: int = 0
    failed_events: int = 0


class PostgresPersistentEventKernelStore:
    """Idempotent durable inbox owned by one fenced paper partition."""

    def __init__(self, connection_factory: Any, *, partition_key: str) -> None:
        self.connection_factory = connection_factory
        self.partition_key = _required(partition_key, "partition_key")

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def initialize_partition_counters(self) -> KernelState:
        """Reconcile durable queue counters while the worker is stopped."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT last_applied_sequence,last_journal_hash,
                       accepted_event_count,failed_event_count,
                       queue_counts_initialized
                FROM quant.paper_global_event_kernel_state
                WHERE partition_key=%s
                FOR UPDATE
                """,
                (self.partition_key,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    SELECT 1
                    FROM quant.paper_global_event_kernel_events
                    WHERE partition_key=%s AND processing_state='APPLIED'
                    LIMIT 1
                    """,
                    (self.partition_key,),
                )
                if cur.fetchone() is not None:
                    raise RuntimeError(
                        "persistent kernel has applied events without durable state"
                    )

            # This control-plane scan also repairs counters after a code
            # rollback temporarily ran a worker that did not maintain them.
            # Ordinary worker starts read only the small state row.
            cur.execute("SET LOCAL statement_timeout = '300s'")
            cur.execute(
                """
                SELECT count(*) AS outstanding_events,
                       count(*) FILTER (
                           WHERE processing_state='FAILED'
                       ) AS failed_events
                FROM quant.paper_global_event_kernel_events
                WHERE partition_key=%s
                  AND processing_state IN ('PENDING','PROCESSING','FAILED')
                """,
                (self.partition_key,),
            )
            counts = cur.fetchone()
            outstanding = int(counts["outstanding_events"] if counts else 0)
            failed = int(counts["failed_events"] if counts else 0)
            last_sequence = int(row["last_applied_sequence"] if row else 0)
            accepted = last_sequence + outstanding
            if row is None:
                cur.execute(
                    """
                    INSERT INTO quant.paper_global_event_kernel_state (
                        partition_key,last_applied_sequence,last_journal_hash,
                        accepted_event_count,failed_event_count,
                        queue_counts_initialized
                    ) VALUES (%s,0,'',%s,%s,TRUE)
                    """,
                    (self.partition_key, accepted, failed),
                )
                journal_hash = ""
            else:
                cur.execute(
                    """
                    UPDATE quant.paper_global_event_kernel_state
                    SET accepted_event_count=%s,failed_event_count=%s,
                        queue_counts_initialized=TRUE,
                        updated_at=clock_timestamp()
                    WHERE partition_key=%s
                    """,
                    (accepted, failed, self.partition_key),
                )
                journal_hash = str(row["last_journal_hash"] or "")
            conn.commit()
        return KernelState(
            partition_key=self.partition_key,
            last_applied_sequence=last_sequence,
            last_journal_hash=journal_hash,
            pending_events=outstanding - failed,
            failed_events=failed,
        )

    def load_state(self) -> KernelState:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT last_applied_sequence,last_journal_hash,
                       accepted_event_count,failed_event_count,
                       queue_counts_initialized
                FROM quant.paper_global_event_kernel_state
                WHERE partition_key=%s
                """,
                (self.partition_key,),
            )
            row = cur.fetchone()
        if row is None or not bool(row["queue_counts_initialized"]):
            raise RuntimeError(
                "persistent kernel counters are not initialized; run init-schema"
            )
        return _kernel_state_from_row(self.partition_key, row)

    def append(self, event: DurableCausalEvent, *, worker_id: str) -> KernelAppendResult:
        return self.append_many((event,), worker_id=worker_id)[0]

    def append_many(
        self,
        events: Sequence[DurableCausalEvent],
        *,
        worker_id: str,
    ) -> tuple[KernelAppendResult, ...]:
        if not events:
            return ()
        owner = _required(worker_id, "worker_id")
        results: list[KernelAppendResult] = []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            state_row = _lock_initialized_state(cur, self.partition_key)
            inserted_count = 0
            recovered_failed_count = 0
            for event in events:
                cur.execute(
                    """
                    INSERT INTO quant.paper_global_event_kernel_events (
                        partition_key,event_id,event_type,event_ts_ns,receive_ts_ns,
                        priority,source_sequence,deterministic_tiebreaker,aggregate_key,
                        intent_id,asset_id,event_ts,record_payload,replay_payload,
                        source_event_id,model_version,event_fingerprint,worker_id,
                        processing_state,attempts,claimed_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,
                        %s,%s,%s,%s,'PROCESSING',1,clock_timestamp()
                    )
                    ON CONFLICT (partition_key,event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        self.partition_key,
                        event.event_id,
                        event.event_type,
                        event.event_ts_ns,
                        event.receive_ts_ns,
                        event.priority,
                        event.source_sequence,
                        event.deterministic_tiebreaker,
                        event.aggregate_key,
                        event.intent_id,
                        event.asset_id,
                        event.event_ts,
                        json.dumps(event.record_payload, sort_keys=True),
                        json.dumps(event.replay_payload, sort_keys=True),
                        event.source_event_id,
                        event.model_version,
                        event.event_fingerprint,
                        owner,
                    ),
                )
                inserted = cur.fetchone() is not None
                inserted_count += int(inserted)
                cur.execute(
                    """
                    SELECT processing_state,applied_sequence,journal_hash,
                           event_fingerprint
                    FROM quant.paper_global_event_kernel_events
                    WHERE partition_key=%s AND event_id=%s
                    FOR UPDATE
                    """,
                    (self.partition_key, event.event_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("persistent kernel insert disappeared")
                if str(row["event_fingerprint"]) != event.event_fingerprint:
                    raise RuntimeError(
                        f"causal event_id collision for {event.event_id}"
                    )
                state = str(row["processing_state"])
                if not inserted and state in {"PENDING", "PROCESSING", "FAILED"}:
                    recovered_failed_count += int(state == "FAILED")
                    cur.execute(
                        """
                        UPDATE quant.paper_global_event_kernel_events
                        SET processing_state='PROCESSING',worker_id=%s,
                            attempts=attempts+1,claimed_at=clock_timestamp(),last_error=NULL
                        WHERE partition_key=%s AND event_id=%s
                        """,
                        (owner, self.partition_key, event.event_id),
                    )
                    state = "PROCESSING"
                results.append(
                    KernelAppendResult(
                        event=event,
                        processing_state=state,
                        applied_sequence=(
                            int(row["applied_sequence"])
                            if row["applied_sequence"] is not None
                            else None
                        ),
                        journal_hash=(
                            str(row["journal_hash"])
                            if row["journal_hash"] is not None
                            else None
                        ),
                        inserted=inserted,
                    )
                )
            accepted = int(state_row["accepted_event_count"]) + inserted_count
            failed = int(state_row["failed_event_count"]) - recovered_failed_count
            _validate_queue_counts(
                accepted=accepted,
                applied=int(state_row["last_applied_sequence"]),
                failed=failed,
            )
            if inserted_count or recovered_failed_count:
                cur.execute(
                    """
                    UPDATE quant.paper_global_event_kernel_state
                    SET accepted_event_count=%s,failed_event_count=%s,
                        updated_at=clock_timestamp()
                    WHERE partition_key=%s
                    """,
                    (accepted, failed, self.partition_key),
                )
            conn.commit()
        return tuple(results)

    def mark_applied(
        self,
        events: Sequence[DurableCausalEvent],
        *,
        worker_id: str,
    ) -> tuple[AppliedKernelEvent, ...]:
        if not events:
            return ()
        owner = _required(worker_id, "worker_id")
        applied: list[AppliedKernelEvent] = []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            state = _lock_initialized_state(cur, self.partition_key)
            sequence = int(state["last_applied_sequence"])
            journal_hash = str(state["last_journal_hash"] or "")
            last_sort_key = _row_sort_key(state)
            applied_from_failed = 0
            for event in events:
                cur.execute(
                    """
                    SELECT processing_state,worker_id,applied_sequence,journal_hash,
                           event_fingerprint
                    FROM quant.paper_global_event_kernel_events
                    WHERE partition_key=%s AND event_id=%s
                    FOR UPDATE
                    """,
                    (self.partition_key, event.event_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"causal event not appended: {event.event_id}")
                if str(row["event_fingerprint"]) != event.event_fingerprint:
                    raise RuntimeError(
                        f"causal event fingerprint changed: {event.event_id}"
                    )
                if str(row["processing_state"]) == "APPLIED":
                    continue
                applied_from_failed += int(str(row["processing_state"]) == "FAILED")
                if str(row["worker_id"] or "") != owner:
                    raise RuntimeError(
                        f"causal event ownership changed: {event.event_id}"
                    )
                late_event = bool(
                    last_sort_key is not None and event.sort_key <= last_sort_key
                )
                sequence += 1
                record = event.causal_record(sequence=sequence)
                journal_hash = hashlib.sha256(
                    f"{journal_hash}|{_canonical_json(record)}".encode()
                ).hexdigest()
                cur.execute(
                    """
                    UPDATE quant.paper_global_event_kernel_events
                    SET processing_state='APPLIED',applied_at=clock_timestamp(),
                        applied_sequence=%s,journal_hash=%s,late_event=%s,last_error=NULL
                    WHERE partition_key=%s AND event_id=%s
                    """,
                    (
                        sequence,
                        journal_hash,
                        late_event,
                        self.partition_key,
                        event.event_id,
                    ),
                )
                if not late_event:
                    last_sort_key = event.sort_key
                applied.append(
                    AppliedKernelEvent(
                        event=event,
                        sequence=sequence,
                        journal_hash=journal_hash,
                        late_event=late_event,
                    )
                )
            if applied:
                last = last_sort_key
                failed = int(state["failed_event_count"]) - applied_from_failed
                _validate_queue_counts(
                    accepted=int(state["accepted_event_count"]),
                    applied=sequence,
                    failed=failed,
                )
                cur.execute(
                    """
                    UPDATE quant.paper_global_event_kernel_state
                    SET last_applied_sequence=%s,last_journal_hash=%s,
                        failed_event_count=%s,
                        last_event_ts_ns=%s,last_priority=%s,last_source_sequence=%s,
                        last_tiebreaker=%s,updated_at=clock_timestamp()
                    WHERE partition_key=%s
                    """,
                    (
                        sequence,
                        journal_hash,
                        failed,
                        last[0] if last is not None else None,
                        last[1] if last is not None else None,
                        last[2] if last is not None else None,
                        last[3] if last is not None else None,
                        self.partition_key,
                    ),
                )
            conn.commit()
        return tuple(applied)

    def recover_pending(
        self,
        *,
        worker_id: str,
        limit: int = 10_000,
    ) -> tuple[DurableCausalEvent, ...]:
        owner = _required(worker_id, "worker_id")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            state = _lock_initialized_state(cur, self.partition_key)
            outstanding = int(state["accepted_event_count"]) - int(
                state["last_applied_sequence"]
            )
            if outstanding == 0:
                conn.commit()
                return ()
            cur.execute(
                """
                SELECT *
                FROM quant.paper_global_event_kernel_events
                WHERE partition_key=%s
                  AND processing_state IN ('PENDING','PROCESSING','FAILED')
                ORDER BY event_ts_ns,priority,source_sequence,deterministic_tiebreaker
                LIMIT %s
                FOR UPDATE
                """,
                (self.partition_key, max(1, int(limit))),
            )
            rows = list(cur.fetchall())
            if not rows:
                raise RuntimeError(
                    "persistent kernel counters report outstanding events but none "
                    "were recoverable"
                )
            recovered_failed = sum(
                1 for row in rows if str(row["processing_state"]) == "FAILED"
            )
            failed = int(state["failed_event_count"]) - recovered_failed
            _validate_queue_counts(
                accepted=int(state["accepted_event_count"]),
                applied=int(state["last_applied_sequence"]),
                failed=failed,
            )
            cur.execute(
                """
                UPDATE quant.paper_global_event_kernel_events
                SET processing_state='PROCESSING',worker_id=%s,
                    attempts=attempts+1,claimed_at=clock_timestamp(),last_error=NULL
                WHERE partition_key=%s AND event_id=ANY(%s)
                """,
                (owner, self.partition_key, [row["event_id"] for row in rows]),
            )
            if recovered_failed:
                cur.execute(
                    """
                    UPDATE quant.paper_global_event_kernel_state
                    SET failed_event_count=%s,updated_at=clock_timestamp()
                    WHERE partition_key=%s
                    """,
                    (failed, self.partition_key),
                )
            conn.commit()
        return tuple(_event_from_row(row) for row in rows)

    def mark_failed(
        self,
        event_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            state = _lock_initialized_state(cur, self.partition_key)
            cur.execute(
                """
                SELECT processing_state
                FROM quant.paper_global_event_kernel_events
                WHERE partition_key=%s AND event_id=%s AND worker_id=%s
                  AND processing_state<>'APPLIED'
                FOR UPDATE
                """,
                (self.partition_key, event_id, worker_id),
            )
            event_row = cur.fetchone()
            changed = event_row is not None
            if changed:
                was_failed = str(event_row["processing_state"]) == "FAILED"
                cur.execute(
                    """
                    UPDATE quant.paper_global_event_kernel_events
                    SET processing_state='FAILED',last_error=%s
                    WHERE partition_key=%s AND event_id=%s
                    """,
                    (str(error)[:1000], self.partition_key, event_id),
                )
                if not was_failed:
                    failed = int(state["failed_event_count"]) + 1
                    _validate_queue_counts(
                        accepted=int(state["accepted_event_count"]),
                        applied=int(state["last_applied_sequence"]),
                        failed=failed,
                    )
                    cur.execute(
                        """
                        UPDATE quant.paper_global_event_kernel_state
                        SET failed_event_count=%s,updated_at=clock_timestamp()
                        WHERE partition_key=%s
                        """,
                        (failed, self.partition_key),
                    )
            conn.commit()
        return changed


def _lock_initialized_state(cur: Any, partition_key: str) -> Mapping[str, Any]:
    cur.execute(
        """
        SELECT last_applied_sequence,last_journal_hash,
               accepted_event_count,failed_event_count,queue_counts_initialized,
               last_event_ts_ns,last_priority,last_source_sequence,last_tiebreaker
        FROM quant.paper_global_event_kernel_state
        WHERE partition_key=%s
        FOR UPDATE
        """,
        (partition_key,),
    )
    row = cur.fetchone()
    if row is None or not bool(row["queue_counts_initialized"]):
        raise RuntimeError(
            "persistent kernel counters are not initialized; run init-schema"
        )
    _kernel_state_from_row(partition_key, row)
    return row


def _kernel_state_from_row(
    partition_key: str,
    row: Mapping[str, Any],
) -> KernelState:
    applied = int(row["last_applied_sequence"])
    accepted = int(row["accepted_event_count"])
    failed = int(row["failed_event_count"])
    _validate_queue_counts(accepted=accepted, applied=applied, failed=failed)
    return KernelState(
        partition_key=partition_key,
        last_applied_sequence=applied,
        last_journal_hash=str(row["last_journal_hash"] or ""),
        pending_events=accepted - applied - failed,
        failed_events=failed,
    )


def _validate_queue_counts(*, accepted: int, applied: int, failed: int) -> None:
    if applied < 0 or accepted < applied or failed < 0 or failed > accepted - applied:
        raise RuntimeError(
            "persistent kernel queue counters are inconsistent: "
            f"accepted={accepted},applied={applied},failed={failed}"
        )


def _event_from_row(row: Mapping[str, Any]) -> DurableCausalEvent:
    event_ts = row["event_ts"]
    if not isinstance(event_ts, datetime):
        event_ts = datetime.fromisoformat(str(event_ts))
    return DurableCausalEvent(
        event_id=str(row["event_id"]),
        event_type=str(row["event_type"]),
        event_ts_ns=int(row["event_ts_ns"]),
        receive_ts_ns=(
            int(row["receive_ts_ns"]) if row["receive_ts_ns"] is not None else None
        ),
        priority=int(row["priority"]),
        source_sequence=int(row["source_sequence"]),
        deterministic_tiebreaker=str(row["deterministic_tiebreaker"]),
        aggregate_key=str(row["aggregate_key"]),
        intent_id=int(row["intent_id"]),
        asset_id=str(row["asset_id"]) if row["asset_id"] is not None else None,
        event_ts=_utc(event_ts),
        record_payload=_json_mapping(row.get("record_payload")),
        replay_payload=_json_mapping(row.get("replay_payload")),
        source_event_id=(
            str(row["source_event_id"])
            if row.get("source_event_id") is not None
            else None
        ),
        model_version=str(row["model_version"]),
        event_fingerprint=str(row["event_fingerprint"]),
    )


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, str] | None:
    if row.get("last_event_ts_ns") is None:
        return None
    return (
        int(row["last_event_ts_ns"]),
        int(row["last_priority"]),
        int(row["last_source_sequence"]),
        str(row["last_tiebreaker"]),
    )


def _json_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value or {}), sort_keys=True, default=str))


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _datetime_to_ns(value: datetime) -> int:
    return int(_utc(value).timestamp() * 1_000_000_000)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized
