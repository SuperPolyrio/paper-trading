"""Paper execution SLO snapshot, alerts, metrics, and status artifacts."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant.core.db import postgres_connection

from .acceptance import build_acceptance_report

DEFAULT_STATUS_PATH = Path("runtime_outputs/paper_live_shadow/status.json")
DEFAULT_MANIFEST_PATH = Path("runtime_outputs/production/paper-worker-build.json")
DEFAULT_OUTPUT_DIR = Path("runtime_outputs/paper_operations")
SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
LEVEL_ORDER = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}


@dataclass(frozen=True)
class PaperOperationsThresholds:
    health_stale_seconds: float = 30.0
    availability_pct: float = 99.9
    ready_book_ratio: float = 0.98
    ready_book_ratio_read_only: float = 0.90
    order_acceptance_p95_ms: float = 300.0
    order_acceptance_p99_ms: float = 1_000.0
    terminal_result_p95_ms: float = 500.0
    terminal_result_p99_ms: float = 2_000.0
    execution_latency_min_samples: int = 20
    execution_queue_age_p99_ms: float = 500.0
    db_latency_warning_ms: float = 50.0
    db_connection_saturation_pct: float = 80.0
    event_lag_warning_ms: float = 30_000.0
    lease_remaining_critical_ms: float = 1_000.0


@dataclass(frozen=True)
class PaperOperationsAdmissionDecision:
    allowed: bool
    operational_level: str
    admission_mode: str
    reasons: tuple[str, ...] = ()


def calculate_error_budget(
    availability_pct: float | None,
    *,
    target_pct: float,
) -> dict[str, float | None]:
    allowed_error_pct = max(0.0, 100.0 - float(target_pct))
    observed_error_pct = (
        max(0.0, 100.0 - float(availability_pct))
        if availability_pct is not None
        else None
    )
    burn_rate = (
        observed_error_pct / allowed_error_pct
        if observed_error_pct is not None and allowed_error_pct > 0
        else None
    )
    return {
        "service_error_rate_pct": observed_error_pct,
        "error_budget_allowed_pct": allowed_error_pct,
        "error_budget_burn_rate": burn_rate,
        "error_budget_remaining_pct": (
            max(0.0, 100.0 * (1.0 - burn_rate)) if burn_rate is not None else None
        ),
    }


def evaluate_paper_admission(
    snapshot: Mapping[str, Any],
    *,
    post_only: bool,
    time_in_force: str,
    order_notional: Decimal,
    now: datetime | None = None,
    max_age_seconds: float = 90.0,
    yellow_max_notional: Decimal = Decimal("20"),
) -> PaperOperationsAdmissionDecision:
    observed_at = _utc(now or _now())
    generated_at = _parse_datetime(snapshot.get("generated_at"))
    if generated_at is None:
        return _admission_block("RED", "FAIL_CLOSED", "operations_status_invalid")
    if (observed_at - generated_at).total_seconds() > max(1.0, max_age_seconds):
        return _admission_block("RED", "FAIL_CLOSED", "operations_status_stale")
    level = str(snapshot.get("operational_level") or "RED").upper()
    mode = str(snapshot.get("admission_mode") or "FAIL_CLOSED").upper()
    if level == "GREEN" and mode == "ACCEPT" and snapshot.get("status") == "PASS":
        return PaperOperationsAdmissionDecision(True, level, mode)
    if level == "YELLOW" and mode == "CALIBRATED_TAKER_SMALL_ONLY":
        reasons: list[str] = []
        if post_only:
            reasons.append("yellow_rejects_maker")
        if str(time_in_force).upper() not in {"FOK", "FAK"}:
            reasons.append("yellow_rejects_resting_order")
        try:
            notional = Decimal(str(order_notional))
        except InvalidOperation:
            reasons.append("order_notional_invalid")
        else:
            if notional > max(Decimal(0), yellow_max_notional):
                reasons.append("yellow_order_notional_exceeded")
        return PaperOperationsAdmissionDecision(
            not reasons,
            level,
            mode,
            tuple(reasons),
        )
    if level == "ORANGE" or mode == "READ_ONLY":
        return _admission_block("ORANGE", "READ_ONLY", "operations_read_only")
    return _admission_block("RED", "FAIL_CLOSED", "operations_fail_closed")


def load_paper_admission(
    status_path: Path,
    **kwargs: Any,
) -> PaperOperationsAdmissionDecision:
    snapshot, error = _read_json(status_path)
    if error:
        return _admission_block("RED", "FAIL_CLOSED", "operations_status_unavailable")
    return evaluate_paper_admission(snapshot, **kwargs)


def _admission_block(
    level: str,
    mode: str,
    reason: str,
) -> PaperOperationsAdmissionDecision:
    return PaperOperationsAdmissionDecision(False, level, mode, (reason,))


def build_operations_snapshot(
    *,
    start_at: datetime,
    status_path: Path = DEFAULT_STATUS_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    connection_factory: Any = postgres_connection,
    thresholds: PaperOperationsThresholds | None = None,
) -> dict[str, Any]:
    observed_at = _now()
    selected_thresholds = thresholds or PaperOperationsThresholds()
    worker_status, status_error = _read_json(status_path)
    manifest, manifest_error = _read_json(manifest_path)
    acceptance: dict[str, Any] = {}
    acceptance_error: str | None = None
    started = time.perf_counter()
    try:
        acceptance = build_acceptance_report(
            start_at=start_at,
            audit_start_at=start_at,
            minimum_hours=0,
            min_intents=0,
            health_stale_seconds=selected_thresholds.health_stale_seconds,
            connection_factory=connection_factory,
        )
    except Exception as exc:  # noqa: BLE001 - operations must emit a RED artifact
        acceptance_error = f"{type(exc).__name__}: {exc}"
    collection_latency_ms = (time.perf_counter() - started) * 1_000
    platform, platform_error = _collect_platform_metrics(
        start_at=start_at,
        connection_factory=connection_factory,
    )
    metrics = _flatten_metrics(
        observed_at=observed_at,
        worker_status=worker_status,
        manifest=manifest,
        acceptance=acceptance,
        platform=platform,
        collection_latency_ms=collection_latency_ms,
        availability_target_pct=selected_thresholds.availability_pct,
    )
    evaluation = evaluate_operations(
        metrics,
        thresholds=selected_thresholds,
        source_errors={
            "worker_status": status_error,
            "manifest": manifest_error,
            "acceptance": acceptance_error,
            "platform": platform_error,
        },
    )
    return {
        "schema_version": "paper_operations_snapshot_v1",
        "generated_at": observed_at.isoformat(),
        "window_start_at": _utc(start_at).isoformat(),
        "status": evaluation["status"],
        "operational_level": evaluation["operational_level"],
        "admission_mode": evaluation["admission_mode"],
        "thresholds": asdict(selected_thresholds),
        "metrics": metrics,
        "checks": evaluation["checks"],
        "alerts": evaluation["alerts"],
        "source_errors": evaluation["source_errors"],
        "sources": {
            "worker_status_path": str(status_path.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "worker_id": worker_status.get("worker_id"),
            "build_id": manifest.get("build_id"),
        },
        "acceptance_status": acceptance.get("status"),
    }


def evaluate_operations(
    metrics: Mapping[str, Any],
    *,
    thresholds: PaperOperationsThresholds | None = None,
    source_errors: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    selected = thresholds or PaperOperationsThresholds()
    errors = {key: value for key, value in (source_errors or {}).items() if value}
    order_latency_ready = (
        int(metrics.get("order_acceptance_sample_count") or 0)
        >= selected.execution_latency_min_samples
    )
    terminal_latency_ready = (
        int(metrics.get("terminal_result_sample_count") or 0)
        >= selected.execution_latency_min_samples
    )
    checks: dict[str, bool | None] = {
        "service_availability": _gte(
            metrics.get("service_availability_pct"), selected.availability_pct
        ),
        "worker_health_freshness": _lt(
            metrics.get("health_age_ms"), selected.health_stale_seconds * 1_000
        ),
        "ready_book_ratio": _gte(
            metrics.get("ready_book_ratio"), selected.ready_book_ratio
        ),
        "book_freshness": _lt(
            metrics.get("book_freshness_p99_ms"),
            metrics.get("execution_gate_ms"),
        ),
        "order_acceptance_p95": (
            _lt(
                metrics.get("order_acceptance_p95_ms"),
                selected.order_acceptance_p95_ms,
            )
            if order_latency_ready
            else None
        ),
        "order_acceptance_p99": (
            _lt(
                metrics.get("order_acceptance_p99_ms"),
                selected.order_acceptance_p99_ms,
            )
            if order_latency_ready
            else None
        ),
        "terminal_result_p95": (
            _lt(
                metrics.get("terminal_result_p95_ms"),
                selected.terminal_result_p95_ms,
            )
            if terminal_latency_ready
            else None
        ),
        "terminal_result_p99": (
            _lt(
                metrics.get("terminal_result_p99_ms"),
                selected.terminal_result_p99_ms,
            )
            if terminal_latency_ready
            else None
        ),
        "execution_queue_age": _lt(
            metrics.get("paper_execution_backlog_age_p99_ms"),
            selected.execution_queue_age_p99_ms,
        ),
        "unsafe_fill_zero": _eq(metrics.get("unsafe_fill_count"), 0),
        "unknown_terminal_zero": _eq(metrics.get("unknown_terminal_order_count"), 0),
        "ledger_mismatch_zero": _eq(
            metrics.get("ledger_reconciliation_mismatch_count"), 0
        ),
        "reservation_mismatch_zero": _eq(metrics.get("reservation_mismatch_count"), 0),
        "journal_mismatch_zero": _eq(metrics.get("journal_hash_mismatch_count"), 0),
        "negative_cash_zero": _eq(metrics.get("negative_cash_count"), 0),
        "invalid_position_zero": _eq(metrics.get("invalid_position_count"), 0),
        "database_available": _eq(metrics.get("database_available"), True),
        "database_connection_headroom": _lt(
            metrics.get("db_connection_saturation_pct"),
            selected.db_connection_saturation_pct,
        ),
        "authority_held": _eq(metrics.get("authority_held"), True),
        "authority_fencing": _eq(metrics.get("authority_fencing_enforced"), True),
        "authority_lease_remaining": _gte(
            metrics.get("worker_lease_remaining_ms"),
            selected.lease_remaining_critical_ms,
        ),
        "deployment_version_match": _eq(metrics.get("deployment_version_skew"), 0),
    }
    alerts: list[dict[str, Any]] = []
    for source, error in sorted(errors.items()):
        alerts.append(
            _alert(
                f"SOURCE_{source.upper()}_FAILED",
                "CRITICAL" if source in {"acceptance", "platform"} else "WARNING",
                f"{source} collection failed",
                error,
                "source-collection-failure",
            )
        )
    critical_checks = {
        "unsafe_fill_zero": "unsafe-fill",
        "unknown_terminal_zero": "unknown-order-terminal",
        "ledger_mismatch_zero": "journal-mismatch",
        "reservation_mismatch_zero": "journal-mismatch",
        "journal_mismatch_zero": "journal-mismatch",
        "negative_cash_zero": "journal-mismatch",
        "invalid_position_zero": "journal-mismatch",
        "database_available": "db-high-latency",
        "authority_held": "worker-lost-lease",
        "authority_fencing": "worker-lost-lease",
        "authority_lease_remaining": "worker-lost-lease",
    }
    orange_checks = {
        "worker_health_freshness": "service-availability",
        "book_freshness": "lob-feed-divergence",
        "order_acceptance_p95": "execution-latency",
        "order_acceptance_p99": "execution-latency",
        "terminal_result_p95": "execution-latency",
        "terminal_result_p99": "execution-latency",
        "execution_queue_age": "execution-backlog",
        "database_connection_headroom": "db-high-latency",
        "deployment_version_match": "deployment-version-skew",
    }
    for name, runbook in critical_checks.items():
        if checks[name] is False:
            alerts.append(
                _alert(
                    name.upper(),
                    "CRITICAL",
                    f"critical SLO check failed: {name}",
                    _metric_evidence(name, metrics),
                    runbook,
                )
            )
    for name, runbook in orange_checks.items():
        if checks[name] is False:
            alerts.append(
                _alert(
                    name.upper(),
                    "WARNING",
                    f"operational SLO check failed: {name}",
                    _metric_evidence(name, metrics),
                    runbook,
                    operational_level="ORANGE",
                )
            )
    if checks["ready_book_ratio"] is False:
        try:
            ready_ratio = float(metrics["ready_book_ratio"])
        except (KeyError, TypeError, ValueError):
            ready_ratio = None
        read_only = (
            ready_ratio is None
            or ready_ratio < selected.ready_book_ratio_read_only
        )
        alerts.append(
            _alert(
                "READY_BOOK_RATIO",
                "WARNING",
                "operational SLO check failed: ready_book_ratio",
                _metric_evidence("ready_book_ratio", metrics),
                "lob-feed-divergence",
                operational_level="ORANGE" if read_only else "YELLOW",
            )
        )
    if checks["service_availability"] is False:
        alerts.append(
            _alert(
                "SERVICE_AVAILABILITY",
                "WARNING",
                "service availability is below target",
                metrics.get("service_availability_pct"),
                "service-availability",
            )
        )
    for name, value in checks.items():
        if value is None:
            alerts.append(
                _alert(
                    f"METRIC_MISSING_{name.upper()}",
                    "WARNING",
                    f"SLO evidence is not yet available: {name}",
                    None,
                    "missing-slo-evidence",
                    operational_level="YELLOW",
                )
            )
    if _gte(metrics.get("feed_divergence_count"), 1) is True:
        alerts.append(
            _alert(
                "FEED_DIVERGENCE",
                "WARNING",
                "one or more watched books disagree across feeds",
                metrics.get("feed_divergence_count"),
                "lob-feed-divergence",
                operational_level="YELLOW",
            )
        )
    if _gte(metrics.get("event_lag_ms"), selected.event_lag_warning_ms) is True:
        alerts.append(
            _alert(
                "EVENT_LAG",
                "WARNING",
                "paper market-event processing is stale",
                metrics.get("event_lag_ms"),
                "lob-feed-divergence",
                operational_level="YELLOW",
            )
        )
    if _gte(metrics.get("db_latency_ms"), selected.db_latency_warning_ms) is True:
        alerts.append(
            _alert(
                "DB_LATENCY",
                "WARNING",
                "paper authority DB latency is elevated",
                metrics.get("db_latency_ms"),
                "db-high-latency",
                operational_level="YELLOW",
            )
        )
    level = "GREEN"
    for alert in alerts:
        candidate = str(alert["operational_level"])
        if LEVEL_ORDER[candidate] > LEVEL_ORDER[level]:
            level = candidate
    observed = [value for value in checks.values() if value is not None]
    status = (
        "FAIL"
        if any(value is False for value in observed)
        else "NOT_ENOUGH_DATA"
        if len(observed) != len(checks)
        else "PASS"
    )
    return {
        "status": status,
        "operational_level": level,
        "admission_mode": {
            "GREEN": "ACCEPT",
            "YELLOW": "CALIBRATED_TAKER_SMALL_ONLY",
            "ORANGE": "READ_ONLY",
            "RED": "FAIL_CLOSED",
        }[level],
        "checks": checks,
        "alerts": sorted(
            alerts,
            key=lambda row: (
                -LEVEL_ORDER[str(row["operational_level"])],
                -SEVERITY_ORDER[str(row["severity"])],
                str(row["alert_id"]),
            ),
        ),
        "source_errors": errors,
    }


def write_operations_artifacts(output_dir: Path, snapshot: Mapping[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(snapshot)
    _write_json(output_dir / "latest.json", payload)
    _write_json(output_dir / "status.json", payload)
    _write_json(output_dir / "metrics.json", dict(payload.get("metrics") or {}))
    _write_json(
        output_dir / "alerts.json",
        {
            "generated_at": payload.get("generated_at"),
            "operational_level": payload.get("operational_level"),
            "alerts": payload.get("alerts") or [],
        },
    )
    (output_dir / "metrics.prom").write_text(
        render_prometheus_metrics(payload), encoding="utf-8"
    )
    (output_dir / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    _append_alert_transitions(output_dir, payload)


def render_prometheus_metrics(snapshot: Mapping[str, Any]) -> str:
    lines = [
        "# HELP poly_quant_paper_operations_level Current paper operational level.",
        "# TYPE poly_quant_paper_operations_level gauge",
    ]
    current_level = str(snapshot.get("operational_level") or "RED")
    for level in LEVEL_ORDER:
        lines.append(
            f'poly_quant_paper_operations_level{{level="{level}"}} '
            f"{1 if level == current_level else 0}"
        )
    for key, value in sorted((snapshot.get("metrics") or {}).items()):
        numeric = _numeric(value)
        if numeric is None:
            continue
        name = re.sub(r"[^a-zA-Z0-9_]", "_", str(key)).lower()
        if name.startswith("paper_"):
            name = name.removeprefix("paper_")
        lines.append(f"# TYPE poly_quant_paper_{name} gauge")
        lines.append(f"poly_quant_paper_{name} {numeric}")
    for alert in snapshot.get("alerts") or []:
        lines.append(
            "poly_quant_paper_alert{"
            f'alert_id="{alert["alert_id"]}",'
            f'severity="{alert["severity"]}",'
            f'level="{alert["operational_level"]}"'
            "} 1"
        )
    return "\n".join(lines) + "\n"


def render_markdown(snapshot: Mapping[str, Any]) -> str:
    alerts = list(snapshot.get("alerts") or [])
    rows = [
        "# Paper Operations Report",
        "",
        f"- Generated: `{snapshot.get('generated_at')}`",
        f"- SLO status: `{snapshot.get('status')}`",
        f"- Operational level: `{snapshot.get('operational_level')}`",
        f"- Admission mode: `{snapshot.get('admission_mode')}`",
        "",
        "## Alerts",
        "",
    ]
    if not alerts:
        rows.append("No active alerts.")
    else:
        rows.extend(
            f"- `{row['severity']}` `{row['alert_id']}`: {row['summary']} "
            f"(runbook: `{row['runbook']}`)"
            for row in alerts
        )
    rows.extend(["", "## SLO Checks", "", "| Check | Result |", "|---|---|"])
    rows.extend(
        f"| `{name}` | `{value if value is not None else 'NOT_ENOUGH_DATA'}` |"
        for name, value in (snapshot.get("checks") or {}).items()
    )
    return "\n".join(rows) + "\n"


def _collect_platform_metrics(
    *,
    start_at: datetime,
    connection_factory: Any,
) -> tuple[dict[str, Any], str | None]:
    started = time.perf_counter()
    try:
        with connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS active_connections,
                       current_setting('max_connections')::int AS max_connections
                FROM pg_stat_activity
                WHERE datname=current_database()
                """
            )
            connections = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE unknown_outcome=TRUE) AS dlq_size,
                       count(*) FILTER (
                           WHERE unknown_outcome=TRUE
                             AND updated_at < clock_timestamp() - interval '3 minutes'
                       ) AS unknown_terminal_order_count
                FROM quant.paper_inflight_commands
                WHERE updated_at >= %s
                """,
                (_utc(start_at),),
            )
            inflight = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT count(*) FILTER (
                           WHERE event_type ILIKE '%%REVERS%%'
                              OR state IN ('FAILED','VOIDED')
                       ) AS finality_reversal_count
                FROM quant.paper_execution_finality_events
                WHERE event_ts >= %s
                """,
                (_utc(start_at),),
            )
            finality = dict(cur.fetchone() or {})
            cur.execute(
                "SELECT to_regclass('quant.paper_execution_market_catalog')::text "
                "AS relation"
            )
            catalog_available = bool((cur.fetchone() or {}).get("relation"))
            coverage: dict[str, int] = {}
            if catalog_available:
                cur.execute(
                    """
                    SELECT coverage_grade,count(*) AS token_count
                    FROM quant.paper_execution_market_catalog
                    WHERE execution_eligible=TRUE
                      AND active=TRUE
                      AND closed=FALSE
                      AND resolved=FALSE
                    GROUP BY coverage_grade
                    """
                )
                coverage = {
                    str(row["coverage_grade"] or "UNKNOWN").upper(): int(
                        row["token_count"] or 0
                    )
                    for row in cur.fetchall()
                }
        active = int(connections.get("active_connections") or 0)
        maximum = int(connections.get("max_connections") or 0)
        return (
            {
                "database_available": True,
                "db_latency_ms": round((time.perf_counter() - started) * 1_000, 3),
                "db_active_connections": active,
                "db_max_connections": maximum,
                "db_connection_saturation_pct": (
                    active / maximum * 100 if maximum else None
                ),
                "dlq_size": int(inflight.get("dlq_size") or 0),
                "platform_unknown_terminal_order_count": int(
                    inflight.get("unknown_terminal_order_count") or 0
                ),
                "finality_reversal_count": int(
                    finality.get("finality_reversal_count") or 0
                ),
                "coverage_grade_distribution": coverage,
                "execution_catalog_available": catalog_available,
                **{
                    f"coverage_grade_{grade.lower()}_count": count
                    for grade, count in coverage.items()
                },
            },
            None,
        )
    except Exception as exc:  # noqa: BLE001 - status artifacts must survive DB loss
        return (
            {
                "database_available": False,
                "db_latency_ms": round((time.perf_counter() - started) * 1_000, 3),
            },
            f"{type(exc).__name__}: {exc}",
        )


