"""Simulator-owned working orders, kept separate from public BookState."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from enum import Enum

from .domain import OmsOrderState, OwnOrder


class CancelRequestStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ALREADY_PENDING = "ALREADY_PENDING"
    NOT_CANCELABLE = "NOT_CANCELABLE"


class OwnOrderBook:
    """In-memory OMS book; it never subtracts from or writes to public L2."""

    def __init__(self) -> None:
        self._orders: dict[str, OwnOrder] = {}
        self._cancel_events: dict[str, CancelRequestStatus] = {}

    def submit(self, order: OwnOrder) -> OwnOrder:
        existing = self._orders.get(order.order_id)
        if existing is not None:
            if existing != order:
                raise ValueError("order id collision in own-order book")
            return existing
        if order.state is not OmsOrderState.WORKING:
            raise ValueError("only working orders may enter own-order book")
        self._orders[order.order_id] = order
        return order

    def get(self, order_id: str) -> OwnOrder | None:
        return self._orders.get(str(order_id))

    def active_orders(self, *, account_id: str, asset_id: str | None = None) -> tuple[OwnOrder, ...]:
        return tuple(
            order
            for order in sorted(self._orders.values(), key=_order_sort_key)
            if order.account_id == account_id and order.is_live and (asset_id is None or order.asset_id == asset_id)
        )

    def crossing_orders(self, incoming: OwnOrder) -> tuple[OwnOrder, ...]:
        return tuple(
            order
            for order in self.active_orders(account_id=incoming.account_id, asset_id=incoming.asset_id)
            if order.order_id != incoming.order_id and _crosses(incoming, order)
        )

    def request_cancel(self, order_id: str, *, event_id: str) -> CancelRequestStatus:
        event_key = str(event_id)
        if event_key in self._cancel_events:
            return self._cancel_events[event_key]
        order = self._orders.get(str(order_id))
        if order is None or not order.is_live:
            outcome = CancelRequestStatus.NOT_CANCELABLE
        elif order.state is OmsOrderState.PENDING_CANCEL:
            outcome = CancelRequestStatus.ALREADY_PENDING
        else:
            self._orders[order.order_id] = order.with_state(OmsOrderState.PENDING_CANCEL)
            outcome = CancelRequestStatus.REQUESTED
        self._cancel_events[event_key] = outcome
        return outcome

    def acknowledge_cancel(self, order_id: str) -> OwnOrder:
        order = self._require(order_id)
        if order.state is OmsOrderState.CANCELED:
            return order
        if order.state is not OmsOrderState.PENDING_CANCEL:
            raise ValueError("cancel acknowledgement requires PENDING_CANCEL order")
        canceled = order.with_state(OmsOrderState.CANCELED)
        self._orders[order.order_id] = canceled
        return canceled

    def apply_fill(self, order_id: str, size) -> OwnOrder:
        order = self._require(order_id)
        if not order.is_live:
            raise ValueError("cannot fill non-live own order")
        filled = min(order.remaining_size, max(Decimal("0"), Decimal(size)))
        updated = order.with_remaining(order.remaining_size - filled)
        self._orders[order.order_id] = updated
        return updated

    def close_strategy(self, *, account_id: str, strategy_id: str, event_prefix: str) -> tuple[str, ...]:
        selected = [
            order.order_id
            for order in self.active_orders(account_id=account_id)
            if order.strategy_id == strategy_id and order.is_cancel_selectable
        ]
        for index, order_id in enumerate(selected):
            self.request_cancel(order_id, event_id=f"{event_prefix}:{index}:{order_id}")
        return tuple(selected)

    def upsert_external(self, order: OwnOrder) -> OwnOrder:
        if order.strategy_id != "EXTERNAL" or not order.external_reference:
            raise ValueError("external imports must use the EXTERNAL strategy and an external reference")
        existing = self._orders.get(order.order_id)
        if existing is None:
            if order.state is OmsOrderState.WORKING:
                return self.submit(order)
            self._orders[order.order_id] = order
            return order
        if (
            existing.account_id != order.account_id
            or existing.asset_id != order.asset_id
            or existing.side != order.side
            or existing.external_reference != order.external_reference
        ):
            raise ValueError("external order id collision")
        updated = replace(
            existing,
            price=order.price,
            original_size=max(existing.original_size, order.original_size),
            remaining_size=order.remaining_size,
            state=order.state,
        )
        self._orders[order.order_id] = updated
        return updated

    def _require(self, order_id: str) -> OwnOrder:
        order = self.get(order_id)
        if order is None:
            raise KeyError(f"unknown own order: {order_id}")
        return order


def _crosses(left: OwnOrder, right: OwnOrder) -> bool:
    if left.side == right.side:
        return False
    buy, sell = (left, right) if left.side == "BUY" else (right, left)
    return buy.price >= sell.price


def _order_sort_key(order: OwnOrder) -> tuple[int, str]:
    return (order.created_sequence, order.order_id)
