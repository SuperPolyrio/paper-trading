"""Conservative surveillance: signals open cases; humans determine violations."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
from decimal import Decimal
from typing import Any

from quant.simulator.admission.domain import stable_hash

from .models import (
    IntegrityEvidence,
    IntegrityFindingType,
    SurveillanceObservation,
)
from .store import PostgresIntegrityCaseStore


class IntegritySurveillanceService:
    def __init__(
        self,
        *,
        store: PostgresIntegrityCaseStore,
        policy_version: str,
        cancel_window: timedelta = timedelta(minutes=5),
        front_run_window: timedelta = timedelta(seconds=30),
        spoof_cancel_ratio: Decimal = Decimal("0.90"),
        spoof_min_orders: int = 20,
    ) -> None:
        self.store = store
        self.policy_version = policy_version
        self.cancel_window = cancel_window
        self.front_run_window = front_run_window
        self.spoof_cancel_ratio = spoof_cancel_ratio
        self.spoof_min_orders = spoof_min_orders
        self._recent: dict[tuple[str, str], deque[SurveillanceObservation]] = defaultdict(
            deque
        )

    def observe(self, observation: SurveillanceObservation) -> tuple[Any, ...]:
        key = (observation.account_id, observation.condition_id)
        bucket = self._recent[key]
        cutoff = observation.event_ts - max(self.cancel_window, self.front_run_window)
        while bucket and bucket[0].event_ts < cutoff:
            bucket.popleft()
        evidence = self._detect(observation, tuple(bucket))
        bucket.append(observation)
        return tuple(
            self.store.record_evidence(item, policy_version=self.policy_version)
            for item in evidence
        )

    def manual_policy_signal(
        self,
        observation: SurveillanceObservation,
        *,
        finding_type: IntegrityFindingType,
        reason_codes: tuple[str, ...],
        evidence_payload: dict[str, Any],
    ) -> Any:
        if finding_type not in {
            IntegrityFindingType.CONFIDENTIAL_INFORMATION,
            IntegrityFindingType.OUTCOME_INFLUENCE,
        }:
            raise ValueError("manual policy signal type is not supported")
        evidence = self._evidence(
            observation,
            finding_type=finding_type,
            severity="HIGH",
            reason_codes=reason_codes,
            observation_ids=(observation.observation_id,),
            payload=evidence_payload,
        )
        return self.store.record_evidence(evidence, policy_version=self.policy_version)

    def _detect(
        self,
        current: SurveillanceObservation,
        recent: tuple[SurveillanceObservation, ...],
    ) -> tuple[IntegrityEvidence, ...]:
        findings: list[IntegrityEvidence] = []
        if (
            current.event_type.upper() == "TRADE"
            and current.beneficial_owner_id
            and current.beneficial_owner_id == current.counterparty_owner_id
        ):
            findings.append(
                self._evidence(
                    current,
                    finding_type=IntegrityFindingType.SELF_DEALING,
                    severity="HIGH",
                    reason_codes=("same_beneficial_owner_both_sides",),
                    observation_ids=(current.observation_id,),
                    payload={"trade_id": current.trade_id},
                )
            )
        reciprocal = [
            item
            for item in recent
            if item.event_type.upper() == "TRADE"
            and current.event_type.upper() == "TRADE"
            and item.counterparty_account_id == current.account_id
            and current.counterparty_account_id == item.account_id
            and item.side != current.side
        ]
        if reciprocal:
            findings.append(
                self._evidence(
                    current,
                    finding_type=IntegrityFindingType.WASH_TRADING,
                    severity="MEDIUM",
                    reason_codes=("rapid_reciprocal_counterparty_cycle",),
                    observation_ids=tuple(
                        [item.observation_id for item in reciprocal]
                        + [current.observation_id]
                    ),
                    payload={"requires_human_review": True},
                )
            )
        order_events = [
            item
            for item in recent
            if item.event_type.upper() in {"ORDER_OPEN", "ORDER_CANCEL", "FILL"}
        ]
        if current.event_type.upper() == "ORDER_CANCEL":
            order_events.append(current)
            opens = sum(item.event_type.upper() == "ORDER_OPEN" for item in order_events)
            cancels = sum(item.event_type.upper() == "ORDER_CANCEL" for item in order_events)
            fills = sum(item.event_type.upper() == "FILL" for item in order_events)
            denominator = max(opens, 1)
            cancel_ratio = Decimal(cancels) / Decimal(denominator)
            near_touch = sum(bool(item.metadata.get("near_touch")) for item in order_events)
            if (
                opens >= self.spoof_min_orders
                and cancel_ratio >= self.spoof_cancel_ratio
                and fills == 0
                and near_touch >= self.spoof_min_orders // 2
            ):
                findings.append(
                    self._evidence(
                        current,
                        finding_type=IntegrityFindingType.SPOOFING_LAYERING,
                        severity="MEDIUM",
                        reason_codes=(
                            "high_near_touch_cancel_ratio",
                            "no_corresponding_fills",
                        ),
                        observation_ids=tuple(item.observation_id for item in order_events),
                        payload={
                            "opens": opens,
                            "cancels": cancels,
                            "fills": fills,
                            "cancel_ratio": str(cancel_ratio),
                            "requires_human_review": True,
                        },
                    )
                )
        if current.event_type.upper() == "CLIENT_ORDER":
            preceding = [
                item
                for item in recent
                if item.event_type.upper() in {"TRADE", "ORDER_OPEN"}
                and item.side == current.side
                and bool(item.metadata.get("employee_or_builder_account"))
                and current.event_ts - item.event_ts <= self.front_run_window
            ]
            if preceding:
                findings.append(
                    self._evidence(
                        current,
                        finding_type=IntegrityFindingType.FRONT_RUNNING,
                        severity="HIGH",
                        reason_codes=("privileged_account_preceded_client_order",),
                        observation_ids=tuple(
                            [item.observation_id for item in preceding]
                            + [current.observation_id]
                        ),
                        payload={"requires_human_review": True},
                    )
                )
        return tuple(findings)

    def _evidence(
        self,
        observation: SurveillanceObservation,
        *,
        finding_type: IntegrityFindingType,
        severity: str,
        reason_codes: tuple[str, ...],
        observation_ids: tuple[str, ...],
        payload: dict[str, Any],
    ) -> IntegrityEvidence:
        evidence_id = stable_hash(
            {
                "finding_type": finding_type.value,
                "account_id": observation.account_id,
                "condition_id": observation.condition_id,
                "observation_ids": observation_ids,
                "reason_codes": reason_codes,
            },
            prefix="integrity-evidence-",
        )
        return IntegrityEvidence(
            evidence_id=evidence_id,
            finding_type=finding_type,
            severity=severity,
            account_id=observation.account_id,
            condition_id=observation.condition_id,
            observed_at=observation.event_ts,
            observation_ids=observation_ids,
            reason_codes=reason_codes,
            payload=payload,
        )
