"""Shared, source-book-immutable counterfactual liquidity coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Any, Iterable

from .allocation import AllocationOrder, AllocationRequest, AllocationResult, AllocationStatus, LiquidityLevelKey


@dataclass
class OverlayLevel:
    key: LiquidityLevelKey
    displayed_size: Decimal
    reserved_size: Decimal = Decimal("0")
    consumed_size: Decimal = Decimal("0")
    released_size: Decimal = Decimal("0")
    first_source_event_id: str = ""
    last_source_event_id: str = ""
    overlay_version: str = "global-liquidity-v1"
    last_order: AllocationOrder | None = None

    @property
    def remaining_size(self) -> Decimal:
        return max(Decimal("0"), self.displayed_size - self.reserved_size - self.consumed_size + self.released_size)


class SharedLiquidityOverlay:
    """One coordinator shared by strategies/accounts/workers in a simulator run.

    It never mutates the source BookState. A newer book generation has a new
    key, so historical-generation commands remain idempotent after snapshots.
    """

    def __init__(self) -> None:
        self._levels: dict[LiquidityLevelKey, OverlayLevel] = {}
        self._allocations: dict[str, AllocationResult] = {}
        self._lock = RLock()

    def allocate(self, request: AllocationRequest) -> AllocationResult:
        with self._lock:
            existing = self._allocations.get(request.allocation_id)
            if existing is not None:
                return existing
            level = self._levels.get(request.level)
            if level is None:
                level = OverlayLevel(
                    key=request.level,
                    displayed_size=request.displayed_size,
                    first_source_event_id=request.source_event_id,
                    last_source_event_id=request.source_event_id,
                    overlay_version=request.overlay_version,
                )
                self._levels[request.level] = level
            if level.last_order is not None and request.order < level.last_order:
                return AllocationResult(
                    request.allocation_id,
                    AllocationStatus.DEFERRED_OUT_OF_ORDER,
                    Decimal("0"),
                    level.remaining_size,
                    "arrival_order_precedes_already_allocated_request",
                    request.level,
                )
            if level.last_order == request.order:
                raise ValueError("distinct allocations cannot share one allocation order on a level")
            allocated = min(request.requested_size, level.remaining_size)
            level.consumed_size += allocated
            level.last_order = request.order
            level.last_source_event_id = request.source_event_id
            result = AllocationResult(
                request.allocation_id,
                AllocationStatus.ALLOCATED,
                allocated,
                level.remaining_size,
                "allocated" if allocated == request.requested_size else "visible_depth_exhausted",
                request.level,
            )
            self._allocations[request.allocation_id] = result
            return result

    def allocate_many(self, requests: Iterable[AllocationRequest]) -> tuple[AllocationResult, ...]:
        """The scheduler/coordinator calls this after sorting all same-window requests."""
        return tuple(self.allocate(request) for request in sorted(requests, key=lambda row: (_level_sort_key(row.level), row.order)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "levels": [
                    {
                        "key": {
                            "venue": level.key.venue,
                            "asset_id": level.key.asset_id,
                            "book_generation": level.key.book_generation,
                            "arrival_window_id": level.key.arrival_window_id,
                            "side": level.key.side,
                            "price_tick": format(level.key.price_tick, "f"),
                        },
                        "displayed_size": format(level.displayed_size, "f"),
                        "reserved_size": format(level.reserved_size, "f"),
                        "consumed_size": format(level.consumed_size, "f"),
                        "released_size": format(level.released_size, "f"),
                        "remaining_size": format(level.remaining_size, "f"),
                        "first_source_event_id": level.first_source_event_id,
                        "last_source_event_id": level.last_source_event_id,
                        "overlay_version": level.overlay_version,
                        "last_order": None if level.last_order is None else {
                            "arrival_ts_ns": level.last_order.arrival_ts_ns,
                            "strategy_priority": level.last_order.strategy_priority,
                            "account_id": level.last_order.account_id,
                            "deterministic_order_id": level.last_order.deterministic_order_id,
                        },
                    }
                    for level in sorted(self._levels.values(), key=lambda row: row.key)
                ],
                "allocations": [
                    {
                        "allocation_id": result.allocation_id,
                        "status": result.status.value,
                        "allocated_size": format(result.allocated_size, "f"),
                        "remaining_size": format(result.remaining_size, "f"),
                        "reason": result.reason,
                        "level": _level_json(result.level),
                    }
                    for _, result in sorted(self._allocations.items())
                ],
            }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "SharedLiquidityOverlay":
        overlay = cls()
        for row in snapshot.get("levels") or []:
            key = _level_from_json(row["key"])
            order = row.get("last_order")
            overlay._levels[key] = OverlayLevel(
                key=key,
                displayed_size=Decimal(row["displayed_size"]),
                reserved_size=Decimal(row["reserved_size"]),
                consumed_size=Decimal(row["consumed_size"]),
                released_size=Decimal(row["released_size"]),
                first_source_event_id=str(row["first_source_event_id"]),
                last_source_event_id=str(row["last_source_event_id"]),
                overlay_version=str(row["overlay_version"]),
                last_order=None if order is None else AllocationOrder(**order),
            )
        for row in snapshot.get("allocations") or []:
            level = _level_from_json(row["level"])
            overlay._allocations[str(row["allocation_id"])] = AllocationResult(
                allocation_id=str(row["allocation_id"]),
                status=AllocationStatus(str(row["status"])),
                allocated_size=Decimal(row["allocated_size"]),
                remaining_size=Decimal(row["remaining_size"]),
                reason=str(row["reason"]),
                level=level,
            )
        return overlay


def _level_sort_key(key: LiquidityLevelKey) -> tuple[str, str, int, str, str, Decimal]:
    return (key.venue, key.asset_id, key.book_generation, key.arrival_window_id, key.side, key.price_tick)


def _level_json(key: LiquidityLevelKey) -> dict[str, Any]:
    return {
        "venue": key.venue,
        "asset_id": key.asset_id,
        "book_generation": key.book_generation,
        "arrival_window_id": key.arrival_window_id,
        "side": key.side,
        "price_tick": format(key.price_tick, "f"),
    }


def _level_from_json(value: dict[str, Any]) -> LiquidityLevelKey:
    return LiquidityLevelKey(
        venue=str(value["venue"]),
        asset_id=str(value["asset_id"]),
        book_generation=int(value["book_generation"]),
        arrival_window_id=str(value["arrival_window_id"]),
        side=str(value["side"]),
        price_tick=Decimal(value["price_tick"]),
    )
