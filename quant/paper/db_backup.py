"""Compressed logical backup and isolated restore drill for paper authority data."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import secrets
import shutil
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg import sql

from quant.core.db import PostgresSettings, postgres_connection

from .db_migration import (
    CORE_PARITY_TABLES,
    SNAPSHOT_TABLES,
    _relation_exists,
    _require_tenant_migration_privilege,
    _table_shape,
    apply_schema,
    table_fingerprint,
)
from .local_db_e2e import (
    _remove_owned_container,
    _start_postgres,
    _wait_postgres,
    validate_disposable_target,
)

BACKUP_SCHEMA_VERSION = "paper_authority_logical_backup_v1"
DRILL_SCHEMA_VERSION = "paper_authority_restore_drill_v1"
DEFAULT_CONTAINER = "poly-quant-paper-db-restore-drill"
DEFAULT_PORT = 55434
DEFAULT_DATABASE = "paper_target_restore_drill"
DEFAULT_OUTPUT_ROOT = Path("runtime_outputs/production/db-restore")
COPY_CHUNK_BYTES = 1024 * 1024
PAPER_LEDGER_FINALITY_STATES = ("CONFIRMED", "FAILED_REVERSED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_json_bytes(unsigned)).hexdigest()


def _copy_to_gzip(
    conn: Any,
    *,
    table: str,
    columns: Sequence[str],
    order_by: Sequence[str],
    path: Path,
) -> None:
    selected = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.Identifier("quant", table),
        sql.SQL(", ").join(map(sql.Identifier, order_by)),
    )
    query = sql.SQL(
        "COPY ({}) TO STDOUT WITH (FORMAT CSV, NULL E'\\\\N', FORCE_QUOTE *)"
    ).format(selected)
    with path.open("wb") as raw, gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=raw,
        compresslevel=6,
        mtime=0,
    ) as compressed, conn.cursor() as cur, cur.copy(query) as copy:
        while chunk := copy.read():
            compressed.write(bytes(chunk))


def create_backup(
    source: PostgresSettings,
    bundle_dir: Path,
    *,
    tables: Sequence[str] = SNAPSHOT_TABLES,
) -> dict[str, Any]:
    """Create an immutable compressed bundle from one repeatable-read snapshot."""

    started = time.perf_counter()
    bundle_dir = Path(bundle_dir)
    if bundle_dir.exists():
        raise FileExistsError(f"backup bundle already exists: {bundle_dir}")
    partial = bundle_dir.with_name(f".{bundle_dir.name}.partial-{secrets.token_hex(4)}")
    partial.mkdir(parents=True)
    table_rows: list[dict[str, Any]] = []
    try:
        with postgres_connection(source, readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute(
                    """
                    SELECT clock_timestamp() AS captured_at,
                           pg_current_wal_lsn()::text AS wal_lsn,
                           txid_current_snapshot()::text AS tx_snapshot,
                           current_database() AS database,
                           current_setting('server_version') AS server_version
                    """
                )
                checkpoint = dict(cur.fetchone())
            missing = [table for table in tables if not _relation_exists(conn, table)]
            if missing:
                raise RuntimeError(f"source is missing backup tables: {missing}")
            _require_tenant_migration_privilege(conn, tables, endpoint="source")
            for index, table in enumerate(tables):
                columns, data_types, primary_key = _table_shape(conn, table)
                if not columns:
                    raise RuntimeError(f"quant.{table} has no columns")
                order_by = primary_key or columns
                file_name = f"{index:03d}-{table}.csv.gz"
                file_path = partial / file_name
                fingerprint = table_fingerprint(conn, table, columns=columns)
                _copy_to_gzip(
                    conn,
                    table=table,
                    columns=columns,
                    order_by=order_by,
                    path=file_path,
                )
                table_rows.append(
                    {
                        "table": table,
                        "file": file_name,
                        "columns": columns,
                        "data_types": data_types,
                        "primary_key": primary_key,
                        "row_count": fingerprint["count"],
                        "content_sha256": fingerprint["sha256"],
                        "file_sha256": _file_sha256(file_path),
                        "compressed_bytes": file_path.stat().st_size,
                    }
                )
        completed_at = _now()
        manifest: dict[str, Any] = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "status": "COMPLETE",
            "created_at": completed_at.isoformat(),
            "source": {
                "database": checkpoint["database"],
                "captured_at": checkpoint["captured_at"].isoformat(),
                "wal_lsn": checkpoint["wal_lsn"],
                "tx_snapshot": checkpoint["tx_snapshot"],
                "server_version": checkpoint["server_version"],
            },
            "table_count": len(table_rows),
            "total_rows": sum(int(row["row_count"]) for row in table_rows),
            "compressed_bytes": sum(
                int(row["compressed_bytes"]) for row in table_rows
            ),
            "tables": table_rows,
            "backup_elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        _write_json(partial / "manifest.json", manifest)
        partial.replace(bundle_dir)
        return manifest
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def validate_backup(bundle_dir: Path) -> dict[str, Any]:
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        failures.append("schema_version")
    if manifest.get("status") != "COMPLETE":
        failures.append("manifest_status")
    if manifest.get("manifest_sha256") != _manifest_digest(manifest):
        failures.append("manifest_sha256")
    seen_files: set[str] = set()
    for row in manifest.get("tables") or []:
        file_name = str(row.get("file") or "")
        if not file_name or Path(file_name).name != file_name:
            failures.append(f"unsafe_file:{file_name}")
            continue
        if file_name in seen_files:
            failures.append(f"duplicate_file:{file_name}")
            continue
        seen_files.add(file_name)
        file_path = bundle_dir / file_name
        if not file_path.is_file():
            failures.append(f"missing_file:{file_name}")
            continue
        if _file_sha256(file_path) != row.get("file_sha256"):
            failures.append(f"file_sha256:{file_name}")
    expected_files = seen_files | {"manifest.json"}
    actual_files = {path.name for path in bundle_dir.iterdir() if path.is_file()}
    for extra in sorted(actual_files - expected_files):
        failures.append(f"unexpected_file:{extra}")
    return {
        "schema_version": "paper_authority_backup_validation_v1",
        "status": "PASS" if not failures else "FAIL",
        "bundle_dir": str(bundle_dir.resolve()),
        "manifest": manifest,
        "failures": failures,
    }


def _reset_sequences(conn: Any, tables: Sequence[Mapping[str, Any]]) -> None:
    for row in tables:
        primary_key = list(row.get("primary_key") or [])
        if len(primary_key) != 1:
            continue
        table = str(row["table"])
        key = str(primary_key[0])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_serial_sequence(%s, %s) AS sequence_name",
                (f"quant.{table}", key),
            )
            sequence_name = cur.fetchone()["sequence_name"]
            if sequence_name:
                cur.execute(
                    sql.SQL(
                        "SELECT setval(%s, COALESCE(MAX({}), 1), COUNT(*) > 0) FROM {}"
                    ).format(sql.Identifier(key), sql.Identifier("quant", table)),
                    (sequence_name,),
                )


def _accounting_checks(settings: PostgresSettings) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n
            FROM (
                SELECT journal_id
                FROM quant.paper_journal_lines
                GROUP BY journal_id
                HAVING sum(debit) <> sum(credit)
            ) unbalanced
            """
        )
        unbalanced = int(cur.fetchone()["n"])
        checks["double_entry_balance"] = {
            "status": "PASS" if unbalanced == 0 else "FAIL",
            "unbalanced_journals": unbalanced,
        }
        cur.execute(
            """
            SELECT count(*) AS n FROM quant.paper_fills
            WHERE size <= 0 OR price < 0 OR price > 1 OR fee < 0
            """
        )
        invalid_fills = int(cur.fetchone()["n"])
        checks["fill_domain"] = {
            "status": "PASS" if invalid_fills == 0 else "FAIL",
            "invalid_fills": invalid_fills,
        }
        cur.execute(
            """
            SELECT count(*) AS n FROM quant.paper_execution_finality
            WHERE state <> ALL(%s)
            """,
            (list(PAPER_LEDGER_FINALITY_STATES),),
        )
        invalid_finality = int(cur.fetchone()["n"])
        checks["finality_domain"] = {
            "status": "PASS" if invalid_finality == 0 else "FAIL",
            "invalid_rows": invalid_finality,
        }
        cur.execute("SELECT count(*) AS n FROM quant.paper_execution_partition_leases")
        leases = int(cur.fetchone()["n"])
        checks["authority_recovery_fence"] = {
            "status": "PASS" if leases == 0 else "FAIL",
            "restored_active_leases": leases,
            "detail": "ephemeral authority leases are deliberately not backed up",
        }
    failures = [name for name, row in checks.items() if row["status"] != "PASS"]
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "checks": checks,
    }


