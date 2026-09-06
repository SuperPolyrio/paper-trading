"""Translate deterministic venue lifecycle records into SimEvents."""

from __future__ import annotations

from typing import Iterable

from quant.simulator.kernel.deterministic_id import deterministic_id
from quant.simulator.kernel.event import SimEvent
from quant.simulator.kernel.event_priority import EventPriority

from .command import CommandState, CommandType, GatewayCommand
from .inflight_queue import CommandTransition


def lifecycle_to_sim_events(
    transitions: Iterable[CommandTransition],
    *,
    command_lookup: dict[str, GatewayCommand],
) -> tuple[SimEvent, ...]:
    """Map gateway transitions to the shared causal event contract.

    The caller schedules these events through ``DeterministicScheduler``. The
    adapter itself never dispatches, sends HTTP, or changes a paper ledger.
    """
    events = [
        transition_to_sim_event(transition, command_lookup[transition.command_id])
        for transition in transitions
    ]
    return tuple(sorted(events, key=lambda event: event.sort_key))


def transition_to_sim_event(transition: CommandTransition, command: GatewayCommand) -> SimEvent:
    event_type, priority, sequence = _event_spec(transition, command)
    content = {
        "command_id": command.command_id,
        "command_type": command.command_type.value,
        "state": transition.state.value,
        "event_ts_ns": transition.event_ts_ns,
        "reason": transition.reason,
    }
    return SimEvent.build(
        event_type=event_type,
        event_ts_ns=transition.event_ts_ns,
        source_sequence=sequence,
        aggregate_key=f"venue-command:{command.command_id}",
        priority=priority,
        source_event_id=deterministic_id("gateway-transition-source", content),
        deterministic_tiebreaker=deterministic_id("gateway-transition-tie", content),
        model_version="venue-gateway-v1",
        payload={
            **content,
            "account_id": command.account_id,
            "signer_id": command.signer_id,
            "ip_id": command.ip_id,
            "endpoint": command.endpoint,
            "order_id": command.order_id,
            "reservation_release_required": transition.reservation_release_required,
        },
    )


def _event_spec(transition: CommandTransition, command: GatewayCommand) -> tuple[str, int, int]:
    if transition.reason == "HEARTBEAT_AUTO_CANCEL" or command.command_type is CommandType.CANCEL:
        return "PAPER_CANCEL", int(EventPriority.PAPER_VENUE_ACTION), 60
    if transition.state is CommandState.ACKED_MATCHED:
        return "PAPER_MATCH", int(EventPriority.PAPER_VENUE_ACTION), 60
    if transition.state in {CommandState.TERMINAL, CommandState.ACKED_LIVE}:
        return "POSITION_OPERATION_CONFIRMATION", int(EventPriority.POSITION_OPERATION), 70
    if transition.state in {
        CommandState.LOCAL_VALIDATING,
        CommandState.QUEUED_FOR_GATEWAY,
        CommandState.THROTTLED,
        CommandState.SIGNING,
        CommandState.SENT,
        CommandState.IN_FLIGHT,
        CommandState.SUBMIT_OUTCOME_UNKNOWN,
        CommandState.RECONCILING,
    }:
        return "PAPER_COMMAND_ARRIVAL", int(EventPriority.PAPER_COMMAND_ARRIVAL), 50
    return "ACCOUNTING_MARK", int(EventPriority.ACCOUNTING), 100
