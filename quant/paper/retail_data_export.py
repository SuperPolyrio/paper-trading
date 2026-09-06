"""Tenant-scoped, checksum-bound retail Paper data export worker."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True)
class RetailDataExportConfig:
    export_root: Path = Path("runtime_outputs/paper_retail/exports")
    lease_seconds: int = 900

    def __post_init__(self) -> None:
        if self.lease_seconds < 60:
            raise ValueError("data export lease must be at least 60 seconds")


class RetailDataExportStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    def claim_next(
        self, *, runner_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_data_subject_requests
                WHERE request_type='EXPORT'
                  AND (status='PENDING'
                       OR (status='PROCESSING'
                           AND lease_expires_at<clock_timestamp()))
                ORDER BY requested_at,request_id
                FOR UPDATE SKIP LOCKED LIMIT 1
                """
            )
            selected = cur.fetchone()
            if selected is None:
                conn.commit()
                return None
            cur.execute(
                """
                UPDATE quant.paper_data_subject_requests SET
                    status='PROCESSING',runner_id=%s,
                    attempt_count=attempt_count+1,
                    lease_expires_at=clock_timestamp()+(%s*interval '1 second')
                WHERE request_id=%s
                RETURNING *
                """,
                (runner_id, int(lease_seconds), selected["request_id"]),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def collect(self, *, tenant_id: UUID, user_id: UUID) -> dict[str, list[dict[str, Any]]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            datasets = _collect_datasets(cur, tenant_id=tenant_id, user_id=user_id)
            conn.commit()
        return datasets

    def finish(
        self,
        *,
        request_id: UUID,
        runner_id: str,
        artifact_path: Path,
        artifact_sha256: str,
        manifest: Mapping[str, Any],
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_data_subject_requests SET
                    status='COMPLETED',artifact_path=%s,artifact_sha256=%s,
                    artifact_manifest=%s::jsonb,lease_expires_at=NULL,
                    completed_at=clock_timestamp()
                WHERE request_id=%s AND status='PROCESSING' AND runner_id=%s
                RETURNING request_id
                """,
                (
                    str(artifact_path),
                    artifact_sha256,
                    json.dumps(manifest, sort_keys=True, default=str),
                    request_id,
                    runner_id,
                ),
            )
            changed = cur.fetchone() is not None
            conn.commit()
        return changed

    def fail(
        self, *, request_id: UUID, runner_id: str, error: str
    ) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_data_subject_requests SET
                    status='REJECTED',artifact_manifest=%s::jsonb,
                    lease_expires_at=NULL,completed_at=clock_timestamp()
                WHERE request_id=%s AND status='PROCESSING' AND runner_id=%s
                RETURNING request_id
                """,
                (
                    json.dumps({"error": error}, sort_keys=True),
                    request_id,
                    runner_id,
                ),
            )
            changed = cur.fetchone() is not None
            conn.commit()
        return changed


class RetailDataExportWorker:
    def __init__(
        self,
        *,
        store: RetailDataExportStore,
        config: RetailDataExportConfig | None = None,
        runner_id: str | None = None,
    ) -> None:
        self.store = store
        self.config = config or RetailDataExportConfig()
        self.runner_id = runner_id or f"retail-data-export-{uuid4()}"

    def run_once(self) -> dict[str, Any]:
        job = self.store.claim_next(
            runner_id=self.runner_id, lease_seconds=self.config.lease_seconds
        )
        if job is None:
            return {"status": "IDLE", "runner_id": self.runner_id}
        request_id = UUID(str(job["request_id"]))
        try:
            datasets = self.store.collect(
                tenant_id=UUID(str(job["tenant_id"])),
                user_id=UUID(str(job["user_id"])),
            )
            artifact_path, artifact_sha256, manifest = build_retail_export(
                export_root=self.config.export_root,
                request_id=request_id,
                tenant_id=UUID(str(job["tenant_id"])),
                user_id=UUID(str(job["user_id"])),
                datasets=datasets,
            )
            applied = self.store.finish(
                request_id=request_id,
                runner_id=self.runner_id,
                artifact_path=artifact_path,
                artifact_sha256=artifact_sha256,
                manifest=manifest,
            )
            return {
                "status": "COMPLETED" if applied else "FENCED",
                "request_id": str(request_id),
                "artifact_path": str(artifact_path),
                "artifact_sha256": artifact_sha256,
                "dataset_count": len(datasets),
                "fenced_write_applied": applied,
            }
        except Exception as exc:  # noqa: BLE001 - durable worker persists failure
            applied = self.store.fail(
                request_id=request_id,
                runner_id=self.runner_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "status": "FAILED",
                "request_id": str(request_id),
                "error": str(exc),
                "fenced_write_applied": applied,
            }


def build_retail_export(
    *,
    export_root: Path,
    request_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    datasets: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[Path, str, dict[str, Any]]:
    export_root = export_root.resolve()
    export_root.mkdir(parents=True, exist_ok=True)
    artifact = export_root / f"paper-retail-export-{request_id}.zip"
    temporary = export_root / f".{artifact.name}.{os.getpid()}.tmp"
    files: dict[str, bytes] = {}
    dataset_manifest: dict[str, Any] = {}
    for name in sorted(datasets):
        rows = [_json_row(row) for row in datasets[name]]
        payloads = {
            f"{name}.jsonl": _jsonl_bytes(rows),
            f"{name}.csv": _csv_bytes(rows),
            f"{name}.parquet": _parquet_bytes(rows),
        }
        files.update(payloads)
        dataset_manifest[name] = {
            "row_count": len(rows),
            "files": {
                filename: {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_count": len(payload),
                }
                for filename, payload in sorted(payloads.items())
            },
        }
    manifest: dict[str, Any] = {
        "schema_version": "paper_retail_user_export_v1",
        "request_id": str(request_id),
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "created_at": datetime.now().astimezone().isoformat(),
        "datasets": dataset_manifest,
        "excluded_sensitive_fields": [
            "api_key_hash",
            "browser_session_token",
            "private_key",
            "mnemonic",
            "api_secret",
        ],
        "formats": ["CSV", "JSONL", "PARQUET"],
    }
    files["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    )
    try:
        with zipfile.ZipFile(
            temporary, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for filename, payload in sorted(files.items()):
                info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, artifact)
    finally:
        temporary.unlink(missing_ok=True)
    payload = artifact.read_bytes()
    return artifact, hashlib.sha256(payload).hexdigest(), manifest


def _collect_datasets(
    cur: Any, *, tenant_id: UUID, user_id: UUID
) -> dict[str, list[dict[str, Any]]]:
    direct = {
        "virtual_wallets": """
            SELECT * FROM quant.paper_virtual_wallets
            WHERE tenant_id=%s AND owner_user_id=%s
            ORDER BY created_at,virtual_wallet_id
        """,
        "identity_bindings": """
            SELECT binding_id,tenant_id,user_id,provider,provider_subject,
                   wallet_address,verified_at,metadata,created_at,updated_at
            FROM quant.paper_identity_bindings
            WHERE tenant_id=%s AND user_id=%s ORDER BY created_at,binding_id
        """,
        "watchlist": """
            SELECT * FROM quant.paper_user_watchlist
            WHERE tenant_id=%s AND user_id=%s ORDER BY created_at,market_slug
        """,
        "preferences": """
            SELECT * FROM quant.paper_user_preferences
            WHERE tenant_id=%s AND user_id=%s
        """,
        "prediction_journal": """
            SELECT * FROM quant.paper_prediction_journal
            WHERE tenant_id=%s AND user_id=%s ORDER BY decision_ts,prediction_id
        """,
        "risk_profiles": """
            SELECT * FROM quant.paper_user_risk_profiles
            WHERE tenant_id=%s AND user_id=%s ORDER BY virtual_wallet_id
        """,
        "order_admissions": """
            SELECT * FROM quant.paper_retail_order_admissions
            WHERE tenant_id=%s AND user_id=%s ORDER BY created_at,admission_id
        """,
        "notifications": """
            SELECT * FROM quant.paper_user_notifications
            WHERE tenant_id=%s AND user_id=%s ORDER BY created_at,notification_id
        """,
        "public_portfolios": """
            SELECT * FROM quant.paper_public_portfolios
            WHERE tenant_id=%s AND user_id=%s ORDER BY virtual_wallet_id
        """,
        "data_subject_requests": """
            SELECT request_id,tenant_id,user_id,request_type,status,reason,
                   artifact_sha256,artifact_manifest,requested_at,completed_at
            FROM quant.paper_data_subject_requests
            WHERE tenant_id=%s AND user_id=%s ORDER BY requested_at,request_id
        """,
        "official_history_syncs": """
            SELECT * FROM quant.paper_official_history_sync_requests
            WHERE tenant_id=%s AND user_id=%s ORDER BY requested_at,request_id
        """,
    }
    datasets: dict[str, list[dict[str, Any]]] = {}
    for name, statement in direct.items():
        cur.execute(statement, (tenant_id, user_id))
        datasets[name] = [dict(row) for row in cur.fetchall()]

    context = """
        WITH owned AS (
            SELECT wallet.virtual_wallet_id,wallet.account_id,
                   account.ledger_strategy_id
            FROM quant.paper_virtual_wallets wallet
            JOIN quant.paper_account_registry account
              ON account.tenant_id=wallet.tenant_id
             AND account.account_id=wallet.account_id
            WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
        )
    """
    linked = {
        "accounts": context
        + """
            SELECT account.* FROM quant.paper_account_registry account
            JOIN owned ON owned.account_id=account.account_id
            ORDER BY account.created_at,account.account_id
        """,
        "strategies": context
        + """
            SELECT strategy.* FROM quant.paper_strategies strategy
            JOIN owned ON owned.account_id=strategy.account_id
            ORDER BY strategy.created_at,strategy.strategy_id
        """,
        "economic_accounts": context
        + """
            SELECT account.* FROM quant.paper_accounts account
            JOIN owned ON owned.ledger_strategy_id=account.strategy_id
            ORDER BY account.strategy_id
        """,
        "orders": context
        + """
            SELECT intent.* FROM quant.paper_live_order_intents intent
            JOIN owned ON owned.ledger_strategy_id=intent.strategy_id
            ORDER BY intent.created_at,intent.intent_id
        """,
        "order_events": context
        + """
            SELECT event.* FROM quant.paper_order_events event
            JOIN quant.paper_live_order_intents intent
              ON intent.intent_id=event.intent_id
            JOIN owned ON owned.ledger_strategy_id=intent.strategy_id
            ORDER BY event.event_ts,event.event_id
        """,
        "fills": context
        + """
            SELECT fill.* FROM quant.paper_fills fill
            JOIN owned ON owned.ledger_strategy_id=fill.strategy_id
            ORDER BY fill.arrival_ts,fill.audit_key,fill.fill_index
        """,
        "ledger": context
        + """
            SELECT ledger.* FROM quant.paper_ledger_entries ledger
            JOIN owned ON owned.ledger_strategy_id=ledger.strategy_id
            ORDER BY ledger.event_ts,ledger.entry_id
        """,
        "positions": context
        + """
            SELECT position.* FROM quant.paper_positions position
            JOIN owned ON owned.ledger_strategy_id=position.strategy_id
            ORDER BY position.strategy_id,position.asset_id
        """,
        "nav_history": context
        + """
            SELECT nav.* FROM quant.paper_portfolio_nav_snapshots nav
            JOIN owned ON owned.ledger_strategy_id=nav.strategy_id
            ORDER BY nav.observed_at,nav.nav_id
        """,
        "competition_memberships": context
        + """
            SELECT membership.*,competition.name,competition.starts_at,
                   competition.ends_at,competition.market_scope,
                   competition.rules_hash
            FROM quant.paper_competition_memberships membership
            JOIN owned ON owned.virtual_wallet_id=membership.virtual_wallet_id
            JOIN quant.paper_competitions competition
              ON competition.competition_id=membership.competition_id
            ORDER BY membership.joined_at,membership.competition_id
        """,
    }
    for name, statement in linked.items():
        cur.execute(statement, (tenant_id, user_id))
        datasets[name] = [dict(row) for row in cur.fetchall()]
    return datasets


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(value) for key, value in row.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(row, sort_keys=True, ensure_ascii=True).encode() + b"\n"
        for row in rows
    )


def _csv_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    columns = sorted({str(key) for row in rows for key in row})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    if columns:
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, sort_keys=True)
                        if isinstance(value, (dict, list))
                        else value
                    )
                    for key, value in row.items()
                }
            )
    return output.getvalue().encode()


def _parquet_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - deployment dependency gate
        raise RuntimeError("pyarrow is required for complete retail exports") from exc
    normalized = [
        {
            key: (
                json.dumps(value, sort_keys=True)
                if isinstance(value, (dict, list))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    ]
    table = pa.Table.from_pylist(normalized) if normalized else pa.table({})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue().to_pybytes()


__all__ = [
    "RetailDataExportConfig",
    "RetailDataExportStore",
    "RetailDataExportWorker",
    "build_retail_export",
]
