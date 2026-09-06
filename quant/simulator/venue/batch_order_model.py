"""Non-atomic paper batch command result contract."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .command import (
    CommandDisposition,
    CommandState,
    GatewayCommand,
    GatewayDecision,
)


@dataclass(frozen=True)
class PaperOrderBatch:
    batch_id: str
    orders: tuple[GatewayCommand, ...]
    submitted_ts_ns: int
    arrival_ts_ns: int | None = None

    @classmethod
    def build(cls, batch_id: str, orders: Iterable[GatewayCommand], *, submitted_ts_ns: int) -> PaperOrderBatch:
        values = tuple(orders)
        if not str(batch_id).strip():
            raise ValueError("paper batch requires batch_id")
        if not values:
            raise ValueError("paper batch requires at least one order")
        if int(submitted_ts_ns) < 0:
            raise ValueError("paper batch submitted_ts_ns must be non-negative")
        command_ids = [item.command_id for item in values]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("paper batch command_id values must be unique")
        account_ids = {item.account_id for item in values}
        if len(account_ids) != 1:
            raise ValueError("paper batch children must use one account_id")
        return cls(str(batch_id), values, int(submitted_ts_ns))

    def with_arrival(self, arrival_ts_ns: int) -> PaperOrderBatch:
        arrival = int(arrival_ts_ns)
        if arrival < self.submitted_ts_ns:
            raise ValueError("paper batch arrival cannot precede submission")
        return PaperOrderBatch(
            batch_id=self.batch_id,
            orders=self.orders,
            submitted_ts_ns=self.submitted_ts_ns,
            arrival_ts_ns=arrival,
        )

    @property
    def account_id(self) -> str:
        return self.orders[0].account_id


@dataclass(frozen=True)
class PaperOrderBatchResult:
    batch_id: str
    child_results: tuple[GatewayDecision, ...]
    response_complete: bool = True

    def __post_init__(self) -> None:
        if not str(self.batch_id).strip():
            raise ValueError("paper batch result requires batch_id")
        command_ids = [item.command_id for item in self.child_results]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("paper batch result command_id values must be unique")

    @property
    def accepted_count(self) -> int:
        return sum(result.disposition.value.startswith("ACCEPTED") for result in self.child_results)

    @property
    def terminally_denied_count(self) -> int:
        return sum("DENIED" in result.disposition.value for result in self.child_results)

    @property
    def terminal_count(self) -> int:
        terminal_states = {CommandState.LOCAL_DENIED, CommandState.TERMINAL}
        return sum(result.state in terminal_states for result in self.child_results)

    @property
    def batch_state(self) -> str:
        if not self.response_complete:
            return "OUTCOME_UNKNOWN"
        denied = self.terminally_denied_count
        accepted = self.accepted_count
        if denied == len(self.child_results):
            return "ALL_DENIED"
        if accepted == len(self.child_results):
            return "ADMITTED"
        if denied or accepted:
            return "PARTIAL_RESULT"
        return "PENDING"

    def ledger_eligible_command_ids(self) -> frozenset[str]:
        denied = {
            CommandDisposition.LOCAL_RATE_LIMIT_DENIED,
            CommandDisposition.LOCAL_DENIED,
            CommandDisposition.VENUE_DENIED,
        }
        return frozenset(
            result.command_id
            for result in self.child_results
            if result.disposition not in denied
        )