def _flatten_metrics(
    *,
    observed_at: datetime,
    worker_status: Mapping[str, Any],
    manifest: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    platform: Mapping[str, Any],
    collection_latency_ms: float,
    availability_target_pct: float,
) -> dict[str, Any]:
    acceptance_metrics = acceptance.get("metrics") or {}
    slo = acceptance_metrics.get("slo_snapshot") or {}
    samples = acceptance_metrics.get("samples") or {}
    account = acceptance_metrics.get("account_consistency") or {}
    positions = acceptance_metrics.get("position_consistency") or {}
    health_age = _age_ms(observed_at, worker_status.get("updated_at"))
    event_lag = _age_ms(observed_at, worker_status.get("last_message_at"))
    lease_remaining = _remaining_ms(
        observed_at, worker_status.get("authority_lease_until")
    )
    sample_count = int(samples.get("samples") or 0)
    unavailable = int(samples.get("disconnected_samples") or 0)
    service_availability = (
        (sample_count - unavailable) / sample_count * 100 if sample_count else None
    )
    error_budget = calculate_error_budget(
        service_availability,
        target_pct=availability_target_pct,
    )
    watched = int(worker_status.get("watched_assets") or 0)
    ready = int(worker_status.get("ready_books") or 0)
    manifest_build = str(manifest.get("build_id") or "")
    worker_build = str(worker_status.get("build_id") or "")
    deployment_skew = (
        int(manifest_build != worker_build) if manifest_build and worker_build else None
    )
    unknown_terminal = max(
        int(slo.get("unknown_terminal_order_count") or 0),
        int(platform.get("platform_unknown_terminal_order_count") or 0),
    )
    ledger_mismatch = (
        int(account.get("cash_reconciliation_mismatches") or 0)
        + int(positions.get("position_reconciliation_mismatches") or 0)
        + int(slo.get("accounting_mismatch_count") or 0)
    )
    return {
        "service_availability_pct": service_availability,
        **error_budget,
        "health_age_ms": health_age,
        "ready_book_ratio": ready / watched if watched else None,
        "ready_books": ready,
        "watched_assets": watched,
        "book_freshness_p99_ms": slo.get("book_freshness_p99_ms"),
        "execution_gate_ms": slo.get("execution_gate_ms"),
        "feed_divergence_count": int(worker_status.get("feed_mismatch_assets") or 0),
        "gap_count": int(worker_status.get("resyncing_assets") or 0),
        "rest_rebuild_count": int(worker_status.get("rest_resync_books") or 0),
        "order_acceptance_sample_count": int(
            slo.get("order_acceptance_sample_count") or 0
        ),
        "order_acceptance_p95_ms": slo.get("order_acceptance_p95_ms"),
        "order_acceptance_p99_ms": slo.get("order_acceptance_p99_ms"),
        "decision_to_admission_p95_ms": slo.get("decision_to_admission_p95_ms"),
        "decision_to_admission_p99_ms": slo.get("decision_to_admission_p99_ms"),
        "admission_to_arrival_p95_ms": slo.get("admission_to_arrival_p95_ms"),
        "admission_to_arrival_p99_ms": slo.get("admission_to_arrival_p99_ms"),
        "arrival_to_result_p95_ms": slo.get("arrival_to_result_p95_ms"),
        "arrival_to_result_p99_ms": slo.get("arrival_to_result_p99_ms"),
        "terminal_result_sample_count": int(
            slo.get("terminal_result_sample_count") or 0
        ),
        "terminal_result_p95_ms": slo.get("terminal_result_p95_ms"),
        "terminal_result_p99_ms": slo.get("terminal_result_p99_ms"),
        "paper_execution_backlog_age_p99_ms": slo.get(
            "paper_execution_backlog_age_p99_ms"
        ),
        "unsafe_fill_count": int(slo.get("unsafe_fill_count") or 0),
        "unknown_terminal_order_count": unknown_terminal,
        "ledger_reconciliation_mismatch_count": ledger_mismatch,
        "reservation_mismatch_count": int(slo.get("reservation_mismatch_count") or 0),
        "journal_hash_mismatch_count": int(slo.get("journal_hash_mismatch_count") or 0),
        "negative_cash_count": int(account.get("negative_cash_accounts") or 0),
        "invalid_position_count": int(positions.get("negative_positions") or 0),
        "capacity_reject_count": int(worker_status.get("risk_rejections") or 0),
        "event_lag_ms": event_lag,
        "authority_held": worker_status.get("authority_state") == "HELD",
        "authority_fencing_enforced": bool(
            worker_status.get("authority_fencing_enforced")
        ),
        "worker_lease_remaining_ms": lease_remaining,
        "deployment_version_skew": deployment_skew,
        "operations_collection_latency_ms": round(collection_latency_ms, 3),
        **platform,
    }


