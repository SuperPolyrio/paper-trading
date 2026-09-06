"""Non-atomic multi-leg order groups for neg-risk and arbitrage strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum


class OrderGroupState(str, Enum):
    PLANNED = "PLANNED"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    HEDGE_REQUIRED = "HEDGE_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class OrderGroupLeg:
    client_order_id: str
    asset_id: str
    side: str
    size: Decimal
    filled_size: Decimal = Decimal("0")


@dataclass(frozen=True)
class OrderGroup:
    group_id: str
    legs: tuple[OrderGroupLeg, ...]
    execution_policy: str
    max_leg_delay: timedelta
    hedge_policy: str

    @property
    def partial_completion_state(self) -> OrderGroupState:
        filled = sum(leg.filled_size > 0 for leg in self.legs)
        complete = sum(leg.filled_size >= leg.size for leg in self.legs)
        if complete == len(self.legs):
            return OrderGroupState.COMPLETE
        if filled:
            return OrderGroupState.HEDGE_REQUIRED
        return OrderGroupState.PLANNED
