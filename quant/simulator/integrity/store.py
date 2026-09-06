"""Append-only integrity evidence and human-reviewed investigation cases."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from quant.core.db import postgres_connection
from quant.simulator.admission.domain import stable_hash

from .models import CaseStatus, IntegrityCase, IntegrityEvidence, IntegrityFindingType

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_integrity_cases (
        case_id TEXT PRIMARY KEY,
        finding_type TEXT NOT NULL,
        account_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        status TEXT NOT NULL,
        severity TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        assigned_to TEXT,
        resolution_reason TEXT,
        opened_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_integrity_cases_open_idx
    ON quant.simulator_integrity_cases(account_id,status,updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_integrity_evidence (
        evidence_id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL,
        finding_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        account_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        observation_ids JSONB NOT NULL,
        reason_codes JSONB NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_integrity_case_actions (
        action_id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        action_ts TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
)


class PostgresIntegrityCaseStore:
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

    def record_evidence(
        self,
        evidence: IntegrityEvidence,
        *,
        policy_version: str,
    ) -> IntegrityCase:
        case_id = stable_hash(
            {
                "finding_type": evidence.finding_type.value,
                "account_id": evidence.account_id,
                "condition_id": evidence.condition_id,
            },
            prefix="integrity-case-",
        )
        digest = stable_hash(evidence.payload)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_integrity_cases (
                    case_id,finding_type,account_id,condition_id,status,severity,
                    policy_version,opened_at
                ) VALUES (%s,%s,%s,%s,'OPEN',%s,%s,%s)
                ON CONFLICT (case_id) DO UPDATE SET
                    severity=CASE
                        WHEN quant.simulator_integrity_cases.severity='HIGH'
                        THEN quant.simulator_integrity_cases.severity
                        ELSE EXCLUDED.severity
                    END,
                    updated_at=clock_timestamp()
                """,
                (
                    case_id,
                    evidence.finding_type.value,
                    evidence.account_id,
                    evidence.condition_id,
                    evidence.severity,
                    policy_version,
                    evidence.observed_at,
                ),
            )
            cur.execute(
                """
                INSERT INTO quant.simulator_integrity_evidence (
                    evidence_id,case_id,finding_type,severity,account_id,
                    condition_id,observed_at,observation_ids,reason_codes,
                    payload_hash,payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)
                ON CONFLICT (evidence_id) DO NOTHING
                """,
                (
                    evidence.evidence_id,
                    case_id,
                    evidence.finding_type.value,
                    evidence.severity,
                    evidence.account_id,
                    evidence.condition_id,
                    evidence.observed_at,
                    json.dumps(evidence.observation_ids),
                    json.dumps(evidence.reason_codes),
                    digest,
                    json.dumps(evidence.payload),
                ),
            )
        return self.get(case_id)

    def transition(
        self,
        *,
        case_id: str,
        to_status: CaseStatus,
        actor: str,
        reason: str,
        action_ts: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> IntegrityCase:
        if not actor or not reason:
            raise ValueError("integrity case review requires actor and reason")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM quant.simulator_integrity_cases WHERE case_id=%s FOR UPDATE",
                (case_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown integrity case: {case_id}")
            before = CaseStatus(str(row["status"]))
            _validate_transition(before, to_status)
            action_payload = dict(payload or {})
            action_id = stable_hash(
                {
                    "case_id": case_id,
                    "from": before.value,
                    "to": to_status.value,
                    "actor": actor,
                    "reason": reason,
                    "action_ts": action_ts,
                },
                prefix="integrity-action-",
            )
            cur.execute(
                """
                UPDATE quant.simulator_integrity_cases
                SET status=%s,assigned_to=CASE WHEN %s='TRIAGED' THEN %s
                                               ELSE assigned_to END,
                    resolution_reason=CASE WHEN %s IN ('DISMISSED','CONFIRMED','REMEDIATED')
                                           THEN %s ELSE resolution_reason END,
                    updated_at=%s
                WHERE case_id=%s
                """,
                (
                    to_status.value,
                    to_status.value,
                    actor,
                    to_status.value,
                    reason,
                    action_ts,
                    case_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO quant.simulator_integrity_case_actions (
                    action_id,case_id,from_status,to_status,actor,reason,
                    action_ts,payload_hash,payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (action_id) DO NOTHING
                """,
                (
                    action_id,
                    case_id,
                    before.value,
                    to_status.value,
                    actor,
                    reason,
                    action_ts,
                    stable_hash(action_payload),
                    json.dumps(action_payload),
                ),
            )
        return self.get(case_id)

    def get(self, case_id: str) -> IntegrityCase:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_integrity_cases WHERE case_id=%s",
                (case_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown integrity case: {case_id}")
        return IntegrityCase(
            case_id=str(row["case_id"]),
            finding_type=IntegrityFindingType(str(row["finding_type"])),
            account_id=str(row["account_id"]),
            condition_id=str(row["condition_id"]),
            status=CaseStatus(str(row["status"])),
            severity=str(row["severity"]),
            policy_version=str(row["policy_version"]),
            opened_at=row["opened_at"],
            assigned_to=row["assigned_to"],
            resolution_reason=row["resolution_reason"],
            updated_at=row["updated_at"],
        )


def _validate_transition(before: CaseStatus, after: CaseStatus) -> None:
    allowed = {
        CaseStatus.OPEN: {CaseStatus.TRIAGED, CaseStatus.DISMISSED},
        CaseStatus.TRIAGED: {CaseStatus.INVESTIGATING, CaseStatus.DISMISSED},
        CaseStatus.INVESTIGATING: {CaseStatus.CONFIRMED, CaseStatus.DISMISSED},
        CaseStatus.CONFIRMED: {CaseStatus.REMEDIATED},
    }
    if after not in allowed.get(before, set()):
        raise ValueError(f"invalid integrity case transition {before}->{after}")
