"""Deterministic CLOB V2/pUSD venue-migration replay and acceptance ledger."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

from quant.core.db import postgres_connection
from quant.simulator.admission.domain import stable_hash


class VenueMigrationState(str, Enum):
    PLANNED = "PLANNED"
    TRADING_HALTED = "TRADING_HALTED"
    V1_BOOKS_CLEARED = "V1_BOOKS_CLEARED"
    V1_ORDERS_CANCELED = "V1_ORDERS_CANCELED"
    BALANCES_CONVERTED = "BALANCES_CONVERTED"
    V2_APPROVALS_READY = "V2_APPROVALS_READY"
    TRADING_RESUMED = "TRADING_RESUMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {VenueMigrationState.COMPLETED, VenueMigrationState.FAILED}


class VenueMigrationEventType(str, Enum):
    HALT_TRADING = "HALT_TRADING"
    CLEAR_V1_BOOKS = "CLEAR_V1_BOOKS"
    CANCEL_V1_ORDERS = "CANCEL_V1_ORDERS"
    CONVERT_COLLATERAL = "CONVERT_COLLATERAL"
    APPROVE_V2_CONTRACTS = "APPROVE_V2_CONTRACTS"
    RESUME_TRADING = "RESUME_TRADING"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"


@dataclass(frozen=True)
class VenueMigrationReplay:
    replay_id: str
    account_id: str
    strategy_id: str
    source_version: str
    target_version: str
    source_collateral: str
    target_collateral: str
    state: VenueMigrationState
    expected_open_orders: int
    expected_v1_books: int
    source_balance: Decimal
    target_balance: Decimal
    active_position_count: int
    last_sequence: int = 0
    transaction_hash: str | None = None


@dataclass(frozen=True)
class VenueMigrationEvent:
    event_id: str
    replay_id: str
    sequence: int
    event_type: VenueMigrationEventType
    event_ts: datetime
    payload: Mapping[str, Any]


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_venue_migration_replays (
        replay_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        source_version TEXT NOT NULL,
        target_version TEXT NOT NULL,
        source_collateral TEXT NOT NULL,
        target_collateral TEXT NOT NULL,
        state TEXT NOT NULL,
        expected_open_orders BIGINT NOT NULL,
        expected_v1_books BIGINT NOT NULL,
        source_balance NUMERIC NOT NULL,
        target_balance NUMERIC NOT NULL,
        active_position_count BIGINT NOT NULL,
        last_sequence BIGINT NOT NULL DEFAULT 0,
        transaction_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_venue_migration_events (
        event_id TEXT PRIMARY KEY,
        replay_id TEXT NOT NULL,
        sequence BIGINT NOT NULL,
        event_type TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (replay_id,sequence)
    )
    """,
)


