"""Admission-controlled Combo orchestration and official-event reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    ExposureEffect,
    UnifiedAdmissionService,
)

from .adapters import OfficialComboRestAdapter
from .models import (
    ComboDirection,
    ComboQuote,
    ComboRequest,
    ComboRfqState,
    ExecutionStatus,
    OfficialRfqSnapshot,
)
from .quoter_ws import OfficialQuoterCommandSession
from .state_machine import ComboRfqMachine
from .store import ComboFillAccounting, PostgresComboStore


class ComboRfqService:
    def __init__(
        self,
        *,
        adapter: OfficialComboRestAdapter,
        store: PostgresComboStore,
        admission: UnifiedAdmissionService,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.admission = admission

    def create(
        self,
        request: ComboRequest,
        *,
        account_id: str,
        strategy_id: str,
        exposure_before: Decimal | None = None,
        exposure_after: Decimal | None = None,
    ) -> ComboRfqMachine:
        decision = self.admission.decide(
            AdmissionRequest(
                request_id=f"combo-create:{request.rfq_id}",
                operation=AdmissionOperation.COMBO_RFQ,
                account_id=account_id,
                strategy_id=strategy_id,
                exposure_effect=(
                    ExposureEffect.INCREASE
                    if request.direction is ComboDirection.BUY
                    else ExposureEffect.REDUCE
                ),
                exposure_before=exposure_before,
                exposure_after=exposure_after,
                observed_at=request.created_at,
                metadata={
                    "rfq_id": request.rfq_id,
                    "leg_position_ids": list(request.leg_position_ids),
                    "size_unit": request.requested_size.unit.value,
                    "size_e6": request.requested_size.value_e6,
                },
            )
        )
        if not decision.allowed:
            raise ValueError(
                "Combo RFQ rejected by Unified Admission: "
                + ",".join(decision.reason_codes)
            )
        command_id = f"combo-command:create:{request.rfq_id}"
        self.store.begin_command(
            command_id=command_id,
            client_request_id=request.rfq_id,
            command_type="CREATE",
            request_payload=request.as_builder_payload(),
            started_at=request.created_at,
            deadline=None,
        )
        try:
            snapshot = self.adapter.create_builder_rfq(request)
        except Exception as exc:
            state = "FAILED" if _definitive_http_error(exc) else "UNKNOWN"
            self.store.finish_command(
                command_id=command_id,
                state=state,
                finished_at=datetime.now(timezone.utc),
                error=exc,
            )
            if state == "UNKNOWN":
                raise ComboCommandOutcomeUnknown(command_id, request.rfq_id) from exc
            raise
        effective_request = _official_request(request, snapshot)
        machine = self.store.create_rfq(
            ComboRfqMachine(request=effective_request),
            account_id=account_id,
            strategy_id=strategy_id,
            admission_decision_id=decision.decision_id,
        )
        self.store.finish_command(
            command_id=command_id,
            state="ACKNOWLEDGED",
            finished_at=datetime.now(timezone.utc),
            official_rfq_id=effective_request.rfq_id,
            response_payload=snapshot.payload,
        )
        return self.reconcile_snapshot(
            machine,
            snapshot,
            event_id=(
                f"combo-create-response:{effective_request.rfq_id}:"
                f"{snapshot.payload_hash}"
            ),
            event_ts=datetime.now(timezone.utc),
        )

    def accept(
        self,
        *,
        rfq_id: str,
        account_id: str,
        strategy_id: str,
        signed_order: Mapping[str, Any],
        observed_at: datetime,
        exposure_before: Decimal | None = None,
        exposure_after: Decimal | None = None,
    ) -> ComboRfqMachine:
        machine = self.store.rfq(rfq_id)
        if machine.quote is None:
            raise ValueError("Combo RFQ has no quote to accept")
        decision = self.admission.decide(
            AdmissionRequest(
                request_id=f"combo-accept:{rfq_id}:{machine.quote.quote_id}",
                operation=AdmissionOperation.COMBO_RFQ,
                account_id=account_id,
                strategy_id=strategy_id,
                exposure_effect=(
                    ExposureEffect.INCREASE
                    if machine.request.direction is ComboDirection.BUY
                    else ExposureEffect.REDUCE
                ),
                exposure_before=exposure_before,
                exposure_after=exposure_after,
                observed_at=observed_at,
                metadata={
                    "rfq_id": rfq_id,
                    "quote_id": machine.quote.quote_id,
                    "command": "ACCEPT",
                },
            )
        )
        if not decision.allowed:
            raise ValueError(
                "Combo acceptance rejected by Unified Admission: "
                + ",".join(decision.reason_codes)
            )
        command_id = f"combo-command:accept:{rfq_id}:{machine.quote.quote_id}"
        request_payload = {
            "rfq_id": rfq_id,
            "quote_id": machine.quote.quote_id,
            "signed_order": dict(signed_order),
        }
        self.store.begin_command(
            command_id=command_id,
            client_request_id=rfq_id,
            command_type="ACCEPT",
            request_payload=request_payload,
            started_at=observed_at,
            deadline=machine.quote.expires_at,
        )
        after, transition = machine.accept_quote(
            event_id=f"{command_id}:local", event_ts=observed_at
        )
        machine = self.store.apply_transition(
            after, transition, source="LOCAL_BUILDER_COMMAND"
        )
        try:
            snapshot = self.adapter.accept_builder_rfq(
                rfq_id=rfq_id,
                quote_id=machine.quote.quote_id,
                signed_order=signed_order,
            )
        except Exception as exc:
            definitive = _definitive_http_error(exc)
            self.store.finish_command(
                command_id=command_id,
                state="FAILED" if definitive else "UNKNOWN",
                finished_at=datetime.now(timezone.utc),
                official_rfq_id=rfq_id,
                error=exc,
            )
            if definitive:
                failed, failed_transition = machine.apply_execution(
                    ExecutionStatus.FAILED,
                    event_id=f"{command_id}:http-failed",
                    event_ts=datetime.now(timezone.utc),
                    error_code=f"HTTP_{exc.response.status_code}",
                )
                if failed_transition is not None:
                    return self.store.apply_transition(
                        failed,
                        failed_transition,
                        source="BUILDER_REST_DEFINITIVE_FAILURE",
                    )
                return failed
            unknown, unknown_transition = machine.local_timeout(
                event_id=f"{command_id}:unknown",
                event_ts=datetime.now(timezone.utc),
            )
            self.store.apply_transition(
                unknown,
                unknown_transition,
                source="BUILDER_REST_OUTCOME_UNKNOWN",
            )
            raise ComboCommandOutcomeUnknown(command_id, rfq_id) from exc
        self.store.finish_command(
            command_id=command_id,
            state="ACKNOWLEDGED",
            finished_at=datetime.now(timezone.utc),
            official_rfq_id=rfq_id,
            response_payload=snapshot.payload,
        )
        return self.reconcile_snapshot(
            machine,
            snapshot,
            event_id=f"combo-accept-response:{rfq_id}:{snapshot.payload_hash}",
            event_ts=datetime.now(timezone.utc),
        )

    def reconcile_status(self, *, rfq_id: str) -> ComboRfqMachine:
        machine = self.store.rfq(rfq_id)
        snapshot = self.adapter.builder_rfq_status(rfq_id=rfq_id)
        return self.reconcile_snapshot(
            machine,
            snapshot,
            event_id=f"combo-status:{rfq_id}:{snapshot.payload_hash}",
            event_ts=datetime.now(timezone.utc),
        )

    def reconcile_snapshot(
        self,
        machine: ComboRfqMachine,
        snapshot: OfficialRfqSnapshot,
        *,
        event_id: str,
        event_ts: datetime,
    ) -> ComboRfqMachine:
        if snapshot.rfq_id and snapshot.rfq_id != machine.request.rfq_id:
            raise ValueError("official status belongs to a different Combo RFQ")
        target = snapshot.state
        if (
            machine.last_event_at is not None
            and event_ts < machine.last_event_at
            and not target.terminal
        ):
            return machine
        if target is ComboRfqState.QUOTE_AVAILABLE and snapshot.quote is not None:
            if machine.request.submission_deadline is not None and (
                machine.state is ComboRfqState.REQUESTED
            ):
                machine, transition = machine.open_competition(
                    event_id=f"{event_id}:competition", event_ts=event_ts
                )
                machine = self.store.apply_transition(
                    machine, transition, source="BUILDER_REST"
                )
            machine, transition = machine.submit_quote(
                snapshot.quote, event_id=event_id, event_ts=event_ts
            )
            return self.store.apply_transition(
                machine, transition, source="BUILDER_REST"
            )
        if target in {
            ComboRfqState.MATCHED,
            ComboRfqState.MINED,
            ComboRfqState.RETRYING,
            ComboRfqState.CONFIRMED,
            ComboRfqState.FAILED,
        }:
            if machine.state is ComboRfqState.MINED and target is ComboRfqState.MATCHED:
                return machine
            status = ExecutionStatus(target.value)
            machine, transition = machine.apply_execution(
                status,
                event_id=event_id,
                event_ts=event_ts,
                tx_hash=snapshot.tx_hash,
                error_code=snapshot.error_code or snapshot.error_message,
            )
            if transition is None:
                return machine
            return self.store.apply_transition(
                machine, transition, source="OFFICIAL_RECONCILIATION"
            )
        if target in {ComboRfqState.EXPIRED, ComboRfqState.CANCELED}:
            status = ExecutionStatus.FAILED
            machine, transition = machine.apply_execution(
                status,
                event_id=event_id,
                event_ts=event_ts,
                error_code=target.value,
            )
            if transition is None:
                return machine
            return self.store.apply_transition(
                machine, transition, source="OFFICIAL_RECONCILIATION"
            )
        if target is ComboRfqState.RECONCILING:
            machine, transition = machine.local_timeout(
                event_id=event_id, event_ts=event_ts
            )
            return self.store.apply_transition(
                machine, transition, source="OFFICIAL_STATUS_UNKNOWN"
            )
        return machine

    def apply_confirmed_accounting(
        self,
        *,
        accounting_event_id: str,
        rfq_id: str,
        account_id: str,
        strategy_id: str,
        shares_e6: int,
        cash_e6: int,
        official_payload: Mapping[str, Any],
        event_ts: datetime,
    ) -> Mapping[str, Any]:
        machine = self.store.rfq(rfq_id)
        if machine.state is not ComboRfqState.CONFIRMED or not machine.tx_hash:
            raise ValueError("Combo accounting requires confirmed official truth")
        return self.store.apply_confirmed_fill(
            ComboFillAccounting(
                accounting_event_id=accounting_event_id,
                rfq_id=rfq_id,
                account_id=account_id,
                strategy_id=strategy_id,
                direction=machine.request.direction,
                combo_position_id=machine.request.yes_position_id,
                shares_e6=shares_e6,
                cash_e6=cash_e6,
                tx_hash=machine.tx_hash,
                event_ts=event_ts,
                official_payload=official_payload,
            )
        )


class CollateralReturnService:
    def __init__(
        self,
        *,
        adapter: OfficialComboRestAdapter,
        store: PostgresComboStore,
        admission: UnifiedAdmissionService,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.admission = admission

    def plan(
        self,
        *,
        account_id: str,
        strategy_id: str,
        observed_at: datetime,
    ) -> Mapping[str, Any]:
        decision = self.admission.decide(
            AdmissionRequest(
                request_id=f"combo-collateral-plan:{account_id}:{int(observed_at.timestamp())}",
                operation=AdmissionOperation.MERGE,
                account_id=account_id,
                strategy_id=strategy_id,
                exposure_effect=ExposureEffect.REDUCE,
                observed_at=observed_at,
                metadata={"operation": "COMBO_COLLATERAL_RETURN_PLAN"},
            )
        )
        if not decision.allowed:
            raise ValueError("collateral return rejected by Unified Admission")
        plan = self.adapter.collateral_return_plan()
        self.store.record_collateral_plan(
            plan,
            account_id=account_id,
            strategy_id=strategy_id,
            admission_decision_id=decision.decision_id,
            created_at=observed_at,
        )
        return plan

    def dry_run(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.adapter.execute_collateral_return_plan(plan=plan, execute=False)
        return dict(result)

    def confirm(
        self,
        *,
        plan_hash: str,
        tx_hash: str,
        confirmed_at: datetime,
    ) -> Mapping[str, Any]:
        return self.store.apply_collateral_confirmation(
            plan_hash=plan_hash,
            tx_hash=tx_hash,
            confirmed_at=confirmed_at,
        )


class ComboQuoterService:
    """Admission-controlled command boundary for the authenticated Quoter WS."""

    def __init__(
        self,
        *,
        session: OfficialQuoterCommandSession,
        admission: UnifiedAdmissionService,
    ) -> None:
        self.session = session
        self.admission = admission

    async def submit_quote(
        self,
        request: ComboRequest,
        quote: ComboQuote,
        *,
        account_id: str,
        strategy_id: str,
        observed_at: datetime,
        exposure_before: Decimal | None = None,
        exposure_after: Decimal | None = None,
    ) -> None:
        self._admit_trade(
            request=request,
            account_id=account_id,
            strategy_id=strategy_id,
            observed_at=observed_at,
            command="QUOTER_SUBMIT_QUOTE",
            exposure_before=exposure_before,
            exposure_after=exposure_after,
        )
        await self.session.submit_quote(request, quote, now=observed_at)

    async def cancel_quote(
        self,
        *,
        rfq_id: str,
        quote_id: str,
        signer_address: str,
        maker_address: str,
        account_id: str,
        strategy_id: str,
        observed_at: datetime,
    ) -> None:
        decision = self.admission.decide(
            AdmissionRequest(
                request_id=f"combo-quoter-cancel:{rfq_id}:{quote_id}",
                operation=AdmissionOperation.ORDER_CANCEL,
                account_id=account_id,
                strategy_id=strategy_id,
                exposure_effect=ExposureEffect.REDUCE,
                observed_at=observed_at,
                metadata={"rfq_id": rfq_id, "quote_id": quote_id},
            )
        )
        if not decision.allowed:
            raise ValueError("Combo quote cancel rejected by Unified Admission")
        await self.session.cancel_quote(
            rfq_id=rfq_id,
            quote_id=quote_id,
            signer_address=signer_address,
            maker_address=maker_address,
        )

    async def respond_last_look(
        self,
        request: ComboRequest,
        *,
        quote_id: str,
        confirm_by: datetime,
        confirm: bool,
        account_id: str,
        strategy_id: str,
        observed_at: datetime,
        exposure_before: Decimal | None = None,
        exposure_after: Decimal | None = None,
    ) -> None:
        if confirm:
            self._admit_trade(
                request=request,
                account_id=account_id,
                strategy_id=strategy_id,
                observed_at=observed_at,
                command="QUOTER_LAST_LOOK_CONFIRM",
                exposure_before=exposure_before,
                exposure_after=exposure_after,
            )
        else:
            decision = self.admission.decide(
                AdmissionRequest(
                    request_id=f"combo-last-look-decline:{request.rfq_id}:{quote_id}",
                    operation=AdmissionOperation.ORDER_CANCEL,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    exposure_effect=ExposureEffect.REDUCE,
                    observed_at=observed_at,
                    metadata={"rfq_id": request.rfq_id, "quote_id": quote_id},
                )
            )
            if not decision.allowed:
                raise ValueError("Combo Last Look decline rejected by Unified Admission")
        await self.session.respond_last_look(
            rfq_id=request.rfq_id,
            quote_id=quote_id,
            confirm_by=confirm_by,
            confirm=confirm,
            now=observed_at,
        )

    def _admit_trade(
        self,
        *,
        request: ComboRequest,
        account_id: str,
        strategy_id: str,
        observed_at: datetime,
        command: str,
        exposure_before: Decimal | None,
        exposure_after: Decimal | None,
    ) -> None:
        decision = self.admission.decide(
            AdmissionRequest(
                request_id=f"combo-quoter:{command}:{request.rfq_id}",
                operation=AdmissionOperation.COMBO_RFQ,
                account_id=account_id,
                strategy_id=strategy_id,
                exposure_effect=(
                    ExposureEffect.INCREASE
                    if request.direction is ComboDirection.BUY
                    else ExposureEffect.REDUCE
                ),
                exposure_before=exposure_before,
                exposure_after=exposure_after,
                observed_at=observed_at,
                metadata={"rfq_id": request.rfq_id, "command": command},
            )
        )
        if not decision.allowed:
            raise ValueError(
                "Combo Quoter command rejected by Unified Admission: "
                + ",".join(decision.reason_codes)
            )


class ComboCommandOutcomeUnknown(RuntimeError):
    def __init__(self, command_id: str, rfq_id: str) -> None:
        super().__init__(
            f"Combo command outcome unknown; query official status: {rfq_id}"
        )
        self.command_id = command_id
        self.rfq_id = rfq_id


def _official_request(
    local: ComboRequest, snapshot: OfficialRfqSnapshot
) -> ComboRequest:
    request_payload = snapshot.payload.get("request")
    if isinstance(request_payload, Mapping):
        try:
            return ComboRequest.from_api(request_payload)
        except (KeyError, TypeError, ValueError):
            pass
    if not snapshot.rfq_id:
        raise ValueError("Builder RFQ response did not provide an official rfq_id")
    return replace(
        local,
        rfq_id=snapshot.rfq_id,
        submission_deadline=None,
    )


def _definitive_http_error(error: BaseException) -> bool:
    return isinstance(error, requests.HTTPError) and error.response is not None and (
        error.response.status_code in {400, 401, 403, 404, 409}
    )
