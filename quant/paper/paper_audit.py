"""Idempotent Postgres audit sink for taker-only paper execution."""

from __future__ import annotations

import json
from typing import Any

from quant.core.db import postgres_connection

from .taker_execution import PaperExecutionResult


PAPER_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS quant.paper_taker_order_audits (
    audit_key TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    side TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    decision_ts TIMESTAMPTZ NOT NULL,
    arrival_ts TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_checkpoint_id TEXT,
    arrival_checkpoint_id TEXT,
    book_generation BIGINT,
    coverage_grade TEXT,
    book_age_ms BIGINT,
    source_manifest_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_files JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_event_start TEXT,
    source_event_end TEXT,
    rest_audit_at TIMESTAMPTZ,
    requested_size NUMERIC NOT NULL,
    filled_size NUMERIC NOT NULL,
    remaining_size NUMERIC NOT NULL,
    avg_fill_price NUMERIC,
    total_fee NUMERIC NOT NULL,
    slippage NUMERIC,
    fills JSONB NOT NULL,
    intent JSONB NOT NULL,
    fidelity JSONB NOT NULL DEFAULT '{}'::jsonb,
    model_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
)
"""

PAPER_AUDIT_MIGRATIONS = (
    "ALTER TABLE quant.paper_taker_order_audits ADD COLUMN IF NOT EXISTS fidelity JSONB NOT NULL DEFAULT '{}'::jsonb",
)


def ensure_paper_audit_schema(cur: Any) -> None:
    cur.execute(PAPER_AUDIT_SCHEMA)
    for statement in PAPER_AUDIT_MIGRATIONS:
        cur.execute(statement)


class PostgresPaperAuditSink:
    def __init__(self, connection_factory: Any = postgres_connection) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            ensure_paper_audit_schema(cur)
            conn.commit()

    def append(self, result: PaperExecutionResult) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            insert_paper_audit(cur, result)
            conn.commit()


def insert_paper_audit(cur: Any, result: PaperExecutionResult) -> bool:
    """Insert one immutable audit row using the caller's transaction."""
    payload = result.as_dict()
    intent = payload["intent"]
    cur.execute(
        """
        INSERT INTO quant.paper_taker_order_audits (
            audit_key, strategy_id, client_order_id, market_id, condition_id,
            asset_id, side, time_in_force, decision_ts, arrival_ts,
            status, reason, decision_checkpoint_id, arrival_checkpoint_id,
            book_generation, coverage_grade, book_age_ms, source_manifest_ids,
            source_files, source_event_start, source_event_end, rest_audit_at,
            requested_size, filled_size, remaining_size, avg_fill_price,
            total_fee, slippage, fills, intent, fidelity, model_version, config_hash
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s::jsonb,
            %s::jsonb, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s
        )
        ON CONFLICT (audit_key) DO NOTHING
        """,
        (
            result.audit_key, intent["strategy_id"], intent["client_order_id"], intent["market_id"], intent["condition_id"],
            intent["asset_id"], intent["side"], intent["order_type"], intent["decision_ts"], payload["arrival_ts"],
            result.status, result.reason, result.decision_checkpoint_id, result.arrival_checkpoint_id,
            result.book_generation, result.coverage_grade, result.book_age_ms, json.dumps(payload["source_manifest_ids"]),
            json.dumps(payload["source_files"]), result.source_event_start, result.source_event_end, payload["rest_audit_at"],
            intent["size"], payload["filled_size"], payload["remaining_size"], payload["avg_fill_price"],
            payload["total_fee"], payload["slippage"], json.dumps(payload["fills"]), json.dumps(intent), json.dumps(payload["fidelity"]), result.model_version, result.config_hash,
        ),
    )
    return int(cur.rowcount or 0) > 0