def _alert(
    alert_id: str,
    severity: str,
    summary: str,
    evidence: Any,
    runbook: str,
    *,
    operational_level: str | None = None,
) -> dict[str, Any]:
    level = operational_level or ("RED" if severity == "CRITICAL" else "YELLOW")
    return {
        "alert_id": alert_id,
        "severity": severity,
        "operational_level": level,
        "summary": summary,
        "evidence": evidence,
        "runbook": runbook,
    }


def _metric_evidence(name: str, metrics: Mapping[str, Any]) -> dict[str, Any]:
    prefixes = {
        "unsafe_fill_zero": ("unsafe_fill_count",),
        "unknown_terminal_zero": ("unknown_terminal_order_count",),
        "ledger_mismatch_zero": ("ledger_reconciliation_mismatch_count",),
        "reservation_mismatch_zero": ("reservation_mismatch_count",),
        "journal_mismatch_zero": ("journal_hash_mismatch_count",),
        "negative_cash_zero": ("negative_cash_count",),
        "invalid_position_zero": ("invalid_position_count",),
        "database_available": ("database_available", "db_latency_ms"),
        "authority_held": ("authority_held",),
        "authority_fencing": ("authority_fencing_enforced",),
        "authority_lease_remaining": ("worker_lease_remaining_ms",),
        "worker_health_freshness": ("health_age_ms",),
        "ready_book_ratio": ("ready_book_ratio",),
        "book_freshness": ("book_freshness_p99_ms", "execution_gate_ms"),
        "order_acceptance_p95": (
            "order_acceptance_p95_ms",
            "order_acceptance_sample_count",
        ),
        "order_acceptance_p99": (
            "order_acceptance_p99_ms",
            "order_acceptance_sample_count",
        ),
        "terminal_result_p95": (
            "terminal_result_p95_ms",
            "terminal_result_sample_count",
        ),
        "terminal_result_p99": (
            "terminal_result_p99_ms",
            "terminal_result_sample_count",
        ),
        "execution_queue_age": ("paper_execution_backlog_age_p99_ms",),
        "database_connection_headroom": ("db_connection_saturation_pct",),
        "deployment_version_match": ("deployment_version_skew",),
    }
    return {key: metrics.get(key) for key in prefixes.get(name, ())}


