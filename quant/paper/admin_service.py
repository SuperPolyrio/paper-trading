"""Tenant-scoped administration, reconciliation, retention, and evidence bundles."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from .live_shadow_store import LiveShadowStore
from .tenant_platform import (
    PaperPermission,
    PostgresTenantPlatformStore,
    TenantPrincipal,
)

ADMIN_SCHEMA_VERSION = "paper-admin-governance-v1"
BUNDLE_SCHEMA_VERSION = "paper-evidence-bundle-v1"
RETENTION_POLICY_VERSION = "paper-retention-v1"

DEFAULT_RETENTION_DAYS: Mapping[str, int] = {
    "API_REQUEST_LOG": 30,
    "IDEMPOTENCY": 7,
    "ADMIN_JOBS": 90,
    "DLQ_RESOLVED": 90,
    "EVIDENCE_BUNDLES": 30,
    "REPLAY_SESSIONS": 180,
    "MAINTENANCE_NOTICES": 365,
}


class PaperAdminError(RuntimeError):
    pass


class PaperAdminNotFound(PaperAdminError):
    pass


class PaperAdminConflict(PaperAdminError):
    pass


class PaperAdminValidationError(PaperAdminError):
    pass


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _parquet_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - deployment dependency gate
        raise PaperAdminError("pyarrow is required for evidence bundle export") from exc
    normalized = []
    for row in rows:
        normalized.append(
            {
                str(key): (
                    json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))
                    if isinstance(value, (Mapping, list, tuple))
                    else _json_value(value)
                )
                for key, value in row.items()
            }
        )
    table = pa.Table.from_pylist(normalized) if normalized else pa.table({"_empty": pa.array([], type=pa.string())})
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd", use_dictionary=True)
    return output.getvalue().to_pybytes()


class PostgresPaperAdminService:
    def __init__(
        self,
        tenant_store: PostgresTenantPlatformStore,
        *,
        live_store: LiveShadowStore | None = None,
        signing_key: bytes | None = None,
    ) -> None:
        self.tenant_store = tenant_store
        self.live_store = live_store or LiveShadowStore()
        self.signing_key = bytes(signing_key or b"")

    def dashboard(self, principal: TenantPrincipal) -> dict[str, Any]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                "SELECT tenant_id,name,status,retention_until,created_at,updated_at "
                "FROM quant.paper_tenants WHERE tenant_id=%s",
                (principal.tenant_id,),
            )
            tenant = cur.fetchone()
            if tenant is None:
                raise PaperAdminNotFound("tenant was not found")
            counts: dict[str, dict[str, int]] = {}
            for name, table in (
                ("accounts", "paper_account_registry"),
                ("jobs", "paper_admin_jobs"),
                ("dlq", "paper_dlq_events"),
                ("incidents", "paper_incidents"),
                ("notices", "paper_maintenance_notices"),
                ("bundles", "paper_evidence_bundles"),
            ):
                cur.execute(
                    f"SELECT status,count(*) AS count FROM quant.{table} "
                    "WHERE tenant_id=%s GROUP BY status ORDER BY status",
                    (principal.tenant_id,),
                )
                counts[name] = {
                    str(row["status"]): int(row["count"]) for row in cur.fetchall()
                }
            cur.execute(
                """
                SELECT freeze_id,resource_type,resource_id,reason,created_at
                FROM quant.paper_resource_freezes
                WHERE tenant_id=%s AND status='ACTIVE'
                ORDER BY created_at DESC
                """,
                (principal.tenant_id,),
            )
            freezes = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return {
            "schema_version": ADMIN_SCHEMA_VERSION,
            "tenant": _json_value(dict(tenant)),
            "counts": counts,
            "active_freezes": _json_value(freezes),
        }

    def set_tenant_freeze(
        self, principal: TenantPrincipal, *, frozen: bool, reason: str
    ) -> dict[str, Any]:
        selected_reason = str(reason).strip()
        if not selected_reason:
            raise PaperAdminValidationError("freeze reason is required")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                "SELECT status FROM quant.paper_tenants WHERE tenant_id=%s FOR UPDATE",
                (principal.tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise PaperAdminNotFound("tenant was not found")
            if str(row["status"]) in {"DELETION_PENDING", "DELETED"}:
                raise PaperAdminConflict("deleted tenant status cannot be changed")
            if frozen:
                cur.execute(
                    """
                    INSERT INTO quant.paper_resource_freezes (
                        freeze_id,tenant_id,resource_type,resource_id,reason,created_by
                    ) VALUES (%s,%s,'TENANT',%s,%s,%s)
                    ON CONFLICT (tenant_id,resource_type,resource_id)
                    WHERE status='ACTIVE' DO UPDATE SET reason=EXCLUDED.reason
                    """,
                    (
                        uuid4(),
                        principal.tenant_id,
                        str(principal.tenant_id),
                        selected_reason,
                        principal.subject_user_id,
                    ),
                )
                status = "FROZEN"
            else:
                cur.execute(
                    """
                    UPDATE quant.paper_resource_freezes
                    SET status='RELEASED',released_by=%s,released_at=clock_timestamp()
                    WHERE tenant_id=%s AND resource_type='TENANT'
                      AND resource_id=%s AND status='ACTIVE'
                    """,
                    (
                        principal.subject_user_id,
                        principal.tenant_id,
                        str(principal.tenant_id),
                    ),
                )
                status = "ACTIVE"
            cur.execute(
                """
                UPDATE quant.paper_tenants SET status=%s,updated_at=clock_timestamp()
                WHERE tenant_id=%s RETURNING tenant_id,name,status,updated_at
                """,
                (status, principal.tenant_id),
            )
            updated = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="TENANT_FROZEN" if frozen else "TENANT_UNFROZEN",
                resource_type="TENANT",
                resource_id=str(principal.tenant_id),
                reason=selected_reason,
                payload={},
            )
            conn.commit()
        return _json_value(updated)

    def set_account_freeze(
        self,
        principal: TenantPrincipal,
        account_id: UUID,
        *,
        frozen: bool,
        reason: str,
    ) -> dict[str, Any]:
        selected_reason = str(reason).strip()
        if not selected_reason:
            raise PaperAdminValidationError("freeze reason is required")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT account_id,status FROM quant.paper_account_registry
                WHERE tenant_id=%s AND account_id=%s FOR UPDATE
                """,
                (principal.tenant_id, account_id),
            )
            account = cur.fetchone()
            if account is None:
                raise PaperAdminNotFound("account was not found")
            if str(account["status"]) == "CLOSED":
                raise PaperAdminConflict("closed account cannot be unfrozen")
            if frozen:
                cur.execute(
                    """
                    SELECT strategy_id FROM quant.paper_strategies
                    WHERE tenant_id=%s AND account_id=%s AND status='ACTIVE'
                    ORDER BY strategy_id
                    """,
                    (principal.tenant_id, account_id),
                )
                paused_strategy_ids = [str(row["strategy_id"]) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT deployment_id FROM quant.paper_strategy_deployments
                    WHERE tenant_id=%s AND account_id=%s
                      AND status IN ('STARTING','RUNNING')
                    ORDER BY deployment_id
                    """,
                    (principal.tenant_id, account_id),
                )
                paused_deployment_ids = [
                    str(row["deployment_id"]) for row in cur.fetchall()
                ]
                freeze_metadata = {
                    "paused_strategy_ids": paused_strategy_ids,
                    "paused_deployment_ids": paused_deployment_ids,
                }
                cur.execute(
                    """
                    INSERT INTO quant.paper_resource_freezes (
                        freeze_id,tenant_id,resource_type,resource_id,reason,
                        metadata,created_by
                    ) VALUES (%s,%s,'ACCOUNT',%s,%s,%s::jsonb,%s)
                    ON CONFLICT (tenant_id,resource_type,resource_id)
                    WHERE status='ACTIVE' DO UPDATE SET reason=EXCLUDED.reason
                    """,
                    (
                        uuid4(),
                        principal.tenant_id,
                        str(account_id),
                        selected_reason,
                        json.dumps(freeze_metadata, sort_keys=True),
                        principal.subject_user_id,
                    ),
                )
                status = "FROZEN"
            else:
                cur.execute(
                    """
                    UPDATE quant.paper_resource_freezes
                    SET status='RELEASED',released_by=%s,released_at=clock_timestamp()
                    WHERE tenant_id=%s AND resource_type='ACCOUNT'
                      AND resource_id=%s AND status='ACTIVE'
                    RETURNING metadata
                    """,
                    (principal.subject_user_id, principal.tenant_id, str(account_id)),
                )
                released = cur.fetchone()
                release_metadata = dict(released["metadata"] or {}) if released else {}
                status = "ACTIVE"
            cur.execute(
                """
                UPDATE quant.paper_account_registry
                SET status=%s,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND account_id=%s
                RETURNING account_id,name,status,updated_at
                """,
                (status, principal.tenant_id, account_id),
            )
            updated = dict(cur.fetchone())
            if frozen:
                cur.execute(
                    """
                    UPDATE quant.paper_strategies SET status='PAUSED',updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND account_id=%s AND status='ACTIVE'
                    """,
                    (principal.tenant_id, account_id),
                )
            else:
                strategy_ids = [
                    UUID(str(value))
                    for value in release_metadata.get("paused_strategy_ids", [])
                ]
                deployment_ids = [
                    UUID(str(value))
                    for value in release_metadata.get("paused_deployment_ids", [])
                ]
                if strategy_ids:
                    cur.execute(
                        """
                        UPDATE quant.paper_strategies
                        SET status='ACTIVE',updated_at=clock_timestamp()
                        WHERE tenant_id=%s AND account_id=%s
                          AND strategy_id=ANY(%s::uuid[]) AND status='PAUSED'
                        """,
                        (principal.tenant_id, account_id, strategy_ids),
                    )
                if deployment_ids:
                    cur.execute(
                        """
                        UPDATE quant.paper_strategy_deployments
                        SET status='RUNNING',updated_at=clock_timestamp()
                        WHERE tenant_id=%s AND account_id=%s
                          AND deployment_id=ANY(%s::uuid[]) AND status='PAUSED'
                        """,
                        (principal.tenant_id, account_id, deployment_ids),
                    )
                cur.execute(
                    """
                    UPDATE quant.paper_strategy_deployments
                    SET status='PAUSED',updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND account_id=%s
                      AND status IN ('STARTING','RUNNING')
                    """,
                    (principal.tenant_id, account_id),
                )
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="ACCOUNT_FROZEN" if frozen else "ACCOUNT_UNFROZEN",
                resource_type="ACCOUNT",
                resource_id=str(account_id),
                reason=selected_reason,
                payload={},
            )
            conn.commit()
        return _json_value(updated)

    def kill_account(
        self, principal: TenantPrincipal, account_id: UUID, *, reason: str
    ) -> dict[str, Any]:
        account = self.set_account_freeze(
            principal, account_id, frozen=True, reason=f"kill:{str(reason).strip()}"
        )
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT ownership.intent_id
                FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                WHERE ownership.tenant_id=%s AND ownership.account_id=%s
                  AND intent.status IN ('QUEUED','PROCESSING','WORKING')
                ORDER BY ownership.intent_id
                """,
                (principal.tenant_id, account_id),
            )
            intent_ids = [int(row["intent_id"]) for row in cur.fetchall()]
            conn.commit()
        cancelled = sum(
            self.live_store.cancel(intent_id, reason=f"admin_account_kill:{reason}")
            for intent_id in intent_ids
        )
        return {**account, "open_intents": len(intent_ids), "cancel_requested": cancelled}

    def reconcile_account(
        self,
        principal: TenantPrincipal,
        account_id: UUID,
        *,
        mode: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        selected_mode = str(mode).upper()
        if selected_mode not in {"DRY_RUN", "APPLY"}:
            raise PaperAdminValidationError("reconciliation mode must be DRY_RUN or APPLY")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            existing = self._existing_job(cur, principal, idempotency_key)
            if existing is not None:
                conn.commit()
                return _json_value(dict(existing))
            job_id = self._insert_job(
                cur,
                principal,
                job_type="RECONCILIATION",
                mode=selected_mode,
                target_type="ACCOUNT",
                target_id=str(account_id),
                request={"account_id": str(account_id)},
                idempotency_key=idempotency_key,
            )
            result = self._account_reconciliation(cur, principal, account_id)
            result["repair_applied"] = False
            result["repair_policy"] = "COMPENSATING_JOURNAL_REQUIRED"
            cur.execute(
                """
                UPDATE quant.paper_admin_jobs
                SET status='COMPLETED',result=%s::jsonb,started_at=clock_timestamp(),
                    completed_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s RETURNING *
                """,
                (json.dumps(result, sort_keys=True), principal.tenant_id, job_id),
            )
            job = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="ACCOUNT_RECONCILIATION_COMPLETED",
                resource_type="ACCOUNT",
                resource_id=str(account_id),
                reason=selected_mode,
                payload={"status": result["status"], "mismatch_count": len(result["mismatches"])},
            )
            conn.commit()
        return _json_value(job)

    @staticmethod
    def _account_reconciliation(
        cur: Any, principal: TenantPrincipal, account_id: UUID
    ) -> dict[str, Any]:
        cur.execute(
            """
            SELECT registry.ledger_strategy_id,ledger.initial_cash,
                   ledger.cash_balance,ledger.cash_reserved,ledger.realized_pnl
            FROM quant.paper_account_registry registry
            JOIN quant.paper_accounts ledger
              ON ledger.strategy_id=registry.ledger_strategy_id
            WHERE registry.tenant_id=%s AND registry.account_id=%s
            """,
            (principal.tenant_id, account_id),
        )
        account = cur.fetchone()
        if account is None:
            raise PaperAdminNotFound("account was not found")
        strategy_id = str(account["ledger_strategy_id"])
        cur.execute(
            """
            SELECT COALESCE(sum(cash_delta),0) AS cash_delta,
                   COALESCE(sum(realized_pnl_delta),0) AS realized_pnl
            FROM quant.paper_ledger_entries WHERE strategy_id=%s
            """,
            (strategy_id,),
        )
        ledger = cur.fetchone()
        cur.execute(
            """
            SELECT COALESCE(sum(reserved_cash),0) AS reserved_cash
            FROM quant.paper_order_reservations
            WHERE strategy_id=%s AND status='ACTIVE'
            """,
            (strategy_id,),
        )
        reservations = cur.fetchone()
        cur.execute(
            """
            SELECT position.asset_id,position.quantity,
                   COALESCE(sum(entry.shares_delta),0) AS ledger_quantity
            FROM quant.paper_positions position
            LEFT JOIN quant.paper_ledger_entries entry
              ON entry.strategy_id=position.strategy_id
             AND entry.asset_id=position.asset_id
            WHERE position.strategy_id=%s
            GROUP BY position.asset_id,position.quantity
            HAVING position.quantity<>COALESCE(sum(entry.shares_delta),0)
            ORDER BY position.asset_id
            """,
            (strategy_id,),
        )
        position_mismatches = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT journal_id,sum(debit) AS debit,sum(credit) AS credit
            FROM quant.paper_journal_lines WHERE strategy_id=%s
            GROUP BY journal_id HAVING sum(debit)<>sum(credit)
            ORDER BY journal_id
            """,
            (strategy_id,),
        )
        journal_mismatches = [dict(row) for row in cur.fetchall()]
        mismatches: list[dict[str, Any]] = []
        expected_cash = Decimal(str(account["initial_cash"])) + Decimal(str(ledger["cash_delta"]))
        if expected_cash != Decimal(str(account["cash_balance"])):
            mismatches.append(
                {
                    "check": "CASH_BALANCE",
                    "expected": format(expected_cash, "f"),
                    "actual": format(Decimal(str(account["cash_balance"])), "f"),
                }
            )
        if Decimal(str(ledger["realized_pnl"])) != Decimal(str(account["realized_pnl"])):
            mismatches.append(
                {
                    "check": "REALIZED_PNL",
                    "expected": format(Decimal(str(ledger["realized_pnl"])), "f"),
                    "actual": format(Decimal(str(account["realized_pnl"])), "f"),
                }
            )
        if Decimal(str(reservations["reserved_cash"])) != Decimal(str(account["cash_reserved"])):
            mismatches.append(
                {
                    "check": "RESERVED_CASH",
                    "expected": format(Decimal(str(reservations["reserved_cash"])), "f"),
                    "actual": format(Decimal(str(account["cash_reserved"])), "f"),
                }
            )
        mismatches.extend(
            {"check": "POSITION_QUANTITY", **_json_value(row)}
            for row in position_mismatches
        )
        mismatches.extend(
            {"check": "JOURNAL_BALANCE", **_json_value(row)}
            for row in journal_mismatches
        )
        return {
            "schema_version": "paper-account-reconciliation-v1",
            "account_id": str(account_id),
            "ledger_strategy_id": strategy_id,
            "status": "PASS" if not mismatches else "FAIL",
            "mismatches": mismatches,
            "checks": {
                "cash_balance": not any(row["check"] == "CASH_BALANCE" for row in mismatches),
                "realized_pnl": not any(row["check"] == "REALIZED_PNL" for row in mismatches),
                "reserved_cash": not any(row["check"] == "RESERVED_CASH" for row in mismatches),
                "position_quantity": not position_mismatches,
                "journal_balance": not journal_mismatches,
            },
        }

    def enqueue_dlq_event(
        self,
        principal: TenantPrincipal,
        *,
        source: str,
        source_event_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        last_error: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                INSERT INTO quant.paper_dlq_events (
                    dlq_event_id,tenant_id,source,source_event_key,event_type,
                    resource_type,resource_id,payload,last_error
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (tenant_id,source,source_event_key) DO UPDATE SET
                    updated_at=quant.paper_dlq_events.updated_at
                RETURNING *
                """,
                (
                    uuid4(),
                    principal.tenant_id,
                    str(source),
                    str(source_event_key),
                    str(event_type).upper(),
                    resource_type,
                    resource_id,
                    json.dumps(_json_value(payload), sort_keys=True),
                    str(last_error),
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return _json_value(row)

    def list_dlq(self, principal: TenantPrincipal, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT * FROM quant.paper_dlq_events
                WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s
                """,
                (principal.tenant_id, max(1, min(int(limit), 200))),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return _json_value(rows)

    def ignore_dlq_event(
        self, principal: TenantPrincipal, dlq_event_id: UUID, *, reason: str
    ) -> dict[str, Any]:
        selected_reason = str(reason).strip()
        if not selected_reason:
            raise PaperAdminValidationError("DLQ ignore reason is required")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                UPDATE quant.paper_dlq_events
                SET status='IGNORED',last_error=%s,resolved_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND dlq_event_id=%s
                  AND status IN ('PENDING','FAILED')
                RETURNING *
                """,
                (selected_reason, principal.tenant_id, dlq_event_id),
            )
            row = cur.fetchone()
            if row is None:
                raise PaperAdminConflict("DLQ event cannot be ignored from its current state")
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="DLQ_EVENT_IGNORED",
                resource_type="DLQ_EVENT",
                resource_id=str(dlq_event_id),
                reason=selected_reason,
                payload={},
            )
            conn.commit()
        return _json_value(dict(row))

    def list_jobs(self, principal: TenantPrincipal, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT * FROM quant.paper_admin_jobs
                WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s
                """,
                (principal.tenant_id, max(1, min(int(limit), 200))),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return _json_value(rows)

    def replay_dlq_event(
        self,
        principal: TenantPrincipal,
        dlq_event_id: UUID,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            existing = self._existing_job(cur, principal, idempotency_key)
            if existing is not None:
                conn.commit()
                return _json_value(dict(existing))
            cur.execute(
                """
                SELECT * FROM quant.paper_dlq_events
                WHERE tenant_id=%s AND dlq_event_id=%s FOR UPDATE
                """,
                (principal.tenant_id, dlq_event_id),
            )
            event = cur.fetchone()
            if event is None:
                raise PaperAdminNotFound("DLQ event was not found")
            if str(event["status"]) == "RESOLVED":
                raise PaperAdminConflict("DLQ event is already resolved")
            job_id = self._insert_job(
                cur,
                principal,
                job_type="EVENT_REPLAY",
                mode="APPLY",
                target_type="DLQ_EVENT",
                target_id=str(dlq_event_id),
                request={"event_type": event["event_type"], "payload": event["payload"]},
                idempotency_key=idempotency_key,
            )
            cur.execute(
                """
                UPDATE quant.paper_dlq_events
                SET status='REPLAYING',attempt_count=attempt_count+1,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND dlq_event_id=%s
                """,
                (principal.tenant_id, dlq_event_id),
            )
            event_type = str(event["event_type"])
            payload = dict(event["payload"] or {})
            if event_type == "ACCOUNT_RECONCILE":
                result = self._account_reconciliation(
                    cur, principal, UUID(str(payload["account_id"]))
                )
                resolved = result["status"] == "PASS"
            elif event_type == "TENANT_AUDIT_VERIFY":
                result = self._verify_audit_chain(cur, principal)
                resolved = result["status"] == "PASS"
            else:
                result = {
                    "status": "FAILED",
                    "reason": "UNSUPPORTED_EVENT_TYPE",
                    "event_type": event_type,
                }
                resolved = False
            cur.execute(
                """
                UPDATE quant.paper_dlq_events
                SET status=%s,replay_result=%s::jsonb,last_error=%s,
                    resolved_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND dlq_event_id=%s
                """,
                (
                    "RESOLVED" if resolved else "FAILED",
                    json.dumps(result, sort_keys=True),
                    "" if resolved else str(result.get("reason") or "replay_failed"),
                    resolved,
                    principal.tenant_id,
                    dlq_event_id,
                ),
            )
            cur.execute(
                """
                UPDATE quant.paper_admin_jobs SET status=%s,result=%s::jsonb,
                    started_at=clock_timestamp(),completed_at=clock_timestamp(),
                    last_error=%s
                WHERE tenant_id=%s AND job_id=%s RETURNING *
                """,
                (
                    "COMPLETED" if resolved else "FAILED",
                    json.dumps(result, sort_keys=True),
                    None if resolved else str(result.get("reason") or "replay_failed"),
                    principal.tenant_id,
                    job_id,
                ),
            )
            job = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="DLQ_EVENT_REPLAYED" if resolved else "DLQ_EVENT_REPLAY_FAILED",
                resource_type="DLQ_EVENT",
                resource_id=str(dlq_event_id),
                reason=event_type,
                payload={"job_id": str(job_id), "result": result},
            )
            conn.commit()
        return _json_value(job)

    @staticmethod
    def _verify_audit_chain(cur: Any, principal: TenantPrincipal) -> dict[str, Any]:
        cur.execute(
            """
            SELECT event_id,actor_user_id,effective_user_id,event_type,resource_type,
                   resource_id,reason,payload,previous_event_hash,event_hash
            FROM quant.paper_tenant_audit_events
            WHERE tenant_id=%s ORDER BY event_id
            """,
            (principal.tenant_id,),
        )
        previous: str | None = None
        mismatches: list[int] = []
        rows = cur.fetchall()
        for row in rows:
            if row["previous_event_hash"] != previous:
                mismatches.append(int(row["event_id"]))
            previous = str(row["event_hash"])
        return {
            "status": "PASS" if not mismatches else "FAIL",
            "event_count": len(rows),
            "chain_link_mismatch_event_ids": mismatches,
            "head_hash": previous,
        }

    def create_notice(
        self,
        principal: TenantPrincipal,
        *,
        title: str,
        message: str,
        starts_at: datetime,
        ends_at: datetime | None,
    ) -> dict[str, Any]:
        if not str(title).strip() or not str(message).strip():
            raise PaperAdminValidationError("notice title and message are required")
        if ends_at is not None and ends_at <= starts_at:
            raise PaperAdminValidationError("notice ends_at must be after starts_at")
        notice_id = uuid4()
        status = "ACTIVE" if starts_at <= datetime.now(timezone.utc) else "SCHEDULED"
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                INSERT INTO quant.paper_maintenance_notices (
                    notice_id,tenant_id,title,message,status,starts_at,ends_at,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    notice_id,
                    principal.tenant_id,
                    str(title).strip(),
                    str(message).strip(),
                    status,
                    starts_at,
                    ends_at,
                    principal.subject_user_id,
                ),
            )
            row = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="MAINTENANCE_NOTICE_CREATED",
                resource_type="MAINTENANCE_NOTICE",
                resource_id=str(notice_id),
                reason=status,
                payload={"starts_at": starts_at.isoformat()},
            )
            conn.commit()
        return _json_value(row)

    def list_notices(self, principal: TenantPrincipal) -> list[dict[str, Any]]:
        return self._list_table(principal, "paper_maintenance_notices", "starts_at")

    def set_notice_status(
        self, principal: TenantPrincipal, notice_id: UUID, *, status: str
    ) -> dict[str, Any]:
        selected_status = str(status).upper()
        if selected_status not in {"ACTIVE", "COMPLETED", "CANCELLED"}:
            raise PaperAdminValidationError("notice status is invalid")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                UPDATE quant.paper_maintenance_notices
                SET status=%s,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND notice_id=%s
                  AND status NOT IN ('COMPLETED','CANCELLED')
                RETURNING *
                """,
                (selected_status, principal.tenant_id, notice_id),
            )
            row = cur.fetchone()
            if row is None:
                raise PaperAdminConflict("notice cannot transition from its current state")
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="MAINTENANCE_NOTICE_STATUS_CHANGED",
                resource_type="MAINTENANCE_NOTICE",
                resource_id=str(notice_id),
                reason=selected_status,
                payload={},
            )
            conn.commit()
        return _json_value(dict(row))

    def create_incident(
        self,
        principal: TenantPrincipal,
        *,
        title: str,
        summary: str,
        severity: str,
        started_at: datetime,
    ) -> dict[str, Any]:
        selected_severity = str(severity).upper()
        if selected_severity not in {"SEV1", "SEV2", "SEV3", "SEV4"}:
            raise PaperAdminValidationError("incident severity must be SEV1-SEV4")
        if not str(title).strip() or not str(summary).strip():
            raise PaperAdminValidationError("incident title and summary are required")
        incident_id = uuid4()
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                INSERT INTO quant.paper_incidents (
                    incident_id,tenant_id,title,summary,severity,started_at,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    incident_id,
                    principal.tenant_id,
                    str(title).strip(),
                    str(summary).strip(),
                    selected_severity,
                    started_at,
                    principal.subject_user_id,
                ),
            )
            row = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="INCIDENT_CREATED",
                resource_type="INCIDENT",
                resource_id=str(incident_id),
                reason=selected_severity,
                payload={},
            )
            conn.commit()
        return _json_value(row)

    def add_incident_note(
        self, principal: TenantPrincipal, incident_id: UUID, *, body: str
    ) -> dict[str, Any]:
        if not str(body).strip():
            raise PaperAdminValidationError("incident note body is required")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                "SELECT 1 FROM quant.paper_incidents WHERE tenant_id=%s AND incident_id=%s",
                (principal.tenant_id, incident_id),
            )
            if cur.fetchone() is None:
                raise PaperAdminNotFound("incident was not found")
            cur.execute(
                """
                INSERT INTO quant.paper_incident_notes (
                    note_id,tenant_id,incident_id,body,created_by
                ) VALUES (%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    uuid4(),
                    principal.tenant_id,
                    incident_id,
                    str(body).strip(),
                    principal.subject_user_id,
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return _json_value(row)

    def list_incidents(self, principal: TenantPrincipal) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT incident.*,
                       COALESCE(jsonb_agg(note ORDER BY note.created_at)
                           FILTER (WHERE note.note_id IS NOT NULL),'[]'::jsonb) AS notes
                FROM quant.paper_incidents incident
                LEFT JOIN quant.paper_incident_notes note
                  ON note.tenant_id=incident.tenant_id
                 AND note.incident_id=incident.incident_id
                WHERE incident.tenant_id=%s
                GROUP BY incident.incident_id
                ORDER BY incident.started_at DESC
                """,
                (principal.tenant_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return _json_value(rows)

    def set_incident_status(
        self, principal: TenantPrincipal, incident_id: UUID, *, status: str
    ) -> dict[str, Any]:
        selected_status = str(status).upper()
        if selected_status not in {"OPEN", "MITIGATING", "RESOLVED", "CLOSED"}:
            raise PaperAdminValidationError("incident status is invalid")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                UPDATE quant.paper_incidents
                SET status=%s,
                    resolved_at=CASE WHEN %s IN ('RESOLVED','CLOSED')
                                     THEN COALESCE(resolved_at,clock_timestamp())
                                     ELSE NULL END,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND incident_id=%s
                RETURNING *
                """,
                (selected_status, selected_status, principal.tenant_id, incident_id),
            )
            row = cur.fetchone()
            if row is None:
                raise PaperAdminNotFound("incident was not found")
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="INCIDENT_STATUS_CHANGED",
                resource_type="INCIDENT",
                resource_id=str(incident_id),
                reason=selected_status,
                payload={},
            )
            conn.commit()
        return _json_value(dict(row))

    def list_retention_policies(self, principal: TenantPrincipal) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT resource_type,retention_days,legal_hold,policy_version,updated_at
                FROM quant.paper_retention_policies WHERE tenant_id=%s
                """,
                (principal.tenant_id,),
            )
            current = {str(row["resource_type"]): dict(row) for row in cur.fetchall()}
            conn.commit()
        return [
            _json_value(
                current.get(resource)
                or {
                    "resource_type": resource,
                    "retention_days": days,
                    "legal_hold": False,
                    "policy_version": RETENTION_POLICY_VERSION,
                    "updated_at": None,
                }
            )
            for resource, days in DEFAULT_RETENTION_DAYS.items()
        ]

    def upsert_retention_policy(
        self,
        principal: TenantPrincipal,
        *,
        resource_type: str,
        retention_days: int,
        legal_hold: bool,
    ) -> dict[str, Any]:
        resource = str(resource_type).upper()
        if resource not in DEFAULT_RETENTION_DAYS:
            raise PaperAdminValidationError("resource is immutable or not retention-managed")
        if int(retention_days) < 1:
            raise PaperAdminValidationError("retention_days must be positive")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                INSERT INTO quant.paper_retention_policies (
                    tenant_id,resource_type,retention_days,legal_hold,
                    policy_version,updated_by
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,resource_type) DO UPDATE SET
                    retention_days=EXCLUDED.retention_days,
                    legal_hold=EXCLUDED.legal_hold,
                    policy_version=EXCLUDED.policy_version,
                    updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp()
                RETURNING *
                """,
                (
                    principal.tenant_id,
                    resource,
                    int(retention_days),
                    bool(legal_hold),
                    RETENTION_POLICY_VERSION,
                    principal.subject_user_id,
                ),
            )
            row = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="RETENTION_POLICY_UPDATED",
                resource_type="RETENTION_POLICY",
                resource_id=resource,
                reason="legal_hold" if legal_hold else "retention_window",
                payload={"retention_days": int(retention_days)},
            )
            conn.commit()
        return _json_value(row)

    def run_retention(
        self,
        principal: TenantPrincipal,
        *,
        resource_type: str,
        mode: str,
        idempotency_key: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        resource = str(resource_type).upper()
        selected_mode = str(mode).upper()
        if resource not in DEFAULT_RETENTION_DAYS:
            raise PaperAdminValidationError("resource is immutable or not retention-managed")
        if selected_mode not in {"DRY_RUN", "APPLY"}:
            raise PaperAdminValidationError("retention mode must be DRY_RUN or APPLY")
        now = observed_at or datetime.now(timezone.utc)
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            existing = self._existing_job(cur, principal, idempotency_key)
            if existing is not None:
                conn.commit()
                return _json_value(dict(existing))
            cur.execute(
                """
                SELECT retention_days,legal_hold FROM quant.paper_retention_policies
                WHERE tenant_id=%s AND resource_type=%s
                """,
                (principal.tenant_id, resource),
            )
            policy = cur.fetchone()
            retention_days = int(policy["retention_days"]) if policy else DEFAULT_RETENTION_DAYS[resource]
            legal_hold = bool(policy["legal_hold"]) if policy else False
            cutoff = now - timedelta(days=retention_days)
            job_id = self._insert_job(
                cur,
                principal,
                job_type="RETENTION",
                mode=selected_mode,
                target_type="RESOURCE",
                target_id=resource,
                request={"cutoff": cutoff.isoformat(), "legal_hold": legal_hold},
                idempotency_key=idempotency_key,
            )
            predicate, params = self._retention_predicate(resource, principal, cutoff)
            cur.execute(f"SELECT count(*) AS count FROM {predicate}", params)
            eligible = int(cur.fetchone()["count"])
            deleted = 0
            if selected_mode == "APPLY" and not legal_hold and eligible:
                cur.execute(f"DELETE FROM {predicate}", params)
                deleted = int(cur.rowcount or 0)
            result = {
                "resource_type": resource,
                "mode": selected_mode,
                "cutoff": cutoff.isoformat(),
                "legal_hold": legal_hold,
                "eligible_rows": eligible,
                "deleted_rows": deleted,
                "immutable_resources_untouched": True,
            }
            cur.execute(
                """
                UPDATE quant.paper_admin_jobs SET status='COMPLETED',result=%s::jsonb,
                    started_at=clock_timestamp(),completed_at=clock_timestamp()
                WHERE tenant_id=%s AND job_id=%s RETURNING *
                """,
                (json.dumps(result, sort_keys=True), principal.tenant_id, job_id),
            )
            job = dict(cur.fetchone())
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="RETENTION_APPLIED" if selected_mode == "APPLY" else "RETENTION_DRY_RUN",
                resource_type="RETENTION_POLICY",
                resource_id=resource,
                reason="legal_hold" if legal_hold else "policy_window",
                payload=result,
            )
            conn.commit()
        return _json_value(job)

    @staticmethod
    def _retention_predicate(
        resource: str, principal: TenantPrincipal, cutoff: datetime
    ) -> tuple[str, tuple[Any, ...]]:
        mapping = {
            "API_REQUEST_LOG": (
                "quant.paper_api_request_log WHERE tenant_id=%s AND created_at<%s",
                (principal.tenant_id, cutoff),
            ),
            "IDEMPOTENCY": (
                (
                    "quant.paper_api_idempotency WHERE tenant_id=%s AND updated_at<%s "
                    "AND state IN ('COMPLETED','FAILED')"
                ),
                (principal.tenant_id, cutoff),
            ),
            "ADMIN_JOBS": (
                (
                    "quant.paper_admin_jobs WHERE tenant_id=%s AND completed_at<%s "
                    "AND status IN ('COMPLETED','FAILED','CANCELLED')"
                ),
                (principal.tenant_id, cutoff),
            ),
            "DLQ_RESOLVED": (
                (
                    "quant.paper_dlq_events WHERE tenant_id=%s AND resolved_at<%s "
                    "AND status IN ('RESOLVED','IGNORED')"
                ),
                (principal.tenant_id, cutoff),
            ),
            "EVIDENCE_BUNDLES": (
                "quant.paper_evidence_bundles WHERE tenant_id=%s AND expires_at<%s",
                (principal.tenant_id, datetime.now(timezone.utc)),
            ),
            "REPLAY_SESSIONS": (
                (
                    "quant.paper_replay_sessions WHERE tenant_id=%s AND completed_at<%s "
                    "AND status IN ('COMPLETED','FAILED','CANCELLED')"
                ),
                (principal.tenant_id, cutoff),
            ),
            "MAINTENANCE_NOTICES": (
                (
                    "quant.paper_maintenance_notices WHERE tenant_id=%s AND updated_at<%s "
                    "AND status IN ('COMPLETED','CANCELLED')"
                ),
                (principal.tenant_id, cutoff),
            ),
        }
        return mapping[resource]

    def create_evidence_bundle(
        self,
        principal: TenantPrincipal,
        *,
        account_id: UUID | None,
        incident_id: UUID | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if len(self.signing_key) < 32:
            raise PaperAdminError("artifact signing key must contain at least 32 bytes")
        with self.tenant_store._transaction(principal, PaperPermission.TENANT_ADMIN) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT * FROM quant.paper_evidence_bundles
                WHERE tenant_id=%s AND manifest->>'idempotency_key'=%s
                """,
                (principal.tenant_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                conn.commit()
                return self._bundle_metadata(existing)
            datasets = self._bundle_datasets(cur, principal, account_id, incident_id)
            generated_at = datetime.now(timezone.utc)
            files: dict[str, bytes] = {
                f"{name}.parquet": _parquet_bytes(rows)
                for name, rows in datasets.items()
            }
            lineage = {
                "schema_version": "paper-data-lineage-v1",
                "source": "TENANT_SCOPED_POSTGRES_SNAPSHOT",
                "tenant_id": str(principal.tenant_id),
                "account_id": None if account_id is None else str(account_id),
                "incident_id": None if incident_id is None else str(incident_id),
                "generated_at": generated_at.isoformat(),
                "frozen_at_export": True,
            }
            files["data_lineage.json"] = _canonical_json(lineage)
            files["model.json"] = _canonical_json(
                {"execution_models": sorted({str(row.get("model_version") or "unknown") for row in datasets["tca"]})}
            )
            files["config.json"] = _canonical_json(
                {"account_id": None if account_id is None else str(account_id), "paper_only": True}
            )
            file_manifest = {
                name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                for name, content in sorted(files.items())
            }
            manifest = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "bundle_id": str(uuid4()),
                "tenant_id": str(principal.tenant_id),
                "account_id": None if account_id is None else str(account_id),
                "incident_id": None if incident_id is None else str(incident_id),
                "generated_at": generated_at.isoformat(),
                "idempotency_key": str(idempotency_key),
                "paper_only": True,
                "files": file_manifest,
            }
            manifest_bytes = _canonical_json(manifest)
            signature = hmac.new(self.signing_key, manifest_bytes, hashlib.sha256).hexdigest()
            files["manifest.json"] = manifest_bytes
            files["signature.json"] = _canonical_json(
                {"algorithm": "HMAC-SHA256", "signature": signature}
            )
            files["SHA256SUMS"] = (
                "".join(
                    f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                    for name, content in sorted(files.items())
                    if name != "SHA256SUMS"
                )
            ).encode("ascii")
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
                for name, content in sorted(files.items()):
                    bundle.writestr(name, content)
            content = archive.getvalue()
            content_hash = hashlib.sha256(content).hexdigest()
            retention_days = self._bundle_retention_days(cur, principal)
            bundle_id = UUID(manifest["bundle_id"])
            cur.execute(
                """
                INSERT INTO quant.paper_evidence_bundles (
                    bundle_id,tenant_id,incident_id,account_id,manifest,
                    content_sha256,signature_algorithm,signature,byte_count,
                    content,expires_at,created_by
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,'HMAC-SHA256',%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    bundle_id,
                    principal.tenant_id,
                    incident_id,
                    account_id,
                    json.dumps(manifest, sort_keys=True),
                    content_hash,
                    signature,
                    len(content),
                    content,
                    generated_at + timedelta(days=retention_days),
                    principal.subject_user_id,
                ),
            )
            row = cur.fetchone()
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="EVIDENCE_BUNDLE_CREATED",
                resource_type="EVIDENCE_BUNDLE",
                resource_id=str(bundle_id),
                reason="support_export",
                payload={"content_sha256": content_hash, "byte_count": len(content)},
            )
            conn.commit()
        return self._bundle_metadata(row)

    def get_evidence_bundle(
        self, principal: TenantPrincipal, bundle_id: UUID
    ) -> tuple[dict[str, Any], bytes]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT * FROM quant.paper_evidence_bundles
                WHERE tenant_id=%s AND bundle_id=%s
                FOR UPDATE
                """,
                (principal.tenant_id, bundle_id),
            )
            row = cur.fetchone()
            if row is None:
                raise PaperAdminNotFound("evidence bundle was not found")
            content = bytes(row["content"])
            if hashlib.sha256(content).hexdigest() != str(row["content_sha256"]):
                raise PaperAdminConflict("evidence bundle hash verification failed")
            cur.execute(
                """
                UPDATE quant.paper_evidence_bundles SET status='EXPORTED'
                WHERE tenant_id=%s AND bundle_id=%s RETURNING *
                """,
                (principal.tenant_id, bundle_id),
            )
            metadata = self._bundle_metadata(cur.fetchone())
            conn.commit()
        return metadata, content

    def list_evidence_bundles(
        self, principal: TenantPrincipal, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                """
                SELECT bundle_id,tenant_id,incident_id,account_id,status,manifest,
                       content_sha256,signature_algorithm,signature,byte_count,
                       expires_at,created_by,created_at
                FROM quant.paper_evidence_bundles
                WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s
                """,
                (principal.tenant_id, max(1, min(int(limit), 200))),
            )
            rows = [self._bundle_metadata(row) for row in cur.fetchall()]
            conn.commit()
        return rows

    @staticmethod
    def verify_bundle(content: bytes, signing_key: bytes) -> dict[str, Any]:
        with zipfile.ZipFile(io.BytesIO(content), "r") as bundle:
            names = set(bundle.namelist())
            manifest = json.loads(bundle.read("manifest.json"))
            signature = json.loads(bundle.read("signature.json"))
            expected = hmac.new(signing_key, _canonical_json(manifest), hashlib.sha256).hexdigest()
            file_errors = []
            for name, metadata in dict(manifest["files"]).items():
                if name not in names or hashlib.sha256(bundle.read(name)).hexdigest() != metadata["sha256"]:
                    file_errors.append(name)
        return {
            "status": "PASS"
            if hmac.compare_digest(expected, str(signature["signature"])) and not file_errors
            else "FAIL",
            "signature_valid": hmac.compare_digest(expected, str(signature["signature"])),
            "file_hash_errors": file_errors,
            "manifest": manifest,
        }

    @staticmethod
    def _bundle_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
        return _json_value(
            {
                key: value
                for key, value in dict(row).items()
                if key != "content"
            }
        )

    @staticmethod
    def _bundle_retention_days(cur: Any, principal: TenantPrincipal) -> int:
        cur.execute(
            """
            SELECT retention_days FROM quant.paper_retention_policies
            WHERE tenant_id=%s AND resource_type='EVIDENCE_BUNDLES'
            """,
            (principal.tenant_id,),
        )
        row = cur.fetchone()
        return int(row["retention_days"]) if row else DEFAULT_RETENTION_DAYS["EVIDENCE_BUNDLES"]

    @staticmethod
    def _bundle_datasets(
        cur: Any,
        principal: TenantPrincipal,
        account_id: UUID | None,
        incident_id: UUID | None,
    ) -> dict[str, list[dict[str, Any]]]:
        if account_id is not None:
            cur.execute(
                """
                SELECT ledger_strategy_id FROM quant.paper_account_registry
                WHERE tenant_id=%s AND account_id=%s
                """,
                (principal.tenant_id, account_id),
            )
            account = cur.fetchone()
            if account is None:
                raise PaperAdminNotFound("account was not found")
            strategy_id = str(account["ledger_strategy_id"])
        else:
            strategy_id = None
        datasets: dict[str, list[dict[str, Any]]] = {}
        queries = {
            "orders": (
                (
                    "SELECT intent.* FROM quant.paper_intent_ownership ownership "
                    "JOIN quant.paper_live_order_intents intent ON intent.intent_id=ownership.intent_id "
                    "WHERE ownership.tenant_id=%s AND (%s::uuid IS NULL OR ownership.account_id=%s)"
                ),
                (principal.tenant_id, account_id, account_id),
            ),
            "lifecycle": (
                (
                    "SELECT event.* FROM quant.paper_intent_ownership ownership "
                    "JOIN quant.paper_order_events event ON event.intent_id=ownership.intent_id "
                    "WHERE ownership.tenant_id=%s AND (%s::uuid IS NULL OR ownership.account_id=%s)"
                ),
                (principal.tenant_id, account_id, account_id),
            ),
            "fills": (
                (
                    "SELECT * FROM quant.paper_tenant_fills_v WHERE tenant_id=%s "
                    "AND (%s::uuid IS NULL OR account_id=%s)"
                ),
                (principal.tenant_id, account_id, account_id),
            ),
            "ledger": (
                (
                    "SELECT * FROM quant.paper_tenant_ledger_v WHERE tenant_id=%s "
                    "AND (%s::uuid IS NULL OR account_id=%s)"
                ),
                (principal.tenant_id, account_id, account_id),
            ),
            "positions": (
                "SELECT * FROM quant.paper_positions WHERE (%s::text IS NULL OR strategy_id=%s)",
                (strategy_id, strategy_id),
            ),
            "nav": (
                (
                    "SELECT * FROM quant.paper_portfolio_nav_snapshots "
                    "WHERE (%s::text IS NULL OR strategy_id=%s)"
                ),
                (strategy_id, strategy_id),
            ),
            "tca": (
                "SELECT * FROM quant.execution_tca WHERE (%s::text IS NULL OR strategy_id=%s)",
                (strategy_id, strategy_id),
            ),
            "replays": (
                (
                    "SELECT * FROM quant.paper_replay_sessions WHERE tenant_id=%s "
                    "AND (%s::uuid IS NULL OR account_id=%s)"
                ),
                (principal.tenant_id, account_id, account_id),
            ),
            "audit": (
                "SELECT * FROM quant.paper_tenant_audit_events WHERE tenant_id=%s",
                (principal.tenant_id,),
            ),
        }
        for name, (query, params) in queries.items():
            cur.execute(query, params)
            datasets[name] = [dict(row) for row in cur.fetchall()]
        if incident_id is not None:
            cur.execute(
                "SELECT * FROM quant.paper_incidents WHERE tenant_id=%s AND incident_id=%s",
                (principal.tenant_id, incident_id),
            )
            incident = cur.fetchone()
            if incident is None:
                raise PaperAdminNotFound("incident was not found")
            datasets["incident"] = [dict(incident)]
            cur.execute(
                "SELECT * FROM quant.paper_incident_notes WHERE tenant_id=%s AND incident_id=%s",
                (principal.tenant_id, incident_id),
            )
            datasets["incident_notes"] = [dict(row) for row in cur.fetchall()]
        else:
            datasets["incident"] = []
            datasets["incident_notes"] = []
        asset_ids = sorted(
            {
                str(row["asset_id"])
                for name in ("orders", "fills", "ledger", "positions")
                for row in datasets[name]
                if row.get("asset_id") not in (None, "")
            }
        )
        cur.execute(
            "SELECT * FROM quant.paper_execution_market_catalog "
            "WHERE asset_id=ANY(%s::text[])",
            (asset_ids,),
        )
        datasets["market_metadata"] = [dict(row) for row in cur.fetchall()]
        return datasets

    def _list_table(
        self, principal: TenantPrincipal, table: str, order_column: str
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(principal, PaperPermission.AUDIT_READ) as (
            conn,
            cur,
            _,
        ):
            cur.execute(
                f"SELECT * FROM quant.{table} WHERE tenant_id=%s ORDER BY {order_column} DESC",
                (principal.tenant_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return _json_value(rows)

    @staticmethod
    def _existing_job(
        cur: Any, principal: TenantPrincipal, idempotency_key: str
    ) -> Mapping[str, Any] | None:
        cur.execute(
            "SELECT * FROM quant.paper_admin_jobs WHERE tenant_id=%s AND idempotency_key=%s",
            (principal.tenant_id, str(idempotency_key)),
        )
        return cur.fetchone()

    @staticmethod
    def _insert_job(
        cur: Any,
        principal: TenantPrincipal,
        *,
        job_type: str,
        mode: str,
        target_type: str | None,
        target_id: str | None,
        request: Mapping[str, Any],
        idempotency_key: str,
    ) -> UUID:
        job_id = uuid4()
        cur.execute(
            """
            INSERT INTO quant.paper_admin_jobs (
                job_id,tenant_id,job_type,status,mode,target_type,target_id,
                request,idempotency_key,created_by
            ) VALUES (%s,%s,%s,'RUNNING',%s,%s,%s,%s::jsonb,%s,%s)
            """,
            (
                job_id,
                principal.tenant_id,
                job_type,
                mode,
                target_type,
                target_id,
                json.dumps(_json_value(request), sort_keys=True),
                str(idempotency_key),
                principal.subject_user_id,
            ),
        )
        return job_id
