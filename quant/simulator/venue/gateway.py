"""Offline deterministic venue gateway around, not instead of, the existing OMS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .batch_order_model import PaperOrderBatch, PaperOrderBatchResult
from .command import CommandDisposition, CommandState, CommandType, GatewayCommand, GatewayDecision
from .command_outcome import ReconciliationOutcome
from .delay_model import SportsDelayModel
from .event_adapter import lifecycle_to_sim_events
from .heartbeat_model import HeartbeatConfig, HeartbeatTracker
from .inflight_queue import CommandTransition, InFlightCommand
from .latency_model import GatewayLatencyModel
from .rate_limit_model import DualRateLimiter, RateLimitConfig
from .venue_state import VenueMode, VenueStateMachine


@dataclass(frozen=True)
class GatewayConfig:
    latency: GatewayLatencyModel = field(default_factory=GatewayLatencyModel)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    sports_delay: SportsDelayModel = field(default_factory=SportsDelayModel)


@dataclass(frozen=True)
class WorkingOrder:
    order_id: str
    account_id: str
    submit_command_id: str
    acknowledged_ts_ns: int
    uncancelable_until_ts_ns: int


@dataclass
class VenueGateway:
    """Deterministic simulator gateway with no network or account side effects.

    Reservation release is emitted as lifecycle evidence for the existing ledger
    to apply idempotently; this component intentionally never mutates balances.
    """

    config: GatewayConfig = field(default_factory=GatewayConfig)
    venue: VenueStateMachine = field(default_factory=VenueStateMachine)
    _rate_limiter: DualRateLimiter = field(init=False)
    _heartbeats: HeartbeatTracker = field(init=False)
    _commands: dict[str, InFlightCommand] = field(default_factory=dict, init=False)
    _working_orders: dict[str, WorkingOrder] = field(default_factory=dict, init=False)
    _lifecycle: list[CommandTransition] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._rate_limiter = DualRateLimiter(self.config.rate_limits)
        self._heartbeats = HeartbeatTracker(self.config.heartbeat)

    def heartbeat(self, account_id: str, *, now_ts_ns: int) -> None:
        self._heartbeats.heartbeat(account_id, now_ts_ns=now_ts_ns)

    def submit(self, command: GatewayCommand, *, now_ts_ns: int | None = None) -> GatewayDecision:
        now = command.created_ts_ns if now_ts_ns is None else int(now_ts_ns)
        if now < command.created_ts_ns:
            raise ValueError("gateway cannot submit before command creation")
        existing = self._commands.get(command.command_id)
        if existing is not None:
            return self._decision_from_record(existing)
        self._heartbeats.register_account(command.account_id, now_ts_ns=now)
        record = InFlightCommand(command, CommandState.CREATED)
        self._commands[command.command_id] = record
        self._transition(record, CommandState.LOCAL_VALIDATING, now, "local_validation_started")
        if command.increases_risk and not self._heartbeats.accepts_new_risk(command.account_id, now_ts_ns=now):
            self._transition(record, CommandState.LOCAL_DENIED, now, "heartbeat_expired")
            return GatewayDecision(command.command_id, CommandDisposition.LOCAL_DENIED, record.state, "heartbeat_expired")
        permission = self.venue.permits(command, now_ts_ns=now)
        if not permission.accepted:
            self._transition(record, CommandState.LOCAL_DENIED, now, permission.reason)
            return GatewayDecision(command.command_id, CommandDisposition.VENUE_DENIED, record.state, permission.reason)
        delayed_until = self._uncancelable_until(command)
        if delayed_until is not None and now < delayed_until:
            record.not_before_ts_ns = delayed_until
            self._transition(record, CommandState.DELAYED_UNCANCELABLE, now, "sports_delay_uncancelable")
            return GatewayDecision(command.command_id, CommandDisposition.DELAYED_UNCANCELABLE, record.state, "sports_delay_uncancelable", delayed_until)
        self._mark_target_pending(command, now)
        return self._admit(record, now)

    def submit_batch(self, batch: PaperOrderBatch, *, now_ts_ns: int | None = None) -> PaperOrderBatchResult:
        now = batch.submitted_ts_ns if now_ts_ns is None else int(now_ts_ns)
        return PaperOrderBatchResult(
            batch_id=batch.batch_id,
            child_results=tuple(self.submit(command, now_ts_ns=now) for command in batch.orders),
        )

    def advance(self, now_ts_ns: int) -> tuple[CommandTransition, ...]:
        """Advance all commands by deterministic command-id order."""
        now = int(now_ts_ns)
        start = len(self._lifecycle)
        for record in sorted(self._commands.values(), key=lambda row: row.command.command_id):
            self._advance_record(record, now)
        self._expire_heartbeats(now)
        return tuple(self._lifecycle[start:])

    def mark_matched(self, order_id: str, *, now_ts_ns: int) -> None:
        """Model a fill that can legally win a race against an in-flight cancel."""
        working = self._working_orders.pop(str(order_id), None)
        if working is None:
            raise ValueError("order is not working")
        submit = self._commands[working.submit_command_id]
        self._transition(submit, CommandState.ACKED_MATCHED, now_ts_ns, "paper_match")
        for record in self._commands.values():
            if record.command.command_type is CommandType.CANCEL and record.command.order_id == order_id and not record.terminal:
                self._transition(record, CommandState.TERMINAL, now_ts_ns, "matched_before_cancel_ack")

    def mark_outcome_unknown(self, command_id: str, *, now_ts_ns: int) -> None:
        record = self._commands[str(command_id)]
        if record.state is not CommandState.IN_FLIGHT:
            raise ValueError("only an in-flight command may have an unknown submit outcome")
        self._transition(record, CommandState.SUBMIT_OUTCOME_UNKNOWN, now_ts_ns, "transport_outcome_unknown")

    def reconcile(self, command_id: str, outcome: ReconciliationOutcome | str, *, now_ts_ns: int) -> None:
        record = self._commands[str(command_id)]
        if record.state is not CommandState.SUBMIT_OUTCOME_UNKNOWN:
            raise ValueError("only unknown submit outcomes may reconcile")
        self._transition(record, CommandState.RECONCILING, now_ts_ns, "reconciliation_started")
        result = outcome if isinstance(outcome, ReconciliationOutcome) else ReconciliationOutcome(str(outcome))
        if result is ReconciliationOutcome.ACCEPTED_LIVE:
            self._acknowledge(record, now_ts_ns)
        elif result is ReconciliationOutcome.MATCHED:
            self._acknowledge(record, now_ts_ns)
            self.mark_matched(self._order_id_for(record), now_ts_ns=now_ts_ns)
        else:
            self._transition(record, CommandState.TERMINAL, now_ts_ns, f"reconciliation_{result.value.lower()}", reservation_release_required=True)

    def command(self, command_id: str) -> InFlightCommand:
        return self._commands[str(command_id)]

    @property
    def lifecycle(self) -> tuple[CommandTransition, ...]:
        return tuple(self._lifecycle)

    @property
    def commands(self) -> tuple[InFlightCommand, ...]:
        """Stable command view for the scheduler-owned shadow evidence path."""
        return tuple(self._commands[key] for key in sorted(self._commands))

    @property
    def working_orders(self) -> tuple[WorkingOrder, ...]:
        return tuple(self._working_orders[key] for key in sorted(self._working_orders))

    def lifecycle_sim_events(self):
        """Expose gateway history through the shared SimEvent contract."""
        return lifecycle_to_sim_events(
            self._lifecycle,
            command_lookup={key: value.command for key, value in self._commands.items()},
        )

    def _admit(self, record: InFlightCommand, now_ts_ns: int) -> GatewayDecision:
        rate = self._rate_limiter.reserve(record.command, now_ts_ns=now_ts_ns)
        if rate.denied:
            self._transition(record, CommandState.LOCAL_DENIED, now_ts_ns, rate.reason)
            return GatewayDecision(record.command.command_id, CommandDisposition.LOCAL_RATE_LIMIT_DENIED, record.state, rate.reason)
        if not rate.accepted:
            record.not_before_ts_ns = rate.available_at_ts_ns
            self._transition(record, CommandState.THROTTLED, now_ts_ns, rate.reason)
            return GatewayDecision(record.command.command_id, CommandDisposition.THROTTLED_UNTIL, record.state, rate.reason, rate.available_at_ts_ns)
        if record.state in {CommandState.THROTTLED, CommandState.DELAYED_UNCANCELABLE}:
            self._transition(record, CommandState.LOCAL_VALIDATING, now_ts_ns, "gateway_admission_resumed")
        record.not_before_ts_ns = None
        record.schedule = self.config.latency.schedule(record.command, start_ts_ns=now_ts_ns)
        return GatewayDecision(record.command.command_id, CommandDisposition.ACCEPTED_IMMEDIATELY, record.state, "accepted", now_ts_ns)

    def _advance_record(self, record: InFlightCommand, now_ts_ns: int) -> None:
        if record.terminal:
            return
        if record.state in {CommandState.THROTTLED, CommandState.DELAYED_UNCANCELABLE}:
            if record.not_before_ts_ns is None or now_ts_ns < record.not_before_ts_ns:
                return
            permission = self.venue.permits(record.command, now_ts_ns=now_ts_ns)
            if not permission.accepted:
                self._transition(record, CommandState.LOCAL_DENIED, now_ts_ns, permission.reason)
                return
            self._admit(record, now_ts_ns)
        schedule = record.schedule
        if schedule is None:
            return
        if now_ts_ns >= schedule.validation_done_ts_ns and record.state is CommandState.LOCAL_VALIDATING:
            self._transition(record, CommandState.QUEUED_FOR_GATEWAY, schedule.validation_done_ts_ns, "local_validation_passed")
        if now_ts_ns >= schedule.signing_done_ts_ns and record.state is CommandState.QUEUED_FOR_GATEWAY:
            self._transition(record, CommandState.SIGNING, schedule.signing_done_ts_ns, "signing_completed")
        if now_ts_ns >= schedule.sent_ts_ns and record.state is CommandState.SIGNING:
            self._transition(record, CommandState.SENT, schedule.sent_ts_ns, "sent_to_venue")
            self._transition(record, CommandState.IN_FLIGHT, schedule.sent_ts_ns, "awaiting_venue")
        if now_ts_ns >= schedule.response_ts_ns and record.state is CommandState.IN_FLIGHT:
            self._acknowledge(record, schedule.response_ts_ns)

    def _acknowledge(self, record: InFlightCommand, now_ts_ns: int) -> None:
        command = record.command
        if command.command_type is CommandType.CANCEL:
            target = self._working_orders.pop(str(command.order_id), None)
            if target is None:
                self._transition(record, CommandState.TERMINAL, now_ts_ns, "cancel_target_not_working")
                return
            original = self._commands[target.submit_command_id]
            self._transition(original, CommandState.TERMINAL, now_ts_ns, "cancel_ack", reservation_release_required=True)
            self._transition(record, CommandState.TERMINAL, now_ts_ns, "cancel_ack")
            return
        if command.command_type is CommandType.REPLACE:
            target = self._working_orders.pop(str(command.order_id), None)
            if target is None:
                self._transition(record, CommandState.TERMINAL, now_ts_ns, "replace_target_not_working")
                return
            original = self._commands[target.submit_command_id]
            self._transition(original, CommandState.TERMINAL, now_ts_ns, "replace_cancel_ack", reservation_release_required=True)
        self._transition(record, CommandState.ACKED_LIVE, now_ts_ns, "venue_ack")
        order_id = self._order_id_for(record)
        self._working_orders[order_id] = WorkingOrder(
            order_id=order_id,
            account_id=command.account_id,
            submit_command_id=command.command_id,
            acknowledged_ts_ns=now_ts_ns,
            uncancelable_until_ts_ns=self.config.sports_delay.uncancelable_until(now_ts_ns),
        )

    def _uncancelable_until(self, command: GatewayCommand) -> int | None:
        if command.command_type is not CommandType.CANCEL:
            return None
        target = self._working_orders.get(str(command.order_id))
        return None if target is None else target.uncancelable_until_ts_ns

    def _mark_target_pending(self, command: GatewayCommand, now_ts_ns: int) -> None:
        if command.command_type not in {CommandType.CANCEL, CommandType.REPLACE}:
            return
        target = self._working_orders.get(str(command.order_id))
        if target is None:
            return
        record = self._commands[target.submit_command_id]
        pending_state = (
            CommandState.PENDING_CANCEL
            if command.command_type is CommandType.CANCEL
            else CommandState.PENDING_REPLACE
        )
        self._transition(record, pending_state, now_ts_ns, f"{command.command_type.value.lower()}_requested")

    def _expire_heartbeats(self, now_ts_ns: int) -> None:
        accounts = {order.account_id for order in self._working_orders.values()}
        for account_id in self._heartbeats.expire_due_accounts(accounts, now_ts_ns=now_ts_ns):
            orders = [order for order in self._working_orders.values() if order.account_id == account_id]
            for order in orders:
                self._working_orders.pop(order.order_id, None)
                record = self._commands[order.submit_command_id]
                self._transition(record, CommandState.TERMINAL, now_ts_ns, "HEARTBEAT_AUTO_CANCEL", reservation_release_required=True)

    def _order_id_for(self, record: InFlightCommand) -> str:
        command = record.command
        if command.command_type is CommandType.REPLACE:
            return str(command.replacement_order_id or f"replacement:{command.command_id}")
        return str(command.order_id or f"paper:{command.command_id}")

    def _transition(self, record: InFlightCommand, state: CommandState, event_ts_ns: int, reason: str, reservation_release_required: bool = False) -> None:
        self._lifecycle.append(record.transition(state, event_ts_ns=event_ts_ns, reason=reason, reservation_release_required=reservation_release_required))

    def _decision_from_record(self, record: InFlightCommand) -> GatewayDecision:
        mapping = {
            CommandState.THROTTLED: CommandDisposition.THROTTLED_UNTIL,
            CommandState.DELAYED_UNCANCELABLE: CommandDisposition.DELAYED_UNCANCELABLE,
            CommandState.LOCAL_DENIED: CommandDisposition.LOCAL_DENIED,
            CommandState.SUBMIT_OUTCOME_UNKNOWN: CommandDisposition.OUTCOME_UNKNOWN,
        }
        return GatewayDecision(
            record.command.command_id,
            mapping.get(record.state, CommandDisposition.ACCEPTED_IMMEDIATELY),
            record.state,
            "idempotent_duplicate",
            record.not_before_ts_ns,
        )
