"""Durable Combo catalog, RFQ lifecycle, positions and collateral plans."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection

from .models import (
    ComboDirection,
    ComboMarket,
    ComboRequest,
    ComboRfqState,
    RequestedSize,
    SizeUnit,
    e6_to_decimal,
    payload_hash,
)
from .state_machine import ComboRfqMachine, RfqTransition

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_market_catalog (
        market_id TEXT PRIMARY KEY,
        condition_id TEXT NOT NULL,
        position_ids JSONB NOT NULL,
        outcomes JSONB NOT NULL,
        outcome_prices JSONB NOT NULL,
        slug TEXT NOT NULL,
        title TEXT NOT NULL,
        volume NUMERIC NOT NULL,
        tags JSONB NOT NULL,
        image TEXT,
        raw_payload_hash TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_combo_market_catalog_condition_idx
    ON quant.simulator_combo_market_catalog(condition_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_official_combo_rfqs (
        rfq_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        admission_decision_id TEXT NOT NULL,
        direction TEXT NOT NULL,
        requested_unit TEXT NOT NULL,
        requested_value_e6 BIGINT NOT NULL,
        leg_position_ids JSONB NOT NULL,
        yes_position_id TEXT NOT NULL,
        no_position_id TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        submission_deadline TIMESTAMPTZ,
        quote JSONB,
        accepted_at TIMESTAMPTZ,
        confirm_by TIMESTAMPTZ,
        tx_hash TEXT,
        error_code TEXT,
        last_event_at TIMESTAMPTZ,
        needs_reconciliation BOOLEAN NOT NULL DEFAULT FALSE,
        request_hash TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_command_attempts (
        command_id TEXT PRIMARY KEY,
        client_request_id TEXT NOT NULL,
        official_rfq_id TEXT,
        command_type TEXT NOT NULL,
        state TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        response_hash TEXT,
        deadline TIMESTAMPTZ,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        error_class TEXT,
        error_message TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_official_combo_rfq_events (
        event_id TEXT PRIMARY KEY,
        rfq_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        source TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_official_combo_rfq_events_rfq_idx
    ON quant.simulator_official_combo_rfq_events(rfq_id,event_ts,event_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_quoter_ws_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        rfq_id TEXT,
        quote_id TEXT,
        execution_status TEXT,
        server_ts TIMESTAMPTZ,
        received_at TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_positions (
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        combo_position_id TEXT NOT NULL,
        shares NUMERIC NOT NULL DEFAULT 0,
        entry_cost_pusd NUMERIC NOT NULL DEFAULT 0,
        realized_pnl NUMERIC NOT NULL DEFAULT 0,
        last_tx_hash TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id,combo_position_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_accounting_events (
        accounting_event_id TEXT PRIMARY KEY,
        rfq_id TEXT,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        combo_position_id TEXT,
        shares_delta NUMERIC NOT NULL DEFAULT 0,
        cash_delta NUMERIC NOT NULL DEFAULT 0,
        realized_pnl_delta NUMERIC NOT NULL DEFAULT 0,
        tx_hash TEXT,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_combo_collateral_plans (
        plan_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        admission_decision_id TEXT NOT NULL,
        block_number BIGINT,
        net_pusd_out NUMERIC NOT NULL,
        required_pusd_input NUMERIC NOT NULL,
        truncated BOOLEAN NOT NULL,
        status TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        plan_payload JSONB NOT NULL,
        tx_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        confirmed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
)


@dataclass(frozen=True)
class ComboFillAccounting:
    accounting_event_id: str
    rfq_id: str
    account_id: str
    strategy_id: str
    direction: ComboDirection
    combo_position_id: str
    shares_e6: int
    cash_e6: int
    tx_hash: str
    event_ts: datetime
    official_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.shares_e6 <= 0 or self.cash_e6 < 0:
            raise ValueError("confirmed Combo fill has invalid amounts")
        if not self.tx_hash:
            raise ValueError("confirmed Combo fill requires a transaction hash")
        if self.event_ts.tzinfo is None:
            raise ValueError("confirmed Combo fill timestamp must be timezone-aware")


class PostgresComboStore:
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

    def upsert_markets(
        self, markets: Sequence[ComboMarket], *, observed_at: datetime
    ) -> int:
        if observed_at.tzinfo is None:
            raise ValueError("Combo catalog observed_at must be timezone-aware")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for market in markets:
                cur.execute(
                    """
                    INSERT INTO quant.simulator_combo_market_catalog (
                        market_id,condition_id,position_ids,outcomes,outcome_prices,
                        slug,title,volume,tags,image,raw_payload_hash,observed_at
                    ) VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,
                              %s,%s,%s)
                    ON CONFLICT (market_id) DO UPDATE SET
                        condition_id=EXCLUDED.condition_id,
                        position_ids=EXCLUDED.position_ids,
                        outcomes=EXCLUDED.outcomes,
                        outcome_prices=EXCLUDED.outcome_prices,
                        slug=EXCLUDED.slug,title=EXCLUDED.title,volume=EXCLUDED.volume,
                        tags=EXCLUDED.tags,image=EXCLUDED.image,
                        raw_payload_hash=EXCLUDED.raw_payload_hash,
                        observed_at=EXCLUDED.observed_at,
                        updated_at=clock_timestamp()
                    """,
                    (
                        market.market_id,
                        market.condition_id,
                        json.dumps(market.position_ids),
                        json.dumps(market.outcomes),
                        json.dumps([str(item) for item in market.outcome_prices]),
                        market.slug,
                        market.title,
                        market.volume,
                        json.dumps(market.tags),
                        market.image,
                        market.raw_payload_hash,
                        observed_at,
                    ),
                )
        return len(markets)

    def begin_command(
        self,
        *,
        command_id: str,
        client_request_id: str,
        command_type: str,
        request_payload: Mapping[str, Any],
        started_at: datetime,
        deadline: datetime | None = None,
    ) -> Mapping[str, Any]:
        digest = payload_hash(request_payload)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_combo_command_attempts (
                    command_id,client_request_id,command_type,state,request_hash,
                    deadline,started_at
                ) VALUES (%s,%s,%s,'SUBMITTING',%s,%s,%s)
                ON CONFLICT (command_id) DO NOTHING
                RETURNING *
                """,
                (
                    command_id,
                    client_request_id,
                    command_type,
                    digest,
                    deadline,
                    started_at,
                ),
            )
            inserted = cur.fetchone()
            if inserted is not None:
                return dict(inserted)
            cur.execute(
                """
                SELECT * FROM quant.simulator_combo_command_attempts
                WHERE command_id=%s
                """,
                (command_id,),
            )
            row = dict(cur.fetchone())
            if str(row["request_hash"]) != digest:
                raise ValueError("Combo command id collision")
            raise ValueError(
                "Combo command was already attempted; reconcile status instead"
            )

    def finish_command(
        self,
        *,
        command_id: str,
        state: str,
        finished_at: datetime,
        official_rfq_id: str | None = None,
        response_payload: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> Mapping[str, Any]:
        selected = state.strip().upper()
        if selected not in {"ACKNOWLEDGED", "FAILED", "UNKNOWN"}:
            raise ValueError("invalid Combo command terminal state")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT state FROM quant.simulator_combo_command_attempts
                WHERE command_id=%s FOR UPDATE
                """,
                (command_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown Combo command: {command_id}")
            if str(row["state"]) != "SUBMITTING":
                cur.execute(
                    """
                    SELECT * FROM quant.simulator_combo_command_attempts
                    WHERE command_id=%s
                    """,
                    (command_id,),
                )
                return dict(cur.fetchone())
            cur.execute(
                """
                UPDATE quant.simulator_combo_command_attempts
                SET state=%s,official_rfq_id=%s,response_hash=%s,
                    finished_at=%s,error_class=%s,error_message=%s,
                    updated_at=clock_timestamp()
                WHERE command_id=%s
                RETURNING *
                """,
                (
                    selected,
                    official_rfq_id,
                    payload_hash(response_payload) if response_payload else None,
                    finished_at,
                    type(error).__name__ if error else None,
                    str(error)[:1000] if error else None,
                    command_id,
                ),
            )
            return dict(cur.fetchone())

    def command(self, command_id: str) -> Mapping[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_combo_command_attempts
                WHERE command_id=%s
                """,
                (command_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown Combo command: {command_id}")
        return dict(row)

    def create_rfq(
        self,
        machine: ComboRfqMachine,
        *,
        account_id: str,
        strategy_id: str,
        admission_decision_id: str,
    ) -> ComboRfqMachine:
        request_hash = payload_hash(machine.request.as_builder_payload())
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_official_combo_rfqs (
                    rfq_id,account_id,strategy_id,admission_decision_id,direction,
                    requested_unit,requested_value_e6,leg_position_ids,
                    yes_position_id,no_position_id,state,created_at,
                    submission_deadline,request_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (rfq_id) DO NOTHING
                """,
                (
                    machine.request.rfq_id,
                    account_id,
                    strategy_id,
                    admission_decision_id,
                    machine.request.direction.value,
                    machine.request.requested_size.unit.value,
                    machine.request.requested_size.value_e6,
                    json.dumps(machine.request.leg_position_ids),
                    machine.request.yes_position_id,
                    machine.request.no_position_id,
                    machine.state.value,
                    machine.request.created_at,
                    machine.request.submission_deadline,
                    request_hash,
                ),
            )
            cur.execute(
                """
                SELECT request_hash FROM quant.simulator_official_combo_rfqs
                WHERE rfq_id=%s
                """,
                (machine.request.rfq_id,),
            )
            if str(cur.fetchone()["request_hash"]) != request_hash:
                raise ValueError("official Combo RFQ id collision")
        return self.rfq(machine.request.rfq_id)

    def apply_transition(
        self,
        machine: ComboRfqMachine,
        transition: RfqTransition,
        *,
        source: str,
    ) -> ComboRfqMachine:
        transition_payload = dict(transition.payload)
        digest = payload_hash(transition_payload)
        quote_payload = _quote_payload(machine)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_state,payload_hash FROM
                    quant.simulator_official_combo_rfq_events
                WHERE event_id=%s
                """,
                (transition.event_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                if (
                    str(existing["to_state"]) != transition.to_state.value
                    or str(existing["payload_hash"]) != digest
                ):
                    raise ValueError("official Combo RFQ event id collision")
                return self.rfq(machine.request.rfq_id)
            cur.execute(
                """
                SELECT state FROM quant.simulator_official_combo_rfqs
                WHERE rfq_id=%s FOR UPDATE
                """,
                (machine.request.rfq_id,),
            )
            current = cur.fetchone()
            if current is None:
                raise KeyError(f"unknown official Combo RFQ: {machine.request.rfq_id}")
            if str(current["state"]) != transition.from_state.value:
                raise ValueError("persisted Combo RFQ state changed before transition")
            cur.execute(
                """
                UPDATE quant.simulator_official_combo_rfqs
                SET state=%s,quote=%s::jsonb,accepted_at=%s,confirm_by=%s,
                    tx_hash=%s,error_code=%s,last_event_at=%s,
                    needs_reconciliation=%s,updated_at=clock_timestamp()
                WHERE rfq_id=%s
                """,
                (
                    machine.state.value,
                    json.dumps(quote_payload) if quote_payload else None,
                    machine.accepted_at,
                    machine.confirm_by,
                    machine.tx_hash,
                    machine.error_code,
                    machine.last_event_at,
                    machine.needs_reconciliation,
                    machine.request.rfq_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO quant.simulator_official_combo_rfq_events (
                    event_id,rfq_id,event_type,from_state,to_state,event_ts,
                    payload_hash,payload,source
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    transition.event_id,
                    machine.request.rfq_id,
                    transition.event_type,
                    transition.from_state.value,
                    transition.to_state.value,
                    transition.event_ts,
                    digest,
                    json.dumps(transition_payload),
                    source,
                ),
            )
        return self.rfq(machine.request.rfq_id)

    def rfq(self, rfq_id: str) -> ComboRfqMachine:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_official_combo_rfqs WHERE rfq_id=%s",
                (rfq_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown official Combo RFQ: {rfq_id}")
        request = ComboRequest(
            rfq_id=str(row["rfq_id"]),
            leg_position_ids=tuple(row["leg_position_ids"]),
            yes_position_id=str(row["yes_position_id"]),
            no_position_id=str(row["no_position_id"]),
            direction=ComboDirection(str(row["direction"])),
            requested_size=RequestedSize(
                unit=SizeUnit(str(row["requested_unit"])),
                value_e6=int(row["requested_value_e6"]),
            ),
            created_at=row["created_at"],
            submission_deadline=row["submission_deadline"],
        )
        quote = _quote_from_payload(row["quote"], rfq_id=rfq_id)
        return ComboRfqMachine(
            request=request,
            state=ComboRfqState(str(row["state"])),
            quote=quote,
            accepted_at=row["accepted_at"],
            confirm_by=row["confirm_by"],
            tx_hash=row["tx_hash"],
            error_code=row["error_code"],
            last_event_at=row["last_event_at"],
            needs_reconciliation=bool(row["needs_reconciliation"]),
        )

    def record_quoter_event(self, event: Any) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_quoter_ws_events (
                    event_id,event_type,rfq_id,quote_id,execution_status,
                    server_ts,received_at,payload_hash,payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.rfq_id,
                    event.quote_id,
                    event.execution_status,
                    event.server_ts,
                    event.received_at,
                    event.payload_hash,
                    json.dumps(event.payload),
                ),
            )
            return cur.fetchone() is not None

    def seen(self, event_id: str) -> bool:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM quant.simulator_quoter_ws_events WHERE event_id=%s",
                (event_id,),
            )
            return cur.fetchone() is not None

    def record(self, event: Any) -> bool:
        return self.record_quoter_event(event)

    def apply_confirmed_fill(self, fill: ComboFillAccounting) -> Mapping[str, Any]:
        shares = e6_to_decimal(fill.shares_e6)
        cash = e6_to_decimal(fill.cash_e6)
        digest = payload_hash(fill.official_payload)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_hash FROM quant.simulator_combo_accounting_events
                WHERE accounting_event_id=%s
                """,
                (fill.accounting_event_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != digest:
                    raise ValueError("Combo accounting event id collision")
                return self._position(cur, fill.account_id, fill.combo_position_id)
            cur.execute(
                """
                SELECT state,tx_hash FROM quant.simulator_official_combo_rfqs
                WHERE rfq_id=%s FOR UPDATE
                """,
                (fill.rfq_id,),
            )
            rfq = cur.fetchone()
            if rfq is None or str(rfq["state"]) != ComboRfqState.CONFIRMED.value:
                raise ValueError("only CONFIRMED official RFQs may enter accounting")
            if str(rfq["tx_hash"] or "") != fill.tx_hash:
                raise ValueError("RFQ and accounting transaction hashes differ")
            cur.execute(
                """
                SELECT cash_balance,cash_reserved FROM quant.paper_accounts
                WHERE strategy_id=%s FOR UPDATE
                """,
                (fill.strategy_id,),
            )
            account = cur.fetchone()
            if account is None:
                raise KeyError(f"unknown paper account: {fill.strategy_id}")
            position = self._position(cur, fill.account_id, fill.combo_position_id)
            old_shares = Decimal(position.get("shares", 0))
            old_cost = Decimal(position.get("entry_cost_pusd", 0))
            realized_delta = Decimal(0)
            if fill.direction is ComboDirection.BUY:
                available = Decimal(account["cash_balance"]) - Decimal(
                    account["cash_reserved"]
                )
                if cash > available:
                    raise ValueError("insufficient available paper cash for Combo BUY")
                new_shares = old_shares + shares
                new_cost = old_cost + cash
                cash_delta = -cash
            else:
                if shares > old_shares:
                    raise ValueError("insufficient Combo shares for SELL")
                allocated_cost = (
                    old_cost * shares / old_shares if old_shares > 0 else Decimal(0)
                )
                new_shares = old_shares - shares
                new_cost = old_cost - allocated_cost
                realized_delta = cash - allocated_cost
                cash_delta = cash
                if new_shares == 0:
                    new_cost = Decimal(0)
            cur.execute(
                """
                INSERT INTO quant.simulator_combo_positions (
                    account_id,strategy_id,combo_position_id,shares,entry_cost_pusd,
                    realized_pnl,last_tx_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (account_id,combo_position_id) DO UPDATE SET
                    strategy_id=EXCLUDED.strategy_id,shares=EXCLUDED.shares,
                    entry_cost_pusd=EXCLUDED.entry_cost_pusd,
                    realized_pnl=quant.simulator_combo_positions.realized_pnl +
                                 EXCLUDED.realized_pnl,
                    last_tx_hash=EXCLUDED.last_tx_hash,
                    updated_at=clock_timestamp()
                """,
                (
                    fill.account_id,
                    fill.strategy_id,
                    fill.combo_position_id,
                    new_shares,
                    new_cost,
                    realized_delta,
                    fill.tx_hash,
                ),
            )
            cur.execute(
                """
                UPDATE quant.paper_accounts
                SET cash_balance=cash_balance + %s,
                    realized_pnl=realized_pnl + %s,
                    updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (cash_delta, realized_delta, fill.strategy_id),
            )
            cur.execute(
                """
                INSERT INTO quant.simulator_combo_accounting_events (
                    accounting_event_id,rfq_id,account_id,strategy_id,
                    operation_type,combo_position_id,shares_delta,cash_delta,
                    realized_pnl_delta,tx_hash,payload_hash,payload,event_ts
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    fill.accounting_event_id,
                    fill.rfq_id,
                    fill.account_id,
                    fill.strategy_id,
                    f"COMBO_{fill.direction.value}",
                    fill.combo_position_id,
                    shares if fill.direction is ComboDirection.BUY else -shares,
                    cash_delta,
                    realized_delta,
                    fill.tx_hash,
                    digest,
                    json.dumps(fill.official_payload),
                    fill.event_ts,
                ),
            )
            return {
                "account_id": fill.account_id,
                "strategy_id": fill.strategy_id,
                "combo_position_id": fill.combo_position_id,
                "shares": new_shares,
                "entry_cost_pusd": new_cost,
                "realized_pnl_delta": realized_delta,
                "cash_delta": cash_delta,
            }

    def record_collateral_plan(
        self,
        plan: Mapping[str, Any],
        *,
        account_id: str,
        strategy_id: str,
        admission_decision_id: str,
        created_at: datetime,
    ) -> str:
        plan_hash = str(plan.get("planHash") or plan.get("plan_hash") or "")
        if not plan_hash:
            raise ValueError("collateral return plan requires planHash")
        digest = payload_hash(plan)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_combo_collateral_plans (
                    plan_hash,account_id,strategy_id,admission_decision_id,
                    block_number,net_pusd_out,required_pusd_input,truncated,status,
                    payload_hash,plan_payload,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PLANNED',%s,%s::jsonb,%s)
                ON CONFLICT (plan_hash) DO NOTHING
                """,
                (
                    plan_hash,
                    account_id,
                    strategy_id,
                    admission_decision_id,
                    _optional_int(plan.get("blockNumber") or plan.get("block_number")),
                    Decimal(str(plan.get("netPusdOut") or plan.get("net_pusd_out") or 0)),
                    Decimal(
                        str(
                            plan.get("requiredPusdInput")
                            or plan.get("required_pusd_input")
                            or 0
                        )
                    ),
                    bool(plan.get("truncated")),
                    digest,
                    json.dumps(plan),
                    created_at,
                ),
            )
            cur.execute(
                """
                SELECT payload_hash FROM quant.simulator_combo_collateral_plans
                WHERE plan_hash=%s
                """,
                (plan_hash,),
            )
            if str(cur.fetchone()["payload_hash"]) != digest:
                raise ValueError("collateral return planHash collision")
        return plan_hash

    def apply_collateral_confirmation(
        self,
        *,
        plan_hash: str,
        tx_hash: str,
        confirmed_at: datetime,
    ) -> Mapping[str, Any]:
        if not tx_hash:
            raise ValueError("collateral confirmation requires tx_hash")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_combo_collateral_plans
                WHERE plan_hash=%s FOR UPDATE
                """,
                (plan_hash,),
            )
            plan = cur.fetchone()
            if plan is None:
                raise KeyError(f"unknown collateral plan: {plan_hash}")
            if str(plan["status"]) == "CONFIRMED":
                if str(plan["tx_hash"]) != tx_hash:
                    raise ValueError("collateral plan already confirmed by another tx")
                return {"status": "CONFIRMED", "idempotent": True}
            payload = dict(plan["plan_payload"])
            summary = payload.get("positionSummary") or payload.get("position_summary") or {}
            consumed = summary.get("consumed") or ()
            created = summary.get("created") or ()
            for item in consumed:
                self._mutate_position(
                    cur,
                    account_id=str(plan["account_id"]),
                    strategy_id=str(plan["strategy_id"]),
                    position_id=str(item.get("positionId") or item.get("position_id")),
                    delta=-Decimal(str(item.get("amount") or 0)),
                    tx_hash=tx_hash,
                )
            for item in created:
                self._mutate_position(
                    cur,
                    account_id=str(plan["account_id"]),
                    strategy_id=str(plan["strategy_id"]),
                    position_id=str(item.get("positionId") or item.get("position_id")),
                    delta=Decimal(str(item.get("amount") or 0)),
                    tx_hash=tx_hash,
                )
            cash_delta = Decimal(plan["net_pusd_out"]) - Decimal(
                plan["required_pusd_input"]
            )
            cur.execute(
                """
                UPDATE quant.paper_accounts
                SET cash_balance=cash_balance + %s,updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (cash_delta, plan["strategy_id"]),
            )
            if cur.rowcount != 1:
                raise KeyError(f"unknown paper account: {plan['strategy_id']}")
            event_payload = {
                "plan_hash": plan_hash,
                "consumed": consumed,
                "created": created,
                "cash_delta": str(cash_delta),
            }
            cur.execute(
                """
                INSERT INTO quant.simulator_combo_accounting_events (
                    accounting_event_id,account_id,strategy_id,operation_type,
                    shares_delta,cash_delta,realized_pnl_delta,tx_hash,
                    payload_hash,payload,event_ts
                ) VALUES (%s,%s,%s,'COLLATERAL_RETURN',0,%s,0,%s,%s,%s::jsonb,%s)
                """,
                (
                    f"collateral:{plan_hash}",
                    plan["account_id"],
                    plan["strategy_id"],
                    cash_delta,
                    tx_hash,
                    payload_hash(event_payload),
                    json.dumps(event_payload),
                    confirmed_at,
                ),
            )
            cur.execute(
                """
                UPDATE quant.simulator_combo_collateral_plans
                SET status='CONFIRMED',tx_hash=%s,confirmed_at=%s,
                    updated_at=clock_timestamp()
                WHERE plan_hash=%s
                """,
                (tx_hash, confirmed_at, plan_hash),
            )
            return {
                "status": "CONFIRMED",
                "idempotent": False,
                "cash_delta": cash_delta,
                "consumed_count": len(consumed),
                "created_count": len(created),
            }

    def _position(self, cur: Any, account_id: str, position_id: str) -> dict[str, Any]:
        cur.execute(
            """
            SELECT shares,entry_cost_pusd,realized_pnl,last_tx_hash
            FROM quant.simulator_combo_positions
            WHERE account_id=%s AND combo_position_id=%s
            """,
            (account_id, position_id),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else {
            "shares": Decimal(0),
            "entry_cost_pusd": Decimal(0),
            "realized_pnl": Decimal(0),
            "last_tx_hash": None,
        }

    def _mutate_position(
        self,
        cur: Any,
        *,
        account_id: str,
        strategy_id: str,
        position_id: str,
        delta: Decimal,
        tx_hash: str,
    ) -> None:
        if not position_id or delta == 0:
            raise ValueError("collateral plan contains an invalid position mutation")
        current = self._position(cur, account_id, position_id)
        after = Decimal(current["shares"]) + delta
        if after < 0:
            raise ValueError("collateral plan would create a negative Combo position")
        old_shares = Decimal(current["shares"])
        old_cost = Decimal(current["entry_cost_pusd"])
        cost_after = (
            old_cost * after / old_shares if delta < 0 and old_shares > 0 else old_cost
        )
        cur.execute(
            """
            INSERT INTO quant.simulator_combo_positions (
                account_id,strategy_id,combo_position_id,shares,entry_cost_pusd,
                realized_pnl,last_tx_hash
            ) VALUES (%s,%s,%s,%s,%s,0,%s)
            ON CONFLICT (account_id,combo_position_id) DO UPDATE SET
                shares=EXCLUDED.shares,entry_cost_pusd=EXCLUDED.entry_cost_pusd,
                last_tx_hash=EXCLUDED.last_tx_hash,updated_at=clock_timestamp()
            """,
            (account_id, strategy_id, position_id, after, cost_after, tx_hash),
        )


def _quote_payload(machine: ComboRfqMachine) -> dict[str, Any] | None:
    if machine.quote is None:
        return None
    return {
        "quote_id": machine.quote.quote_id,
        "rfq_id": machine.quote.rfq_id,
        "price_e6": machine.quote.price_e6,
        "size_e6": machine.quote.size_e6,
        "expires_at": machine.quote.expires_at.isoformat(),
        "total_required_e6": machine.quote.total_required_e6,
        "net_receive_e6": machine.quote.net_receive_e6,
        "signed_order": dict(machine.quote.signed_order),
    }


def _quote_from_payload(payload: Any, *, rfq_id: str) -> Any:
    if not payload:
        return None
    from .models import ComboQuote

    return ComboQuote(
        quote_id=str(payload["quote_id"]),
        rfq_id=rfq_id,
        price_e6=int(payload["price_e6"]),
        size_e6=int(payload["size_e6"]),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        total_required_e6=(
            int(payload["total_required_e6"])
            if payload.get("total_required_e6") is not None
            else None
        ),
        net_receive_e6=(
            int(payload["net_receive_e6"])
            if payload.get("net_receive_e6") is not None
            else None
        ),
        signed_order=dict(payload.get("signed_order") or {}),
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value not in {None, ""} else None