def restore_backup(
    target: PostgresSettings,
    bundle_dir: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    validation = validate_backup(bundle_dir)
    if validation["status"] != "PASS":
        raise RuntimeError(f"backup validation failed: {validation['failures']}")
    manifest = validation["manifest"]
    table_rows = list(manifest["tables"])
    apply_schema(target)
    with postgres_connection(target, readonly=False) as conn:
        missing = [
            str(row["table"])
            for row in table_rows
            if not _relation_exists(conn, str(row["table"]))
        ]
        if missing:
            raise RuntimeError(f"target is missing restore tables: {missing}")
        _require_tenant_migration_privilege(
            conn,
            [str(row["table"]) for row in table_rows],
            endpoint="target",
        )
        identifiers = [
            sql.Identifier("quant", str(row["table"])) for row in table_rows
        ]
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(identifiers)
                )
            )
        for row in table_rows:
            table = str(row["table"])
            columns = [str(column) for column in row["columns"]]
            target_columns, target_types, _ = _table_shape(conn, table)
            if set(columns) - set(target_columns):
                raise RuntimeError(f"quant.{table} target columns do not match backup")
            for column in columns:
                if target_types[column] != row["data_types"][column]:
                    raise RuntimeError(
                        f"quant.{table}.{column} type changed: "
                        f"{row['data_types'][column]} -> {target_types[column]}"
                    )
            query = sql.SQL(
                "COPY {} ({}) FROM STDIN WITH (FORMAT CSV, NULL E'\\\\N')"
            ).format(
                sql.Identifier("quant", table),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
            )
            with (
                gzip.open(Path(bundle_dir) / str(row["file"]), "rb") as source,
                conn.cursor() as cur,
                cur.copy(query) as copy,
            ):
                while chunk := source.read(COPY_CHUNK_BYTES):
                    copy.write(chunk)
        _reset_sequences(conn, table_rows)
        conn.commit()

    parity: dict[str, Any] = {}
    with postgres_connection(target, readonly=True) as conn:
        for row in table_rows:
            table = str(row["table"])
            fingerprint = table_fingerprint(
                conn,
                table,
                columns=[str(column) for column in row["columns"]],
            )
            parity[table] = {
                "status": (
                    "PASS"
                    if fingerprint["count"] == row["row_count"]
                    and fingerprint["sha256"] == row["content_sha256"]
                    else "FAIL"
                ),
                "expected_count": row["row_count"],
                "actual_count": fingerprint["count"],
                "expected_sha256": row["content_sha256"],
                "actual_sha256": fingerprint["sha256"],
            }
    failures = [table for table, row in parity.items() if row["status"] != "PASS"]
    accounting = _accounting_checks(target)
    if accounting["status"] != "PASS":
        failures.extend(f"accounting:{name}" for name in accounting["failures"])
    core = [table for table in CORE_PARITY_TABLES if table in parity]
    return {
        "schema_version": "paper_authority_restore_v1",
        "status": "PASS" if not failures else "FAIL",
        "restored_tables": len(table_rows),
        "restored_rows": sum(int(row["row_count"]) for row in table_rows),
        "core_tables": core,
        "parity": parity,
        "accounting": accounting,
        "failures": failures,
        "restore_elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def run_restore_drill(
    *,
    source: PostgresSettings,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    port: int = DEFAULT_PORT,
    database: str = DEFAULT_DATABASE,
    container: str = DEFAULT_CONTAINER,
    keep_target: bool = False,
) -> dict[str, Any]:
    run_id = _now().strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(output_root) / f"restore-drill-{run_id}"
    bundle_dir = run_dir / "backup"
    report_path = run_dir / "report.json"
    latest_path = Path(output_root) / "restore-drill-latest.json"
    target = PostgresSettings(
        host="127.0.0.1",
        port=int(port),
        user="paper_restore_drill",
        password=secrets.token_urlsafe(32),
        database=database,
        search_path="quant,core,oracle,ops,public",
        connect_timeout_seconds=3,
    )
    validate_disposable_target(source, target)
    report: dict[str, Any] = {
        "schema_version": DRILL_SCHEMA_VERSION,
        "status": "RUNNING",
        "started_at": _now().isoformat(),
        "scope": {
            "local_only": True,
            "source_database": source.database,
            "target_host": target.host,
            "target_port": target.port,
            "target_database": target.database,
            "gcp_accessed": False,
            "live_submission_performed": False,
            "source_writes_performed": False,
        },
        "report_path": str(report_path.resolve()),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(report_path, report)
    try:
        report["backup"] = create_backup(source, bundle_dir)
        report["backup_validation"] = validate_backup(bundle_dir)
        _start_postgres(
            name=container,
            port=target.port,
            user=target.user,
            password=target.password,
            database=target.database,
        )
        _wait_postgres(target)
        report["restore"] = restore_backup(target, bundle_dir)
        captured = datetime.fromisoformat(report["backup"]["source"]["captured_at"])
        completed = _now()
        report["measured_recovery"] = {
            "rpo_model": "logical_snapshot",
            "snapshot_age_at_restore_seconds": round(
                (completed - captured).total_seconds(), 3
            ),
            "rto_seconds": report["restore"]["restore_elapsed_seconds"],
            "pitr_validated": False,
            "note": "This drill validates logical snapshot recovery, not WAL PITR.",
        }
        if report["backup_validation"]["status"] != "PASS":
            raise RuntimeError("backup bundle validation failed")
        if report["restore"]["status"] != "PASS":
            raise RuntimeError("restored authority parity failed")
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["completed_at"] = _now().isoformat()
        report["target_retained"] = bool(keep_target)
        _write_json(report_path, report)
        _write_json(latest_path, report)
        if not keep_target:
            _remove_owned_container(container)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--keep-target", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_restore_drill(
        source=PostgresSettings(),
        output_root=args.output_root,
        port=args.port,
        database=args.database,
        container=args.container,
        keep_target=args.keep_target,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_path": report["report_path"],
                "backup_rows": report["backup"]["total_rows"],
                "backup_bytes": report["backup"]["compressed_bytes"],
                "restore_status": report["restore"]["status"],
                "rto_seconds": report["measured_recovery"]["rto_seconds"],
                "pitr_validated": False,
                "live_submission_performed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
