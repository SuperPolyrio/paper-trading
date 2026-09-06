"""Side-by-side venue admission evidence for the existing paper worker.

The adapter deliberately has no authority over the paper execution path.  It
keeps one long-lived ``VenueGateway`` so rate-limit and maintenance state carry
across commands, then records whether the current paper path and the modeled
venue would both admit the same submit at that instant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from quant.simulator.kernel.deterministic_id import deterministic_id
from quant.simulator.kernel.event import SimEvent

from .command import CommandDisposition, CommandState, CommandType, GatewayCommand
from .command_outcome import ReconciliationOutcome
from .event_adapter import lifecycle_to_sim_events
from .gateway import VenueGateway
from .inflight_queue import InFlightCommand


class VenueShadowArtifactSink(Protocol):
    def persist_event(self, event: SimEvent, *, run_id: str) -> bool: ...

    def persist_inflight(self, item: InFlightCommand, **kwargs: Any) -> None: ...


class VenueReservationReleaseSink(Protocol):
    def release_order_reservation_from_command(
        self,
        intent_id: int,
        *,
        release_event_key: str,
        command_id: str,
        event_ts_ns: int,
        reason: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class VenueAdmissionRequest:
    intent_id: str
    account_id: str
    signer_id: str
    ip_id: str
    created_ts_ns: int
    client_order_id: str
    paper_admitted: bool
    endpoint: str = "/order"
    post_only: bool = False
    side: str = ""
    time_in_force: str = ""
    asset_id: str = ""
    limit_price: str = ""
    size: str = ""

    def __post_init__(self) -> None:
        required = {
            "intent_id": self.intent_id,
            "account_id": self.account_id,
            "signer_id": self.signer_id,
            "ip_id": self.ip_id,
            "client_order_id": self.client_order_id,
            "endpoint": self.endpoint,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"venue shadow request missing fields: {missing}")
        if int(self.created_ts_ns) < 0:
            raise ValueError("created_ts_ns must be non-negative")
        object.__setattr__(self, "created_ts_ns", int(self.created_ts_ns))


@dataclass(frozen=True)
class VenueAdmissionShadowResult:
    run_id: str
    intent_id: str
    command_id: str
    paper_admitted: bool
    gateway_accepted_now: bool
    gateway_queued: bool
    gateway_terminally_denied: bool
    agreement: bool
    disposition: str
    command_state: str
    reason: str
    venue_mode: str
    lifecycle_events_persisted: int
    comparison_event_persisted: bool
    reservation_release_events: int = 0
    live_submission_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VenueShadowPulseResult:
    run_id: str
    event_ts_ns: int
    heartbeat_accounts: int
    lifecycle_events_persisted: int
    heartbeat_auto_cancels: int
    reservation_release_events: int
    live_submission_performed: bool = False


@dataclass(frozen=True)
class VenueShadowCommandResult:
    run_id: str
    command_id: str
    operation: str
    command_state: str
    lifecycle_events_persisted: int
    reservation_release_events: int
    live_submission_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VenueShadowGatewayCommandResult:
    run_id: str
    command_id: str
    disposition: str
    command_state: str
    reason: str
    venue_mode: str
    lifecycle_events_persisted: int
    reservation_release_events: int
    live_submission_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class VenueAdmissionShadow:
    """Observe admission semantics without enforcing them or using a network."""

    MODEL_VERSION = "paper-venue-admission-shadow-v1"

    def __init__(
        self,
        *,
        run_id: str,
        gateway: VenueGateway | None = None,
        artifact_sink: VenueShadowArtifactSink | None = None,
        reservation_release_sink: VenueReservationReleaseSink | None = None,
    ) -> None:
        if not str(run_id).strip():
            raise ValueError("run_id is required")
        self.run_id = str(run_id)
        self.gateway = gateway or VenueGateway()
        self.artifact_sink = artifact_sink
        self.reservation_release_sink = reservation_release_sink
        self._last_ts_ns = -1
        timeout_ns = int(self.gateway.config.heartbeat.timeout_ns)
        self._heartbeat_persist_interval_ns = (
            max(1_000_000_000, timeout_ns // 3)
            if self.gateway.config.heartbeat.required
            else 5_000_000_000
        )
        self._last_persisted_heartbeat_ns = -1
        self._results: dict[str, VenueAdmissionShadowResult] = {}
        self._accounts: set[str] = set()

    def evaluate(self, request: VenueAdmissionRequest) -> VenueAdmissionShadowResult:
        requested_ts_ns = int(request.created_ts_ns)
        # A live intent can reach the gateway after market-data heartbeats have
        # advanced this process-wide clock. Admit it at the current gateway
        # watermark instead of deferring the intent forever as a time-travel
        # error; execution pricing still uses its causal decision/arrival
        # checkpoints outside this venue-control model.
        now_ts_ns = max(requested_ts_ns, self._last_ts_ns)
        late_by_ns = max(0, now_ts_ns - requested_ts_ns)
        command_id = f"paper-shadow:{request.intent_id}"
        existing = self._results.get(command_id)
        if existing is not None:
            if not existing.gateway_queued:
                return existing
            lifecycle_start = len(self.gateway.lifecycle)
            self._last_ts_ns = now_ts_ns
            self._advance_due(now_ts_ns)
            venue_mode = self.gateway.venue.mode_at(now_ts_ns).value
            persisted, releases, _auto_cancels = self._persist_lifecycle(
                lifecycle_start,
                venue_mode=venue_mode,
            )
            record = self.gateway.command(command_id)
            queued = record.state in {
                CommandState.THROTTLED,
                CommandState.DELAYED_UNCANCELABLE,
            }
            denied = record.state in {
                CommandState.LOCAL_DENIED,
                CommandState.TERMINAL,
            }
            accepted = record.schedule is not None and not queued and not denied
            refreshed = VenueAdmissionShadowResult(
                run_id=existing.run_id,
                intent_id=existing.intent_id,
                command_id=existing.command_id,
                paper_admitted=existing.paper_admitted,
                gateway_accepted_now=accepted,
                gateway_queued=queued,
                gateway_terminally_denied=denied,
                agreement=bool(existing.paper_admitted) == accepted,
                disposition=(
                    CommandDisposition.ACCEPTED_IMMEDIATELY.value
                    if accepted
                    else existing.disposition
                ),
                command_state=record.state.value,
                reason=("accepted_after_queue" if accepted else existing.reason),
                venue_mode=venue_mode,
                lifecycle_events_persisted=(
                    existing.lifecycle_events_persisted + persisted
                ),
                comparison_event_persisted=existing.comparison_event_persisted,
                reservation_release_events=(
                    existing.reservation_release_events + releases
                ),
            )
            self._results[command_id] = refreshed
            return refreshed
        self._last_ts_ns = now_ts_ns

        lifecycle_start = len(self.gateway.lifecycle)
        self._advance_due(now_ts_ns)
        self.gateway.heartbeat(request.account_id, now_ts_ns=now_ts_ns)
        self._accounts.add(request.account_id)
        venue_mode = self.gateway.venue.mode_at(now_ts_ns).value
        command = GatewayCommand.build(
            command_type=CommandType.SUBMIT,
            account_id=request.account_id,
            signer_id=request.signer_id,
            ip_id=request.ip_id,
            endpoint=request.endpoint,
            created_ts_ns=now_ts_ns,
            order_id=request.client_order_id,
            post_only=request.post_only,
            command_id=command_id,
            payload={
                "run_id": self.run_id,
                "intent_id": request.intent_id,
                "requested_created_ts_ns": requested_ts_ns,
                "effective_created_ts_ns": now_ts_ns,
                "late_by_ns": late_by_ns,
                "asset_id": request.asset_id,
                "side": request.side,
                "time_in_force": request.time_in_force,
                "limit_price": request.limit_price,
                "size": request.size,
                "paper_admitted": request.paper_admitted,
            },
        )
        decision = self.gateway.submit(command, now_ts_ns=now_ts_ns)
        if decision.disposition is CommandDisposition.ACCEPTED_IMMEDIATELY:
            self.gateway.advance(now_ts_ns)

        accepted_now = decision.disposition is CommandDisposition.ACCEPTED_IMMEDIATELY
        queued = decision.disposition in {
            CommandDisposition.THROTTLED_UNTIL,
            CommandDisposition.DELAYED_UNCANCELABLE,
        }
        terminally_denied = decision.disposition in {
            CommandDisposition.LOCAL_RATE_LIMIT_DENIED,
            CommandDisposition.LOCAL_DENIED,
            CommandDisposition.VENUE_DENIED,
        }
        agreement = bool(request.paper_admitted) == accepted_now
        record = self.gateway.command(command.command_id)
        persisted_lifecycle, release_events, _auto_cancels = self._persist_lifecycle(
            lifecycle_start,
            venue_mode=venue_mode,
            rate_limit_reason=decision.reason,
        )
        comparison_persisted = False

        if self.artifact_sink is not None:
            comparison = SimEvent.build(
                event_type="REPORTING",
                event_ts_ns=now_ts_ns,
                source_sequence=110,
                aggregate_key=f"paper-intent:{request.intent_id}",
                source_event_id=command.command_id,
                model_version=self.MODEL_VERSION,
                payload={
                    "intent_id": request.intent_id,
                    "command_id": command.command_id,
                    "requested_created_ts_ns": requested_ts_ns,
                    "effective_created_ts_ns": now_ts_ns,
                    "late_by_ns": late_by_ns,
                    "paper_admitted": bool(request.paper_admitted),
                    "gateway_accepted_now": accepted_now,
                    "gateway_queued": queued,
                    "gateway_terminally_denied": terminally_denied,
                    "agreement": agreement,
                    "disposition": decision.disposition.value,
                    "command_state": record.state.value,
                    "reason": decision.reason,
                    "venue_mode": venue_mode,
                    "live_submission_performed": False,
                },
            )
            comparison_persisted = self.artifact_sink.persist_event(
                comparison,
                run_id=self.run_id,
            )

        result = VenueAdmissionShadowResult(
            run_id=self.run_id,
            intent_id=request.intent_id,
            command_id=command.command_id,
            paper_admitted=bool(request.paper_admitted),
            gateway_accepted_now=accepted_now,
            gateway_queued=queued,
            gateway_terminally_denied=terminally_denied,
            agreement=agreement,
            disposition=decision.disposition.value,
            command_state=record.state.value,
            reason=decision.reason,
            venue_mode=venue_mode,
            lifecycle_events_persisted=persisted_lifecycle,
            comparison_event_persisted=bool(comparison_persisted),
            reservation_release_events=release_events,
        )
        self._results[command_id] = result
        self._persist_run_heartbeat(now_ts_ns, status="RUNNING")
        return result

    def pulse(self, *, now_ts_ns: int) -> VenueShadowPulseResult:
        """Advance due commands before refreshing account heartbeats."""

        now = int(now_ts_ns)
        if now < self._last_ts_ns:
            raise ValueError("venue shadow clock cannot move backwards")
        self._last_ts_ns = now
        lifecycle_start = len(self.gateway.lifecycle)
        self._advance_due(now)
        venue_mode = self.gateway.venue.mode_at(now).value
        persisted, releases, auto_cancels = self._persist_lifecycle(
            lifecycle_start,
            venue_mode=venue_mode,
        )
        for account_id in sorted(self._accounts):
            self.gateway.heartbeat(account_id, now_ts_ns=now)
        self._persist_run_heartbeat(now, status="RUNNING")
        return VenueShadowPulseResult(
            run_id=self.run_id,
            event_ts_ns=now,
            heartbeat_accounts=len(self._accounts),
            lifecycle_events_persisted=persisted,
            heartbeat_auto_cancels=auto_cancels,
            reservation_release_events=releases,
        )

    def mark_outcome_unknown(
        self,
        intent_id: str,
        *,
        now_ts_ns: int,
    ) -> VenueShadowCommandResult:
        """Record a transport-unknown outcome without retrying the command."""

        now = self._claim_timestamp(now_ts_ns)
        command_id = f"paper-shadow:{intent_id}"
        lifecycle_start = len(self.gateway.lifecycle)
        self.gateway.mark_outcome_unknown(command_id, now_ts_ns=now)
        persisted, releases, _auto_cancels = self._persist_lifecycle(
            lifecycle_start,
            venue_mode=self.gateway.venue.mode_at(now).value,
            reconciliation_status="OUTCOME_UNKNOWN",
        )
        self._persist_run_heartbeat(now, status="RUNNING")
        return VenueShadowCommandResult(
            run_id=self.run_id,
            command_id=command_id,
            operation="MARK_OUTCOME_UNKNOWN",
            command_state=self.gateway.command(command_id).state.value,
            lifecycle_events_persisted=persisted,
            reservation_release_events=releases,
        )

    def observe_gateway_command(
        self,
        command: GatewayCommand,
        *,
        now_ts_ns: int | None = None,
    ) -> VenueShadowGatewayCommandResult:
        """Persist one offline gateway command without granting execution authority."""

        now = self._claim_timestamp(
            command.created_ts_ns if now_ts_ns is None else now_ts_ns
        )
        lifecycle_start = len(self.gateway.lifecycle)
        self._advance_due(now)
        self.gateway.heartbeat(command.account_id, now_ts_ns=now)
        self._accounts.add(command.account_id)
        venue_mode = self.gateway.venue.mode_at(now).value
        decision = self.gateway.submit(command, now_ts_ns=now)
        if decision.disposition is CommandDisposition.ACCEPTED_IMMEDIATELY:
            self.gateway.advance(now)
        persisted, releases, _auto_cancels = self._persist_lifecycle(
            lifecycle_start,
            venue_mode=venue_mode,
            rate_limit_reason=decision.reason,
        )
        self._persist_run_heartbeat(now, status="RUNNING")
        return VenueShadowGatewayCommandResult(
            run_id=self.run_id,
            command_id=command.command_id,
            disposition=decision.disposition.value,
            command_state=self.gateway.command(command.command_id).state.value,
            reason=decision.reason,
            venue_mode=venue_mode,
            lifecycle_events_persisted=persisted,
            reservation_release_events=releases,
        )

    def reconcile(
        self,
        intent_id: str,
        outcome: ReconciliationOutcome | str,
        *,
        now_ts_ns: int,
    ) -> VenueShadowCommandResult:
        """Resolve an unknown command through the gateway reconciliation path."""

        now = self._claim_timestamp(now_ts_ns)
        command_id = f"paper-shadow:{intent_id}"
        result = (
            outcome
            if isinstance(outcome, ReconciliationOutcome)
            else ReconciliationOutcome(str(outcome))
        )
        lifecycle_start = len(self.gateway.lifecycle)
        self.gateway.reconcile(command_id, result, now_ts_ns=now)
        persisted, releases, _auto_cancels = self._persist_lifecycle(
            lifecycle_start,
            venue_mode=self.gateway.venue.mode_at(now).value,
            reconciliation_status=result.value,
        )
        self._persist_run_heartbeat(now, status="RUNNING")
        return VenueShadowCommandResult(
            run_id=self.run_id,
            command_id=command_id,
            operation="RECONCILE",
            command_state=self.gateway.command(command_id).state.value,
            lifecycle_events_persisted=persisted,
            reservation_release_events=releases,
        )

    def mark_stopped(self, *, now_ts_ns: int) -> None:
        now = max(int(now_ts_ns), self._last_ts_ns)
        self._persist_run_heartbeat(now, status="STOPPED")

    def reconcile_stale_reservations(self) -> int:
        sink = self.reservation_release_sink
        reconcile = getattr(sink, "reconcile_stale_shadow_reservations", None)
        if reconcile is None or not self.gateway.config.heartbeat.required:
            return 0
        return int(
            reconcile(
                current_run_id=self.run_id,
                timeout_ns=self.gateway.config.heartbeat.timeout_ns,
            )
        )

    def _persist_run_heartbeat(self, event_ts_ns: int, *, status: str) -> None:
        persist = getattr(self.artifact_sink, "persist_shadow_run_heartbeat", None)
        if persist is None:
            return
        timestamp = int(event_ts_ns)
        if (
            str(status) == "RUNNING"
            and self._last_persisted_heartbeat_ns >= 0
            and timestamp - self._last_persisted_heartbeat_ns
            < self._heartbeat_persist_interval_ns
        ):
            return
        persist(
            run_id=self.run_id,
            heartbeat_ts_ns=timestamp,
            status=str(status),
        )
        self._last_persisted_heartbeat_ns = timestamp

    def _persist_lifecycle(
        self,
        lifecycle_start: int,
        *,
        venue_mode: str,
        rate_limit_reason: str | None = None,
        reconciliation_status: str | None = None,
    ) -> tuple[int, int, int]:
        transitions = self.gateway.lifecycle[lifecycle_start:]
        if not transitions:
            return 0, 0, 0
        commands = {
            item.command.command_id: item
            for item in self.gateway.commands
        }
        persisted = 0
        if self.artifact_sink is not None:
            for command_id in sorted({item.command_id for item in transitions}):
                record = commands[command_id]
                schedule = record.schedule
                command_transitions = [
                    item for item in transitions if item.command_id == command_id
                ]
                acknowledged = next(
                    (
                        item
                        for item in reversed(command_transitions)
                        if item.state.value in {"ACKED_LIVE", "ACKED_MATCHED"}
                    ),
                    None,
                )
                responded = next(
                    (
                        item
                        for item in reversed(command_transitions)
                        if item.state.value
                        in {"ACKED_LIVE", "ACKED_MATCHED", "TERMINAL"}
                    ),
                    None,
                )
                self.artifact_sink.persist_inflight(
                    record,
                    send_ts_ns=None if schedule is None else schedule.sent_ts_ns,
                    arrival_ts_ns=None if schedule is None else schedule.arrival_ts_ns,
                    ack_ts_ns=(
                        None if acknowledged is None else acknowledged.event_ts_ns
                    ),
                    response_ts_ns=(
                        None if responded is None else responded.event_ts_ns
                    ),
                    venue_state_at_send=venue_mode,
                    rate_limit_bucket=rate_limit_reason,
                    latency_model_version="gateway-latency-v1",
                    reconciliation_status=reconciliation_status,
                )
            events = lifecycle_to_sim_events(
                transitions,
                command_lookup={key: value.command for key, value in commands.items()},
            )
            for event in events:
                persisted += int(
                    self.artifact_sink.persist_event(event, run_id=self.run_id)
                )

        release_events = 0
        auto_cancels = 0
        for transition in transitions:
            if transition.reason == "HEARTBEAT_AUTO_CANCEL":
                auto_cancels += 1
            if (
                not transition.reservation_release_required
                or self.reservation_release_sink is None
            ):
                continue
            command = commands[transition.command_id].command
            raw_intent_id = command.payload.get("intent_id")
            try:
                intent_id = int(str(raw_intent_id))
            except (TypeError, ValueError):
                continue
            release_event_key = deterministic_id(
                "paper-reservation-release",
                {
                    "command_id": transition.command_id,
                    "intent_id": intent_id,
                    "event_ts_ns": transition.event_ts_ns,
                    "reason": transition.reason,
                },
            )
            release = self.reservation_release_sink
            result = release.release_order_reservation_from_command(
                intent_id,
                release_event_key=release_event_key,
                command_id=transition.command_id,
                event_ts_ns=transition.event_ts_ns,
                reason=transition.reason,
            )
            release_events += int(
                str(result.get("application_status")) == "APPLIED"
            )
        return persisted, release_events, auto_cancels

    def _advance_due(self, now_ts_ns: int) -> None:
        """Run lazy gateway timers at their modeled time before a new submit."""

        while True:
            due = sorted(
                {
                    int(item.not_before_ts_ns)
                    for item in self.gateway.commands
                    if not item.terminal
                    and item.not_before_ts_ns is not None
                    and int(item.not_before_ts_ns) <= int(now_ts_ns)
                }
            )
            if not due:
                break
            before = tuple(
                (item.command.command_id, item.state, item.not_before_ts_ns)
                for item in self.gateway.commands
            )
            self.gateway.advance(due[0])
            after = tuple(
                (item.command.command_id, item.state, item.not_before_ts_ns)
                for item in self.gateway.commands
            )
            if after == before:
                break
        self.gateway.advance(now_ts_ns)

    def _claim_timestamp(self, now_ts_ns: int) -> int:
        now = int(now_ts_ns)
        if now < self._last_ts_ns:
            raise ValueError("venue shadow clock cannot move backwards")
        self._last_ts_ns = now
        return now
