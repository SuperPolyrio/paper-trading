"""Production deployment checks and health endpoints for the paper worker."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

os.environ.setdefault("POLY_QUANT_DISABLE_DOTENV", "1")

from quant.core.db import PostgresSettings, postgres_connection
from quant.paper.security import (
    audit_paper_dependency_boundary,
    enforce_paper_security_boundary,
    inspect_environment_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATUS_PATH = Path(
    os.environ.get(
        "PAPER_LIVE_STATUS_PATH",
        "/mnt/l2-archive/.health/paper-live-shadow/status.json",
    )
)
DEFAULT_MANIFEST_PATH = Path(
    os.environ.get(
        "PAPER_WORKER_BUILD_MANIFEST",
        "/opt/paper-trading/runtime_outputs/production/paper-worker-build.json",
    )
)
DEFAULT_EVENT_SOCKET = Path(
    os.environ.get(
        "PAPER_LIVE_GCP_EVENT_SOCKET",
        f"/run/user/{os.getuid()}/poly-quant/paper-events.sock",
    )
)
DEFAULT_ARCHIVE_ROOT = Path("/mnt/l2-archive")
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEALTH_PORT = 18700
DEFAULT_OPERATIONS_STATUS_PATH = Path(
    os.environ.get(
        "PAPER_OPERATIONS_STATUS_PATH",
        "runtime_outputs/paper_operations/status.json",
    )
)
DEFAULT_OPERATIONS_METRICS_PATH = Path(
    os.environ.get(
        "PAPER_OPERATIONS_METRICS_PATH",
        "runtime_outputs/paper_operations/metrics.prom",
    )
)

MANIFEST_FILES = (
    "quant/paper/authority.py",
    "quant/paper/acceptance.py",
    "quant/paper/canary.py",
    "quant/paper/cash_reconciliation.py",
    "quant/paper/db_migration.py",
    "quant/paper/execution_profile.py",
    "quant/paper/live_shadow_store.py",
    "quant/paper/live_shadow_service.py",
    "quant/paper/operations.py",
    "quant/paper/paper_ledger.py",
    "quant/paper/persistent_event_kernel.py",
    "quant/paper/professional_execution.py",
    "quant/paper/production_runtime.py",
    "quant/paper/public_api.py",
    "quant/paper/security.py",
    "quant/paper/tenant_platform.py",
    "quant/adapters/polymarket_paper_clob_client.py",
    "quant/calibration/calibration_domain.py",
    "quant/calibration/clean_v2_cohort.py",
    "quant/calibration/order_rest_reconciler.py",
    "quant/calibration/paired_probe_bridge.py",
    "quant/calibration/reconcile.py",
    "quant/calibration/signed_order_prediction.py",
    "quant/calibration/store.py",
    "quant/core/db.py",
    "quant/maker/own_order_truth.py",
    "quant/execution/models/maker_model_domain.py",
    "quant/execution/models/maker_queue.py",
    "quant/simulator/kernel/event_priority.py",
    "quant/simulator/oms/domain.py",
    "quant/simulator/oms/external_order_import.py",
    "quant/simulator/oms/oms_store.py",
    "quant/simulator/oms/paper_adapter.py",
    "quant/simulator/oms/position_assignment.py",
    "quant/simulator/oms/self_trade_prevention.py",
    "quant/simulator/oms/strategy_subledger.py",
    "quant/simulator/admission/__init__.py",
    "quant/simulator/admission/client.py",
    "quant/simulator/admission/domain.py",
    "quant/simulator/admission/policy.py",
    "quant/simulator/admission/runtime.py",
    "quant/simulator/admission/service.py",
    "quant/simulator/admission/store.py",
    "scripts/paper_runtime_credentials.sh",
    "scripts/run_live_calibration_probe_secure.sh",
    "scripts/live_calibration_credentials.sh",
    "scripts/install_live_calibration_secret_from_stdin.sh",
    "scripts/run_maker_calibration_collector.py",
    "scripts/run_maker_calibration_collector_secure.sh",
    "scripts/run_calibration_drift_monitor_secure.sh",
    "scripts/run_calibration_portfolio_monitor_secure.sh",
    "scripts/run_calibration_settlement_watcher_secure.sh",
    "scripts/run_paper_health_secure.sh",
    "scripts/run_paper_execution_catalog_sync.sh",
    "scripts/run_paper_security_acceptance.py",
    "scripts/run_unified_admission_acceptance.py",
    "scripts/run_live_geoblock_route_acceptance.py",
    "scripts/run_paper_tenant_rls_probe.py",
    "scripts/run_paper_tenant_postgres_acceptance.py",
    "scripts/manage_paper_tenants.py",
    "scripts/paper_api_server.py",
    "scripts/api/routes/paper_v1.py",
    "scripts/run_paper_api_secure.sh",
    "scripts/export_paper_openapi.py",
    "scripts/install_paper_secret_from_stdin.sh",
    "scripts/run_paper_daily_accounting.sh",
    "scripts/manage_gcp_paper_db_cutover.sh",
    "scripts/run_gcp_paper_live_shadow.sh",
    "scripts/install_gcp_paper_worker.sh",
    "scripts/run_paper_operations_snapshot.sh",
    "scripts/run_paper_live_shadow_acceptance_soak.sh",
    "scripts/run_gcp_paper_final_soak.sh",
    "scripts/start_gcp_paper_soak_24h_after_pass.sh",
    "scripts/start_gcp_paper_soak_7d_after_pass.sh",
    "deploy/systemd/paper-db-target.env.example",
    "deploy/systemd/paper-runtime.env.example",
    "deploy/systemd/paper-api.env.example",
    "deploy/systemd/live-calibration-runtime.env.example",
    "deploy/systemd/poly-quant-live-calibration-probe.service",
    "deploy/systemd/poly-quant-maker-calibration-collector.service",
    "deploy/systemd/poly-quant-calibration-drift-monitor.service",
    "deploy/systemd/poly-quant-calibration-portfolio-monitor.service",
    "deploy/systemd/poly-quant-calibration-settlement-watcher.service",
    "deploy/systemd/poly-quant-paper-daily-accounting.service",
    "deploy/systemd/poly-quant-gcp-paper-live-shadow.service",
    "deploy/systemd/poly-quant-paper-execution-catalog-sync.service",
    "deploy/systemd/poly-quant-paper-execution-catalog-sync.timer",
    "deploy/systemd/poly-quant-gcp-paper-health.service",
    "deploy/systemd/poly-quant-gcp-paper-operations-snapshot.service",
    "deploy/systemd/poly-quant-gcp-paper-operations-snapshot.timer",
    "deploy/systemd/poly-quant-gcp-paper-soak-6h.service",
    "deploy/systemd/poly-quant-gcp-paper-soak-24h.service",
    "deploy/systemd/poly-quant-gcp-paper-soak-7d.service",
    "deploy/systemd/poly-quant-paper-api.service",
    "deploy/systemd/poly-quant-paper-operations-snapshot.service",
    "deploy/systemd/poly-quant-paper-operations-snapshot.timer",
    "deploy/prometheus/paper-alert-rules.yml",
    "deploy/grafana/paper-operations-dashboard.json",
    "deploy/gcp/provision_paper_live_isolation.sh",
    "deploy/gcp/provision_paper_egress_policy.sh",
    "deploy/gcp/secret-manager-audit-config.fragment.yaml",
    "deploy/postgres/paper_runtime_role.sql",
    "deploy/postgres/verify_paper_runtime_role.sql",
    "deploy/postgres/live_calibration_runtime_role.sql",
    "deploy/postgres/paper_tenant_runtime_role.sql",
    "deploy/postgres/verify_paper_tenant_runtime_role.sql",
    "docs/模拟盘/paper_operations_runbook.md",
    "docs/模拟盘/paper_live_security_runbook.md",
    "docs/模拟盘/paper_tenant_platform_runbook.md",
    "docs/模拟盘/paper_public_api_runbook.md",
    "docs/api/paper-v1-openapi.json",
    "sdk/python/polymarket_paper/__init__.py",
    "sdk/python/polymarket_paper/client.py",
    "sdk/typescript/package.json",
    "sdk/typescript/src/index.ts",
)
PRODUCTION_IMPORT_MODULES = (
    "quant.paper.live_shadow_service",
    "quant.paper.operations",
    "quant.calibration.clean_v2_cohort",
    "quant.maker.own_order_truth",
    "quant.simulator.oms.paper_adapter",
)
REQUIRED_TABLES = (
    "paper_schema_migrations",
    "paper_execution_partition_leases",
    "paper_authority_config",
    "paper_execution_market_catalog",
    "paper_accounts",
    "paper_tenants",
    "paper_users",
    "paper_memberships",
    "paper_account_registry",
    "paper_account_generations",
    "paper_strategies",
    "paper_strategy_deployments",
    "paper_intent_ownership",
    "paper_quotas",
    "paper_usage_meter",
    "paper_quota_consumptions",
    "paper_api_keys",
    "paper_api_idempotency",
    "paper_api_request_log",
    "paper_tenant_audit_events",
    "paper_tenant_deletion_requests",
    "paper_positions",
    "paper_fills",
    "paper_ledger_entries",
    "paper_live_order_intents",
    "paper_order_events",
    "paper_execution_profile_decisions",
    "paper_live_maker_trade_events",
    "maker_queue_states",
    "paper_global_event_kernel_state",
    "paper_global_event_kernel_events",
    "paper_live_shadow_health",
    "paper_sim_events",
    "paper_inflight_commands",
    "simulator_admission_policies",
    "simulator_geoblock_snapshots",
    "simulator_admission_decisions",
)
REQUIRED_COLUMNS = {
    "paper_execution_profile_decisions": {
        "intent_id",
        "decision_hash",
        "profile",
        "execution_config_hash",
        "decision",
        "authority_partition_key",
        "authority_lease_epoch",
    },
    "paper_live_maker_trade_events": {
        "event_id",
        "processing_state",
        "disposition_reason",
        "processed_at",
    },
    "maker_queue_states": {
        "paper_order_id",
        "accepted_at",
        "accepted_book_generation",
        "queue_epoch",
        "last_event_ts",
        "model_domain_decision",
        "model_domain_decision_hash",
        "authority_partition_key",
        "authority_lease_epoch",
    },
    "paper_global_event_kernel_state": {
        "partition_key",
        "last_applied_sequence",
        "last_journal_hash",
        "accepted_event_count",
        "failed_event_count",
        "queue_counts_initialized",
        "authority_partition_key",
        "authority_lease_epoch",
    },
    "paper_global_event_kernel_events": {
        "partition_key",
        "event_id",
        "processing_state",
        "applied_sequence",
        "journal_hash",
        "authority_partition_key",
        "authority_lease_epoch",
    },
}


def _now() -> datetime:
    return datetime.now(UTC)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def build_manifest(
    root: Path,
    *,
    source_git_sha: str | None = None,
    source_dirty: bool | None = None,
) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for relative in MANIFEST_FILES:
        path = root / relative
        files[relative] = {
            "exists": path.is_file(),
            "sha256": _sha256_file(path) if path.is_file() else None,
            "size": path.stat().st_size if path.is_file() else None,
        }
    git_sha = source_git_sha or _git_value(root, "rev-parse", "HEAD")
    if source_dirty is None:
        porcelain = _git_value(root, "status", "--porcelain")
        source_dirty = None if porcelain is None else bool(porcelain)
    payload: dict[str, Any] = {
        "schema_version": "paper_worker_build_manifest_v1",
        "artifact_kind": "source_systemd",
        "generated_at": _now().isoformat(),
        "git_sha": git_sha,
        "git_dirty": source_dirty,
        "python_version": sys.version.split()[0],
        "execution_schema_version": "paper-live-authority-v5-public-api",
        "files": files,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["build_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def verify_manifest(root: Path, manifest: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return ["manifest.files is missing"]
    for relative, evidence in files.items():
        if not isinstance(relative, str) or not isinstance(evidence, Mapping):
            issues.append("manifest contains an invalid file entry")
            continue
        path = root / relative
        if not path.is_file():
            issues.append(f"missing file: {relative}")
            continue
        expected = str(evidence.get("sha256") or "")
        if not expected or _sha256_file(path) != expected:
            issues.append(f"hash mismatch: {relative}")
    return issues


def _load_env_files(paths: Sequence[Path]) -> None:
    if not paths:
        return
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise RuntimeError("python-dotenv is required when --env-file is used") from exc
    for path in paths:
        inspection = inspect_environment_file(path)
        unsafe_keys = (
            inspection["forbidden_live_keys"] + inspection["plaintext_secret_keys"]
        )
        if unsafe_keys:
            raise RuntimeError(
                f"paper runtime refused unsafe environment file {path}: "
                f"keys={sorted(unsafe_keys)}"
            )
        load_dotenv(path, override=True)


def _postgres_settings() -> PostgresSettings:
    paper_port = os.environ.get("PAPER_LIVE_POSTGRES_PORT", "").strip()
    return PostgresSettings(port=int(paper_port)) if paper_port else PostgresSettings()


def _check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    detail: Any,
) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def run_preflight(
    *,
    root: Path,
    manifest_path: Path,
    status_path: Path,
    archive_root: Path,
    event_socket: Path,
    env_files: Sequence[Path],
    check_db: bool = True,
    check_event_socket: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    required = [root / relative for relative in MANIFEST_FILES]
    missing = [str(path) for path in required if not path.is_file()]
    _check(checks, "required_files", "PASS" if not missing else "FAIL", missing)

    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_issues = verify_manifest(root, manifest)
        except (OSError, json.JSONDecodeError) as exc:
            manifest_issues = [f"{type(exc).__name__}: {exc}"]
    else:
        manifest_issues = [f"missing manifest: {manifest_path}"]
    _check(
        checks,
        "build_manifest",
        "PASS" if not manifest_issues else "FAIL",
        {"build_id": manifest.get("build_id"), "issues": manifest_issues},
    )
    if manifest.get("git_dirty") is True:
        _check(checks, "clean_source", "WARN", "source worktree was dirty")
    elif not manifest.get("git_sha"):
        _check(checks, "clean_source", "WARN", "git SHA is unavailable")
    else:
        _check(checks, "clean_source", "PASS", manifest.get("git_sha"))

    _check(
        checks,
        "archive_mount",
        "PASS" if archive_root.is_mount() else "FAIL",
        str(archive_root),
    )
    if check_event_socket:
        socket_ok = False
        if event_socket.exists():
            try:
                socket_ok = stat.S_ISSOCK(event_socket.stat().st_mode)
            except OSError:
                socket_ok = False
        _check(
            checks,
            "event_socket",
            "PASS" if socket_ok else "FAIL",
            str(event_socket),
        )
    else:
        _check(
            checks,
            "event_socket",
            "SKIP",
            "paper worker owns this socket; checked after startup by canary",
        )

    forbidden: dict[str, list[str]] = {}
    plaintext_secrets: dict[str, list[str]] = {}
    permissive: list[str] = []
    missing_env: list[str] = []
    for path in env_files:
        if not path.is_file():
            missing_env.append(str(path))
            continue
        inspection = inspect_environment_file(path)
        if inspection["forbidden_live_keys"]:
            forbidden[str(path)] = inspection["forbidden_live_keys"]
        if inspection["plaintext_secret_keys"]:
            plaintext_secrets[str(path)] = inspection["plaintext_secret_keys"]
        if not inspection["owner_only"]:
            permissive.append(f"{path}:{inspection['mode']}")
    _check(
        checks,
        "environment_files",
        "PASS" if not missing_env else "FAIL",
        {"missing": missing_env, "paths": [str(path) for path in env_files]},
    )
    _check(
        checks,
        "paper_live_key_isolation",
        "PASS" if not forbidden else "FAIL",
        forbidden,
    )
    _check(
        checks,
        "plaintext_secret_isolation",
        "PASS" if not plaintext_secrets else "FAIL",
        plaintext_secrets,
    )
    if permissive:
        _check(checks, "environment_permissions", "WARN", permissive)
    else:
        _check(checks, "environment_permissions", "PASS", "owner-only")

    dependency_issues = audit_paper_dependency_boundary(root)
    _check(
        checks,
        "paper_dependency_boundary",
        "PASS" if not dependency_issues else "FAIL",
        dependency_issues,
    )

    import_issues: dict[str, str] = {}
    for module_name in PRODUCTION_IMPORT_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - report every broken runtime import
            import_issues[module_name] = f"{type(exc).__name__}: {exc}"
    _check(
        checks,
        "runtime_imports",
        "PASS" if not import_issues else "FAIL",
        import_issues,
    )

    if status_path.exists():
        _check(checks, "status_path", "PASS", str(status_path))
    else:
        _check(checks, "status_path", "WARN", f"not yet created: {status_path}")

    if check_db:
        started = time.perf_counter()
        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}
        db_error: str | None = None
        db_in_recovery: bool | None = None
        transaction_read_only: str | None = None
        write_probe_ok = False
        settings = _postgres_settings()
        try:
            with (
                postgres_connection(settings, readonly=False) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    SELECT pg_is_in_recovery() AS in_recovery,
                           current_setting('transaction_read_only')
                               AS transaction_read_only
                    """
                )
                db_state = dict(cur.fetchone())
                db_in_recovery = bool(db_state["in_recovery"])
                transaction_read_only = str(db_state["transaction_read_only"])
                if db_in_recovery:
                    raise RuntimeError("database is still in recovery")
                if transaction_read_only != "off":
                    raise RuntimeError(
                        "database transaction_read_only is not off: "
                        f"{transaction_read_only}"
                    )
                cur.execute(
                    """
                    CREATE TEMP TABLE paper_worker_preflight_write_probe (
                        probe_value integer NOT NULL
                    ) ON COMMIT DROP
                    """
                )
                cur.execute(
                    "INSERT INTO paper_worker_preflight_write_probe VALUES (1)"
                )
                cur.execute(
                    "SELECT count(*) AS probe_count "
                    "FROM paper_worker_preflight_write_probe"
                )
                write_probe_ok = int(cur.fetchone()["probe_count"]) == 1
                if not write_probe_ok:
                    raise RuntimeError("database temporary write probe did not persist")
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema='quant' AND table_name = ANY(%s)
                    """,
                    (list(REQUIRED_TABLES),),
                )
                found = {str(row["table_name"]) for row in cur.fetchall()}
                missing_tables = sorted(set(REQUIRED_TABLES) - found)
                cur.execute(
                    """
                    SELECT table_name,column_name
                    FROM information_schema.columns
                    WHERE table_schema='quant' AND table_name = ANY(%s)
                    """,
                    (list(REQUIRED_COLUMNS),),
                )
                found_columns: dict[str, set[str]] = {
                    table: set() for table in REQUIRED_COLUMNS
                }
                for row in cur.fetchall():
                    found_columns[str(row["table_name"])].add(
                        str(row["column_name"])
                    )
                missing_columns = {
                    table: sorted(required - found_columns[table])
                    for table, required in REQUIRED_COLUMNS.items()
                    if required - found_columns[table]
                }
                conn.rollback()
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            db_error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        _check(
            checks,
            "database",
            (
                "PASS"
                if db_error is None and not missing_tables and not missing_columns
                else "FAIL"
            ),
            {
                "host": settings.host,
                "port": settings.port,
                "latency_ms": latency_ms,
                "missing_tables": missing_tables,
                "missing_columns": missing_columns,
                "in_recovery": db_in_recovery,
                "transaction_read_only": transaction_read_only,
                "write_probe_ok": write_probe_ok,
                "error": db_error,
            },
        )
        if settings.host in {"127.0.0.1", "localhost"} and settings.port != 5432:
            _check(
                checks,
                "database_route",
                "WARN",
                f"loopback nonstandard port {settings.port}; verify reverse SSH is not the production hot path",
            )

    failures = [row for row in checks if row["status"] == "FAIL"]
    warnings = [row for row in checks if row["status"] == "WARN"]
    status = "FAIL" if failures else "PASS_WITH_WARNINGS" if warnings else "PASS"
    return {
        "schema_version": "paper_worker_preflight_v1",
        "generated_at": _now().isoformat(),
        "status": status,
        "build_id": manifest.get("build_id"),
        "checks": checks,
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (
        parsed.astimezone(UTC)
        if parsed.tzinfo
        else parsed.replace(tzinfo=UTC)
    )


def _load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return (payload if isinstance(payload, dict) else {}), None


def evaluate_live(
    status_path: Path, *, max_age_seconds: float
) -> tuple[int, dict[str, Any]]:
    payload, error = _load_json(status_path)
    updated = _parse_time(payload.get("updated_at") or payload.get("sampled_at"))
    age = (_now() - updated).total_seconds() if updated else None
    transport = str(payload.get("transport_state") or "UNKNOWN")
    live = (
        error is None
        and age is not None
        and age <= max_age_seconds
        and transport not in {"STOPPED", "STARTING", "UNKNOWN"}
    )
    result = {
        "status": "PASS" if live else "FAIL",
        "updated_at": updated.isoformat() if updated else None,
        "age_seconds": round(age, 3) if age is not None else None,
        "transport_state": transport,
        "worker_id": payload.get("worker_id"),
        "error": error,
    }
    return (HTTPStatus.OK if live else HTTPStatus.SERVICE_UNAVAILABLE), result


def evaluate_ready(
    status_path: Path, *, max_age_seconds: float
) -> tuple[int, dict[str, Any]]:
    live_code, live = evaluate_live(status_path, max_age_seconds=max_age_seconds)
    authority_code, authority = evaluate_authority(
        status_path,
        max_age_seconds=max_age_seconds,
    )
    payload, error = _load_json(status_path)
    reasons: list[str] = []
    if live_code != HTTPStatus.OK:
        reasons.append("worker_not_live")
    if authority_code != HTTPStatus.OK:
        reasons.append("authority_not_ready")
    if str(payload.get("transport_state")) != "REDUNDANT":
        reasons.append("transport_not_redundant")
    if int(payload.get("ready_books") or 0) <= 0:
        reasons.append("no_ready_books")
    if str(payload.get("backpressure_status") or "") != "ACCEPT":
        reasons.append("backpressure_not_accepting")
    if payload.get("last_error"):
        reasons.append("worker_last_error_present")
    if int(payload.get("watch_refresh_consecutive_failures") or 0) > 0:
        reasons.append("watch_refresh_failing")
    ready = error is None and not reasons
    result = {
        "status": "PASS" if ready else "FAIL",
        "reasons": reasons,
        "live": live,
        "authority": authority,
        "ready_books": int(payload.get("ready_books") or 0),
        "watched_assets": int(payload.get("watched_assets") or 0),
        "queued_intents": int(payload.get("queued_intents") or 0),
        "processing_intents": int(payload.get("processing_intents") or 0),
        "backpressure_status": payload.get("backpressure_status"),
        "last_error": payload.get("last_error"),
    }
    return (HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE), result


def evaluate_authority(
    status_path: Path,
    *,
    max_age_seconds: float,
) -> tuple[int, dict[str, Any]]:
    payload, error = _load_json(status_path)
    updated = _parse_time(payload.get("updated_at") or payload.get("sampled_at"))
    lease_until = _parse_time(payload.get("authority_lease_until"))
    age = (_now() - updated).total_seconds() if updated else None
    reasons: list[str] = []
    if error is not None:
        reasons.append("status_unreadable")
    if age is None or age > max_age_seconds:
        reasons.append("authority_status_stale")
    if str(payload.get("authority_mode") or "") != "FENCED":
        reasons.append("authority_mode_not_fenced")
    if str(payload.get("authority_state") or "") != "HELD":
        reasons.append("authority_lease_not_held")
    if not bool(payload.get("authority_fencing_enforced")):
        reasons.append("database_fencing_not_enforced")
    if not str(payload.get("authority_partition_key") or "").strip():
        reasons.append("authority_partition_missing")
    if not str(payload.get("authority_owner_instance_id") or "").strip():
        reasons.append("authority_owner_missing")
    if int(payload.get("authority_lease_epoch") or 0) <= 0:
        reasons.append("authority_epoch_missing")
    if lease_until is None or lease_until <= _now():
        reasons.append("authority_lease_expired")
    passed = not reasons
    result = {
        "status": "PASS" if passed else "FAIL",
        "reasons": reasons,
        "worker_id": payload.get("worker_id"),
        "partition_key": payload.get("authority_partition_key"),
        "owner_instance_id": payload.get("authority_owner_instance_id"),
        "lease_epoch": payload.get("authority_lease_epoch"),
        "lease_until": lease_until.isoformat() if lease_until else None,
        "heartbeat_at": payload.get("authority_heartbeat_at"),
        "fencing_enforced": bool(payload.get("authority_fencing_enforced")),
        "age_seconds": round(age, 3) if age is not None else None,
        "error": error,
    }
    return (HTTPStatus.OK if passed else HTTPStatus.SERVICE_UNAVAILABLE), result


def evaluate_operations_status(
    status_path: Path,
    *,
    max_age_seconds: float,
    require_slo_pass: bool,
) -> tuple[int, dict[str, Any]]:
    payload, error = _load_json(status_path)
    generated_at = _parse_time(payload.get("generated_at"))
    age = (_now() - generated_at).total_seconds() if generated_at else None
    reasons: list[str] = []
    if error is not None:
        reasons.append("operations_status_unreadable")
    if age is None or age > max_age_seconds:
        reasons.append("operations_status_stale")
    if require_slo_pass and str(payload.get("status") or "") != "PASS":
        reasons.append("slo_not_passing")
    passed = not reasons
    result = dict(payload)
    result.update(
        {
            "endpoint_status": "PASS" if passed else "FAIL",
            "endpoint_reasons": reasons,
            "age_seconds": round(age, 3) if age is not None else None,
            "error": error,
        }
    )
    return (HTTPStatus.OK if passed else HTTPStatus.SERVICE_UNAVAILABLE), result


def evaluate_deep(
    *,
    root: Path,
    manifest_path: Path,
    event_socket: Path,
) -> tuple[int, dict[str, Any]]:
    checks: dict[str, Any] = {}
    manifest, manifest_error = _load_json(manifest_path)
    manifest_issues = (
        [manifest_error] if manifest_error else verify_manifest(root, manifest)
    )
    checks["manifest"] = {"ok": not manifest_issues, "issues": manifest_issues}
    try:
        socket_ok = event_socket.exists() and stat.S_ISSOCK(event_socket.stat().st_mode)
    except OSError:
        socket_ok = False
    checks["event_socket"] = {"ok": socket_ok, "path": str(event_socket)}
    settings = _postgres_settings()
    started = time.perf_counter()
    db_error: str | None = None
    try:
        with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        db_error = f"{type(exc).__name__}: {exc}"
    checks["database"] = {
        "ok": db_error is None,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "host": settings.host,
        "port": settings.port,
        "error": db_error,
    }
    passed = all(bool(row.get("ok")) for row in checks.values())
    return (
        HTTPStatus.OK if passed else HTTPStatus.SERVICE_UNAVAILABLE,
        {
            "status": "PASS" if passed else "FAIL",
            "build_id": manifest.get("build_id"),
            "checks": checks,
        },
    )


def state_snapshot() -> dict[str, Any]:
    queries = {
        "intent_ids": "SELECT intent_id::text AS value FROM quant.paper_live_order_intents ORDER BY intent_id",
        "fill_keys": "SELECT audit_key || ':' || fill_index::text AS value FROM quant.paper_fills ORDER BY audit_key, fill_index",
        "ledger_keys": "SELECT idempotency_key AS value FROM quant.paper_ledger_entries ORDER BY idempotency_key",
    }
    collections: dict[str, list[str]] = {}
    with (
        postgres_connection(_postgres_settings(), readonly=True) as conn,
        conn.cursor() as cur,
    ):
        for name, sql in queries.items():
            cur.execute(sql)
            collections[name] = [str(row["value"]) for row in cur.fetchall()]
    return {
        "schema_version": "paper_worker_state_snapshot_v1",
        "generated_at": _now().isoformat(),
        "collections": collections,
        "counts": {name: len(values) for name, values in collections.items()},
    }


def verify_state(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    before_collections = before.get("collections") or {}
    after_collections = after.get("collections") or {}
    missing: dict[str, list[str]] = {}
    for name, values in before_collections.items():
        before_values = {str(value) for value in values}
        after_values = {str(value) for value in after_collections.get(name, [])}
        difference = sorted(before_values - after_values)
        if difference:
            missing[str(name)] = difference[:20]
    return {
        "schema_version": "paper_worker_state_verification_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS" if not missing else "FAIL",
        "before_counts": before.get("counts"),
        "after_counts": after.get("counts"),
        "missing": missing,
    }


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "PaperWorkerHealth/1"

    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, PaperHealthServer):
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        path = self.path.split("?", 1)[0]
        if path == "/health/live":
            code, payload = evaluate_live(
                server.status_path,
                max_age_seconds=server.max_age_seconds,
            )
        elif path == "/health/ready":
            code, payload = evaluate_ready(
                server.status_path,
                max_age_seconds=server.max_age_seconds,
            )
        elif path == "/health/deep":
            code, payload = evaluate_deep(
                root=server.root,
                manifest_path=server.manifest_path,
                event_socket=server.event_socket,
            )
        elif path == "/health/authority":
            code, payload = evaluate_authority(
                server.status_path,
                max_age_seconds=server.max_age_seconds,
            )
        elif path == "/health/slo":
            code, payload = evaluate_operations_status(
                server.operations_status_path,
                max_age_seconds=server.operations_max_age_seconds,
                require_slo_pass=True,
            )
        elif path == "/status":
            code, payload = evaluate_operations_status(
                server.operations_status_path,
                max_age_seconds=server.operations_max_age_seconds,
                require_slo_pass=False,
            )
        elif path == "/metrics":
            try:
                body = server.operations_metrics_path.read_text(encoding="utf-8")
            except OSError as exc:
                code = HTTPStatus.SERVICE_UNAVAILABLE
                payload = {
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                self._send_text(HTTPStatus.OK, body)
                return
        elif path == "/version":
            payload, error = _load_json(server.manifest_path)
            code = HTTPStatus.OK if error is None else HTTPStatus.SERVICE_UNAVAILABLE
            if error:
                payload = {"status": "FAIL", "error": error}
        else:
            code, payload = HTTPStatus.NOT_FOUND, {"status": "NOT_FOUND", "path": path}
        self._send_json(code, payload)

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, payload: Mapping[str, Any]) -> None:
        body = (json.dumps(payload, sort_keys=True, default=str) + "\n").encode("utf-8")
        self.send_response(int(code))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, payload: str) -> None:
        body = payload.encode("utf-8")
        self.send_response(int(code))
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PaperHealthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        root: Path,
        status_path: Path,
        manifest_path: Path,
        event_socket: Path,
        operations_status_path: Path,
        operations_metrics_path: Path,
        max_age_seconds: float,
        operations_max_age_seconds: float,
    ) -> None:
        super().__init__(address, _HealthHandler)
        self.root = root
        self.status_path = status_path
        self.manifest_path = manifest_path
        self.event_socket = event_socket
        self.operations_status_path = operations_status_path
        self.operations_metrics_path = operations_metrics_path
        self.max_age_seconds = max_age_seconds
        self.operations_max_age_seconds = operations_max_age_seconds


def _fetch_json(url: str, timeout: float) -> tuple[int, dict[str, Any]]:
    try:
        with urlopen(url, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"status": "HTTP_ERROR", "error": str(exc)}
        return int(exc.code), payload
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return 0, {
            "status": "CONNECTION_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_canary(
    base_url: str,
    *,
    timeout: float,
    expected_build_id: str | None,
    require_ready: bool,
    require_authority: bool,
) -> dict[str, Any]:
    endpoints: dict[str, Any] = {}
    for name in ("live", "ready", "deep", "authority"):
        code, payload = _fetch_json(f"{base_url.rstrip('/')}/health/{name}", timeout)
        endpoints[name] = {"http_status": code, "payload": payload}
    version_code, version = _fetch_json(f"{base_url.rstrip('/')}/version", timeout)
    endpoints["version"] = {"http_status": version_code, "payload": version}
    failures: list[str] = []
    if endpoints["live"]["http_status"] != HTTPStatus.OK:
        failures.append("live")
    if endpoints["deep"]["http_status"] != HTTPStatus.OK:
        failures.append("deep")
    if version_code != HTTPStatus.OK:
        failures.append("version")
    if expected_build_id and version.get("build_id") != expected_build_id:
        failures.append("build_id")
    if require_ready and endpoints["ready"]["http_status"] != HTTPStatus.OK:
        failures.append("ready")
    if require_authority and endpoints["authority"]["http_status"] != HTTPStatus.OK:
        failures.append("authority")
    blockers = [
        name
        for name in ("ready", "authority")
        if endpoints[name]["http_status"] != HTTPStatus.OK
    ]
    return {
        "schema_version": "paper_worker_deploy_canary_v1",
        "generated_at": _now().isoformat(),
        "status": "FAIL" if failures else "PASS_WITH_BLOCKERS" if blockers else "PASS",
        "failures": failures,
        "production_blockers": blockers,
        "endpoints": endpoints,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", action="append", type=Path, default=[])
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest")
    manifest.add_argument("--root", type=Path, default=PROJECT_ROOT)
    manifest.add_argument("--output", type=Path, default=DEFAULT_MANIFEST_PATH)
    manifest.add_argument("--source-git-sha")
    manifest.add_argument(
        "--source-dirty", choices=("true", "false", "unknown"), default="unknown"
    )

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--root", type=Path, default=PROJECT_ROOT)
    preflight.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    preflight.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    preflight.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    preflight.add_argument("--event-socket", type=Path, default=DEFAULT_EVENT_SOCKET)
    preflight.add_argument("--output", type=Path)
    preflight.add_argument("--skip-db", action="store_true")
    preflight.add_argument("--skip-event-socket", action="store_true")

    serve = sub.add_parser("serve")
    serve.add_argument(
        "--host", default=os.environ.get("PAPER_HEALTH_HOST", DEFAULT_HEALTH_HOST)
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PAPER_HEALTH_PORT", DEFAULT_HEALTH_PORT)),
    )
    serve.add_argument("--root", type=Path, default=PROJECT_ROOT)
    serve.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    serve.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    serve.add_argument("--event-socket", type=Path, default=DEFAULT_EVENT_SOCKET)
    serve.add_argument(
        "--operations-status-path",
        type=Path,
        default=DEFAULT_OPERATIONS_STATUS_PATH,
    )
    serve.add_argument(
        "--operations-metrics-path",
        type=Path,
        default=DEFAULT_OPERATIONS_METRICS_PATH,
    )
    serve.add_argument(
        "--max-age-seconds",
        type=float,
        default=float(os.environ.get("PAPER_HEALTH_MAX_AGE_SECONDS", "30")),
    )
    serve.add_argument(
        "--operations-max-age-seconds",
        type=float,
        default=float(os.environ.get("PAPER_OPERATIONS_MAX_AGE_SECONDS", "120")),
    )

    canary = sub.add_parser("canary")
    canary.add_argument(
        "--base-url", default=f"http://{DEFAULT_HEALTH_HOST}:{DEFAULT_HEALTH_PORT}"
    )
    canary.add_argument("--timeout", type=float, default=10)
    canary.add_argument("--expected-build-id")
    canary.add_argument("--require-ready", action="store_true")
    canary.add_argument("--require-authority", action="store_true")
    canary.add_argument("--output", type=Path)

    snapshot = sub.add_parser("state-snapshot")
    snapshot.add_argument("--output", type=Path, required=True)

    verify = sub.add_parser("verify-state")
    verify.add_argument("--before", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "preflight":
        _load_env_files(args.env_file)
    if args.command in {"serve", "state-snapshot", "verify-state"}:
        enforce_paper_security_boundary(
            environ=os.environ,
            source_root=PROJECT_ROOT,
        )
    if args.command == "manifest":
        source_dirty = (
            None if args.source_dirty == "unknown" else args.source_dirty == "true"
        )
        payload = build_manifest(
            args.root,
            source_git_sha=args.source_git_sha,
            source_dirty=source_dirty,
        )
        _write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "preflight":
        payload = run_preflight(
            root=args.root,
            manifest_path=args.manifest,
            status_path=args.status_path,
            archive_root=args.archive_root,
            event_socket=args.event_socket,
            env_files=args.env_file,
            check_db=not args.skip_db,
            check_event_socket=not args.skip_event_socket,
        )
        if args.output:
            _write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if payload["status"] == "FAIL" else 0
    if args.command == "serve":
        server = PaperHealthServer(
            (args.host, args.port),
            root=args.root,
            status_path=args.status_path,
            manifest_path=args.manifest,
            event_socket=args.event_socket,
            operations_status_path=args.operations_status_path,
            operations_metrics_path=args.operations_metrics_path,
            max_age_seconds=args.max_age_seconds,
            operations_max_age_seconds=args.operations_max_age_seconds,
        )
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if args.command == "canary":
        payload = run_canary(
            args.base_url,
            timeout=args.timeout,
            expected_build_id=args.expected_build_id,
            require_ready=args.require_ready,
            require_authority=args.require_authority,
        )
        if args.output:
            _write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if payload["status"] == "FAIL" else 0
    if args.command == "state-snapshot":
        payload = state_snapshot()
        _write_json(args.output, payload)
        print(json.dumps(payload["counts"], indent=2, sort_keys=True))
        return 0
    if args.command == "verify-state":
        before, error = _load_json(args.before)
        if error:
            print(json.dumps({"status": "FAIL", "error": error}, indent=2))
            return 1
        payload = verify_state(before, state_snapshot())
        if args.output:
            _write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if payload["status"] == "FAIL" else 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
