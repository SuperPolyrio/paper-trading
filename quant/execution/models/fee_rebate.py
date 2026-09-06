"""Versioned fees and non-cash rebate lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from quant.simulator.economics import FeeEngine, FeeSchedule as UnifiedFeeSchedule


class RebateState(str, Enum):
    ESTIMATED = "ESTIMATED"
    ACCRUED = "ACCRUED"
    RECEIVED = "RECEIVED"


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    effective_from: datetime
    fee_rate: Decimal
    minimum_rounding_unit: Decimal = Decimal("0.00001")
    fee_exponent: Decimal = Decimal(1)

    def fee(self, *, shares: Decimal, price: Decimal) -> Decimal:
        """Compatibility adapter to the authoritative dynamic fee engine."""

        schedule = UnifiedFeeSchedule(
            schedule_id=f"compat:{self.version}",
            asset_id="compat",
            condition_id="compat",
            effective_from=self.effective_from,
            effective_until=None,
            platform_fee_rate=self.fee_rate,
            platform_fee_exponent=self.fee_exponent,
            rounding_unit=self.minimum_rounding_unit,
            source="LEGACY_FEE_SCHEDULE_ADAPTER",
        )
        return FeeEngine.calculate(
            fill_id=f"compat:{self.version}:{price}:{shares}",
            schedule=schedule,
            liquidity_role="TAKER",
            price=price,
            shares=shares,
        ).total_fee


@dataclass(frozen=True)
class RebateRecord:
    rebate_id: str
    rebate_type: str
    schedule_version: str
    amount: Decimal
    state: RebateState
    estimated_at: datetime
    accrued_at: datetime | None = None
    received_at: datetime | None = None

    @property
    def confirmed_cash(self) -> Decimal:
        return self.amount if self.state == RebateState.RECEIVED else Decimal("0")


def transition_rebate(
    record: RebateRecord,
    target: RebateState,
    *,
    at: datetime,
) -> RebateRecord:
    from dataclasses import replace

    allowed = {
        RebateState.ESTIMATED: {RebateState.ACCRUED, RebateState.RECEIVED},
        RebateState.ACCRUED: {RebateState.RECEIVED},
        RebateState.RECEIVED: set(),
    }
    if target == record.state:
        return record
    if target not in allowed[record.state]:
        raise ValueError(
            f"invalid rebate transition {record.state.value} -> {target.value}"
        )
    return replace(
        record,
        state=target,
        accrued_at=at if target == RebateState.ACCRUED else record.accrued_at,
        received_at=at if target == RebateState.RECEIVED else record.received_at,
    )
