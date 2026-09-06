"""Persistence policy for sampled local order book snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Literal

from .local_book import BookMetrics


BookEventType = Literal["snapshot", "price_change", "resnapshot", "status"]


@dataclass(frozen=True)
class SnapshotPersistenceState:
    generation: int
    status: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread_bucket: int | None
    imbalance_bucket: int | None
    persisted_at_ms: int | None


@dataclass(frozen=True)
class SnapshotPersistDecision:
    persist: bool
    reason: str
    storage_tier: str
    state: SnapshotPersistenceState


def should_persist_snapshot(
    metrics: BookMetrics,
    *,
    event_type: BookEventType,
    previous: SnapshotPersistenceState | None = None,
    now_ms: int | None = None,
    focused: bool = False,
    strategy_monitored: bool = False,
    sample_interval_ms: int = 5_000,
    spread_bucket_size: Decimal = Decimal("0.01"),
    imbalance_bucket_size: Decimal = Decimal("0.10"),
) -> SnapshotPersistDecision:
    """Decide whether the current book projection should be persisted."""

    state = state_from_metrics(
        metrics,
        persisted_at_ms=int(now_ms) if now_ms is not None else None,
        spread_bucket_size=spread_bucket_size,
        imbalance_bucket_size=imbalance_bucket_size,
    )
    tier = "debug" if metrics.status != "ready" else ("high" if focused or strategy_monitored else "sampled")
    if previous is None:
        return SnapshotPersistDecision(True, "first_projection", tier, state)
    if event_type in {"snapshot", "resnapshot"} and metrics.generation != previous.generation:
        return SnapshotPersistDecision(True, "new_snapshot_generation", tier, state)
    if metrics.status != previous.status:
        return SnapshotPersistDecision(True, "status_transition", tier, state)
    if focused or strategy_monitored:
        return SnapshotPersistDecision(True, "priority_token", tier, state)
    if metrics.best_bid != previous.best_bid or metrics.best_ask != previous.best_ask:
        return SnapshotPersistDecision(True, "bbo_changed", tier, state)
    if state.spread_bucket != previous.spread_bucket:
        return SnapshotPersistDecision(True, "spread_bucket_changed", tier, state)
    if state.imbalance_bucket != previous.imbalance_bucket:
        return SnapshotPersistDecision(True, "imbalance_bucket_changed", tier, state)
    if now_ms is not None and previous.persisted_at_ms is not None:
        if int(now_ms) - int(previous.persisted_at_ms) >= int(sample_interval_ms):
            return SnapshotPersistDecision(True, "sample_interval_elapsed", tier, state)
    return SnapshotPersistDecision(False, "unchanged", tier, state)


def state_from_metrics(
    metrics: BookMetrics,
    *,
    persisted_at_ms: int | None,
    spread_bucket_size: Decimal = Decimal("0.01"),
    imbalance_bucket_size: Decimal = Decimal("0.10"),
) -> SnapshotPersistenceState:
    return SnapshotPersistenceState(
        generation=int(metrics.generation),
        status=str(metrics.status),
        best_bid=metrics.best_bid,
        best_ask=metrics.best_ask,
        spread_bucket=_bucket(metrics.spread, spread_bucket_size),
        imbalance_bucket=_bucket(metrics.depth_imbalance, imbalance_bucket_size),
        persisted_at_ms=persisted_at_ms,
    )


def _bucket(value: Decimal | None, bucket_size: Decimal) -> int | None:
    if value is None:
        return None
    size = max(Decimal("0.0000000001"), Decimal(bucket_size))
    return int((value / size).to_integral_value(rounding=ROUND_FLOOR))
