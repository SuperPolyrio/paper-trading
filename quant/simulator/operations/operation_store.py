"""Durable position-operation lifecycle and confirmed-only paper accounting."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection
from quant.simulator.complete_set import (
    COMPLETE_SET_SCHEMA_STATEMENTS,
    CompleteSetLotLegInput,
    CompleteSetLotStore,
    CompleteSetProvenance,
)

from .domain import (
    PositionOperationIntent,
    PositionOperationState,
    PositionOperationType,
)
from .position_operations import OperationRecord
from .reservation import (
    PositionOperationReservationError,
    operation_reservation_requirements,
)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_position_operations (
        operation_id TEXT PRIMARY KEY,
        operation_type TEXT NOT NULL,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        collateral_delta NUMERIC NOT NULL,
        token_deltas JSONB NOT NULL,
        token_decimals INTEGER NOT NULL,
        decision_ts TIMESTAMPTZ NOT NULL,
        state TEXT NOT NULL,
        allowance_approved BOOLEAN,
        nonce BIGINT,
        transaction_hash TEXT,
        reason TEXT,
        balance_applied BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_position_operation_events (
        event_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        transaction_hash TEXT,
        reason TEXT NOT NULL DEFAULT '',
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_position_operation_events_op_idx
        ON quant.simulator_position_operation_events
        (operation_id,event_ts,created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_position_operation_nonces (
        account_id TEXT NOT NULL,
        nonce BIGINT NOT NULL,
        operation_id TEXT NOT NULL,
        status TEXT NOT NULL,
        reserved_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id,nonce)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_position_operation_applications (
        operation_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        collateral_delta NUMERIC NOT NULL,
        realized_pnl_delta NUMERIC NOT NULL,
        transaction_hash TEXT,
        applied_event_id TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_position_operation_reservations (
        operation_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        reserved_cash NUMERIC NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        final_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        finalized_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_position_operation_reservations_active_idx
        ON quant.simulator_position_operation_reservations (strategy_id)
        WHERE status='ACTIVE'
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_position_operation_token_reservations (
        operation_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        reserved_quantity NUMERIC NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        finalized_at TIMESTAMPTZ,
        PRIMARY KEY (operation_id,asset_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS
        simulator_position_operation_token_reservations_active_idx
        ON quant.simulator_position_operation_token_reservations (strategy_id,asset_id)
        WHERE status='ACTIVE'
    """,
)


@dataclass(frozen=True)
class RelayerOperationUpdate:
    operation_id: str
    event_id: str
    state: PositionOperationState
    observed_at: datetime
    transaction_hash: str | None = None
    reason: str = ""
    payload: dict[str, Any] | None = None


