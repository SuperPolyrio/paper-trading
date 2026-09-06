"""Server-deadline-driven Combo RFQ lifecycle with late-event recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .models import ComboQuote, ComboRequest, ComboRfqState, ExecutionStatus


@dataclass(frozen=True)
class RfqTransition:
    event_id: str
    event_type: str
    from_state: ComboRfqState
    to_state: ComboRfqState
    event_ts: datetime
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ComboRfqMachine:
    request: ComboRequest
    state: ComboRfqState = ComboRfqState.REQUESTED
    quote: ComboQuote | None = None
    accepted_at: datetime | None = None
    confirm_by: datetime | None = None
    tx_hash: str | None = None
    error_code: str | None = None
    last_event_at: datetime | None = None
    needs_reconciliation: bool = False

    def __post_init__(self) -> None:
        for value in (self.accepted_at, self.confirm_by, self.last_event_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("RFQ machine timestamps must be timezone-aware")

    def open_competition(
        self, *, event_id: str, event_ts: datetime
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        self._require_not_terminal()
        if (
            self.request.submission_deadline is not None
            and event_ts > self.request.submission_deadline
        ):
            return self._deadline_failure(
                event_id=event_id,
                event_ts=event_ts,
                reason="submission_deadline_elapsed",
            )
        return self._move(
            event_id=event_id,
            event_type="QUOTE_COMPETITION_OPENED",
            to_state=ComboRfqState.QUOTE_COMPETITION,
            event_ts=event_ts,
        )

    def submit_quote(
        self,
        quote: ComboQuote,
        *,
        event_id: str,
        event_ts: datetime,
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        self._require_not_terminal()
        if quote.rfq_id != self.request.rfq_id:
            raise ValueError("quote belongs to a different RFQ")
        deadline = self.request.submission_deadline
        if deadline is not None and event_ts > deadline:
            return self._deadline_failure(
                event_id=event_id,
                event_ts=event_ts,
                reason="quote_submitted_after_submission_deadline",
            )
        machine, transition = self._move(
            event_id=event_id,
            event_type="QUOTE_SUBMITTED",
            to_state=ComboRfqState.QUOTE_AVAILABLE,
            event_ts=event_ts,
            payload={"quote_id": quote.quote_id},
        )
        return replace(machine, quote=quote), transition

    def accept_quote(
        self, *, event_id: str, event_ts: datetime
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        self._require_not_terminal()
        if self.quote is None:
            raise ValueError("RFQ has no quote to accept")
        if event_ts > self.quote.expires_at:
            return self._deadline_failure(
                event_id=event_id,
                event_ts=event_ts,
                reason="quote_expires_at_elapsed",
                terminal_state=ComboRfqState.EXPIRED,
            )
        machine, transition = self._move(
            event_id=event_id,
            event_type="QUOTE_ACCEPTED",
            to_state=ComboRfqState.ACCEPTING,
            event_ts=event_ts,
            payload={"quote_id": self.quote.quote_id},
        )
        return replace(machine, accepted_at=event_ts), transition

    def request_last_look(
        self,
        *,
        confirm_by: datetime,
        event_id: str,
        event_ts: datetime,
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        self._require_not_terminal()
        if confirm_by.tzinfo is None:
            raise ValueError("confirm_by must be timezone-aware")
        if confirm_by <= event_ts:
            return self._deadline_failure(
                event_id=event_id,
                event_ts=event_ts,
                reason="confirm_by_already_elapsed",
            )
        machine, transition = self._move(
            event_id=event_id,
            event_type="LAST_LOOK_REQUESTED",
            to_state=ComboRfqState.AWAITING_MAKER_CONFIRMATION,
            event_ts=event_ts,
            payload={"confirm_by": confirm_by.isoformat()},
        )
        return replace(machine, confirm_by=confirm_by), transition

    def confirm_last_look(
        self,
        *,
        confirm: bool,
        event_id: str,
        event_ts: datetime,
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        self._require_not_terminal()
        if self.confirm_by is None:
            raise ValueError("RFQ has no official confirm_by deadline")
        if event_ts > self.confirm_by:
            return self._deadline_failure(
                event_id=event_id,
                event_ts=event_ts,
                reason="last_look_confirm_by_elapsed",
            )
        target = ComboRfqState.MATCHED if confirm else ComboRfqState.FAILED
        return self._move(
            event_id=event_id,
            event_type="LAST_LOOK_CONFIRMED" if confirm else "LAST_LOOK_DECLINED",
            to_state=target,
            event_ts=event_ts,
            payload={"decision": "CONFIRM" if confirm else "DECLINE"},
        )

    def apply_execution(
        self,
        status: ExecutionStatus,
        *,
        event_id: str,
        event_ts: datetime,
        tx_hash: str | None = None,
        error_code: str | None = None,
    ) -> tuple[ComboRfqMachine, RfqTransition | None]:
        target = ComboRfqState(status.value)
        if self.state.terminal:
            if self.state is target and self.tx_hash == tx_hash:
                return self, None
            raise ValueError("terminal RFQ cannot be overwritten by a different event")
        if not self._execution_transition_allowed(target):
            raise ValueError(f"invalid RFQ execution transition {self.state}->{target}")
        machine, transition = self._move(
            event_id=event_id,
            event_type=f"EXECUTION_{status.value}",
            to_state=target,
            event_ts=event_ts,
            payload={"tx_hash": tx_hash, "error_code": error_code},
        )
        return (
            replace(
                machine,
                tx_hash=tx_hash or machine.tx_hash,
                error_code=error_code,
                needs_reconciliation=not status.terminal,
            ),
            transition,
        )

    def local_timeout(
        self, *, event_id: str, event_ts: datetime
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        self._require_not_terminal()
        machine, transition = self._move(
            event_id=event_id,
            event_type="LOCAL_TIMEOUT_STATUS_REQUIRED",
            to_state=ComboRfqState.RECONCILING,
            event_ts=event_ts,
            payload={"retry_policy": "QUERY_STATUS_DO_NOT_RESUBMIT"},
        )
        return replace(machine, needs_reconciliation=True), transition

    def _execution_transition_allowed(self, target: ComboRfqState) -> bool:
        if target in {ComboRfqState.CONFIRMED, ComboRfqState.FAILED}:
            return True
        allowed = {
            ComboRfqState.REQUESTED: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
            ComboRfqState.QUOTE_COMPETITION: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
            ComboRfqState.QUOTE_AVAILABLE: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
            ComboRfqState.ACCEPTING: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
            ComboRfqState.AWAITING_MAKER_CONFIRMATION: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
            ComboRfqState.MATCHED: {
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
            ComboRfqState.MINED: {ComboRfqState.RETRYING},
            ComboRfqState.RETRYING: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
            },
            ComboRfqState.RECONCILING: {
                ComboRfqState.MATCHED,
                ComboRfqState.MINED,
                ComboRfqState.RETRYING,
            },
        }
        return target in allowed.get(self.state, set())

    def _deadline_failure(
        self,
        *,
        event_id: str,
        event_ts: datetime,
        reason: str,
        terminal_state: ComboRfqState = ComboRfqState.FAILED,
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        machine, transition = self._move(
            event_id=event_id,
            event_type="OFFICIAL_DEADLINE_REJECTED",
            to_state=terminal_state,
            event_ts=event_ts,
            payload={"reason": reason},
        )
        return replace(machine, error_code=reason), transition

    def _move(
        self,
        *,
        event_id: str,
        event_type: str,
        to_state: ComboRfqState,
        event_ts: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[ComboRfqMachine, RfqTransition]:
        if event_ts.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        late_terminal = (
            self.last_event_at is not None
            and event_ts < self.last_event_at
            and to_state.terminal
        )
        if (
            self.last_event_at is not None
            and event_ts < self.last_event_at
            and not late_terminal
        ):
            raise ValueError("late non-terminal RFQ event requires reconciliation")
        transition = RfqTransition(
            event_id=event_id,
            event_type=event_type,
            from_state=self.state,
            to_state=to_state,
            event_ts=event_ts,
            payload=dict(payload or {}),
        )
        return (
            replace(
                self,
                state=to_state,
                last_event_at=max(self.last_event_at, event_ts)
                if self.last_event_at is not None
                else event_ts,
                needs_reconciliation=to_state is ComboRfqState.RECONCILING,
            ),
            transition,
        )

    def _require_not_terminal(self) -> None:
        if self.state.terminal:
            raise ValueError("terminal RFQ cannot transition")
