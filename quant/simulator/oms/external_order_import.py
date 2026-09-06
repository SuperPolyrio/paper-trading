"""Import manually placed venue orders under the explicit EXTERNAL bucket."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import EXTERNAL_STRATEGY_ID, OmsOrderState, OwnOrder
from .own_order_book import OwnOrderBook
from .position_assignment import PositionAssignmentBook


@dataclass(frozen=True)
class ExternalOrderSnapshot:
    venue_order_id: str
    account_id: str
    asset_id: str
    side: str
    price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    observed_sequence: int
    state: OmsOrderState = OmsOrderState.WORKING


class ExternalOrderImporter:
    def __init__(self, assignments: PositionAssignmentBook) -> None:
        self.assignments = assignments

    def import_snapshot(self, book: OwnOrderBook, snapshot: ExternalOrderSnapshot) -> OwnOrder:
        order_id = f"external:{snapshot.venue_order_id}"
        order = OwnOrder(
            order_id=order_id,
            account_id=snapshot.account_id,
            strategy_id=EXTERNAL_STRATEGY_ID,
            asset_id=snapshot.asset_id,
            side=snapshot.side,
            price=Decimal(snapshot.price),
            original_size=Decimal(snapshot.original_size),
            remaining_size=Decimal(snapshot.remaining_size),
            created_sequence=int(snapshot.observed_sequence),
            state=snapshot.state,
            external_reference=snapshot.venue_order_id,
        )
        imported = book.upsert_external(order)
        self.assignments.assign_external(order_id=order_id, account_id=imported.account_id, asset_id=imported.asset_id)
        return imported
