"""Fail-closed execution backpressure gate."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackpressureSnapshot:
    intent_backlog: int
    db_writer_lag_ms: int
    book_apply_lag_ms: int
    ws_connected: bool
    clock_skew_ms: int
    model_registry_available: bool


@dataclass(frozen=True)
class BackpressureLimits:
    max_intent_backlog: int = 100
    max_db_writer_lag_ms: int = 2_000
    max_book_apply_lag_ms: int = 2_000
    max_clock_skew_ms: int = 500


def evaluate_backpressure(
    snapshot: BackpressureSnapshot,
    limits: BackpressureLimits | None = None,
) -> dict[str, object]:
    limit = limits or BackpressureLimits()
    reasons = []
    if snapshot.intent_backlog > limit.max_intent_backlog:
        reasons.append("intent_backlog")
    if snapshot.db_writer_lag_ms > limit.max_db_writer_lag_ms:
        reasons.append("db_writer_lag")
    if snapshot.book_apply_lag_ms > limit.max_book_apply_lag_ms:
        reasons.append("book_apply_lag")
    if not snapshot.ws_connected:
        reasons.append("ws_disconnected")
    if abs(snapshot.clock_skew_ms) > limit.max_clock_skew_ms:
        reasons.append("clock_skew")
    if not snapshot.model_registry_available:
        reasons.append("model_registry_unavailable")
    return {
        "status": "ACCEPT" if not reasons else "DEFER_OR_REJECT",
        "fail_closed": bool(reasons),
        "reasons": reasons,
    }
