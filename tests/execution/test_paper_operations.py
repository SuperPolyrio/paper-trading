from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quant.paper import acceptance
from quant.paper.operations import (
    PaperOperationsThresholds,
    calculate_error_budget,
    evaluate_operations,
    evaluate_paper_admission,
    write_operations_artifacts,
)


def test_book_freshness_excludes_fail_closed_data_rejections() -> None:
    source = Path(acceptance.__file__).read_text(encoding="utf-8")

    query_start = source.index("SELECT count(*) AS eligible_books")
    query_end = source.index('"""', query_start)
    query = source[query_start:query_end]
    assert "coverage_grade IN ('A_PLUS','A','B')" in query
    assert "%%stale%%" in query
    assert "%%gap%%" in query
    assert "%%data_not_ready%%" in query
    assert "%%data_unsafe%%" in query


def _green_metrics() -> dict[str, object]:
    return {
        "service_availability_pct": 100.0,
        "health_age_ms": 100,
        "ready_book_ratio": 1.0,
        "book_freshness_p99_ms": 100,
        "execution_gate_ms": 2_000,
        "order_acceptance_p95_ms": 100,
        "order_acceptance_p99_ms": 200,
        "order_acceptance_sample_count": 20,
        "terminal_result_p95_ms": 200,
        "terminal_result_p99_ms": 400,
        "terminal_result_sample_count": 20,
        "paper_execution_backlog_age_p99_ms": 20,
        "unsafe_fill_count": 0,
        "unknown_terminal_order_count": 0,
        "ledger_reconciliation_mismatch_count": 0,
        "reservation_mismatch_count": 0,
        "journal_hash_mismatch_count": 0,
        "negative_cash_count": 0,
        "invalid_position_count": 0,
        "database_available": True,
        "db_latency_ms": 5,
        "db_connection_saturation_pct": 10,
        "authority_held": True,
        "authority_fencing_enforced": True,
        "worker_lease_remaining_ms": 10_000,
        "deployment_version_skew": 0,
        "feed_divergence_count": 0,
        "event_lag_ms": 100,
    }


def test_green_operations_snapshot_accepts_paper_orders() -> None:
    result = evaluate_operations(_green_metrics())

    assert result["status"] == "PASS"
    assert result["operational_level"] == "GREEN"
    assert result["admission_mode"] == "ACCEPT"
    assert result["alerts"] == []


def test_error_budget_reports_burn_and_remaining_capacity() -> None:
    healthy = calculate_error_budget(100.0, target_pct=99.9)
    exhausted = calculate_error_budget(99.8, target_pct=99.9)

    assert healthy["error_budget_burn_rate"] == 0
    assert healthy["error_budget_remaining_pct"] == 100
    assert float(exhausted["error_budget_burn_rate"] or 0) > 1
    assert exhausted["error_budget_remaining_pct"] == 0


def test_unsafe_fill_forces_red_fail_closed() -> None:
    metrics = _green_metrics()
    metrics["unsafe_fill_count"] = 1

    result = evaluate_operations(metrics)

    assert result["status"] == "FAIL"
    assert result["operational_level"] == "RED"
    assert result["admission_mode"] == "FAIL_CLOSED"
    assert result["alerts"][0]["alert_id"] == "UNSAFE_FILL_ZERO"


def test_execution_latency_failure_enters_read_only_mode() -> None:
    metrics = _green_metrics()
    metrics["terminal_result_p99_ms"] = 2_500

    result = evaluate_operations(metrics)

    assert result["status"] == "FAIL"
    assert result["operational_level"] == "ORANGE"
    assert result["admission_mode"] == "READ_ONLY"


def test_partial_ready_book_degradation_keeps_small_taker_available() -> None:
    metrics = _green_metrics()
    metrics["ready_book_ratio"] = 0.96

    result = evaluate_operations(metrics)

    assert result["status"] == "FAIL"
    assert result["operational_level"] == "YELLOW"
    assert result["admission_mode"] == "CALIBRATED_TAKER_SMALL_ONLY"
    assert result["alerts"][0]["alert_id"] == "READY_BOOK_RATIO"


def test_systemic_ready_book_degradation_enters_read_only_mode() -> None:
    metrics = _green_metrics()
    metrics["ready_book_ratio"] = 0.89

    result = evaluate_operations(metrics)

    assert result["status"] == "FAIL"
    assert result["operational_level"] == "ORANGE"
    assert result["admission_mode"] == "READ_ONLY"


def test_missing_order_samples_are_yellow_not_fabricated_pass() -> None:
    metrics = _green_metrics()
    metrics["order_acceptance_p95_ms"] = None
    metrics["order_acceptance_p99_ms"] = None

    result = evaluate_operations(metrics)

    assert result["status"] == "NOT_ENOUGH_DATA"
    assert result["operational_level"] == "YELLOW"
    assert result["admission_mode"] == "CALIBRATED_TAKER_SMALL_ONLY"


