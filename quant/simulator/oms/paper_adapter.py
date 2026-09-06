"""Adapter between live paper intents/results and the durable simulator OMS."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .domain import OmsAdmission, OmsAdmissionStatus, OwnOrder, SelfTradePolicy
from .oms_store import PostgresOwnOrderStore


@dataclass(frozen=True)
class PaperOmsDecision:
    intent_id: int
    order_id: str
    admission: OmsAdmission

    @property
    def accepted(self) -> bool:
        return self.admission.status in {
            OmsAdmissionStatus.ACCEPTED,
            OmsAdmissionStatus.RESEARCH_ONLY_INTERNAL_CROSS,
        }


class DurablePaperOmsGate:
    """Persist own-order admission before the simulated venue boundary."""

    def __init__(
        self,
        *,
        store: PostgresOwnOrderStore,
        account_id: str,
        policy: SelfTradePolicy = SelfTradePolicy.REJECT_INCOMING,
        research_mode: bool = False,
    ) -> None:
        if not str(account_id).strip():
            raise ValueError("paper OMS account_id is required")
        self.store = store
        self.account_id = str(account_id)
        self.policy = SelfTradePolicy(policy)
        self.research_mode = bool(research_mode)

    def evaluate(self, intent_id: int, intent: Any) -> PaperOmsDecision:
        order_id = self.order_id(intent_id)
        size = Decimal(intent.size)
        if str(intent.amount_unit).upper() == "QUOTE":
            size = size / Decimal(intent.limit_price)
        incoming = OwnOrder(
            order_id=order_id,
            account_id=self.account_id,
            strategy_id=str(intent.strategy_id),
            asset_id=str(intent.asset_id),
            side=str(intent.side),
            price=Decimal(intent.limit_price),
            original_size=size,
            remaining_size=size,
            created_sequence=int(intent_id),
        )
        admission = self.store.admit(
            admission_id=f"paper-oms-admission:{self.account_id}:{intent_id}",
            incoming=incoming,
            policy=self.policy,
            research_mode=self.research_mode,
            source_intent_id=int(intent_id),
        )
        return PaperOmsDecision(int(intent_id), order_id, admission)

    def finalize(self, intent_id: int, result: Any) -> OwnOrder | None:
        execution_status = str(result.status)
        order_type = str(getattr(getattr(result, "intent", None), "order_type", ""))
        if (
            order_type.upper() in {"FAK", "FOK"}
            and Decimal(result.remaining_size) > 0
            and execution_status.upper() in {"WORKING", "PARTIAL"}
        ):
            execution_status = "CANCELED"
        return self.store.finalize(
            order_id=self.order_id(intent_id),
            execution_status=execution_status,
            remaining_size=Decimal(result.remaining_size),
            event_id=f"paper-oms-result:{self.account_id}:{intent_id}:{result.audit_key}",
        )

    @staticmethod
    def rejection_reason(decision: PaperOmsDecision) -> str:
        return (
            f"own_order_oms:{decision.admission.status.value.lower()}:"
            f"{decision.admission.reason}"
        )

    @staticmethod
    def order_id(intent_id: int) -> str:
        return f"paper-intent:{int(intent_id)}"