def _append_alert_transitions(output_dir: Path, snapshot: Mapping[str, Any]) -> None:
    state_path = output_dir / "alert-state.json"
    previous, _ = _read_json(state_path)
    previous_alerts = {
        str(row.get("alert_id")): row
        for row in previous.get("alerts") or []
        if row.get("alert_id")
    }
    current_alerts = {
        str(row.get("alert_id")): row
        for row in snapshot.get("alerts") or []
        if row.get("alert_id")
    }
    transitions: list[dict[str, Any]] = []
    for alert_id, alert in current_alerts.items():
        if alert_id not in previous_alerts:
            transitions.append(
                {
                    "event": "FIRING",
                    "alert_id": alert_id,
                    "at": snapshot.get("generated_at"),
                    "alert": alert,
                }
            )
    for alert_id, alert in previous_alerts.items():
        if alert_id not in current_alerts:
            transitions.append(
                {
                    "event": "RESOLVED",
                    "alert_id": alert_id,
                    "at": snapshot.get("generated_at"),
                    "alert": alert,
                }
            )
    if transitions:
        with (output_dir / "alert-events.jsonl").open("a", encoding="utf-8") as handle:
            for row in transitions:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    _write_json(
        state_path,
        {
            "generated_at": snapshot.get("generated_at"),
            "alerts": list(current_alerts.values()),
        },
    )


