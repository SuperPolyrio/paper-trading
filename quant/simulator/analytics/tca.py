"""Transparent paper TCA decomposition with explicit no-fill opportunity cost."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class TcaInput:
    order_id: str
    side: str
    requested_size: Decimal
    filled_size: Decimal
    decision_price: Decimal
    arrival_price: Decimal
    executed_vwap: Decimal | None
    fee: Decimal
    opportunity_reference_price: Decimal | None = None
    markouts: Mapping[str, Decimal] | None = None


@dataclass(frozen=True)
class TcaReport:
    delay_cost: Decimal
    book_walk_cost: Decimal
    fee_cost: Decimal
    opportunity_cost: Decimal
    implementation_shortfall: Decimal
    unfilled_quantity: Decimal
    adverse_selection_markouts: dict[str, Decimal]


def build_tca(source: TcaInput) -> TcaReport:
    side = str(source.side).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("TCA side must be BUY or SELL")
    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    requested = max(Decimal("0"), Decimal(source.requested_size))
    filled = min(requested, max(Decimal("0"), Decimal(source.filled_size)))
    decision = Decimal(source.decision_price)
    arrival = Decimal(source.arrival_price)
    executed = (
        arrival if source.executed_vwap is None else Decimal(source.executed_vwap)
    )
    delay_cost = direction * (arrival - decision) * filled
    book_walk_cost = direction * (executed - arrival) * filled
    unfilled = requested - filled
    opportunity = Decimal("0")
    if source.opportunity_reference_price is not None:
        opportunity = max(
            Decimal("0"),
            direction
            * (Decimal(source.opportunity_reference_price) - decision)
            * unfilled,
        )
    fee = max(Decimal("0"), Decimal(source.fee))
    markouts = {
        str(key): Decimal(value) for key, value in (source.markouts or {}).items()
    }
    return TcaReport(
        delay_cost=delay_cost,
        book_walk_cost=book_walk_cost,
        fee_cost=fee,
        opportunity_cost=opportunity,
        implementation_shortfall=delay_cost + book_walk_cost + fee + opportunity,
        unfilled_quantity=unfilled,
        adverse_selection_markouts=markouts,
    )
