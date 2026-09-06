"""Explicit venue boundary for non-atomic CLOB legs and atomic Combo/RFQ."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    UnifiedAdmissionService,
    multileg_exposure_effect,
)

from .execution_plan import AtomicityPolicy, ExecutionLeg
from .plan_store import (
    DurableMultiLegPlan,
    LegExecutionOutcome,
    PostgresMultiLegPlanStore,
)
from .rfq import RfqState


@dataclass(frozen=True)
class VenueCapabilities:
    venue_name: str
    supports_independent_clob_legs: bool
    supports_atomic_combo: bool
    supports_rfq: bool


class MultiLegVenueAdapter(Protocol):
    @property
    def capabilities(self) -> VenueCapabilities: ...

    def execute_leg(self, plan_id: str, leg: ExecutionLeg) -> LegExecutionOutcome: ...

    def execute_atomic(
        self, plan: DurableMultiLegPlan
    ) -> tuple[LegExecutionOutcome, ...]: ...


class DurableMultiLegCoordinator:
    def __init__(
        self,
        *,
        store: PostgresMultiLegPlanStore,
        adapter: MultiLegVenueAdapter,
        admission_service: UnifiedAdmissionService | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        if admission_service is None:
            raise ValueError("unified admission service is required")
        self.admission_service = admission_service

    def execute(
        self,
        plan_id: str,
        *,
        event_prefix: str,
        event_ts: datetime,
    ) -> DurableMultiLegPlan:
        current = self.store.plan(plan_id)
        if current is None:
            raise KeyError(f"unknown multi-leg plan: {plan_id}")
        self._admit(
            current,
            request_id=f"multileg:{plan_id}:{event_prefix}:execute",
            event_ts=event_ts,
            source="multileg_coordinator",
        )
        policy = current.plan.policy
        capabilities = self.adapter.capabilities
        if policy is AtomicityPolicy.VENUE_ATOMIC:
            if not capabilities.supports_atomic_combo:
                raise ValueError(
                    "venue adapter does not support atomic combo execution"
                )
            outcomes = self.adapter.execute_atomic(current)
            return self.store.record_atomic_outcomes(
                plan_id,
                outcomes,
                event_id=f"{event_prefix}:atomic-result",
                event_ts=event_ts,
            )
        if policy is AtomicityPolicy.ALL_OR_NONE_SIMULATED:
            if not capabilities.supports_atomic_combo:
                raise ValueError(
                    "simulated all-or-none requires an atomic paper adapter"
                )
            outcomes = self.adapter.execute_atomic(current)
            return self.store.record_atomic_outcomes(
                plan_id,
                outcomes,
                event_id=f"{event_prefix}:simulated-aon-result",
                event_ts=event_ts,
            )
        if not capabilities.supports_independent_clob_legs:
            raise ValueError("venue adapter does not support ordinary CLOB legs")

        result = current
        for sequence, leg in enumerate(current.plan.legs):
            if leg.remaining_size <= 0:
                continue
            outcome = self.adapter.execute_leg(plan_id, leg)
            result = self.store.record_leg_outcome(
                plan_id,
                outcome,
                event_id=f"{event_prefix}:leg:{sequence}:{leg.leg_id}",
                event_ts=event_ts,
            )
            if policy is AtomicityPolicy.SEQUENTIAL and outcome.filled_size < leg.size:
                break
        return result

    def execute_confirmed_rfq(
        self,
        rfq_id: str,
        *,
        event_prefix: str,
        event_ts: datetime,
    ) -> DurableMultiLegPlan:
        lifecycle = self.store.rfq(rfq_id)
        if lifecycle is None:
            raise KeyError(f"unknown RFQ: {rfq_id}")
        if lifecycle.state is not RfqState.CONFIRMED:
            raise ValueError("RFQ must pass last look before execution")
        if not (
            self.adapter.capabilities.supports_rfq
            and self.adapter.capabilities.supports_atomic_combo
        ):
            raise ValueError("venue adapter does not support atomic RFQ execution")
        plan = self.store.plan_for_rfq(rfq_id)
        self._admit(
            plan,
            request_id=f"rfq:{rfq_id}:{event_prefix}:execute",
            event_ts=event_ts,
            source="confirmed_rfq",
        )
        if plan.state == "COMPLETE":
            self.store.mark_rfq_executed(
                rfq_id,
                event_id=f"{event_prefix}:rfq-executed",
                event_ts=event_ts,
            )
            return plan
        outcomes = self.adapter.execute_atomic(plan)
        result = self.store.record_atomic_outcomes(
            plan.plan.plan_id,
            outcomes,
            event_id=f"{event_prefix}:rfq-atomic-result",
            event_ts=event_ts,
        )
        self.store.mark_rfq_executed(
            rfq_id,
            event_id=f"{event_prefix}:rfq-executed",
            event_ts=event_ts,
        )
        return result

    def _admit(
        self,
        plan: DurableMultiLegPlan,
        *,
        request_id: str,
        event_ts: datetime,
        source: str,
    ) -> None:
        decision = self.admission_service.decide(
            AdmissionRequest(
                request_id=request_id,
                operation=AdmissionOperation.COMBO_RFQ,
                account_id=plan.account_id,
                strategy_id=plan.strategy_id,
                exposure_effect=multileg_exposure_effect(plan.plan.legs),
                observed_at=event_ts,
                metadata={
                    "source": source,
                    "plan_id": plan.plan.plan_id,
                    "asset_ids": [leg.asset_id for leg in plan.plan.legs],
                    "atomicity_policy": plan.plan.policy.value,
                },
            )
        )
        if not decision.allowed:
            raise ValueError(
                "multi-leg execution rejected by unified admission: "
                + ",".join(decision.reason_codes)
            )


class ScriptedPaperMultiLegAdapter:
    """Deterministic paper adapter; it never calls a venue or submits an order."""

    def __init__(
        self,
        *,
        capabilities: VenueCapabilities,
        leg_outcomes: dict[str, LegExecutionOutcome] | None = None,
        atomic_outcomes: tuple[LegExecutionOutcome, ...] = (),
    ) -> None:
        self._capabilities = capabilities
        self.leg_outcomes = dict(leg_outcomes or {})
        self.atomic_outcomes = tuple(atomic_outcomes)
        self.calls: list[tuple[str, str]] = []

    @property
    def capabilities(self) -> VenueCapabilities:
        return self._capabilities

    def execute_leg(self, plan_id: str, leg: ExecutionLeg) -> LegExecutionOutcome:
        self.calls.append(("LEG", leg.leg_id))
        try:
            return self.leg_outcomes[leg.leg_id]
        except KeyError as exc:
            raise ValueError(f"missing scripted leg outcome: {leg.leg_id}") from exc

    def execute_atomic(
        self, plan: DurableMultiLegPlan
    ) -> tuple[LegExecutionOutcome, ...]:
        self.calls.append(("ATOMIC", plan.plan.plan_id))
        if not self.atomic_outcomes:
            raise ValueError("missing scripted atomic outcome")
        return self.atomic_outcomes