def test_single_slow_order_is_not_treated_as_a_latency_distribution() -> None:
    metrics = _green_metrics()
    metrics.update(
        {
            "order_acceptance_p95_ms": 713.233,
            "order_acceptance_p99_ms": 713.233,
            "order_acceptance_sample_count": 1,
            "terminal_result_p95_ms": 888.141,
            "terminal_result_p99_ms": 888.141,
            "terminal_result_sample_count": 1,
        }
    )

    result = evaluate_operations(metrics)

    assert result["status"] == "NOT_ENOUGH_DATA"
    assert result["operational_level"] == "YELLOW"
    assert result["admission_mode"] == "CALIBRATED_TAKER_SMALL_ONLY"
    assert result["checks"]["order_acceptance_p95"] is None
    assert result["checks"]["terminal_result_p95"] is None


def test_artifacts_emit_prometheus_and_alert_transitions(tmp_path: Path) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    red = evaluate_operations({**_green_metrics(), "unknown_terminal_order_count": 1})
    snapshot = {
        "schema_version": "paper_operations_snapshot_v1",
        "generated_at": generated_at,
        "status": red["status"],
        "operational_level": red["operational_level"],
        "admission_mode": red["admission_mode"],
        "metrics": {**_green_metrics(), "unknown_terminal_order_count": 1},
        "checks": red["checks"],
        "alerts": red["alerts"],
    }
    write_operations_artifacts(tmp_path, snapshot)

    metrics_text = (tmp_path / "metrics.prom").read_text(encoding="utf-8")
    events = [
        json.loads(line)
        for line in (tmp_path / "alert-events.jsonl").read_text().splitlines()
    ]
    assert 'poly_quant_paper_operations_level{level="RED"} 1' in metrics_text
    assert events[0]["event"] == "FIRING"

    green = evaluate_operations(_green_metrics())
    recovered = {
        **snapshot,
        "generated_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "status": green["status"],
        "operational_level": green["operational_level"],
        "admission_mode": green["admission_mode"],
        "metrics": _green_metrics(),
        "checks": green["checks"],
        "alerts": green["alerts"],
    }
    write_operations_artifacts(tmp_path, recovered)
    events = [
        json.loads(line)
        for line in (tmp_path / "alert-events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["event"] == "RESOLVED"
    assert json.loads((tmp_path / "alerts.json").read_text())["alerts"] == []


def test_custom_queue_threshold_is_honored() -> None:
    metrics = _green_metrics()
    metrics["paper_execution_backlog_age_p99_ms"] = 200
    result = evaluate_operations(
        metrics,
        thresholds=PaperOperationsThresholds(
            execution_queue_age_p99_ms=100,
        ),
    )
    assert result["checks"]["execution_queue_age"] is False


def test_operational_admission_enforces_green_yellow_orange_and_staleness() -> None:
    now = datetime.now(timezone.utc)
    base = {"generated_at": now.isoformat()}

    green = evaluate_paper_admission(
        {
            **base,
            "status": "PASS",
            "operational_level": "GREEN",
            "admission_mode": "ACCEPT",
        },
        post_only=False,
        time_in_force="FOK",
        order_notional=Decimal("100"),
        now=now,
    )
    yellow_taker = evaluate_paper_admission(
        {
            **base,
            "status": "NOT_ENOUGH_DATA",
            "operational_level": "YELLOW",
            "admission_mode": "CALIBRATED_TAKER_SMALL_ONLY",
        },
        post_only=False,
        time_in_force="FAK",
        order_notional=Decimal("5"),
        now=now,
    )
    yellow_maker = evaluate_paper_admission(
        {
            **base,
            "status": "NOT_ENOUGH_DATA",
            "operational_level": "YELLOW",
            "admission_mode": "CALIBRATED_TAKER_SMALL_ONLY",
        },
        post_only=True,
        time_in_force="GTC",
        order_notional=Decimal("5"),
        now=now,
    )
    orange = evaluate_paper_admission(
        {
            **base,
            "status": "FAIL",
            "operational_level": "ORANGE",
            "admission_mode": "READ_ONLY",
        },
        post_only=False,
        time_in_force="FOK",
        order_notional=Decimal("1"),
        now=now,
    )
    stale = evaluate_paper_admission(
        {
            "generated_at": (now - timedelta(minutes=5)).isoformat(),
            "status": "PASS",
            "operational_level": "GREEN",
            "admission_mode": "ACCEPT",
        },
        post_only=False,
        time_in_force="FOK",
        order_notional=Decimal("1"),
        now=now,
        max_age_seconds=30,
    )

    assert green.allowed
    assert yellow_taker.allowed
    assert not yellow_maker.allowed
    assert "yellow_rejects_maker" in yellow_maker.reasons
    assert not orange.allowed and orange.admission_mode == "READ_ONLY"
    assert stale.reasons == ("operations_status_stale",)
