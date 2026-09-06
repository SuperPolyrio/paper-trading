"""Fixed integer-nanosecond latency model for simulated venue commands."""

from __future__ import annotations

from dataclasses import dataclass

from .command import CommandType, GatewayCommand


@dataclass(frozen=True)
class CommandSchedule:
    validation_done_ts_ns: int
    signing_done_ts_ns: int
    sent_ts_ns: int
    arrival_ts_ns: int
    response_ts_ns: int


@dataclass(frozen=True)
class GatewayLatencyModel:
    local_validation_ns: int = 0
    signing_ns: int = 0
    submit_entry_ns: int = 0
    venue_processing_ns: int = 0
    response_ns: int = 0
    cancel_entry_ns: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.__dict__.values()):
            raise ValueError("gateway latencies must be non-negative")

    def schedule(self, command: GatewayCommand, *, start_ts_ns: int | None = None) -> CommandSchedule:
        start = command.created_ts_ns if start_ts_ns is None else int(start_ts_ns)
        validation = start + self.local_validation_ns
        signing = validation + self.signing_ns
        sent = signing
        entry = self.cancel_entry_ns if command.command_type is CommandType.CANCEL else self.submit_entry_ns
        arrival = sent + entry + self.venue_processing_ns
        return CommandSchedule(
            validation_done_ts_ns=validation,
            signing_done_ts_ns=signing,
            sent_ts_ns=sent,
            arrival_ts_ns=arrival,
            response_ts_ns=arrival + self.response_ns,
        )
