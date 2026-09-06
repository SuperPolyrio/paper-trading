"""Durable-in-memory lifecycle record for gateway commands awaiting a venue outcome."""

from __future__ import annotations

from dataclasses import dataclass, field

from .command import CommandState, GatewayCommand
from .latency_model import CommandSchedule


@dataclass(frozen=True)
class CommandTransition:
    command_id: str
    state: CommandState
    event_ts_ns: int
    reason: str
    reservation_release_required: bool = False


@dataclass
class InFlightCommand:
    command: GatewayCommand
    state: CommandState
    schedule: CommandSchedule | None = None
    not_before_ts_ns: int | None = None
    transitions: list[CommandTransition] = field(default_factory=list)

    def transition(
        self,
        state: CommandState,
        *,
        event_ts_ns: int,
        reason: str,
        reservation_release_required: bool = False,
    ) -> CommandTransition:
        item = CommandTransition(
            command_id=self.command.command_id,
            state=state,
            event_ts_ns=int(event_ts_ns),
            reason=str(reason),
            reservation_release_required=bool(reservation_release_required),
        )
        if self.transitions and self.transitions[-1] == item:
            return item
        self.state = state
        self.transitions.append(item)
        return item

    @property
    def terminal(self) -> bool:
        return self.state in {CommandState.LOCAL_DENIED, CommandState.TERMINAL}
