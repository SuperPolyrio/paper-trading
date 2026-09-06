"""Non-atomic multi-leg execution plans and explicit residual hedge exposure."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum


class AtomicityPolicy(str, Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL_NON_ATOMIC = "PARALLEL_NON_ATOMIC"
    ALL_OR_NONE_SIMULATED = "ALL_OR_NONE_SIMULATED"
    VENUE_ATOMIC = "VENUE_ATOMIC"


class LegState(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    FAILED = "FAILED"


class PlanState(str, Enum):
    PLANNED = "PLANNED"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    HEDGE_REQUIRED = "HEDGE_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ExecutionLeg:
    leg_id: str
    asset_id: str
    side: str
    size: Decimal
    max_unit_loss: Decimal
    state: LegState = LegState.PENDING
    filled_size: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.leg_id or not self.asset_id:
            raise ValueError("leg_id and asset_id are required")
        side = str(self.side).upper()
        size = Decimal(self.size)
        filled = Decimal(self.filled_size)
        max_loss = Decimal(self.max_unit_loss)
        if side not in {"BUY", "SELL"}:
            raise ValueError("execution leg side must be BUY or SELL")
        if size <= 0 or filled < 0 or filled > size or max_loss < 0:
            raise ValueError("execution leg economics are invalid")
        state = self.state
        if filled == size:
            state = LegState.FILLED
        elif filled > 0:
            state = LegState.PARTIAL
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "filled_size", filled)
        object.__setattr__(self, "max_unit_loss", max_loss)
        object.__setattr__(self, "state", state)

    @property
    def remaining_size(self) -> Decimal:
        return max(Decimal(0), self.size - self.filled_size)


@dataclass(frozen=True)
class MultiLegExecutionPlan:
    plan_id: str
    policy: AtomicityPolicy
    legs: tuple[ExecutionLeg, ...]
    hedge_timeout: timedelta
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.plan_id or len(self.legs) < 2:
            raise ValueError("multi-leg plan requires an id and at least two legs")
        if len({leg.leg_id for leg in self.legs}) != len(self.legs):
            raise ValueError("multi-leg plan leg ids must be unique")
        if self.hedge_timeout <= timedelta(0):
            raise ValueError("multi-leg hedge timeout must be positive")

    @property
    def state(self) -> PlanState:
        filled = sum(leg.state is LegState.FILLED for leg in self.legs)
        failed = sum(leg.state is LegState.FAILED for leg in self.legs)
        if filled == len(self.legs):
            return PlanState.COMPLETE
        if (
            self.policy
            in {AtomicityPolicy.ALL_OR_NONE_SIMULATED, AtomicityPolicy.VENUE_ATOMIC}
            and failed
        ):
            return PlanState.FAILED
        if any(leg.filled_size > 0 for leg in self.legs):
            return PlanState.HEDGE_REQUIRED
        if failed:
            return PlanState.FAILED
        return PlanState.PLANNED

    @property
    def remaining_leg_exposure(self) -> Decimal:
        return sum(
            (leg.remaining_size * leg.max_unit_loss for leg in self.legs),
            Decimal(0),
        )

    @property
    def worst_case_leg_loss(self) -> Decimal:
        return sum((leg.size * leg.max_unit_loss for leg in self.legs), Decimal("0"))

    def record_leg(self, leg_id: str, state: LegState) -> "MultiLegExecutionPlan":
        if state is LegState.PENDING:
            raise ValueError("leg result cannot remain pending")
        matched = False
        next_legs = []
        for leg in self.legs:
            if leg.leg_id != leg_id:
                next_legs.append(leg)
                continue
            matched = True
            if leg.state is not LegState.PENDING and leg.state is not state:
                raise ValueError("terminal leg result cannot change")
            next_legs.append(
                replace(
                    leg,
                    state=state,
                    filled_size=leg.size if state is LegState.FILLED else 0,
                )
            )
        if not matched:
            raise KeyError(f"unknown execution leg: {leg_id}")
        return replace(self, legs=tuple(next_legs))

    def record_leg_fill(
        self,
        leg_id: str,
        *,
        filled_size: Decimal,
        average_fill_price: Decimal | None,
        failed: bool = False,
    ) -> "MultiLegExecutionPlan":
        matched = False
        next_legs: list[ExecutionLeg] = []
        for leg in self.legs:
            if leg.leg_id != leg_id:
                next_legs.append(leg)
                continue
            matched = True
            filled = Decimal(filled_size)
            if leg.state in {LegState.FILLED, LegState.FAILED}:
                if filled != leg.filled_size:
                    raise ValueError("terminal leg result cannot change")
                next_legs.append(leg)
                continue
            state = (
                LegState.FAILED
                if failed and filled == 0
                else LegState.FILLED
                if filled == leg.size
                else LegState.PARTIAL
                if filled > 0
                else LegState.PENDING
            )
            next_legs.append(
                replace(
                    leg,
                    state=state,
                    filled_size=filled,
                    average_fill_price=average_fill_price,
                )
            )
        if not matched:
            raise KeyError(f"unknown execution leg: {leg_id}")
        return replace(self, legs=tuple(next_legs))