class PostgresVenueMigrationReplayStore:
    def __init__(
        self,
        connection_factory: Callable[..., AbstractContextManager[Any]] = (
            postgres_connection
        ),
    ) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def create(
        self, replay: VenueMigrationReplay, *, created_at: datetime
    ) -> VenueMigrationReplay:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_venue_migration_replays (
                    replay_id,account_id,strategy_id,source_version,target_version,
                    source_collateral,target_collateral,state,expected_open_orders,
                    expected_v1_books,source_balance,target_balance,
                    active_position_count,last_sequence,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (replay_id) DO NOTHING
                """,
                (
                    replay.replay_id,
                    replay.account_id,
                    replay.strategy_id,
                    replay.source_version,
                    replay.target_version,
                    replay.source_collateral,
                    replay.target_collateral,
                    replay.state.value,
                    replay.expected_open_orders,
                    replay.expected_v1_books,
                    replay.source_balance,
                    replay.target_balance,
                    replay.active_position_count,
                    replay.last_sequence,
                    created_at,
                ),
            )
        return self.get(replay.replay_id)

    def apply(
        self,
        before: VenueMigrationReplay,
        after: VenueMigrationReplay,
        event: VenueMigrationEvent,
    ) -> VenueMigrationReplay:
        digest = stable_hash(event.payload)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_venue_migration_events WHERE event_id=%s",
                (event.event_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != digest:
                    raise ValueError("venue migration event id collision")
                return self.get(before.replay_id)
            cur.execute(
                """
                SELECT state,last_sequence FROM quant.simulator_venue_migration_replays
                WHERE replay_id=%s FOR UPDATE
                """,
                (before.replay_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown migration replay: {before.replay_id}")
            if (
                str(row["state"]) != before.state.value
                or int(row["last_sequence"]) + 1 != event.sequence
            ):
                raise ValueError("migration replay sequence or state mismatch")
            cur.execute(
                """
                UPDATE quant.simulator_venue_migration_replays
                SET state=%s,expected_open_orders=%s,expected_v1_books=%s,
                    target_balance=%s,last_sequence=%s,transaction_hash=%s,
                    updated_at=clock_timestamp()
                WHERE replay_id=%s
                """,
                (
                    after.state.value,
                    after.expected_open_orders,
                    after.expected_v1_books,
                    after.target_balance,
                    after.last_sequence,
                    after.transaction_hash,
                    after.replay_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO quant.simulator_venue_migration_events (
                    event_id,replay_id,sequence,event_type,from_state,to_state,
                    event_ts,payload_hash,payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    event.event_id,
                    event.replay_id,
                    event.sequence,
                    event.event_type.value,
                    before.state.value,
                    after.state.value,
                    event.event_ts,
                    digest,
                    json.dumps(event.payload),
                ),
            )
        return self.get(before.replay_id)

    def get(self, replay_id: str) -> VenueMigrationReplay:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_venue_migration_replays WHERE replay_id=%s",
                (replay_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown migration replay: {replay_id}")
        return VenueMigrationReplay(
            replay_id=str(row["replay_id"]),
            account_id=str(row["account_id"]),
            strategy_id=str(row["strategy_id"]),
            source_version=str(row["source_version"]),
            target_version=str(row["target_version"]),
            source_collateral=str(row["source_collateral"]),
            target_collateral=str(row["target_collateral"]),
            state=VenueMigrationState(str(row["state"])),
            expected_open_orders=int(row["expected_open_orders"]),
            expected_v1_books=int(row["expected_v1_books"]),
            source_balance=Decimal(row["source_balance"]),
            target_balance=Decimal(row["target_balance"]),
            active_position_count=int(row["active_position_count"]),
            last_sequence=int(row["last_sequence"]),
            transaction_hash=row["transaction_hash"],
        )


class VenueMigrationReplayEngine:
    _TRANSITIONS: ClassVar[
        dict[VenueMigrationEventType, tuple[VenueMigrationState, VenueMigrationState]]
    ] = {
        VenueMigrationEventType.HALT_TRADING: (
            VenueMigrationState.PLANNED,
            VenueMigrationState.TRADING_HALTED,
        ),
        VenueMigrationEventType.CLEAR_V1_BOOKS: (
            VenueMigrationState.TRADING_HALTED,
            VenueMigrationState.V1_BOOKS_CLEARED,
        ),
        VenueMigrationEventType.CANCEL_V1_ORDERS: (
            VenueMigrationState.V1_BOOKS_CLEARED,
            VenueMigrationState.V1_ORDERS_CANCELED,
        ),
        VenueMigrationEventType.CONVERT_COLLATERAL: (
            VenueMigrationState.V1_ORDERS_CANCELED,
            VenueMigrationState.BALANCES_CONVERTED,
        ),
        VenueMigrationEventType.APPROVE_V2_CONTRACTS: (
            VenueMigrationState.BALANCES_CONVERTED,
            VenueMigrationState.V2_APPROVALS_READY,
        ),
        VenueMigrationEventType.RESUME_TRADING: (
            VenueMigrationState.V2_APPROVALS_READY,
            VenueMigrationState.TRADING_RESUMED,
        ),
        VenueMigrationEventType.COMPLETE: (
            VenueMigrationState.TRADING_RESUMED,
            VenueMigrationState.COMPLETED,
        ),
    }

    def __init__(self, store: PostgresVenueMigrationReplayStore) -> None:
        self.store = store

    def apply(self, event: VenueMigrationEvent) -> VenueMigrationReplay:
        before = self.store.get(event.replay_id)
        if before.state.terminal:
            raise ValueError("terminal migration replay cannot advance")
        if event.event_ts.tzinfo is None:
            raise ValueError("migration event timestamp must be timezone-aware")
        if event.sequence != before.last_sequence + 1:
            raise ValueError("migration event sequence must be contiguous")
        if event.event_type is VenueMigrationEventType.FAIL:
            after = replace(
                before,
                state=VenueMigrationState.FAILED,
                last_sequence=event.sequence,
            )
            return self.store.apply(before, after, event)
        required, target = self._TRANSITIONS[event.event_type]
        if before.state is not required:
            raise ValueError(f"invalid migration transition {before.state}->{target}")
        after = self._apply_evidence(before, event, target)
        return self.store.apply(before, after, event)

    def replay(self, events: Sequence[VenueMigrationEvent]) -> VenueMigrationReplay:
        if not events:
            raise ValueError("migration replay requires events")
        current: VenueMigrationReplay | None = None
        for event in sorted(events, key=lambda item: item.sequence):
            current = self.apply(event)
        assert current is not None
        return current

    def _apply_evidence(
        self,
        before: VenueMigrationReplay,
        event: VenueMigrationEvent,
        target: VenueMigrationState,
    ) -> VenueMigrationReplay:
        payload = event.payload
        open_orders = before.expected_open_orders
        books = before.expected_v1_books
        target_balance = before.target_balance
        tx_hash = before.transaction_hash
        if event.event_type is VenueMigrationEventType.CLEAR_V1_BOOKS:
            if int(payload.get("remaining_v1_books", -1)) != 0:
                raise ValueError("migration cannot advance while V1 books remain")
            books = 0
        elif event.event_type is VenueMigrationEventType.CANCEL_V1_ORDERS:
            canceled = int(payload.get("canceled_order_count", -1))
            remaining = int(payload.get("remaining_open_orders", -1))
            if remaining != 0 or canceled != before.expected_open_orders:
                raise ValueError("all expected V1 orders must be canceled")
            open_orders = 0
        elif event.event_type is VenueMigrationEventType.CONVERT_COLLATERAL:
            source_debited = Decimal(str(payload.get("source_debited", -1)))
            target_credited = Decimal(str(payload.get("target_credited", -1)))
            if source_debited != before.source_balance or target_credited != source_debited:
                raise ValueError("pUSD conversion must preserve collateral 1:1")
            target_balance = target_credited
            tx_hash = str(payload.get("transaction_hash") or "") or None
            if tx_hash is None:
                raise ValueError("collateral conversion requires transaction evidence")
        elif event.event_type is VenueMigrationEventType.APPROVE_V2_CONTRACTS:
            if not bool(payload.get("exchange_v2_approved")) or not bool(
                payload.get("neg_risk_exchange_v2_approved")
            ):
                raise ValueError("all required V2 approvals must be confirmed")
        elif event.event_type is VenueMigrationEventType.RESUME_TRADING:
            if open_orders != 0 or books != 0:
                raise ValueError("trading cannot resume with surviving V1 state")
            if before.target_balance != before.source_balance:
                raise ValueError("trading cannot resume before 1:1 balance conversion")
            if int(payload.get("active_position_count", -1)) != before.active_position_count:
                raise ValueError("active positions must survive venue migration")
        elif event.event_type is VenueMigrationEventType.COMPLETE:
            if not bool(payload.get("post_migration_reconciliation_passed")):
                raise ValueError("migration completion requires reconciliation PASS")
        return replace(
            before,
            state=target,
            expected_open_orders=open_orders,
            expected_v1_books=books,
            target_balance=target_balance,
            last_sequence=event.sequence,
            transaction_hash=tx_hash,
        )
