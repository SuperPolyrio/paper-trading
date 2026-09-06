"""Stable request and result contracts for shared simulated liquidity."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True, order=True)
class AllocationOrder:
    arrival_ts_ns: int
    strategy_priority: int
    account_id: str
    deterministic_order_id: str

    def __post_init__(self) -> None:
        if self.arrival_ts_ns < 0 or not self.account_id or not self.deterministic_order_id:
            raise ValueError("allocation order requires timestamp, account and deterministic order id")


@dataclass(frozen=True)
class LiquidityLevelKey:
    venue: str
    asset_id: str
    book_generation: int
    arrival_window_id: str
    side: str
    price_tick: Decimal

    def __post_init__(self) -> None:
        if not self.venue or not self.asset_id or not self.arrival_window_id or self.book_generation < 0:
            raise ValueError("liquidity level key is incomplete")
        object.__setattr__(self, "side", str(self.side).upper())
        object.__setattr__(self, "price_tick", Decimal(self.price_tick))


@dataclass(frozen=True)
class AllocationRequest:
    allocation_id: str
    order: AllocationOrder
    strategy_id: str
    level: LiquidityLevelKey
    displayed_size: Decimal
    requested_size: Decimal
    source_event_id: str
    overlay_version: str = "global-liquidity-v1"

    def __post_init__(self) -> None:
        if not self.allocation_id or not self.strategy_id or not self.source_event_id:
            raise ValueError("allocation id, strategy id and source event id are required")
        if Decimal(self.displayed_size) < 0 or Decimal(self.requested_size) < 0:
            raise ValueError("liquidity sizes must be non-negative")
        object.__setattr__(self, "displayed_size", Decimal(self.displayed_size))
        object.__setattr__(self, "requested_size", Decimal(self.requested_size))


class AllocationStatus(str, Enum):
    ALLOCATED = "ALLOCATED"
    DEFERRED_OUT_OF_ORDER = "DEFERRED_OUT_OF_ORDER"
    RELEASED = "RELEASED"


@dataclass(frozen=True)
class AllocationResult:
    allocation_id: str
    status: AllocationStatus
    allocated_size: Decimal
    remaining_size: Decimal
    reason: str
    level: LiquidityLevelKey
