"""Professional simulator SLO evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def evaluate_slos(metrics: Mapping[str, Any]) -> dict[str, Any]:
    execution_backlog_age = metrics.get("paper_execution_backlog_age_p99_ms")
    if execution_backlog_age is None:
        # Compatibility for pre-v3 soak artifacts. New reports must use the
        # paper execution queue rather than the global registry outbox.
        execution_backlog_age = metrics.get("outbox_pending_age_p99_ms")
    checks = {
        "book_freshness_p99": _lt(
            metrics.get("book_freshness_p99_ms"), metrics.get("execution_gate_ms")
        ),
        "prediction_p99": _lt(
            metrics.get("prediction_p99_ms"), metrics.get("prediction_budget_ms")
        ),
        "paper_execution_backlog_age_p99": _lt(execution_backlog_age, 500),
        "paper_order_recovery": _eq(metrics.get("paper_order_recovery_pct"), 100),
        "unsafe_fill_zero": _eq(metrics.get("unsafe_fill_count"), 0),
        "unknown_terminal_zero": _eq(metrics.get("unknown_terminal_order_count"), 0),
        "accounting_mismatch_zero": _eq(metrics.get("accounting_mismatch_count"), 0),
        "replay_parity": _eq(metrics.get("replay_parity_pct"), 100),
    }
    available = {key: value for key, value in checks.items() if value is not None}
    return {
        "status": (
            "PASS"
            if available and all(available.values()) and len(available) == len(checks)
            else "FAIL"
            if any(value is False for value in available.values())
            else "NOT_ENOUGH_DATA"
        ),
        "checks": checks,
    }


def _lt(value: Any, maximum: Any) -> bool | None:
    if value is None or maximum is None:
        return None
    return float(value) < float(maximum)


def _eq(value: Any, expected: Any) -> bool | None:
    if value is None:
        return None
    return value == expected
