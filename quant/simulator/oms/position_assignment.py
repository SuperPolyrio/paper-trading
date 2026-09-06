"""Stable order-to-strategy ownership assignment for a shared account."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import EXTERNAL_STRATEGY_ID


@dataclass(frozen=True)
class PositionAssignment:
    order_id: str
    account_id: str
    strategy_id: str
    asset_id: str


class PositionAssignmentBook:
    def __init__(self) -> None:
        self._by_order: dict[str, PositionAssignment] = {}

    def assign(self, assignment: PositionAssignment) -> PositionAssignment:
        existing = self._by_order.get(assignment.order_id)
        if existing is not None and existing != assignment:
            raise ValueError("order ownership cannot be reassigned")
        self._by_order[assignment.order_id] = assignment
        return assignment

    def assign_external(self, *, order_id: str, account_id: str, asset_id: str) -> PositionAssignment:
        return self.assign(PositionAssignment(order_id, account_id, EXTERNAL_STRATEGY_ID, asset_id))

    def get(self, order_id: str) -> PositionAssignment | None:
        return self._by_order.get(str(order_id))
