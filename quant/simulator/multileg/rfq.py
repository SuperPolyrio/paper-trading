"""Optional Combo/RFQ lifecycle; it is separate from ordinary token CLOB orders."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum


class RfqState(str, Enum):
    RFQ_REQUESTED = "RFQ_REQUESTED"
    QUOTE_SUBMITTED = "QUOTE_SUBMITTED"
    USER_ACCEPTED = "USER_ACCEPTED"
    LAST_LOOK = "LAST_LOOK"
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"


@dataclass(frozen=True)
class RfqLifecycle:
    rfq_id: str
    requested_at: datetime
    state: RfqState = RfqState.RFQ_REQUESTED
    quote_at: datetime | None = None
    accepted_at: datetime | None = None

    def quote(self, *, at: datetime) -> "RfqLifecycle":
        if self.state is not RfqState.RFQ_REQUESTED:
            raise ValueError("RFQ is not open for a quote")
        if at > self.requested_at + timedelta(milliseconds=400):
            return replace(self, state=RfqState.EXPIRED)
        return replace(self, state=RfqState.QUOTE_SUBMITTED, quote_at=at)

    def accept(self, *, at: datetime) -> "RfqLifecycle":
        if self.state is not RfqState.QUOTE_SUBMITTED or self.quote_at is None:
            raise ValueError("RFQ has no accepted quote")
        if at > self.quote_at + timedelta(seconds=10):
            return replace(self, state=RfqState.EXPIRED)
        return replace(self, state=RfqState.USER_ACCEPTED, accepted_at=at)

    def last_look(self, *, at: datetime, accepted: bool) -> "RfqLifecycle":
        if self.state is not RfqState.USER_ACCEPTED or self.accepted_at is None:
            raise ValueError("RFQ is not ready for last look")
        if at > self.accepted_at + timedelta(seconds=1):
            return replace(self, state=RfqState.EXPIRED)
        return replace(
            self, state=RfqState.CONFIRMED if accepted else RfqState.DECLINED
        )

    def execute(self) -> "RfqLifecycle":
        if self.state is not RfqState.CONFIRMED:
            raise ValueError("only confirmed RFQ may execute")
        return replace(self, state=RfqState.EXECUTED)
