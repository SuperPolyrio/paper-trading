"""Value objects for the simulator-owned order-management layer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum


EXTERNAL_STRATEGY_ID = "EXTERNAL"


class OmsOrderState(str, Enum):
    WORKING = "WORKING"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class SelfTradePolicy(str, Enum):
    CANCEL_NEWEST = "CANCEL_NEWEST"
    CANCEL_OLDEST = "CANCEL_OLDEST"
    CANCEL_BOTH = "CANCEL_BOTH"
    REJECT_INCOMING = "REJECT_INCOMING"
    ALLOW_INTERNAL_CROSS_FOR_RESEARCH_ONLY = "ALLOW_INTERNAL_CROSS_FOR_RESEARCH_ONLY"


class OmsAdmissionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED_SELF_TRADE = "REJECTED_SELF_TRADE"
    DEFERRED_CANCEL_PENDING = "DEFERRED_CANCEL_PENDING"
    RESEARCH_ONLY_INTERNAL_CROSS = "RESEARCH_ONLY_INTERNAL_CROSS"


@dataclass(frozen=True)
class OwnOrder:
    order_id: str
    account_id: str
    strategy_id: str
    asset_id: str
    side: str
    price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    created_sequence: int
    state: OmsOrderState = OmsOrderState.WORKING
    external_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("order_id", "account_id", "strategy_id", "asset_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        side = str(self.side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("own order side must be BUY or SELL")
        if int(self.created_sequence) < 0:
            raise ValueError("created sequence must be non-negative")
        price = Decimal(self.price)
        original = Decimal(self.original_size)
        remaining = Decimal(self.remaining_size)
        if not Decimal("0") < price < Decimal("1"):
            raise ValueError("own order price must be strictly between zero and one")
        if original <= 0 or remaining < 0 or remaining > original:
            raise ValueError("own order size is invalid")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "original_size", original)
        object.__setattr__(self, "remaining_size", remaining)

    @property
    def is_live(self) -> bool:
        return self.state in {OmsOrderState.WORKING, OmsOrderState.PENDING_CANCEL} and self.remaining_size > 0

    @property
    def is_cancel_selectable(self) -> bool:
        return self.state is OmsOrderState.WORKING and self.remaining_size > 0

    def with_state(self, state: OmsOrderState) -> "OwnOrder":
        return replace(self, state=state)

    def with_remaining(self, remaining_size: Decimal) -> "OwnOrder":
        remaining = max(Decimal("0"), Decimal(remaining_size))
        return replace(
            self,
            remaining_size=remaining,
            state=OmsOrderState.FILLED if remaining == 0 else self.state,
        )


@dataclass(frozen=True)
class OmsAdmission:
    incoming: OwnOrder
    status: OmsAdmissionStatus
    reason: str
    cancel_order_ids: tuple[str, ...] = ()
