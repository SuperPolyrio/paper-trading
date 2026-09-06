"""Durable non-atomic plan, residual hedge and RFQ lifecycle state."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection

from .execution_plan import (
    AtomicityPolicy,
    ExecutionLeg,
    LegState,
    MultiLegExecutionPlan,
)
from .rfq import RfqLifecycle, RfqState


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_multileg_plans (
        plan_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        venue_kind TEXT NOT NULL,
        atomicity_policy TEXT NOT NULL,
        state TEXT NOT NULL,
        hedge_timeout_ms BIGINT NOT NULL,
        hedge_deadline TIMESTAMPTZ,
        residual_exposure NUMERIC NOT NULL DEFAULT 0,
        hedge_covered_exposure NUMERIC NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_multileg_legs (
        plan_id TEXT NOT NULL,
        leg_id TEXT NOT NULL,
        leg_index INTEGER NOT NULL,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        size NUMERIC NOT NULL,
        max_unit_loss NUMERIC NOT NULL,
        state TEXT NOT NULL,
        filled_size NUMERIC NOT NULL DEFAULT 0,
        average_fill_price NUMERIC,
        venue_order_id TEXT,
        failure_reason TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (plan_id,leg_id)
    )
    """,
    """
    ALTER TABLE quant.simulator_multileg_legs
    ADD COLUMN IF NOT EXISTS leg_index INTEGER NOT NULL DEFAULT 0
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_multileg_events (
        event_id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_multileg_hedges (
        hedge_id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        requested_exposure NUMERIC NOT NULL,
        covered_exposure NUMERIC NOT NULL,
        status TEXT NOT NULL,
        venue_order_id TEXT,
        event_ts TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_rfqs (
        rfq_id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        state TEXT NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL,
        quote_at TIMESTAMPTZ,
        accepted_at TIMESTAMPTZ,
        quote_id TEXT,
        quote_price NUMERIC,
        decline_reason TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_rfq_events (
        event_id TEXT PRIMARY KEY,
        rfq_id TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
)


@dataclass(frozen=True)
class LegExecutionOutcome:
    leg_id: str
    filled_size: Decimal
    average_fill_price: Decimal | None
    venue_order_id: str | None = None
    failed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class DurableMultiLegPlan:
    plan: MultiLegExecutionPlan
    strategy_id: str
    account_id: str
    venue_kind: str
    state: str
    residual_exposure: Decimal
    hedge_covered_exposure: Decimal
    hedge_deadline: datetime | None


class PostgresMultiLegPlanStore:
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

    def create_plan(
        self,
        plan: MultiLegExecutionPlan,
        *,
        strategy_id: str,
        account_id: str,
        venue_kind: str,
        event_id: str,
    ) -> DurableMultiLegPlan:
        venue_kind = str(venue_kind).upper()
        if venue_kind == "CLOB" and plan.policy is AtomicityPolicy.VENUE_ATOMIC:
            raise ValueError("ordinary CLOB plans cannot claim venue atomicity")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock(cur, plan.plan_id)
            current = self._plan(cur, plan.plan_id, lock=True)
            if current is not None:
                if (
                    current.plan != plan
                    or current.strategy_id != strategy_id
                    or current.account_id != account_id
                    or current.venue_kind != venue_kind
                ):
                    raise ValueError("multi-leg plan id collision")
                return current
            cur.execute(
                """
                INSERT INTO quant.simulator_multileg_plans (
                    plan_id,strategy_id,account_id,venue_kind,atomicity_policy,
                    state,hedge_timeout_ms,residual_exposure,created_at
                ) VALUES (%s,%s,%s,%s,%s,'PLANNED',%s,%s,%s)
                """,
                (
                    plan.plan_id,
                    strategy_id,
                    account_id,
                    venue_kind,
                    plan.policy.value,
                    int(plan.hedge_timeout.total_seconds() * 1000),
                    plan.remaining_leg_exposure,
                    plan.created_at,
                ),
            )
            for leg_index, leg in enumerate(plan.legs):
                cur.execute(
                    """
                    INSERT INTO quant.simulator_multileg_legs (
                        plan_id,leg_id,leg_index,asset_id,side,size,max_unit_loss,state,
                        filled_size,average_fill_price
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        plan.plan_id,
                        leg.leg_id,
                        leg_index,
                        leg.asset_id,
                        leg.side,
                        leg.size,
                        leg.max_unit_loss,
                        leg.state.value,
                        leg.filled_size,
                        leg.average_fill_price,
                    ),
                )
            self._append_plan_event(
                cur,
                event_id=event_id,
                plan_id=plan.plan_id,
                event_type="PLAN_CREATED",
                from_state=None,
                to_state="PLANNED",
                event_ts=plan.created_at,
                payload={"venue_kind": venue_kind, "policy": plan.policy.value},
            )
            return self._require_plan(cur, plan.plan_id, lock=False)

    def record_leg_outcome(
        self,
        plan_id: str,
        outcome: LegExecutionOutcome,
        *,
        event_id: str,
        event_ts: datetime,
    ) -> DurableMultiLegPlan:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock(cur, plan_id)
            current = self._require_plan(cur, plan_id, lock=True)
            if self._duplicate_plan_event(cur, event_id, plan_id):
                return current
            return self._apply_outcomes(
                cur,
                current,
                (outcome,),
                event_id=event_id,
                event_ts=event_ts,
                atomic=False,
            )

    def record_atomic_outcomes(
        self,
        plan_id: str,
        outcomes: tuple[LegExecutionOutcome, ...],
        *,
        event_id: str,
        event_ts: datetime,
    ) -> DurableMultiLegPlan:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock(cur, plan_id)
            current = self._require_plan(cur, plan_id, lock=True)
            if self._duplicate_plan_event(cur, event_id, plan_id):
                return current
            if current.plan.policy not in {
                AtomicityPolicy.VENUE_ATOMIC,
                AtomicityPolicy.ALL_OR_NONE_SIMULATED,
            }:
                raise ValueError("non-atomic plan cannot accept atomic outcome")
            by_leg = {item.leg_id: item for item in outcomes}
            if set(by_leg) != {leg.leg_id for leg in current.plan.legs}:
                raise ValueError("atomic outcome must include every leg")
            all_filled = all(
                by_leg[leg.leg_id].filled_size == leg.size
                and not by_leg[leg.leg_id].failed
                for leg in current.plan.legs
            )
            all_failed = all(item.filled_size == 0 and item.failed for item in outcomes)
            if not (all_filled or all_failed):
                raise ValueError("atomic venue result cannot contain partial legs")
            return self._apply_outcomes(
                cur,
                current,
                outcomes,
                event_id=event_id,
                event_ts=event_ts,
                atomic=True,
            )

    def record_hedge(
        self,
        plan_id: str,
        *,
        hedge_id: str,
        covered_exposure: Decimal,
        status: str,
        event_id: str,
        event_ts: datetime,
        venue_order_id: str | None = None,
    ) -> DurableMultiLegPlan:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock(cur, plan_id)
            current = self._require_plan(cur, plan_id, lock=True)
            if self._duplicate_plan_event(cur, event_id, plan_id):
                return current
            if current.state != "HEDGE_REQUIRED":
                raise ValueError("plan does not currently require a residual hedge")
            covered = Decimal(covered_exposure)
            if covered < 0:
                raise ValueError("covered hedge exposure cannot be negative")
            cur.execute(
                """
                SELECT * FROM quant.simulator_multileg_hedges
                WHERE hedge_id=%s
                """,
                (hedge_id,),
            )
            existing_hedge = cur.fetchone()
            if existing_hedge is not None:
                same_hedge = (
                    str(existing_hedge["plan_id"]) == plan_id
                    and Decimal(existing_hedge["covered_exposure"]) == covered
                    and str(existing_hedge["status"]) == str(status).upper()
                    and existing_hedge.get("venue_order_id") == venue_order_id
                )
                if not same_hedge:
                    raise ValueError("residual hedge id collision")
                return current
            total_covered = current.hedge_covered_exposure + covered
            next_state = (
                "HEDGED"
                if str(status).upper() == "FILLED"
                and total_covered >= current.residual_exposure
                else "HEDGE_REQUIRED"
            )
            cur.execute(
                """
                INSERT INTO quant.simulator_multileg_hedges (
                    hedge_id,plan_id,requested_exposure,covered_exposure,status,
                    venue_order_id,event_ts
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    hedge_id,
                    plan_id,
                    current.residual_exposure,
                    covered,
                    str(status).upper(),
                    venue_order_id,
                    event_ts,
                ),
            )
            cur.execute(
                """
                UPDATE quant.simulator_multileg_plans
                SET state=%s,hedge_covered_exposure=%s,
                    updated_at=clock_timestamp()
                WHERE plan_id=%s
                """,
                (next_state, total_covered, plan_id),
            )
            self._append_plan_event(
                cur,
                event_id=event_id,
                plan_id=plan_id,
                event_type="RESIDUAL_HEDGE",
                from_state=current.state,
                to_state=next_state,
                event_ts=event_ts,
                payload={
                    "hedge_id": hedge_id,
                    "covered_exposure": format(covered, "f"),
                    "venue_order_id": venue_order_id,
                },
            )
            return self._require_plan(cur, plan_id, lock=False)

    def create_rfq(
        self,
        plan_id: str,
        lifecycle: RfqLifecycle,
        *,
        event_id: str,
    ) -> RfqLifecycle:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            plan = self._require_plan(cur, plan_id, lock=True)
            if plan.plan.policy is not AtomicityPolicy.VENUE_ATOMIC:
                raise ValueError("RFQ requires an explicitly venue-atomic plan")
            cur.execute(
                """
                INSERT INTO quant.simulator_combo_rfqs (
                    rfq_id,plan_id,state,requested_at
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (rfq_id) DO NOTHING RETURNING rfq_id
                """,
                (
                    lifecycle.rfq_id,
                    plan_id,
                    lifecycle.state.value,
                    lifecycle.requested_at,
                ),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """
                    SELECT plan_id FROM quant.simulator_combo_rfqs
                    WHERE rfq_id=%s
                    """,
                    (lifecycle.rfq_id,),
                )
                existing = cur.fetchone()
                if existing is None or str(existing["plan_id"]) != plan_id:
                    raise ValueError("RFQ id collision")
                current = self._require_rfq(cur, lifecycle.rfq_id, lock=False)
                if current.requested_at != lifecycle.requested_at:
                    raise ValueError("RFQ id collision")
                return current
            self._append_rfq_event(
                cur,
                event_id=event_id,
                lifecycle=lifecycle,
                from_state=None,
                event_ts=lifecycle.requested_at,
                payload={"plan_id": plan_id},
            )
            return lifecycle

    def quote_rfq(
        self,
        rfq_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        quote_id: str,
        quote_price: Decimal,
    ) -> RfqLifecycle:
        return self._transition_rfq(
            rfq_id,
            event_id=event_id,
            event_ts=event_ts,
            transition=lambda lifecycle: lifecycle.quote(at=event_ts),
            payload={"quote_id": quote_id, "quote_price": format(quote_price, "f")},
            updates={"quote_id": quote_id, "quote_price": quote_price},
        )

    def accept_rfq(
        self, rfq_id: str, *, event_id: str, event_ts: datetime
    ) -> RfqLifecycle:
        return self._transition_rfq(
            rfq_id,
            event_id=event_id,
            event_ts=event_ts,
            transition=lambda lifecycle: lifecycle.accept(at=event_ts),
        )

    def last_look_rfq(
        self,
        rfq_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        accepted: bool,
        reason: str = "",
    ) -> RfqLifecycle:
        return self._transition_rfq(
            rfq_id,
            event_id=event_id,
            event_ts=event_ts,
            transition=lambda lifecycle: lifecycle.last_look(
                at=event_ts, accepted=accepted
            ),
            payload={"accepted": accepted, "reason": reason},
            updates={"decline_reason": None if accepted else reason},
        )

    def mark_rfq_executed(
        self, rfq_id: str, *, event_id: str, event_ts: datetime
    ) -> RfqLifecycle:
        return self._transition_rfq(
            rfq_id,
            event_id=event_id,
            event_ts=event_ts,
            transition=lambda lifecycle: lifecycle.execute(),
        )

    def plan(self, plan_id: str) -> DurableMultiLegPlan | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            return self._plan(cur, plan_id, lock=False)

    def rfq(self, rfq_id: str) -> RfqLifecycle | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            return self._rfq(cur, rfq_id, lock=False)

    def plan_for_rfq(self, rfq_id: str) -> DurableMultiLegPlan:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT plan_id FROM quant.simulator_combo_rfqs WHERE rfq_id=%s",
                (rfq_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown RFQ: {rfq_id}")
            return self._require_plan(cur, str(row["plan_id"]), lock=False)

    def events(self, plan_id: str) -> tuple[dict[str, Any], ...]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_multileg_events
                WHERE plan_id=%s ORDER BY event_ts,created_at,event_id
                """,
                (plan_id,),
            )
            return tuple(dict(row) for row in cur.fetchall())

    def rfq_events(self, rfq_id: str) -> tuple[dict[str, Any], ...]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_combo_rfq_events
                WHERE rfq_id=%s ORDER BY event_ts,created_at,event_id
                """,
                (rfq_id,),
            )
            return tuple(dict(row) for row in cur.fetchall())

    def _apply_outcomes(
        self,
        cur: Any,
        current: DurableMultiLegPlan,
        outcomes: tuple[LegExecutionOutcome, ...],
        *,
        event_id: str,
        event_ts: datetime,
        atomic: bool,
    ) -> DurableMultiLegPlan:
        plan = current.plan
        for outcome in outcomes:
            plan = plan.record_leg_fill(
                outcome.leg_id,
                filled_size=outcome.filled_size,
                average_fill_price=outcome.average_fill_price,
                failed=outcome.failed,
            )
            leg = next(item for item in plan.legs if item.leg_id == outcome.leg_id)
            cur.execute(
                """
                UPDATE quant.simulator_multileg_legs
                SET state=%s,filled_size=%s,average_fill_price=%s,
                    venue_order_id=%s,failure_reason=%s,
                    updated_at=clock_timestamp()
                WHERE plan_id=%s AND leg_id=%s
                """,
                (
                    leg.state.value,
                    leg.filled_size,
                    leg.average_fill_price,
                    outcome.venue_order_id,
                    outcome.reason,
                    plan.plan_id,
                    leg.leg_id,
                ),
            )
        next_state = plan.state.value
        residual = plan.remaining_leg_exposure
        hedge_deadline = (
            event_ts + plan.hedge_timeout
            if next_state == "HEDGE_REQUIRED"
            else current.hedge_deadline
        )
        cur.execute(
            """
            UPDATE quant.simulator_multileg_plans
            SET state=%s,residual_exposure=%s,hedge_deadline=%s,
                updated_at=clock_timestamp()
            WHERE plan_id=%s
            """,
            (next_state, residual, hedge_deadline, plan.plan_id),
        )
        self._append_plan_event(
            cur,
            event_id=event_id,
            plan_id=plan.plan_id,
            event_type="ATOMIC_RESULT" if atomic else "LEG_RESULT",
            from_state=current.state,
            to_state=next_state,
            event_ts=event_ts,
            payload={
                "outcomes": [
                    {
                        "leg_id": item.leg_id,
                        "filled_size": format(item.filled_size, "f"),
                        "failed": item.failed,
                        "venue_order_id": item.venue_order_id,
                    }
                    for item in outcomes
                ],
                "residual_exposure": format(residual, "f"),
            },
        )
        return self._require_plan(cur, plan.plan_id, lock=False)

    def _transition_rfq(
        self,
        rfq_id: str,
        *,
        event_id: str,
        event_ts: datetime,
        transition: Callable[[RfqLifecycle], RfqLifecycle],
        payload: dict[str, Any] | None = None,
        updates: dict[str, Any] | None = None,
    ) -> RfqLifecycle:
        updates = updates or {}
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"paper-rfq:{rfq_id}",),
            )
            current = self._require_rfq(cur, rfq_id, lock=True)
            if self._duplicate_rfq_event(cur, event_id, rfq_id):
                return current
            next_lifecycle = transition(current)
            cur.execute(
                """
                UPDATE quant.simulator_combo_rfqs
                SET state=%s,quote_at=%s,accepted_at=%s,
                    quote_id=COALESCE(%s,quote_id),
                    quote_price=COALESCE(%s,quote_price),
                    decline_reason=COALESCE(%s,decline_reason),
                    updated_at=clock_timestamp()
                WHERE rfq_id=%s
                """,
                (
                    next_lifecycle.state.value,
                    next_lifecycle.quote_at,
                    next_lifecycle.accepted_at,
                    updates.get("quote_id"),
                    updates.get("quote_price"),
                    updates.get("decline_reason"),
                    rfq_id,
                ),
            )
            self._append_rfq_event(
                cur,
                event_id=event_id,
                lifecycle=next_lifecycle,
                from_state=current.state,
                event_ts=event_ts,
                payload=payload,
            )
            return next_lifecycle

    @staticmethod
    def _lock(cur: Any, plan_id: str) -> None:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"paper-multileg:{plan_id}",),
        )

    def _require_plan(
        self, cur: Any, plan_id: str, *, lock: bool
    ) -> DurableMultiLegPlan:
        result = self._plan(cur, plan_id, lock=lock)
        if result is None:
            raise KeyError(f"unknown multi-leg plan: {plan_id}")
        return result

    @staticmethod
    def _plan(cur: Any, plan_id: str, *, lock: bool) -> DurableMultiLegPlan | None:
        cur.execute(
            "SELECT * FROM quant.simulator_multileg_plans WHERE plan_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            """
            SELECT * FROM quant.simulator_multileg_legs
            WHERE plan_id=%s ORDER BY leg_index,leg_id
            """,
            (plan_id,),
        )
        legs = tuple(
            ExecutionLeg(
                leg_id=str(item["leg_id"]),
                asset_id=str(item["asset_id"]),
                side=str(item["side"]),
                size=Decimal(item["size"]),
                max_unit_loss=Decimal(item["max_unit_loss"]),
                state=LegState(str(item["state"])),
                filled_size=Decimal(item["filled_size"]),
                average_fill_price=(
                    Decimal(item["average_fill_price"])
                    if item.get("average_fill_price") is not None
                    else None
                ),
            )
            for item in cur.fetchall()
        )
        plan = MultiLegExecutionPlan(
            plan_id=str(row["plan_id"]),
            policy=AtomicityPolicy(str(row["atomicity_policy"])),
            legs=legs,
            hedge_timeout=timedelta(milliseconds=int(row["hedge_timeout_ms"])),
            created_at=row["created_at"],
        )
        return DurableMultiLegPlan(
            plan=plan,
            strategy_id=str(row["strategy_id"]),
            account_id=str(row["account_id"]),
            venue_kind=str(row["venue_kind"]),
            state=str(row["state"]),
            residual_exposure=Decimal(row["residual_exposure"]),
            hedge_covered_exposure=Decimal(row["hedge_covered_exposure"]),
            hedge_deadline=row.get("hedge_deadline"),
        )

    def _require_rfq(self, cur: Any, rfq_id: str, *, lock: bool) -> RfqLifecycle:
        result = self._rfq(cur, rfq_id, lock=lock)
        if result is None:
            raise KeyError(f"unknown RFQ: {rfq_id}")
        return result

    @staticmethod
    def _rfq(cur: Any, rfq_id: str, *, lock: bool) -> RfqLifecycle | None:
        cur.execute(
            "SELECT * FROM quant.simulator_combo_rfqs WHERE rfq_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (rfq_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return RfqLifecycle(
            rfq_id=str(row["rfq_id"]),
            requested_at=row["requested_at"],
            state=RfqState(str(row["state"])),
            quote_at=row.get("quote_at"),
            accepted_at=row.get("accepted_at"),
        )

    @staticmethod
    def _duplicate_plan_event(cur: Any, event_id: str, plan_id: str) -> bool:
        cur.execute(
            "SELECT plan_id FROM quant.simulator_multileg_events WHERE event_id=%s",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if str(row["plan_id"]) != plan_id:
            raise ValueError("multi-leg event id collision")
        return True

    @staticmethod
    def _duplicate_rfq_event(cur: Any, event_id: str, rfq_id: str) -> bool:
        cur.execute(
            "SELECT rfq_id FROM quant.simulator_combo_rfq_events WHERE event_id=%s",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if str(row["rfq_id"]) != rfq_id:
            raise ValueError("RFQ event id collision")
        return True

    @staticmethod
    def _append_plan_event(
        cur: Any,
        *,
        event_id: str,
        plan_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str,
        event_ts: datetime,
        payload: dict[str, Any],
    ) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_multileg_events (
                event_id,plan_id,event_type,from_state,to_state,event_ts,payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                event_id,
                plan_id,
                event_type,
                from_state,
                to_state,
                event_ts,
                json.dumps(payload, sort_keys=True, default=str),
            ),
        )

    @staticmethod
    def _append_rfq_event(
        cur: Any,
        *,
        event_id: str,
        lifecycle: RfqLifecycle,
        from_state: RfqState | None,
        event_ts: datetime,
        payload: dict[str, Any] | None,
    ) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_combo_rfq_events (
                event_id,rfq_id,from_state,to_state,event_ts,payload
            ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                event_id,
                lifecycle.rfq_id,
                from_state.value if from_state else None,
                lifecycle.state.value,
                event_ts,
                json.dumps(payload or {}, sort_keys=True, default=str),
            ),
        )
