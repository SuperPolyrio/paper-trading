"""Fail-closed self-trade prevention decisions for one simulator account."""

from __future__ import annotations

from .domain import OmsAdmission, OmsAdmissionStatus, OwnOrder, SelfTradePolicy


class SelfTradePrevention:
    def __init__(self, policy: SelfTradePolicy = SelfTradePolicy.REJECT_INCOMING) -> None:
        self.policy = policy

    def evaluate(
        self,
        incoming: OwnOrder,
        *,
        conflicts: tuple[OwnOrder, ...],
        research_mode: bool = False,
    ) -> OmsAdmission:
        if not conflicts:
            return OmsAdmission(incoming, OmsAdmissionStatus.ACCEPTED, "no_self_cross")
        conflict_ids = tuple(order.order_id for order in conflicts)
        if self.policy in {SelfTradePolicy.REJECT_INCOMING, SelfTradePolicy.CANCEL_NEWEST}:
            return OmsAdmission(incoming, OmsAdmissionStatus.REJECTED_SELF_TRADE, "incoming_would_cross_own_working_order")
        if self.policy is SelfTradePolicy.CANCEL_OLDEST:
            return OmsAdmission(
                incoming,
                OmsAdmissionStatus.DEFERRED_CANCEL_PENDING,
                "conflicting_own_orders_must_cancel_before_incoming_is_retried",
                conflict_ids,
            )
        if self.policy is SelfTradePolicy.CANCEL_BOTH:
            return OmsAdmission(
                incoming,
                OmsAdmissionStatus.REJECTED_SELF_TRADE,
                "incoming_rejected_and_conflicting_own_orders_cancel_requested",
                conflict_ids,
            )
        if self.policy is SelfTradePolicy.ALLOW_INTERNAL_CROSS_FOR_RESEARCH_ONLY and research_mode:
            return OmsAdmission(
                incoming,
                OmsAdmissionStatus.RESEARCH_ONLY_INTERNAL_CROSS,
                "research_only_internal_cross_no_synthetic_volume_created",
                conflict_ids,
            )
        return OmsAdmission(incoming, OmsAdmissionStatus.REJECTED_SELF_TRADE, "internal_cross_requires_explicit_research_mode")