class PostgresPositionOperationStore:
    """Persist lifecycle truth and apply deltas only at confirmed finality."""

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
            for statement in COMPLETE_SET_SCHEMA_STATEMENTS:
                cur.execute(statement)

    def create(
        self,
        intent: PositionOperationIntent,
        *,
        event_id: str,
    ) -> OperationRecord:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock_operation(cur, intent.event_id)
            current = self._record(cur, intent.event_id, lock=True)
            if current is not None:
                if current.intent != intent:
                    raise ValueError("operation id collision with different intent")
                if current.state not in {
                    PositionOperationState.CONFIRMED,
                    PositionOperationState.FAILED,
                    PositionOperationState.RECONCILED,
                }:
                    self._reserve_resources(cur, intent)
                return current
            cur.execute(
                """
                INSERT INTO quant.simulator_position_operations (
                    operation_id,operation_type,account_id,strategy_id,market_id,
                    condition_id,amount,collateral_delta,token_deltas,
                    token_decimals,decision_ts,state
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                """,
                (
                    intent.event_id,
                    intent.operation_type.value,
                    intent.account_id,
                    intent.strategy_id,
                    intent.market_id,
                    intent.condition_id,
                    intent.amount,
                    intent.collateral_delta,
                    json.dumps(
                        {
                            asset: format(value, "f")
                            for asset, value in intent.token_deltas.items()
                        },
                        sort_keys=True,
                    ),
                    intent.token_decimals,
                    intent.decision_ts,
                    PositionOperationState.CREATED.value,
                ),
            )
            record = OperationRecord(intent)
            self._reserve_resources(cur, intent)
            self._append_event(
                cur,
                event_id=event_id,
                record=record,
                from_state=None,
                event_ts=intent.decision_ts,
                reason="operation_created",
            )
            return record

    def allowance_checked(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        approved: bool,
        reason: str = "",
    ) -> OperationRecord:
        target = (
            PositionOperationState.ALLOWANCE_CHECKED
            if approved
            else PositionOperationState.FAILED
        )
        return self._simple_transition(
            operation_id,
            event_id=event_id,
            event_ts=event_ts,
            expected={PositionOperationState.CREATED},
            target=target,
            reason=reason
            or ("allowance_approved" if approved else "allowance_missing"),
            updates={"allowance_approved": bool(approved)},
        )

    def reserve_nonce(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        nonce: int,
    ) -> OperationRecord:
        nonce = int(nonce)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock_operation(cur, operation_id)
            record = self._require(cur, operation_id, lock=True)
            duplicate = self._duplicate_event(cur, event_id, operation_id)
            if duplicate:
                return record
            self._require_state(record, {PositionOperationState.ALLOWANCE_CHECKED})
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"paper-operation-nonce:{record.intent.account_id}:{nonce}",),
            )
            cur.execute(
                """
                SELECT * FROM quant.simulator_position_operation_nonces
                WHERE account_id=%s AND nonce=%s FOR UPDATE
                """,
                (record.intent.account_id, nonce),
            )
            existing = cur.fetchone()
            if existing is not None and (
                str(existing["operation_id"]) != operation_id
                and str(existing["status"]) != "RELEASED"
            ):
                raise ValueError("nonce already reserved by another operation")
            cur.execute(
                """
                INSERT INTO quant.simulator_position_operation_nonces (
                    account_id,nonce,operation_id,status,reserved_at
                ) VALUES (%s,%s,%s,'RESERVED',%s)
                ON CONFLICT (account_id,nonce) DO UPDATE SET
                    operation_id=EXCLUDED.operation_id,status='RESERVED',
                    reserved_at=EXCLUDED.reserved_at,
                    updated_at=clock_timestamp()
                """,
                (record.intent.account_id, nonce, operation_id, event_ts),
            )
            next_record = OperationRecord(
                record.intent,
                PositionOperationState.NONCE_RESERVED,
                nonce=nonce,
            )
            self._update_record(cur, next_record)
            self._append_event(
                cur,
                event_id=event_id,
                record=next_record,
                from_state=record.state,
                event_ts=event_ts,
                reason="nonce_reserved",
                payload={"nonce": nonce},
            )
            return next_record

    def submit(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        transaction_hash: str,
    ) -> OperationRecord:
        if not str(transaction_hash).strip():
            raise ValueError("submitted operation requires transaction hash")
        record = self._simple_transition(
            operation_id,
            event_id=event_id,
            event_ts=event_ts,
            expected={PositionOperationState.NONCE_RESERVED},
            target=PositionOperationState.SUBMITTED,
            reason="relayer_submitted",
            updates={"transaction_hash": str(transaction_hash)},
        )
        return record

    def mined(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
    ) -> OperationRecord:
        return self._simple_transition(
            operation_id,
            event_id=event_id,
            event_ts=event_ts,
            expected={PositionOperationState.SUBMITTED},
            target=PositionOperationState.MINED,
            reason="transaction_mined",
        )

    def confirm(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
    ) -> OperationRecord:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock_operation(cur, operation_id)
            record = self._require(cur, operation_id, lock=True)
            if self._duplicate_event(cur, event_id, operation_id):
                return record
            self._require_state(record, {PositionOperationState.MINED})
            self._reserve_resources(cur, record.intent)
            self._apply_confirmed(cur, record, event_id=event_id, event_ts=event_ts)
            self._finalize_resource_reservation(
                cur,
                record,
                status="CONSUMED",
                reason="confirmed_finality_balance_applied",
                event_ts=event_ts,
            )
            next_record = OperationRecord(
                record.intent,
                PositionOperationState.CONFIRMED,
                nonce=record.nonce,
                transaction_hash=record.transaction_hash,
            )
            self._update_record(cur, next_record, balance_applied=True)
            self._consume_nonce(cur, next_record)
            self._append_event(
                cur,
                event_id=event_id,
                record=next_record,
                from_state=record.state,
                event_ts=event_ts,
                reason="confirmed_finality_balance_applied",
            )
            return next_record

    def fail(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        reason: str,
    ) -> OperationRecord:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock_operation(cur, operation_id)
            record = self._require(cur, operation_id, lock=True)
            if self._duplicate_event(cur, event_id, operation_id):
                return record
            if record.state in {
                PositionOperationState.CONFIRMED,
                PositionOperationState.RECONCILED,
            }:
                raise ValueError("confirmed operation cannot fail")
            next_record = OperationRecord(
                record.intent,
                PositionOperationState.FAILED,
                nonce=record.nonce,
                transaction_hash=record.transaction_hash,
                reason=str(reason),
            )
            self._finalize_resource_reservation(
                cur,
                record,
                status="RELEASED",
                reason=str(reason),
                event_ts=event_ts,
            )
            self._update_record(cur, next_record)
            self._release_or_consume_nonce(cur, record)
            self._append_event(
                cur,
                event_id=event_id,
                record=next_record,
                from_state=record.state,
                event_ts=event_ts,
                reason=str(reason),
            )
            return next_record

    def reconcile(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        reason: str = "relayer_reconciled",
    ) -> OperationRecord:
        return self._simple_transition(
            operation_id,
            event_id=event_id,
            event_ts=event_ts,
            expected={
                PositionOperationState.CONFIRMED,
                PositionOperationState.FAILED,
            },
            target=PositionOperationState.RECONCILED,
            reason=reason,
        )

    def reconcile_relayer(self, update: RelayerOperationUpdate) -> OperationRecord:
        if update.state is PositionOperationState.SUBMITTED:
            return self.submit(
                update.operation_id,
                event_id=update.event_id,
                event_ts=update.observed_at,
                transaction_hash=str(update.transaction_hash or ""),
            )
        if update.state is PositionOperationState.MINED:
            return self.mined(
                update.operation_id,
                event_id=update.event_id,
                event_ts=update.observed_at,
            )
        if update.state is PositionOperationState.CONFIRMED:
            return self.confirm(
                update.operation_id,
                event_id=update.event_id,
                event_ts=update.observed_at,
            )
        if update.state is PositionOperationState.FAILED:
            return self.fail(
                update.operation_id,
                event_id=update.event_id,
                event_ts=update.observed_at,
                reason=update.reason or "relayer_failed",
            )
        raise ValueError(f"unsupported relayer state: {update.state.value}")

    def operation(self, operation_id: str) -> OperationRecord | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            return self._record(cur, operation_id, lock=False)

    def events(self, operation_id: str) -> tuple[dict[str, Any], ...]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_position_operation_events
                WHERE operation_id=%s ORDER BY event_ts,created_at,event_id
                """,
                (operation_id,),
            )
            return tuple(dict(row) for row in cur.fetchall())

    def reservation(self, operation_id: str) -> dict[str, Any] | None:
        """Return the durable resource freeze and its token components."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_position_operation_reservations
                WHERE operation_id=%s
                """,
                (operation_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                SELECT asset_id,reserved_quantity,status,finalized_at
                FROM quant.simulator_position_operation_token_reservations
                WHERE operation_id=%s ORDER BY asset_id
                """,
                (operation_id,),
            )
            result = dict(row)
            result["tokens"] = [dict(item) for item in cur.fetchall()]
            return result

    def _simple_transition(
        self,
        operation_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        expected: set[PositionOperationState],
        target: PositionOperationState,
        reason: str,
        updates: dict[str, Any] | None = None,
    ) -> OperationRecord:
        updates = updates or {}
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock_operation(cur, operation_id)
            record = self._require(cur, operation_id, lock=True)
            if self._duplicate_event(cur, event_id, operation_id):
                return record
            self._require_state(record, expected)
            next_record = OperationRecord(
                record.intent,
                target,
                nonce=record.nonce,
                transaction_hash=str(
                    updates.get("transaction_hash") or record.transaction_hash or ""
                )
                or None,
                reason=(
                    reason if target is PositionOperationState.FAILED else record.reason
                ),
            )
            self._update_record(
                cur,
                next_record,
                allowance_approved=updates.get("allowance_approved"),
            )
            if target is PositionOperationState.FAILED:
                self._finalize_resource_reservation(
                    cur,
                    record,
                    status="RELEASED",
                    reason=reason,
                    event_ts=event_ts,
                )
            if target is PositionOperationState.SUBMITTED:
                cur.execute(
                    """
                    UPDATE quant.simulator_position_operation_nonces
                    SET status='SUBMITTED',updated_at=clock_timestamp()
                    WHERE account_id=%s AND nonce=%s AND operation_id=%s
                    """,
                    (record.intent.account_id, record.nonce, operation_id),
                )
            self._append_event(
                cur,
                event_id=event_id,
                record=next_record,
                from_state=record.state,
                event_ts=event_ts,
                reason=reason,
                payload=updates,
            )
            return next_record

    @staticmethod
    def _reserve_resources(cur: Any, intent: PositionOperationIntent) -> None:
        requirements = operation_reservation_requirements(intent)
        cur.execute(
            """
            SELECT status,reserved_cash
            FROM quant.simulator_position_operation_reservations
            WHERE operation_id=%s FOR UPDATE
            """,
            (intent.event_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if str(existing["status"]) != "ACTIVE":
                raise PositionOperationReservationError(
                    "operation resource reservation is already finalized"
                )
            if Decimal(existing["reserved_cash"]) != requirements.reserved_cash:
                raise PositionOperationReservationError(
                    "operation cash reservation does not match intent"
                )
            return

        token_assets = sorted(requirements.reserved_tokens)
        for asset_id in token_assets:
            cur.execute(
                """
                INSERT INTO quant.paper_positions (
                    strategy_id,asset_id,market_id,condition_id
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (strategy_id,asset_id) DO UPDATE SET
                    market_id=EXCLUDED.market_id,
                    condition_id=EXCLUDED.condition_id,
                    updated_at=clock_timestamp()
                """,
                (
                    intent.strategy_id,
                    asset_id,
                    intent.market_id,
                    intent.condition_id,
                ),
            )
        cur.execute(
            """
            SELECT cash_balance,cash_reserved FROM quant.paper_accounts
            WHERE strategy_id=%s FOR UPDATE
            """,
            (intent.strategy_id,),
        )
        account = cur.fetchone()
        if account is None:
            raise PositionOperationReservationError(
                "paper operation account does not exist"
            )
        available_cash = Decimal(account["cash_balance"]) - Decimal(
            account["cash_reserved"]
        )
        if requirements.reserved_cash > available_cash:
            raise PositionOperationReservationError(
                "insufficient_available_paper_cash:"
                f"{available_cash}:{requirements.reserved_cash}"
            )

        positions: dict[str, dict[str, Any]] = {}
        if token_assets:
            cur.execute(
                """
                SELECT asset_id,quantity,reserved_quantity
                FROM quant.paper_positions
                WHERE strategy_id=%s AND asset_id=ANY(%s::text[])
                ORDER BY asset_id FOR UPDATE
                """,
                (intent.strategy_id, token_assets),
            )
            positions = {
                str(row["asset_id"]): dict(row) for row in cur.fetchall()
            }
        for asset_id, required in requirements.reserved_tokens.items():
            row = positions[asset_id]
            available = Decimal(row["quantity"]) - Decimal(
                row["reserved_quantity"]
            )
            if required > available:
                raise PositionOperationReservationError(
                    "insufficient_available_paper_position:"
                    f"{asset_id}:{available}:{required}"
                )

        cur.execute(
            """
            INSERT INTO quant.simulator_position_operation_reservations (
                operation_id,strategy_id,operation_type,reserved_cash
            ) VALUES (%s,%s,%s,%s)
            """,
            (
                intent.event_id,
                intent.strategy_id,
                intent.operation_type.value,
                requirements.reserved_cash,
            ),
        )
        for asset_id, quantity in requirements.reserved_tokens.items():
            cur.execute(
                """
                INSERT INTO quant.simulator_position_operation_token_reservations (
                    operation_id,strategy_id,asset_id,reserved_quantity
                ) VALUES (%s,%s,%s,%s)
                """,
                (intent.event_id, intent.strategy_id, asset_id, quantity),
            )
        if requirements.reserved_cash:
            cur.execute(
                """
                UPDATE quant.paper_accounts
                SET cash_reserved=cash_reserved+%s,updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (requirements.reserved_cash, intent.strategy_id),
            )
        for asset_id, quantity in requirements.reserved_tokens.items():
            cur.execute(
                """
                UPDATE quant.paper_positions
                SET reserved_quantity=reserved_quantity+%s,
                    updated_at=clock_timestamp()
                WHERE strategy_id=%s AND asset_id=%s
                """,
                (quantity, intent.strategy_id, asset_id),
            )

    @staticmethod
    def _finalize_resource_reservation(
        cur: Any,
        record: OperationRecord,
        *,
        status: str,
        reason: str,
        event_ts: datetime,
    ) -> None:
        if status not in {"CONSUMED", "RELEASED"}:
            raise ValueError(f"unsupported reservation final status: {status}")
        cur.execute(
            """
            SELECT reserved_cash,status
            FROM quant.simulator_position_operation_reservations
            WHERE operation_id=%s FOR UPDATE
            """,
            (record.intent.event_id,),
        )
        reservation = cur.fetchone()
        if reservation is None:
            return
        current_status = str(reservation["status"])
        if current_status == status:
            return
        if current_status != "ACTIVE":
            raise PositionOperationReservationError(
                f"reservation already finalized as {current_status}"
            )
        reserved_cash = Decimal(reservation["reserved_cash"])
        cur.execute(
            """
            SELECT cash_reserved FROM quant.paper_accounts
            WHERE strategy_id=%s FOR UPDATE
            """,
            (record.intent.strategy_id,),
        )
        account = cur.fetchone()
        if account is None or Decimal(account["cash_reserved"]) < reserved_cash:
            raise PositionOperationReservationError(
                "paper cash reservation aggregate is inconsistent"
            )
        cur.execute(
            """
            SELECT asset_id,reserved_quantity
            FROM quant.simulator_position_operation_token_reservations
            WHERE operation_id=%s AND status='ACTIVE'
            ORDER BY asset_id FOR UPDATE
            """,
            (record.intent.event_id,),
        )
        token_rows = [dict(row) for row in cur.fetchall()]
        for token in token_rows:
            cur.execute(
                """
                SELECT reserved_quantity FROM quant.paper_positions
                WHERE strategy_id=%s AND asset_id=%s FOR UPDATE
                """,
                (record.intent.strategy_id, str(token["asset_id"])),
            )
            position = cur.fetchone()
            quantity = Decimal(token["reserved_quantity"])
            if position is None or Decimal(position["reserved_quantity"]) < quantity:
                raise PositionOperationReservationError(
                    "paper token reservation aggregate is inconsistent:"
                    f"{token['asset_id']}"
                )
        if reserved_cash:
            cur.execute(
                """
                UPDATE quant.paper_accounts
                SET cash_reserved=cash_reserved-%s,updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (reserved_cash, record.intent.strategy_id),
            )
        for token in token_rows:
            cur.execute(
                """
                UPDATE quant.paper_positions
                SET reserved_quantity=reserved_quantity-%s,
                    updated_at=clock_timestamp()
                WHERE strategy_id=%s AND asset_id=%s
                """,
                (
                    token["reserved_quantity"],
                    record.intent.strategy_id,
                    str(token["asset_id"]),
                ),
            )
        cur.execute(
            """
            UPDATE quant.simulator_position_operation_token_reservations
            SET status=%s,updated_at=clock_timestamp(),finalized_at=%s
            WHERE operation_id=%s AND status='ACTIVE'
            """,
            (status, event_ts, record.intent.event_id),
        )
        cur.execute(
            """
            UPDATE quant.simulator_position_operation_reservations
            SET status=%s,final_reason=%s,updated_at=clock_timestamp(),
                finalized_at=%s
            WHERE operation_id=%s AND status='ACTIVE'
            """,
            (status, reason, event_ts, record.intent.event_id),
        )

    @staticmethod
    def _lock_operation(cur: Any, operation_id: str) -> None:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"paper-position-operation:{operation_id}",),
        )

    def _require(self, cur: Any, operation_id: str, *, lock: bool) -> OperationRecord:
        record = self._record(cur, operation_id, lock=lock)
        if record is None:
            raise KeyError(f"unknown position operation: {operation_id}")
        return record

    @staticmethod
    def _record(cur: Any, operation_id: str, *, lock: bool) -> OperationRecord | None:
        cur.execute(
            "SELECT * FROM quant.simulator_position_operations WHERE operation_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (operation_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        intent = PositionOperationIntent(
            event_id=str(row["operation_id"]),
            operation_type=PositionOperationType(str(row["operation_type"])),
            account_id=str(row["account_id"]),
            strategy_id=str(row["strategy_id"]),
            condition_id=str(row["condition_id"]),
            amount=Decimal(row["amount"]),
            decision_ts=row["decision_ts"],
            collateral_delta=Decimal(row["collateral_delta"]),
            token_deltas={
                str(asset): Decimal(str(value))
                for asset, value in dict(row["token_deltas"]).items()
            },
            token_decimals=int(row["token_decimals"]),
            market_id=str(row["market_id"]),
        )
        return OperationRecord(
            intent,
            PositionOperationState(str(row["state"])),
            nonce=int(row["nonce"]) if row.get("nonce") is not None else None,
            transaction_hash=row.get("transaction_hash"),
            reason=row.get("reason"),
        )

    @staticmethod
    def _require_state(
        record: OperationRecord, expected: set[PositionOperationState]
    ) -> None:
        if record.state not in expected:
            allowed = ",".join(sorted(item.value for item in expected))
            raise ValueError(
                f"invalid operation state {record.state.value}; expected {allowed}"
            )

    @staticmethod
    def _duplicate_event(cur: Any, event_id: str, operation_id: str) -> bool:
        cur.execute(
            """
            SELECT operation_id FROM quant.simulator_position_operation_events
            WHERE event_id=%s
            """,
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if str(row["operation_id"]) != operation_id:
            raise ValueError("operation event id collision")
        return True

    @staticmethod
    def _update_record(
        cur: Any,
        record: OperationRecord,
        *,
        allowance_approved: bool | None = None,
        balance_applied: bool | None = None,
    ) -> None:
        cur.execute(
            """
            UPDATE quant.simulator_position_operations
            SET state=%s,
                allowance_approved=COALESCE(%s,allowance_approved),
                nonce=%s,transaction_hash=%s,reason=%s,
                balance_applied=COALESCE(%s,balance_applied),
                updated_at=clock_timestamp()
            WHERE operation_id=%s
            """,
            (
                record.state.value,
                allowance_approved,
                record.nonce,
                record.transaction_hash,
                record.reason,
                balance_applied,
                record.intent.event_id,
            ),
        )

    def _append_event(
        self,
        cur: Any,
        *,
        event_id: str,
        record: OperationRecord,
        from_state: PositionOperationState | None,
        event_ts: datetime,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_position_operation_events (
                event_id,operation_id,from_state,to_state,event_ts,
                transaction_hash,reason,payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (event_id) DO NOTHING RETURNING event_id
            """,
            (
                event_id,
                record.intent.event_id,
                from_state.value if from_state else None,
                record.state.value,
                event_ts,
                record.transaction_hash,
                reason,
                json.dumps(payload or {}, sort_keys=True, default=str),
            ),
        )
        if cur.fetchone() is not None:
            return
        if not self._duplicate_event(cur, event_id, record.intent.event_id):
            raise ValueError("operation event was not persisted")

    @staticmethod
    def _release_or_consume_nonce(cur: Any, record: OperationRecord) -> None:
        if record.nonce is None:
            return
        status = (
            "CONSUMED"
            if record.state
            in {PositionOperationState.SUBMITTED, PositionOperationState.MINED}
            else "RELEASED"
        )
        cur.execute(
            """
            UPDATE quant.simulator_position_operation_nonces
            SET status=%s,updated_at=clock_timestamp()
            WHERE account_id=%s AND nonce=%s AND operation_id=%s
            """,
            (
                status,
                record.intent.account_id,
                record.nonce,
                record.intent.event_id,
            ),
        )

    @staticmethod
    def _consume_nonce(cur: Any, record: OperationRecord) -> None:
        if record.nonce is None:
            return
        cur.execute(
            """
            UPDATE quant.simulator_position_operation_nonces
            SET status='CONSUMED',updated_at=clock_timestamp()
            WHERE account_id=%s AND nonce=%s AND operation_id=%s
            """,
            (record.intent.account_id, record.nonce, record.intent.event_id),
        )

    @staticmethod
    def _apply_confirmed(
        cur: Any,
        record: OperationRecord,
        *,
        event_id: str,
        event_ts: datetime,
    ) -> None:
        intent = record.intent
        cur.execute(
            """
            SELECT cash_balance,realized_pnl FROM quant.paper_accounts
            WHERE strategy_id=%s FOR UPDATE
            """,
            (intent.strategy_id,),
        )
        account = cur.fetchone()
        if account is None:
            raise ValueError("paper operation account does not exist")
        assets = sorted(intent.token_deltas)
        for asset_id in assets:
            cur.execute(
                """
                INSERT INTO quant.paper_positions (
                    strategy_id,asset_id,market_id,condition_id
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (strategy_id,asset_id) DO NOTHING
                """,
                (
                    intent.strategy_id,
                    asset_id,
                    intent.market_id,
                    intent.condition_id,
                ),
            )
        cur.execute(
            """
            SELECT asset_id,quantity,cost_basis,realized_pnl
            FROM quant.paper_positions
            WHERE strategy_id=%s AND asset_id=ANY(%s::text[])
            ORDER BY asset_id FOR UPDATE
            """,
            (intent.strategy_id, assets),
        )
        positions = {str(row["asset_id"]): dict(row) for row in cur.fetchall()}
        for asset_id, row in positions.items():
            old_quantity = Decimal(row["quantity"])
            if old_quantity <= 0:
                continue
            CompleteSetLotStore.ensure_position_coverage(
                cur,
                strategy_id=intent.strategy_id,
                market_id=str(intent.market_id),
                condition_id=intent.condition_id,
                asset_id=asset_id,
                quantity=old_quantity,
                cost_basis=Decimal(row["cost_basis"]),
                observed_at=event_ts,
            )
        removed_basis: dict[str, Decimal] = {}
        removed_total = Decimal(0)
        for asset_id, delta in intent.token_deltas.items():
            if delta >= 0:
                continue
            row = positions[asset_id]
            quantity = Decimal(row["quantity"])
            consume = -delta
            if quantity < consume:
                raise ValueError(
                    "confirmed operation would create negative token balance"
                )
            basis = Decimal(row["cost_basis"])
            allocated = basis * consume / quantity if quantity else Decimal(0)
            removed_basis[asset_id] = allocated
            removed_total += allocated

        positive_total = sum(
            (delta for delta in intent.token_deltas.values() if delta > 0),
            Decimal(0),
        )
        negative_total = sum(
            (-delta for delta in intent.token_deltas.values() if delta < 0),
            Decimal(0),
        )
        positive_basis_pool = Decimal(0)
        if positive_total > 0:
            positive_basis_pool = max(-intent.collateral_delta, Decimal(0))
            if removed_total > 0:
                positive_basis_pool += removed_total
        realized_total = (
            intent.collateral_delta - removed_total
            if positive_total == 0
            else max(intent.collateral_delta, Decimal(0))
        )
        cash_before = Decimal(account["cash_balance"])
        cash_after = cash_before + intent.collateral_delta
        if cash_after < 0:
            raise ValueError("confirmed operation would create negative collateral")

        after_rows: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        added_basis_by_asset: dict[str, Decimal] = {}
        for asset_id in assets:
            row = positions[asset_id]
            delta = intent.token_deltas[asset_id]
            old_quantity = Decimal(row["quantity"])
            old_basis = Decimal(row["cost_basis"])
            old_realized = Decimal(row["realized_pnl"])
            new_quantity = old_quantity + delta
            if new_quantity < 0:
                raise ValueError(
                    "confirmed operation would create negative token balance"
                )
            if delta < 0:
                new_basis = old_basis - removed_basis[asset_id]
                realized_delta = (
                    realized_total * (-delta) / negative_total
                    if realized_total != 0 and negative_total > 0
                    else Decimal(0)
                )
            else:
                added_basis = (
                    positive_basis_pool * delta / positive_total
                    if positive_total > 0
                    else Decimal(0)
                )
                added_basis_by_asset[asset_id] = added_basis
                new_basis = old_basis + added_basis
                realized_delta = Decimal(0)
            if new_quantity == 0:
                new_basis = Decimal(0)
            after_rows[asset_id] = (
                new_quantity,
                new_basis,
                old_realized + realized_delta,
            )

        cur.execute(
            """
            INSERT INTO quant.paper_position_operation_applications (
                operation_id,strategy_id,operation_type,collateral_delta,
                realized_pnl_delta,transaction_hash,applied_event_id,applied_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (operation_id) DO NOTHING RETURNING operation_id
            """,
            (
                intent.event_id,
                intent.strategy_id,
                intent.operation_type.value,
                intent.collateral_delta,
                realized_total,
                record.transaction_hash,
                event_id,
                event_ts,
            ),
        )
        if cur.fetchone() is None:
            raise ValueError("position operation balance already applied")
        if removed_basis:
            CompleteSetLotStore.consume_assets(
                cur,
                consumption_id=f"position-operation-consumption:{intent.event_id}",
                strategy_id=intent.strategy_id,
                market_id=str(intent.market_id),
                condition_id=intent.condition_id,
                consumer_type=f"POSITION_OPERATION_{intent.operation_type.value}",
                consumer_ref=intent.event_id,
                quantities={
                    asset_id: -intent.token_deltas[asset_id]
                    for asset_id in removed_basis
                },
                expected_basis_by_asset=removed_basis,
                cash_delta=intent.collateral_delta,
                realized_pnl_delta=realized_total,
                consumed_at=event_ts,
                metadata={
                    "operation_id": intent.event_id,
                    "transaction_hash": record.transaction_hash,
                },
            )
        if added_basis_by_asset:
            provenance = (
                CompleteSetProvenance.SPLIT
                if intent.operation_type is PositionOperationType.SPLIT
                else CompleteSetProvenance.NEG_RISK_CONVERSION
            )
            CompleteSetLotStore.create_lot(
                cur,
                strategy_id=intent.strategy_id,
                market_id=str(intent.market_id),
                condition_id=intent.condition_id,
                provenance=provenance,
                created_by_type="POSITION_OPERATION",
                source_ref=intent.event_id,
                legs={
                    asset_id: CompleteSetLotLegInput(
                        quantity=intent.token_deltas[asset_id],
                        cost_basis=added_basis,
                    )
                    for asset_id, added_basis in added_basis_by_asset.items()
                },
                created_at=event_ts,
                metadata={
                    "operation_id": intent.event_id,
                    "operation_type": intent.operation_type.value,
                    "transaction_hash": record.transaction_hash,
                },
            )
        cur.execute(
            """
            UPDATE quant.paper_accounts
            SET cash_balance=%s,realized_pnl=realized_pnl+%s,
                updated_at=clock_timestamp()
            WHERE strategy_id=%s
            """,
            (cash_after, realized_total, intent.strategy_id),
        )
        first_asset = assets[0]
        for asset_id in assets:
            quantity, basis, realized = after_rows[asset_id]
            cur.execute(
                """
                UPDATE quant.paper_positions
                SET quantity=%s,cost_basis=%s,realized_pnl=%s,
                    updated_at=clock_timestamp()
                WHERE strategy_id=%s AND asset_id=%s
                """,
                (quantity, basis, realized, intent.strategy_id, asset_id),
            )
            cash_delta = (
                intent.collateral_delta if asset_id == first_asset else Decimal(0)
            )
            realized_delta = realized - Decimal(positions[asset_id]["realized_pnl"])
            cur.execute(
                """
                INSERT INTO quant.paper_ledger_entries (
                    idempotency_key,strategy_id,event_type,market_id,condition_id,
                    asset_id,event_ts,shares_delta,cash_delta,realized_pnl_delta,
                    cash_after,position_after,cost_basis_after,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    f"position-operation:{intent.event_id}:{asset_id}",
                    intent.strategy_id,
                    f"POSITION_OPERATION_{intent.operation_type.value}",
                    intent.market_id,
                    intent.condition_id,
                    asset_id,
                    event_ts,
                    intent.token_deltas[asset_id],
                    cash_delta,
                    realized_delta,
                    cash_after,
                    quantity,
                    basis,
                    json.dumps(
                        {
                            "operation_id": intent.event_id,
                            "account_id": intent.account_id,
                            "transaction_hash": record.transaction_hash,
                            "confirmed_event_id": event_id,
                        },
                        sort_keys=True,
                    ),
                ),
            )
