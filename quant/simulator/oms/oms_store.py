"""Durable own-order, self-trade and strategy-attribution state."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Callable, ContextManager

from quant.core.db import postgres_connection

from .domain import (
    EXTERNAL_STRATEGY_ID,
    OmsAdmission,
    OmsAdmissionStatus,
    OmsOrderState,
    OwnOrder,
    SelfTradePolicy,
)
from .external_order_import import ExternalOrderSnapshot
from .position_assignment import PositionAssignment
from .self_trade_prevention import SelfTradePrevention
from .strategy_subledger import AttributedFill, StrategyAttribution

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_oms_orders (
        order_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL, asset_id TEXT NOT NULL,
        side TEXT NOT NULL, price NUMERIC NOT NULL,
        original_size NUMERIC NOT NULL, remaining_size NUMERIC NOT NULL,
        created_sequence BIGINT NOT NULL, state TEXT NOT NULL,
        external_reference TEXT, source_intent_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (source_intent_id),
        UNIQUE (account_id, external_reference)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_oms_orders_active_idx
        ON quant.simulator_oms_orders (account_id, asset_id, state, created_sequence)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_oms_admissions (
        admission_id TEXT PRIMARY KEY, incoming_order_id TEXT NOT NULL,
        account_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL, policy TEXT NOT NULL,
        status TEXT NOT NULL, reason TEXT NOT NULL,
        cancel_order_ids TEXT[] NOT NULL DEFAULT '{}',
        research_mode BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_oms_events (
        event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
        event_type TEXT NOT NULL, from_state TEXT, to_state TEXT,
        reason TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_oms_position_assignments (
        order_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL, asset_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_oms_strategy_attribution (
        account_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL, quantity NUMERIC NOT NULL DEFAULT 0,
        cost_basis NUMERIC NOT NULL DEFAULT 0,
        cash_attribution NUMERIC NOT NULL DEFAULT 0,
        fee_attribution NUMERIC NOT NULL DEFAULT 0,
        realized_pnl NUMERIC NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id, strategy_id, asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_oms_attributed_fills (
        fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
        account_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL, side TEXT NOT NULL,
        price NUMERIC NOT NULL, size NUMERIC NOT NULL, fee NUMERIC NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
)


class PostgresOwnOrderStore:
    """One transactional OMS shared by all paper workers for an account."""

    def __init__(
        self,
        connection_factory: Callable[..., ContextManager[Any]] = postgres_connection,
    ) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def admit(
        self,
        *,
        admission_id: str,
        incoming: OwnOrder,
        policy: SelfTradePolicy = SelfTradePolicy.REJECT_INCOMING,
        research_mode: bool = False,
        source_intent_id: int | None = None,
    ) -> OmsAdmission:
        """Evaluate and persist admission while holding the account/asset lock."""

        if not str(admission_id).strip():
            raise ValueError("admission_id is required")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"simulator-oms:{incoming.account_id}:{incoming.asset_id}",),
            )
            existing = self._load_admission(cur, admission_id, incoming)
            if existing is not None:
                return existing
            cur.execute(
                """
                SELECT * FROM quant.simulator_oms_orders
                WHERE account_id=%s AND asset_id=%s
                  AND state IN ('WORKING','PENDING_CANCEL')
                  AND remaining_size>0 AND order_id<>%s
                ORDER BY created_sequence,order_id FOR UPDATE
                """,
                (incoming.account_id, incoming.asset_id, incoming.order_id),
            )
            active = tuple(_order_from_row(row) for row in cur.fetchall())
            conflicts = tuple(order for order in active if _crosses(incoming, order))
            admission = SelfTradePrevention(policy).evaluate(
                incoming,
                conflicts=conflicts,
                research_mode=research_mode,
            )
            for index, order_id in enumerate(admission.cancel_order_ids):
                self._request_cancel(
                    cur,
                    order_id,
                    event_id=f"{admission_id}:cancel:{index}:{order_id}",
                    reason=f"self_trade_policy:{policy.value}",
                )
            if admission.status in {
                OmsAdmissionStatus.ACCEPTED,
                OmsAdmissionStatus.RESEARCH_ONLY_INTERNAL_CROSS,
            }:
                self._insert_order(cur, incoming, source_intent_id=source_intent_id)
                self._assign(cur, _assignment(incoming))
                self._event(
                    cur,
                    event_id=f"{admission_id}:admitted",
                    order_id=incoming.order_id,
                    event_type="ADMITTED",
                    from_state=None,
                    to_state=OmsOrderState.WORKING.value,
                    reason=admission.reason,
                )
            cur.execute(
                """
                INSERT INTO quant.simulator_oms_admissions (
                    admission_id,incoming_order_id,account_id,strategy_id,asset_id,
                    policy,status,reason,cancel_order_ids,research_mode
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    admission_id,
                    incoming.order_id,
                    incoming.account_id,
                    incoming.strategy_id,
                    incoming.asset_id,
                    policy.value,
                    admission.status.value,
                    admission.reason,
                    list(admission.cancel_order_ids),
                    bool(research_mode),
                ),
            )
            return admission

    def finalize(
        self,
        *,
        order_id: str,
        execution_status: str,
        remaining_size: Decimal,
        event_id: str,
    ) -> OwnOrder | None:
        """Project a paper result into durable own-order state exactly once."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_oms_orders WHERE order_id=%s FOR UPDATE",
                (str(order_id),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            current = _order_from_row(row)
            cur.execute(
                "SELECT 1 FROM quant.simulator_oms_events WHERE event_id=%s",
                (str(event_id),),
            )
            if cur.fetchone() is not None:
                return current
            remaining = max(Decimal("0"), Decimal(remaining_size))
            status = str(execution_status).upper()
            if status == "FILLED" or remaining == 0:
                next_state = OmsOrderState.FILLED
                remaining = Decimal("0")
            elif status in {"WORKING", "PARTIAL"}:
                next_state = (
                    OmsOrderState.PENDING_CANCEL
                    if current.state is OmsOrderState.PENDING_CANCEL
                    else OmsOrderState.WORKING
                )
            elif status in {"CANCELLED", "CANCELED", "EXPIRED"}:
                next_state = OmsOrderState.CANCELED
            else:
                next_state = OmsOrderState.REJECTED
            cur.execute(
                """
                UPDATE quant.simulator_oms_orders
                SET remaining_size=%s,state=%s,updated_at=clock_timestamp()
                WHERE order_id=%s
                """,
                (remaining, next_state.value, current.order_id),
            )
            self._event(
                cur,
                event_id=str(event_id),
                order_id=current.order_id,
                event_type="EXECUTION_FINALIZED",
                from_state=current.state.value,
                to_state=next_state.value,
                reason=f"paper_result:{status}",
            )
            return replace(current, remaining_size=remaining, state=next_state)

    def request_cancel(self, order_id: str, *, event_id: str, reason: str) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            return self._request_cancel(
                cur,
                str(order_id),
                event_id=str(event_id),
                reason=str(reason),
            )

    def request_cancel_for_intent(
        self,
        intent_id: int,
        *,
        event_id: str,
        reason: str,
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT order_id FROM quant.simulator_oms_orders WHERE source_intent_id=%s",
                (int(intent_id),),
            )
            row = cur.fetchone()
            if row is None:
                return False
            return self._request_cancel(
                cur,
                str(row["order_id"]),
                event_id=str(event_id),
                reason=str(reason),
            )

    def finalize_intent(
        self,
        intent_id: int,
        *,
        execution_status: str,
        event_id: str,
    ) -> OwnOrder | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT order_id FROM quant.simulator_oms_orders WHERE source_intent_id=%s",
                (int(intent_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self.finalize(
            order_id=str(row["order_id"]),
            execution_status=execution_status,
            remaining_size=Decimal("0"),
            event_id=event_id,
        )

    def acknowledge_cancel(self, order_id: str, *, event_id: str) -> OwnOrder:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_oms_orders WHERE order_id=%s FOR UPDATE",
                (str(order_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown durable own order: {order_id}")
            order = _order_from_row(row)
            if order.state is OmsOrderState.CANCELED:
                return order
            if order.state is not OmsOrderState.PENDING_CANCEL:
                raise ValueError("cancel acknowledgement requires PENDING_CANCEL order")
            cur.execute(
                """
                UPDATE quant.simulator_oms_orders SET state='CANCELED',
                    updated_at=clock_timestamp() WHERE order_id=%s
                """,
                (order.order_id,),
            )
            self._event(
                cur,
                event_id=str(event_id),
                order_id=order.order_id,
                event_type="CANCEL_ACKNOWLEDGED",
                from_state=order.state.value,
                to_state=OmsOrderState.CANCELED.value,
                reason="cancel_acknowledged",
            )
            return order.with_state(OmsOrderState.CANCELED)

    def close_strategy(
        self,
        *,
        account_id: str,
        strategy_id: str,
        event_prefix: str,
    ) -> tuple[str, ...]:
        """Cancel only one strategy's selectable working orders."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_id FROM quant.simulator_oms_orders
                WHERE account_id=%s AND strategy_id=%s AND state='WORKING'
                  AND remaining_size>0
                ORDER BY created_sequence,order_id FOR UPDATE
                """,
                (str(account_id), str(strategy_id)),
            )
            selected = tuple(str(row["order_id"]) for row in cur.fetchall())
            for index, order_id in enumerate(selected):
                self._request_cancel(
                    cur,
                    order_id,
                    event_id=f"{event_prefix}:{index}:{order_id}",
                    reason=f"strategy_close:{strategy_id}",
                )
            return selected

    def import_external(self, snapshot: ExternalOrderSnapshot) -> OwnOrder:
        order = OwnOrder(
            order_id=f"external:{snapshot.venue_order_id}",
            account_id=snapshot.account_id,
            strategy_id=EXTERNAL_STRATEGY_ID,
            asset_id=snapshot.asset_id,
            side=snapshot.side,
            price=Decimal(snapshot.price),
            original_size=Decimal(snapshot.original_size),
            remaining_size=Decimal(snapshot.remaining_size),
            created_sequence=int(snapshot.observed_sequence),
            state=snapshot.state,
            external_reference=snapshot.venue_order_id,
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"simulator-oms:{order.account_id}:{order.asset_id}",),
            )
            cur.execute(
                "SELECT * FROM quant.simulator_oms_orders WHERE order_id=%s FOR UPDATE",
                (order.order_id,),
            )
            row = cur.fetchone()
            if row is None:
                self._insert_order(cur, order, source_intent_id=None)
                stored = order
            else:
                existing = _order_from_row(row)
                if (
                    existing.account_id,
                    existing.asset_id,
                    existing.side,
                    existing.external_reference,
                ) != (
                    order.account_id,
                    order.asset_id,
                    order.side,
                    order.external_reference,
                ):
                    raise ValueError("external order id collision")
                stored = replace(
                    existing,
                    price=order.price,
                    original_size=max(existing.original_size, order.original_size),
                    remaining_size=order.remaining_size,
                    state=order.state,
                )
                cur.execute(
                    """
                    UPDATE quant.simulator_oms_orders
                    SET price=%s,original_size=%s,remaining_size=%s,state=%s,
                        updated_at=clock_timestamp()
                    WHERE order_id=%s
                    """,
                    (
                        stored.price,
                        stored.original_size,
                        stored.remaining_size,
                        stored.state.value,
                        stored.order_id,
                    ),
                )
            self._assign(cur, _assignment(stored))
            self._event(
                cur,
                event_id=f"external-snapshot:{snapshot.venue_order_id}:{snapshot.observed_sequence}",
                order_id=stored.order_id,
                event_type="EXTERNAL_RECONCILED",
                from_state=None,
                to_state=stored.state.value,
                reason="external_order_snapshot",
            )
            return stored

    def active_orders(self, *, account_id: str, asset_id: str | None = None) -> tuple[OwnOrder, ...]:
        query = """
            SELECT * FROM quant.simulator_oms_orders
            WHERE account_id=%s AND state IN ('WORKING','PENDING_CANCEL')
              AND remaining_size>0
        """
        params: list[Any] = [str(account_id)]
        if asset_id is not None:
            query += " AND asset_id=%s"
            params.append(str(asset_id))
        query += " ORDER BY created_sequence,order_id"
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return tuple(_order_from_row(row) for row in cur.fetchall())

    def reconcile_terminal_intents(self) -> int:
        """Close OMS rows whose authoritative paper intent is already terminal."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.order_id,o.state,o.remaining_size,
                       i.status AS intent_status,i.order_state,
                       i.remaining_size AS intent_remaining,i.result
                FROM quant.simulator_oms_orders o
                JOIN quant.paper_live_order_intents i
                  ON i.intent_id=o.source_intent_id
                WHERE o.state IN ('WORKING','PENDING_CANCEL')
                  AND o.remaining_size>0
                  AND i.status IN ('COMPLETED','FAILED','CANCELED','EXPIRED')
                ORDER BY o.created_sequence,o.order_id
                FOR UPDATE OF o
                """
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                result = dict(row.get("result") or {})
                filled = Decimal(str(result.get("filled_size") or 0))
                intent_remaining = row.get("intent_remaining")
                remaining = Decimal(
                    str(
                        intent_remaining
                        if intent_remaining is not None
                        else row.get("remaining_size") or 0
                    )
                )
                order_state = str(row.get("order_state") or "").upper()
                intent_status = str(row.get("intent_status") or "").upper()
                if remaining <= 0 and (filled > 0 or order_state == "CONFIRMED"):
                    next_state = OmsOrderState.FILLED
                    remaining = Decimal(0)
                elif intent_status in {"CANCELED", "EXPIRED"} or order_state in {
                    "CANCELED",
                    "EXPIRED",
                    "REPLACED",
                }:
                    next_state = OmsOrderState.CANCELED
                elif filled > 0:
                    # A terminal FAK partial has an unfilled remainder, but that
                    # remainder is no longer resting at the venue.
                    next_state = OmsOrderState.CANCELED
                else:
                    next_state = OmsOrderState.REJECTED
                cur.execute(
                    """
                    UPDATE quant.simulator_oms_orders
                    SET remaining_size=%s,state=%s,updated_at=clock_timestamp()
                    WHERE order_id=%s
                    """,
                    (remaining, next_state.value, str(row["order_id"])),
                )
                self._event(
                    cur,
                    event_id=(
                        f"paper-oms-terminal-intent-reconcile:{row['order_id']}:"
                        f"{intent_status}:{order_state}"
                    ),
                    order_id=str(row["order_id"]),
                    event_type="TERMINAL_INTENT_RECONCILED",
                    from_state=str(row["state"]),
                    to_state=next_state.value,
                    reason=f"paper_intent_terminal:{intent_status}:{order_state}",
                )
            return len(rows)

    def assignment(self, order_id: str) -> PositionAssignment | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_oms_position_assignments WHERE order_id=%s",
                (str(order_id),),
            )
            row = cur.fetchone()
        return _assignment_from_row(row) if row is not None else None

    def apply_fill(self, fill: AttributedFill) -> StrategyAttribution:
        """Apply one assigned fill to a durable strategy bucket exactly once."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"simulator-oms-fill:{fill.fill_id}",),
            )
            cur.execute(
                "SELECT * FROM quant.simulator_oms_position_assignments WHERE order_id=%s",
                (fill.order_id,),
            )
            assignment_row = cur.fetchone()
            if assignment_row is None:
                raise ValueError("fill has no durable strategy assignment")
            assignment = _assignment_from_row(assignment_row)
            if assignment.account_id != fill.account_id or assignment.asset_id != fill.asset_id:
                raise ValueError("fill identity does not match durable assignment")
            cur.execute(
                "SELECT 1 FROM quant.simulator_oms_attributed_fills WHERE fill_id=%s",
                (fill.fill_id,),
            )
            if cur.fetchone() is not None:
                return self._position(cur, assignment)
            cur.execute(
                """
                INSERT INTO quant.simulator_oms_strategy_attribution (
                    account_id,strategy_id,asset_id
                ) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                """,
                (assignment.account_id, assignment.strategy_id, assignment.asset_id),
            )
            cur.execute(
                """
                SELECT * FROM quant.simulator_oms_strategy_attribution
                WHERE account_id=%s AND strategy_id=%s AND asset_id=%s FOR UPDATE
                """,
                (assignment.account_id, assignment.strategy_id, assignment.asset_id),
            )
            current = _attribution_from_row(cur.fetchone())
            if fill.side == "BUY":
                quantity = current.quantity + fill.size
                cost_basis = current.cost_basis + fill.price * fill.size + fill.fee
                cash = current.cash_attribution - fill.price * fill.size - fill.fee
                realized = current.realized_pnl
            else:
                if fill.size > current.quantity:
                    raise ValueError("strategy attribution cannot sell more than assigned quantity")
                unit_cost = current.cost_basis / current.quantity if current.quantity else Decimal("0")
                released = unit_cost * fill.size
                proceeds = fill.price * fill.size - fill.fee
                quantity = current.quantity - fill.size
                cost_basis = current.cost_basis - released
                cash = current.cash_attribution + proceeds
                realized = current.realized_pnl + proceeds - released
            cur.execute(
                """
                UPDATE quant.simulator_oms_strategy_attribution
                SET quantity=%s,cost_basis=%s,cash_attribution=%s,
                    fee_attribution=fee_attribution+%s,realized_pnl=%s,
                    updated_at=clock_timestamp()
                WHERE account_id=%s AND strategy_id=%s AND asset_id=%s
                RETURNING *
                """,
                (
                    quantity,
                    cost_basis,
                    cash,
                    fill.fee,
                    realized,
                    assignment.account_id,
                    assignment.strategy_id,
                    assignment.asset_id,
                ),
            )
            updated = _attribution_from_row(cur.fetchone())
            cur.execute(
                """
                INSERT INTO quant.simulator_oms_attributed_fills (
                    fill_id,order_id,account_id,strategy_id,asset_id,side,price,size,fee
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.account_id,
                    assignment.strategy_id,
                    fill.asset_id,
                    fill.side,
                    fill.price,
                    fill.size,
                    fill.fee,
                ),
            )
            return updated

    def position(self, *, account_id: str, strategy_id: str, asset_id: str) -> StrategyAttribution:
        assignment = PositionAssignment("lookup", account_id, strategy_id, asset_id)
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            return self._position(cur, assignment)

    def account_position(self, *, account_id: str, asset_id: str) -> Decimal:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(sum(quantity),0) AS quantity
                FROM quant.simulator_oms_strategy_attribution
                WHERE account_id=%s AND asset_id=%s
                """,
                (str(account_id), str(asset_id)),
            )
            return Decimal(cur.fetchone()["quantity"])

    @staticmethod
    def _load_admission(cur: Any, admission_id: str, incoming: OwnOrder) -> OmsAdmission | None:
        cur.execute(
            "SELECT * FROM quant.simulator_oms_admissions WHERE admission_id=%s",
            (str(admission_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if (
            str(row["incoming_order_id"]) != incoming.order_id
            or str(row["account_id"]) != incoming.account_id
            or str(row["strategy_id"]) != incoming.strategy_id
            or str(row["asset_id"]) != incoming.asset_id
        ):
            raise ValueError("admission id collision")
        return OmsAdmission(
            incoming,
            OmsAdmissionStatus(str(row["status"])),
            str(row["reason"]),
            tuple(row["cancel_order_ids"] or ()),
        )

    @staticmethod
    def _insert_order(cur: Any, order: OwnOrder, *, source_intent_id: int | None) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_oms_orders (
                order_id,account_id,strategy_id,asset_id,side,price,
                original_size,remaining_size,created_sequence,state,
                external_reference,source_intent_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (order_id) DO NOTHING
            """,
            (
                order.order_id,
                order.account_id,
                order.strategy_id,
                order.asset_id,
                order.side,
                order.price,
                order.original_size,
                order.remaining_size,
                order.created_sequence,
                order.state.value,
                order.external_reference,
                source_intent_id,
            ),
        )
        if int(cur.rowcount or 0) == 1:
            return
        cur.execute("SELECT * FROM quant.simulator_oms_orders WHERE order_id=%s", (order.order_id,))
        existing = cur.fetchone()
        if existing is None or _order_from_row(existing) != order:
            raise ValueError("order id collision in durable own-order book")

    @staticmethod
    def _assign(cur: Any, assignment: PositionAssignment) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_oms_position_assignments (
                order_id,account_id,strategy_id,asset_id
            ) VALUES (%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING
            """,
            (
                assignment.order_id,
                assignment.account_id,
                assignment.strategy_id,
                assignment.asset_id,
            ),
        )
        cur.execute(
            "SELECT * FROM quant.simulator_oms_position_assignments WHERE order_id=%s",
            (assignment.order_id,),
        )
        row = cur.fetchone()
        if row is None or _assignment_from_row(row) != assignment:
            raise ValueError("order ownership cannot be reassigned")

    @staticmethod
    def _request_cancel(cur: Any, order_id: str, *, event_id: str, reason: str) -> bool:
        cur.execute("SELECT 1 FROM quant.simulator_oms_events WHERE event_id=%s", (event_id,))
        if cur.fetchone() is not None:
            return False
        cur.execute(
            "SELECT * FROM quant.simulator_oms_orders WHERE order_id=%s FOR UPDATE",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        order = _order_from_row(row)
        if order.state is not OmsOrderState.WORKING or order.remaining_size <= 0:
            return False
        cur.execute(
            """
            UPDATE quant.simulator_oms_orders SET state='PENDING_CANCEL',
                updated_at=clock_timestamp() WHERE order_id=%s
            """,
            (order_id,),
        )
        PostgresOwnOrderStore._event(
            cur,
            event_id=event_id,
            order_id=order_id,
            event_type="CANCEL_REQUESTED",
            from_state=OmsOrderState.WORKING.value,
            to_state=OmsOrderState.PENDING_CANCEL.value,
            reason=reason,
        )
        return True

    @staticmethod
    def _event(
        cur: Any,
        *,
        event_id: str,
        order_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        reason: str,
    ) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_oms_events (
                event_id,order_id,event_type,from_state,to_state,reason
            ) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING
            """,
            (event_id, order_id, event_type, from_state, to_state, reason),
        )

    @staticmethod
    def _position(cur: Any, assignment: PositionAssignment) -> StrategyAttribution:
        cur.execute(
            """
            SELECT * FROM quant.simulator_oms_strategy_attribution
            WHERE account_id=%s AND strategy_id=%s AND asset_id=%s
            """,
            (assignment.account_id, assignment.strategy_id, assignment.asset_id),
        )
        row = cur.fetchone()
        return (
            _attribution_from_row(row)
            if row is not None
            else StrategyAttribution(
                assignment.account_id,
                assignment.strategy_id,
                assignment.asset_id,
            )
        )


def _order_from_row(row: Any) -> OwnOrder:
    return OwnOrder(
        order_id=str(row["order_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        asset_id=str(row["asset_id"]),
        side=str(row["side"]),
        price=Decimal(row["price"]),
        original_size=Decimal(row["original_size"]),
        remaining_size=Decimal(row["remaining_size"]),
        created_sequence=int(row["created_sequence"]),
        state=OmsOrderState(str(row["state"])),
        external_reference=(
            str(row["external_reference"])
            if row.get("external_reference") is not None
            else None
        ),
    )


def _attribution_from_row(row: Any) -> StrategyAttribution:
    return StrategyAttribution(
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        asset_id=str(row["asset_id"]),
        quantity=Decimal(row["quantity"]),
        cost_basis=Decimal(row["cost_basis"]),
        cash_attribution=Decimal(row["cash_attribution"]),
        fee_attribution=Decimal(row["fee_attribution"]),
        realized_pnl=Decimal(row["realized_pnl"]),
    )


def _assignment(order: OwnOrder) -> PositionAssignment:
    return PositionAssignment(order.order_id, order.account_id, order.strategy_id, order.asset_id)


def _assignment_from_row(row: Any) -> PositionAssignment:
    return PositionAssignment(
        order_id=str(row["order_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        asset_id=str(row["asset_id"]),
    )


def _crosses(left: OwnOrder, right: OwnOrder) -> bool:
    if left.side == right.side:
        return False
    buy, sell = (left, right) if left.side == "BUY" else (right, left)
    return buy.price >= sell.price
