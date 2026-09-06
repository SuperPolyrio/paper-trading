"""Resource requirements for durable position operations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import PositionOperationIntent


class PositionOperationReservationError(ValueError):
    """Raised when an operation cannot reserve its required paper resources."""


@dataclass(frozen=True)
class PositionOperationReservationRequirements:
    reserved_cash: Decimal
    reserved_tokens: dict[str, Decimal]


def operation_reservation_requirements(
    intent: PositionOperationIntent,
) -> PositionOperationReservationRequirements:
    """Return collateral and token debits that must be frozen before submission."""

    return PositionOperationReservationRequirements(
        reserved_cash=max(-intent.collateral_delta, Decimal(0)),
        reserved_tokens={
            asset_id: -delta
            for asset_id, delta in intent.token_deltas.items()
            if delta < 0
        },
    )