def _numeric(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return format(float(value), ".12g")


def _age_ms(now: datetime, value: Any) -> float | None:
    parsed = _parse_datetime(value)
    return max(0.0, (now - parsed).total_seconds() * 1_000) if parsed else None


def _remaining_ms(now: datetime, value: Any) -> float | None:
    parsed = _parse_datetime(value)
    return (parsed - now).total_seconds() * 1_000 if parsed else None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _lt(value: Any, maximum: Any) -> bool | None:
    if value is None or maximum is None:
        return None
    return float(value) < float(maximum)


def _gte(value: Any, minimum: Any) -> bool | None:
    if value is None or minimum is None:
        return None
    return float(value) >= float(minimum)


def _eq(value: Any, expected: Any) -> bool | None:
    return None if value is None else value == expected


def _read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, "JSON root is not an object"
    return payload, None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _utc(value: datetime) -> datetime:
    selected = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return selected.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-minutes", type=float, default=15.0)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="return non-zero when the current SLO snapshot is not PASS",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot = build_operations_snapshot(
        start_at=_now() - timedelta(minutes=max(1.0, args.window_minutes)),
        status_path=args.status_path,
        manifest_path=args.manifest_path,
    )
    write_operations_artifacts(args.output_dir, snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 1 if args.strict_exit and snapshot["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
