"""In-flight submit, fill, cancel, expiry, and finality state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from quant.execution.domain import InternalOrderState

TERMINAL_STATES = {
    InternalOrderState.CONFIRMED,
    InternalOrderState.FAILED_REVERSED,
    InternalOrderState.CANCELED,
    InternalOrderState.REJECTED,
}


@dataclass(frozen=True)
class InflightOrder:
    order_id: str
    client_order_id: str
    requested_size: Decimal
    remaining_size: Decimal
    state: InternalOrderState
    created_at: datetime
    submit_arrival_ts: datetime | None = None
    cancel_request_ts: datetime | None = None
    cancel_arrival_ts: datetime | None = None
    cancel_ack_ts: datetime | None = None
    replace_request_ts: datetime | None = None
    replace_arrival_ts: datetime | None = None
    provisional_filled_size: Decimal = Decimal("0")
    confirmed_filled_size: Decimal = Decimal("0")
    queue_priority_epoch: int = 0


class InflightOrderMachine:
    def __init__(self, order: InflightOrder) -> None:
        self.order = order
        self._event_ids: set[str] = set()

    def risk_accept(self, event_id: str) -> InflightOrder:
        return self._transition(event_id, InternalOrderState.RISK_ACCEPTED)

    def schedule_submit(
        self,
        event_id: str,
        *,
        request_ts: datetime,
        latency: timedelta,
    ) -> InflightOrder:
        self._require_not_terminal()
        if not self._dedupe(event_id):
            return self.order
        self.order = replace(
            self.order,
            state=InternalOrderState.SUBMIT_INFLIGHT,
            submit_arrival_ts=request_ts + max(timedelta(0), latency),
        )
        return self.order

    def venue_accept(self, event_id: str, *, event_ts: datetime) -> InflightOrder:
        if self.order.submit_arrival_ts and event_ts < self.order.submit_arrival_ts:
            raise ValueError("venue cannot accept before submit arrival")
        return self._transition(event_id, InternalOrderState.WORKING)

    def request_cancel(
        self,
        event_id: str,
        *,
        request_ts: datetime,
        latency: timedelta,
    ) -> InflightOrder:
        self._require_not_terminal()
        if not self._dedupe(event_id):
            return self.order
        self.order = replace(
            self.order,
            state=InternalOrderState.CANCEL_INFLIGHT,
            cancel_request_ts=request_ts,
            cancel_arrival_ts=request_ts + max(timedelta(0), latency),
        )
        return self.order

    def provisional_fill(
        self,
        event_id: str,
        *,
        event_ts: datetime,
        size: Decimal,
    ) -> InflightOrder:
        self._require_not_terminal()
        if self.order.submit_arrival_ts and event_ts < self.order.submit_arrival_ts:
            raise ValueError("look-ahead fill before order arrival")
        if (
            self.order.cancel_arrival_ts is not None
            and event_ts >= self.order.cancel_arrival_ts
        ):
            raise ValueError("fill arrived after cancel became effective")
        if not self._dedupe(event_id):
            return self.order
        filled = min(self.order.remaining_size, max(Decimal("0"), size))
        remaining = self.order.remaining_size - filled
        state = (
            InternalOrderState.MATCHED_PROVISIONAL
            if remaining == 0
            else InternalOrderState.PARTIALLY_MATCHED_PROVISIONAL
        )
        self.order = replace(
            self.order,
            remaining_size=remaining,
            provisional_filled_size=self.order.provisional_filled_size + filled,
            state=state,
        )
        return self.order

    def cancel_ack(self, event_id: str, *, event_ts: datetime) -> InflightOrder:
        if (
            self.order.cancel_arrival_ts is None
            or event_ts < self.order.cancel_arrival_ts
        ):
            raise ValueError("cancel ack precedes cancel arrival")
        if not self._dedupe(event_id):
            return self.order
        self.order = replace(
            self.order,
            state=InternalOrderState.CANCELED,
            cancel_ack_ts=event_ts,
        )
        return self.order

    def confirm(self, event_id: str) -> InflightOrder:
        if not self._dedupe(event_id):
            return self.order
        self.order = replace(
            self.order,
            state=InternalOrderState.CONFIRMED,
            confirmed_filled_size=self.order.provisional_filled_size,
        )
        return self.order

    def fail_and_reverse(self, event_id: str) -> InflightOrder:
        if not self._dedupe(event_id):
            return self.order
        self.order = replace(
            self.order,
            state=InternalOrderState.FAILED_REVERSED,
            remaining_size=self.order.requested_size,
            provisional_filled_size=Decimal("0"),
            confirmed_filled_size=Decimal("0"),
        )
        return self.order

    def replace_order(
        self,
        event_id: str,
        *,
        request_ts: datetime,
        latency: timedelta,
        new_size: Decimal,
    ) -> InflightOrder:
        self._require_not_terminal()
        if not self._dedupe(event_id):
            return self.order
        if self.order.replace_request_ts is not None:
            raise ValueError("replacement already pending")
        self.order = replace(
            self.order,
            state=InternalOrderState.REPLACE_REQUESTED,
            replace_request_ts=request_ts,
            replace_arrival_ts=request_ts + max(timedelta(0), latency),
            requested_size=new_size,
            remaining_size=new_size,
            queue_priority_epoch=self.order.queue_priority_epoch + 1,
        )
        return self.order

    def _transition(self, event_id: str, state: InternalOrderState) -> InflightOrder:
        self._require_not_terminal()
        if self._dedupe(event_id):
            self.order = replace(self.order, state=state)
        return self.order

    def _dedupe(self, event_id: str) -> bool:
        key = str(event_id)
        if key in self._event_ids:
            return False
        self._event_ids.add(key)
        return True

    def _require_not_terminal(self) -> None:
        if self.order.state in TERMINAL_STATES:
            raise ValueError(
                f"terminal order cannot transition: {self.order.state.value}"
            )
