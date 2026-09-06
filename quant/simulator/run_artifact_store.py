"""Idempotent PostgreSQL persistence for simulator evidence artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection

from .analytics.paper_tca import PaperTcaArtifact
from .analytics.tca import TcaReport
from .kernel.event import SimEvent
from .observability import DegradationScope, DegradationTransition
from .venue.batch_order_model import PaperOrderBatch, PaperOrderBatchResult
from .venue.inflight_queue import InFlightCommand

SIMULATOR_ARTIFACT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_sim_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        event_ts_ns BIGINT NOT NULL,
        receive_ts_ns BIGINT,
        priority INTEGER NOT NULL,
        source_sequence BIGINT NOT NULL,
        deterministic_tiebreaker TEXT NOT NULL,
        aggregate_key TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        source_event_id TEXT,
        model_version TEXT NOT NULL,
        run_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_inflight_commands (
        command_id TEXT PRIMARY KEY,
        order_id TEXT,
        command_type TEXT NOT NULL,
        state TEXT NOT NULL,
        account_id TEXT NOT NULL,
        signer_id TEXT NOT NULL,
        ip_id TEXT NOT NULL,
        created_ts_ns BIGINT NOT NULL,
        send_ts_ns BIGINT,
        arrival_ts_ns BIGINT,
        ack_ts_ns BIGINT,
        response_ts_ns BIGINT,
        venue_state_at_send TEXT,
        rate_limit_bucket TEXT,
        latency_model_version TEXT,
        unknown_outcome BOOLEAN NOT NULL DEFAULT FALSE,
        reconciliation_status TEXT,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_venue_shadow_runs (
        run_id TEXT PRIMARY KEY,
        heartbeat_ts_ns BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'RUNNING',
        started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_order_batches (
        batch_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        submitted_ts_ns BIGINT NOT NULL,
        arrival_ts_ns BIGINT,
        child_count INTEGER NOT NULL,
        terminal_count INTEGER NOT NULL,
        batch_state TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_order_batch_children (
        batch_id TEXT NOT NULL REFERENCES quant.paper_order_batches(batch_id),
        child_index INTEGER NOT NULL,
        intent_id BIGINT,
        command_id TEXT NOT NULL UNIQUE,
        order_id TEXT,
        disposition TEXT NOT NULL,
        command_state TEXT NOT NULL,
        reason TEXT NOT NULL,
        effective_ts_ns BIGINT,
        ledger_eligible BOOLEAN NOT NULL,
        execution_status TEXT,
        audit_key TEXT,
        ledger_status TEXT NOT NULL DEFAULT 'PENDING',
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (batch_id, child_index)
    )
    """,
    "ALTER TABLE quant.paper_order_batch_children ADD COLUMN IF NOT EXISTS intent_id BIGINT",
    """
    CREATE TABLE IF NOT EXISTS quant.portfolio_scenario_results (
        run_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        scenario_id TEXT NOT NULL,
        valuation_ts TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        cash_value NUMERIC NOT NULL,
        position_value NUMERIC,
        nav NUMERIC,
        max_loss NUMERIC,
        locked_capital NUMERIC NOT NULL DEFAULT 0,
        unmarked_value NUMERIC NOT NULL DEFAULT 0,
        model_versions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        PRIMARY KEY (run_id, account_id, scenario_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.execution_tca (
        order_id TEXT PRIMARY KEY,
        decision_price NUMERIC,
        arrival_price NUMERIC,
        fill_vwap NUMERIC,
        spread_cost NUMERIC NOT NULL DEFAULT 0,
        delay_cost NUMERIC NOT NULL,
        book_walk_cost NUMERIC NOT NULL,
        fee_cost NUMERIC NOT NULL,
        opportunity_cost NUMERIC NOT NULL,
        markouts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        capacity_status TEXT NOT NULL,
        fidelity_level TEXT NOT NULL,
        strategy_id TEXT,
        intent_id BIGINT,
        audit_key TEXT,
        side TEXT,
        requested_size NUMERIC,
        filled_size NUMERIC,
        unfilled_quantity NUMERIC,
        implementation_shortfall NUMERIC,
        status TEXT NOT NULL DEFAULT 'LEGACY_COMPLETE',
        reasons TEXT[] NOT NULL DEFAULT '{}',
        decision_checkpoint_id TEXT,
        arrival_checkpoint_id TEXT,
        venue_regime_id TEXT,
        venue_regime_source_hash TEXT,
        model_version TEXT,
        source_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "ALTER TABLE quant.execution_tca ALTER COLUMN decision_price DROP NOT NULL",
    "ALTER TABLE quant.execution_tca ALTER COLUMN arrival_price DROP NOT NULL",
    "ALTER TABLE quant.execution_tca ALTER COLUMN delay_cost DROP NOT NULL",
    "ALTER TABLE quant.execution_tca ALTER COLUMN book_walk_cost DROP NOT NULL",
    "ALTER TABLE quant.execution_tca ALTER COLUMN fee_cost DROP NOT NULL",
    "ALTER TABLE quant.execution_tca ALTER COLUMN opportunity_cost DROP NOT NULL",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS strategy_id TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS intent_id BIGINT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS audit_key TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS side TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS requested_size NUMERIC",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS filled_size NUMERIC",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS unfilled_quantity NUMERIC",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS implementation_shortfall NUMERIC",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'LEGACY_COMPLETE'",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS reasons TEXT[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS decision_checkpoint_id TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS arrival_checkpoint_id TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS venue_regime_id TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS venue_regime_source_hash TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS model_version TEXT",
    "ALTER TABLE quant.execution_tca ADD COLUMN IF NOT EXISTS source_payload JSONB NOT NULL DEFAULT '{}'::jsonb",
    """
    CREATE TABLE IF NOT EXISTS quant.benchmark_episode_runs (
        run_id TEXT PRIMARY KEY,
        episode_id TEXT NOT NULL,
        code_commit TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        data_hash TEXT NOT NULL,
        result_hash TEXT NOT NULL,
        reference_result_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        diff_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_degradation_events (
        transition_key TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        identifier TEXT NOT NULL,
        previous_state TEXT NOT NULL,
        state TEXT NOT NULL,
        signal TEXT NOT NULL,
        requires_global_kill_switch BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_sim_events_run_time ON quant.paper_sim_events (run_id, event_ts_ns)",
    "CREATE INDEX IF NOT EXISTS idx_quant_inflight_command_state ON quant.paper_inflight_commands (state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_quant_batch_children_status ON quant.paper_order_batch_children (batch_id, ledger_status, child_index)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_quant_batch_children_intent ON quant.paper_order_batch_children (intent_id) WHERE intent_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_quant_scenario_run_account ON quant.portfolio_scenario_results (run_id, account_id)",
    "CREATE INDEX IF NOT EXISTS idx_quant_degradation_scope_identifier ON quant.simulator_degradation_events (scope, identifier, created_at DESC)",
)


@dataclass(frozen=True)
class PortfolioScenarioArtifact:
    """A durable, explicitly valued scenario outcome for one paper account."""

    run_id: str
    account_id: str
    scenario_id: str
    cash_value: Decimal
    position_value: Decimal | None
    nav: Decimal | None
    max_loss: Decimal | None
    locked_capital: Decimal = Decimal(0)
    unmarked_value: Decimal = Decimal(0)
    model_versions: Mapping[str, str] | None = None

    def validate(self) -> None:
        required = {
            "run_id": self.run_id,
            "account_id": self.account_id,
            "scenario_id": self.scenario_id,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"portfolio scenario missing required fields: {missing}")
        if Decimal(self.locked_capital) < 0 or Decimal(self.unmarked_value) < 0:
            raise ValueError("locked_capital and unmarked_value must be non-negative")


class PostgresSimulatorArtifactStore:
    """Small adapter: simulation logic never embeds SQL or direct database calls."""

    def __init__(self, connection_factory: Any = postgres_connection) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SIMULATOR_ARTIFACT_SCHEMA:
                cur.execute(statement)
            conn.commit()

    def persist_event(self, event: SimEvent, *, run_id: str) -> bool:
        values = event.to_dict()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_sim_events (
                    event_id,event_type,event_ts_ns,receive_ts_ns,priority,
                    source_sequence,deterministic_tiebreaker,aggregate_key,
                    payload_json,source_event_id,model_version,run_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.event_ts_ns,
                    event.receive_ts_ns,
                    event.priority,
                    event.source_sequence,
                    event.deterministic_tiebreaker,
                    event.aggregate_key,
                    json.dumps(values["payload"], default=str),
                    event.source_event_id,
                    event.model_version,
                    str(run_id),
                ),
            )
            inserted = int(cur.rowcount or 0) > 0
            if not inserted:
                cur.execute(
                    """
                    SELECT event_type,event_ts_ns,receive_ts_ns,priority,
                           source_sequence,deterministic_tiebreaker,aggregate_key,
                           payload_json,source_event_id,model_version,run_id
                    FROM quant.paper_sim_events
                    WHERE event_id=%s
                    """,
                    (event.event_id,),
                )
                existing = cur.fetchone()
                expected = {
                    "event_type": event.event_type,
                    "event_ts_ns": event.event_ts_ns,
                    "receive_ts_ns": event.receive_ts_ns,
                    "priority": event.priority,
                    "source_sequence": event.source_sequence,
                    "deterministic_tiebreaker": event.deterministic_tiebreaker,
                    "aggregate_key": event.aggregate_key,
                    "payload_json": values["payload"],
                    "source_event_id": event.source_event_id,
                    "model_version": event.model_version,
                    "run_id": str(run_id),
                }
                actual = dict(existing) if existing is not None else {}
                if actual != expected:
                    raise ValueError(
                        f"simulator event identity changed on replay: {event.event_id}"
                    )
            conn.commit()
        return inserted

    def persist_inflight(
        self,
        item: InFlightCommand,
        *,
        send_ts_ns: int | None = None,
        arrival_ts_ns: int | None = None,
        ack_ts_ns: int | None = None,
        response_ts_ns: int | None = None,
        venue_state_at_send: str | None = None,
        rate_limit_bucket: str | None = None,
        latency_model_version: str | None = None,
        reconciliation_status: str | None = None,
    ) -> None:
        command = item.command
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_inflight_commands (
                    command_id,order_id,command_type,state,account_id,signer_id,ip_id,
                    created_ts_ns,send_ts_ns,arrival_ts_ns,ack_ts_ns,response_ts_ns,
                    venue_state_at_send,rate_limit_bucket,latency_model_version,
                    unknown_outcome,reconciliation_status,payload_json
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (command_id) DO UPDATE SET
                    state=EXCLUDED.state, send_ts_ns=COALESCE(EXCLUDED.send_ts_ns, quant.paper_inflight_commands.send_ts_ns),
                    arrival_ts_ns=COALESCE(EXCLUDED.arrival_ts_ns, quant.paper_inflight_commands.arrival_ts_ns),
                    ack_ts_ns=COALESCE(EXCLUDED.ack_ts_ns, quant.paper_inflight_commands.ack_ts_ns),
                    response_ts_ns=COALESCE(EXCLUDED.response_ts_ns, quant.paper_inflight_commands.response_ts_ns),
                    venue_state_at_send=COALESCE(EXCLUDED.venue_state_at_send, quant.paper_inflight_commands.venue_state_at_send),
                    rate_limit_bucket=COALESCE(EXCLUDED.rate_limit_bucket, quant.paper_inflight_commands.rate_limit_bucket),
                    latency_model_version=COALESCE(EXCLUDED.latency_model_version, quant.paper_inflight_commands.latency_model_version),
                    unknown_outcome=EXCLUDED.unknown_outcome,
                    reconciliation_status=COALESCE(EXCLUDED.reconciliation_status, quant.paper_inflight_commands.reconciliation_status),
                    payload_json=EXCLUDED.payload_json, updated_at=clock_timestamp()
                """,
                (
                    command.command_id,
                    command.order_id,
                    command.command_type.value,
                    item.state.value,
                    command.account_id,
                    command.signer_id,
                    command.ip_id,
                    command.created_ts_ns,
                    send_ts_ns,
                    arrival_ts_ns,
                    ack_ts_ns,
                    response_ts_ns,
                    venue_state_at_send,
                    rate_limit_bucket,
                    latency_model_version,
                    item.state.value == "SUBMIT_OUTCOME_UNKNOWN",
                    reconciliation_status,
                    json.dumps(dict(command.payload), default=str),
                ),
            )
            conn.commit()

    def persist_shadow_run_heartbeat(
        self,
        *,
        run_id: str,
        heartbeat_ts_ns: int,
        status: str = "RUNNING",
    ) -> None:
        if not str(run_id).strip() or int(heartbeat_ts_ns) < 0:
            raise ValueError("valid run_id and heartbeat_ts_ns are required")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_venue_shadow_runs (
                    run_id, heartbeat_ts_ns, status
                ) VALUES (%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET
                    heartbeat_ts_ns=GREATEST(
                        quant.paper_venue_shadow_runs.heartbeat_ts_ns,
                        EXCLUDED.heartbeat_ts_ns
                    ),
                    status=EXCLUDED.status,
                    updated_at=clock_timestamp()
                """,
                (str(run_id), int(heartbeat_ts_ns), str(status)),
            )
            conn.commit()

    def persist_order_batch(
        self,
        batch: PaperOrderBatch,
        result: PaperOrderBatchResult,
        *,
        strategy_id: str,
        config_hash: str,
    ) -> None:
        """Persist one non-atomic batch and each independent gateway result."""

        if result.batch_id != batch.batch_id:
            raise ValueError("batch result id does not match batch")
        if not str(strategy_id).strip() or not str(config_hash).strip():
            raise ValueError("strategy_id and config_hash are required")
        expected = [item.command_id for item in batch.orders]
        actual = [item.command_id for item in result.child_results]
        if result.response_complete and expected != actual:
            raise ValueError("batch child results must preserve command order")
        if not set(actual).issubset(expected):
            raise ValueError("batch result contains an unknown command_id")

        eligible = result.ledger_eligible_command_ids()
        decisions = {decision.command_id: decision for decision in result.child_results}
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT child_index, command_id
                FROM quant.paper_order_batch_children
                WHERE batch_id = %s
                ORDER BY child_index
                FOR UPDATE
                """,
                (batch.batch_id,),
            )
            existing = {
                int(row["child_index"]): str(row["command_id"])
                for row in cur.fetchall()
            }
            for child_index, command_id in enumerate(expected):
                if child_index in existing and existing[child_index] != command_id:
                    raise ValueError("batch child identity is immutable")

            cur.execute(
                """
                INSERT INTO quant.paper_order_batches (
                    batch_id,account_id,strategy_id,submitted_ts_ns,arrival_ts_ns,
                    child_count,terminal_count,batch_state,config_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (batch_id) DO UPDATE SET
                    arrival_ts_ns=COALESCE(
                        EXCLUDED.arrival_ts_ns,
                        quant.paper_order_batches.arrival_ts_ns
                    ),
                    terminal_count=GREATEST(
                        quant.paper_order_batches.terminal_count,
                        EXCLUDED.terminal_count
                    ),
                    batch_state=EXCLUDED.batch_state,
                    updated_at=clock_timestamp()
                WHERE quant.paper_order_batches.account_id=EXCLUDED.account_id
                  AND quant.paper_order_batches.strategy_id=EXCLUDED.strategy_id
                  AND quant.paper_order_batches.child_count=EXCLUDED.child_count
                  AND quant.paper_order_batches.config_hash=EXCLUDED.config_hash
                """,
                (
                    batch.batch_id,
                    batch.account_id,
                    str(strategy_id),
                    batch.submitted_ts_ns,
                    batch.arrival_ts_ns,
                    len(batch.orders),
                    result.terminal_count,
                    result.batch_state,
                    str(config_hash),
                ),
            )
            if int(cur.rowcount or 0) != 1:
                raise ValueError("batch identity or configuration changed on replay")

            for child_index, command in enumerate(batch.orders):
                decision = decisions.get(command.command_id)
                if decision is None:
                    cur.execute(
                        """
                        INSERT INTO quant.paper_order_batch_children (
                            batch_id,child_index,command_id,order_id,disposition,
                            command_state,reason,ledger_eligible,ledger_status,
                            result_json
                        ) VALUES (
                            %s,%s,%s,%s,'OUTCOME_UNKNOWN',
                            'SUBMIT_OUTCOME_UNKNOWN','batch_result_missing',FALSE,
                            'WAITING_RECONCILIATION',%s::jsonb
                        )
                        ON CONFLICT (batch_id,child_index) DO NOTHING
                        """,
                        (
                            batch.batch_id,
                            child_index,
                            command.command_id,
                            command.order_id,
                            json.dumps(
                                {
                                    "command_id": command.command_id,
                                    "response_missing": True,
                                }
                            ),
                        ),
                    )
                    continue
                ledger_status = (
                    "PENDING" if command.command_id in eligible else "NOT_ELIGIBLE"
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_order_batch_children (
                        batch_id,child_index,command_id,order_id,disposition,
                        command_state,reason,effective_ts_ns,ledger_eligible,
                        ledger_status,result_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (batch_id,child_index) DO UPDATE SET
                        disposition=EXCLUDED.disposition,
                        command_state=EXCLUDED.command_state,
                        reason=EXCLUDED.reason,
                        effective_ts_ns=COALESCE(
                            EXCLUDED.effective_ts_ns,
                            quant.paper_order_batch_children.effective_ts_ns
                        ),
                        ledger_eligible=EXCLUDED.ledger_eligible,
                        ledger_status=CASE
                            WHEN quant.paper_order_batch_children.ledger_status
                                 IN ('APPLIED','NO_EFFECT')
                            THEN quant.paper_order_batch_children.ledger_status
                            ELSE EXCLUDED.ledger_status
                        END,
                        result_json=EXCLUDED.result_json,
                        updated_at=clock_timestamp()
                    WHERE quant.paper_order_batch_children.command_id=EXCLUDED.command_id
                    """,
                    (
                        batch.batch_id,
                        child_index,
                        command.command_id,
                        command.order_id,
                        decision.disposition.value,
                        decision.state.value,
                        decision.reason,
                        decision.effective_ts_ns,
                        command.command_id in eligible,
                        ledger_status,
                        json.dumps(
                            {
                                "command_id": decision.command_id,
                                "disposition": decision.disposition.value,
                                "state": decision.state.value,
                                "reason": decision.reason,
                                "effective_ts_ns": decision.effective_ts_ns,
                            },
                            default=str,
                        ),
                    ),
                )
                if int(cur.rowcount or 0) != 1:
                    raise ValueError("batch child command changed on replay")
            conn.commit()

    def batch_ledger_eligible_command_ids(
        self,
        *,
        batch_id: str,
        command_ids: tuple[str, ...],
    ) -> frozenset[str]:
        if not str(batch_id).strip() or not command_ids:
            return frozenset()
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT command_id
                FROM quant.paper_order_batch_children
                WHERE batch_id = %s
                  AND command_id = ANY(%s)
                  AND ledger_eligible
                  AND ledger_status NOT IN ('APPLIED','NO_EFFECT')
                """,
                (str(batch_id), list(command_ids)),
            )
            return frozenset(str(row["command_id"]) for row in cur.fetchall())

    def mark_batch_child_execution(
        self,
        *,
        batch_id: str,
        command_id: str,
        execution_status: str,
        audit_key: str,
        ledger_status: str,
        result_payload: Mapping[str, Any],
    ) -> None:
        allowed = {"APPLIED", "NO_EFFECT", "FAILED"}
        if str(ledger_status) not in allowed:
            raise ValueError(f"unsupported child ledger status: {ledger_status}")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_order_batch_children
                SET execution_status=%s,
                    audit_key=%s,
                    ledger_status=CASE
                        WHEN ledger_status IN ('APPLIED','NO_EFFECT')
                        THEN ledger_status
                        ELSE %s
                    END,
                    result_json=result_json || %s::jsonb,
                    updated_at=clock_timestamp()
                WHERE batch_id=%s AND command_id=%s AND ledger_eligible
                """,
                (
                    str(execution_status),
                    str(audit_key),
                    str(ledger_status),
                    json.dumps(dict(result_payload), default=str),
                    str(batch_id),
                    str(command_id),
                ),
            )
            if int(cur.rowcount or 0) != 1:
                raise ValueError("batch child is missing or not ledger eligible")
            conn.commit()

    def persist_tca(
        self,
        *,
        order_id: str,
        decision_price: Decimal,
        arrival_price: Decimal,
        fill_vwap: Decimal | None,
        report: TcaReport,
        capacity_status: str,
        fidelity_level: str,
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.execution_tca (
                    order_id,decision_price,arrival_price,fill_vwap,delay_cost,
                    book_walk_cost,fee_cost,opportunity_cost,markouts_json,
                    capacity_status,fidelity_level
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (order_id) DO UPDATE SET
                    decision_price=EXCLUDED.decision_price, arrival_price=EXCLUDED.arrival_price,
                    fill_vwap=EXCLUDED.fill_vwap, delay_cost=EXCLUDED.delay_cost,
                    book_walk_cost=EXCLUDED.book_walk_cost, fee_cost=EXCLUDED.fee_cost,
                    opportunity_cost=EXCLUDED.opportunity_cost, markouts_json=EXCLUDED.markouts_json,
                    capacity_status=EXCLUDED.capacity_status, fidelity_level=EXCLUDED.fidelity_level,
                    updated_at=clock_timestamp()
                """,
                (
                    str(order_id),
                    decision_price,
                    arrival_price,
                    fill_vwap,
                    report.delay_cost,
                    report.book_walk_cost,
                    report.fee_cost,
                    report.opportunity_cost,
                    json.dumps(
                        {
                            key: str(value)
                            for key, value in report.adverse_selection_markouts.items()
                        }
                    ),
                    str(capacity_status),
                    str(fidelity_level),
                ),
            )
            conn.commit()

    def persist_paper_tca(self, artifact: PaperTcaArtifact) -> None:
        report = artifact.report
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.execution_tca (
                    order_id,decision_price,arrival_price,fill_vwap,delay_cost,
                    book_walk_cost,fee_cost,opportunity_cost,markouts_json,
                    capacity_status,fidelity_level,strategy_id,intent_id,audit_key,
                    side,requested_size,filled_size,unfilled_quantity,
                    implementation_shortfall,status,reasons,decision_checkpoint_id,
                    arrival_checkpoint_id,venue_regime_id,
                    venue_regime_source_hash,model_version,source_payload
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                )
                ON CONFLICT (order_id) DO UPDATE SET
                    decision_price=EXCLUDED.decision_price,
                    arrival_price=EXCLUDED.arrival_price,
                    fill_vwap=EXCLUDED.fill_vwap,
                    delay_cost=EXCLUDED.delay_cost,
                    book_walk_cost=EXCLUDED.book_walk_cost,
                    fee_cost=EXCLUDED.fee_cost,
                    opportunity_cost=EXCLUDED.opportunity_cost,
                    markouts_json=EXCLUDED.markouts_json,
                    capacity_status=EXCLUDED.capacity_status,
                    fidelity_level=EXCLUDED.fidelity_level,
                    strategy_id=EXCLUDED.strategy_id,
                    intent_id=EXCLUDED.intent_id,
                    audit_key=EXCLUDED.audit_key,
                    side=EXCLUDED.side,
                    requested_size=EXCLUDED.requested_size,
                    filled_size=EXCLUDED.filled_size,
                    unfilled_quantity=EXCLUDED.unfilled_quantity,
                    implementation_shortfall=EXCLUDED.implementation_shortfall,
                    status=EXCLUDED.status,
                    reasons=EXCLUDED.reasons,
                    decision_checkpoint_id=EXCLUDED.decision_checkpoint_id,
                    arrival_checkpoint_id=EXCLUDED.arrival_checkpoint_id,
                    venue_regime_id=EXCLUDED.venue_regime_id,
                    venue_regime_source_hash=EXCLUDED.venue_regime_source_hash,
                    model_version=EXCLUDED.model_version,
                    source_payload=EXCLUDED.source_payload,
                    updated_at=clock_timestamp()
                """,
                (
                    artifact.order_id,
                    artifact.decision_price,
                    artifact.arrival_price,
                    artifact.fill_vwap,
                    report.delay_cost if report is not None else None,
                    report.book_walk_cost if report is not None else None,
                    report.fee_cost if report is not None else None,
                    report.opportunity_cost if report is not None else None,
                    json.dumps(
                        {
                            key: str(value)
                            for key, value in (
                                report.adverse_selection_markouts.items()
                                if report is not None
                                else ()
                            )
                        }
                    ),
                    artifact.capacity_status,
                    artifact.fidelity_level,
                    artifact.strategy_id,
                    artifact.intent_id,
                    artifact.audit_key,
                    artifact.side,
                    artifact.requested_size,
                    artifact.filled_size,
                    report.unfilled_quantity if report is not None else None,
                    report.implementation_shortfall if report is not None else None,
                    artifact.status,
                    list(artifact.reasons),
                    artifact.decision_checkpoint_id,
                    artifact.arrival_checkpoint_id,
                    artifact.venue_regime_id,
                    artifact.venue_regime_source_hash,
                    artifact.model_version,
                    json.dumps(artifact.as_dict(), default=str),
                ),
            )
            conn.commit()

    def persist_portfolio_scenario(self, artifact: PortfolioScenarioArtifact) -> None:
        """Idempotently store a valuation scenario, without inferring a mark."""

        artifact.validate()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.portfolio_scenario_results (
                    run_id,account_id,scenario_id,cash_value,position_value,nav,max_loss,
                    locked_capital,unmarked_value,model_versions_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (run_id,account_id,scenario_id) DO UPDATE SET
                    cash_value=EXCLUDED.cash_value,
                    position_value=EXCLUDED.position_value,
                    nav=EXCLUDED.nav,
                    max_loss=EXCLUDED.max_loss,
                    locked_capital=EXCLUDED.locked_capital,
                    unmarked_value=EXCLUDED.unmarked_value,
                    model_versions_json=EXCLUDED.model_versions_json,
                    valuation_ts=clock_timestamp()
                """,
                (
                    str(artifact.run_id),
                    str(artifact.account_id),
                    str(artifact.scenario_id),
                    Decimal(artifact.cash_value),
                    None
                    if artifact.position_value is None
                    else Decimal(artifact.position_value),
                    None if artifact.nav is None else Decimal(artifact.nav),
                    None if artifact.max_loss is None else Decimal(artifact.max_loss),
                    Decimal(artifact.locked_capital),
                    Decimal(artifact.unmarked_value),
                    json.dumps(dict(artifact.model_versions or {}), default=str),
                ),
            )
            conn.commit()

    def persist_benchmark_run(self, payload: Mapping[str, Any]) -> None:
        required = (
            "run_id",
            "episode_id",
            "code_commit",
            "config_hash",
            "data_hash",
            "result_hash",
            "reference_result_hash",
            "status",
        )
        missing = [
            name for name in required if not str(payload.get(name) or "").strip()
        ]
        if missing:
            raise ValueError(f"benchmark run missing required fields: {missing}")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.benchmark_episode_runs (
                    run_id,episode_id,code_commit,config_hash,data_hash,result_hash,
                    reference_result_hash,status,diff_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (run_id) DO NOTHING
                """,
                tuple(str(payload[name]) for name in required)
                + (json.dumps(payload.get("diff") or {}, default=str),),
            )
            conn.commit()

    def persist_degradation(
        self, transition: DegradationTransition, *, transition_key: str
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_degradation_events (
                    transition_key,scope,identifier,previous_state,state,signal,
                    requires_global_kill_switch
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (transition_key) DO NOTHING
                """,
                (
                    str(transition_key),
                    transition.scope.value,
                    transition.identifier,
                    transition.previous_state,
                    transition.state,
                    transition.signal,
                    transition.requires_global_kill_switch,
                ),
            )
            conn.commit()

    def load_latest_degradations(self) -> tuple[DegradationTransition, ...]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (scope,identifier)
                       scope,identifier,previous_state,state,signal,
                       requires_global_kill_switch
                FROM quant.simulator_degradation_events
                ORDER BY scope,identifier,created_at DESC,transition_key DESC
                """
            )
            rows = cur.fetchall()
        return tuple(
            DegradationTransition(
                scope=DegradationScope(str(row["scope"])),
                identifier=str(row["identifier"]),
                previous_state=str(row["previous_state"]),
                state=str(row["state"]),
                signal=str(row["signal"]),
                requires_global_kill_switch=bool(row["requires_global_kill_switch"]),
            )
            for row in rows
        )
