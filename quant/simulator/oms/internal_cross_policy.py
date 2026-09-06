"""Coordinator that applies self-trade policy before an own order is working."""

from __future__ import annotations

from .domain import OmsAdmission, OmsAdmissionStatus, OwnOrder, SelfTradePolicy
from .own_order_book import OwnOrderBook
from .self_trade_prevention import SelfTradePrevention


class InternalCrossPolicy:
    """No policy manufactures a synthetic fill from two simulator strategies."""

    def __init__(self, policy: SelfTradePolicy = SelfTradePolicy.REJECT_INCOMING) -> None:
        self.prevention = SelfTradePrevention(policy)

    def admit(self, book: OwnOrderBook, incoming: OwnOrder, *, research_mode: bool = False) -> OmsAdmission:
        admission = self.prevention.evaluate(
            incoming,
            conflicts=book.crossing_orders(incoming),
            research_mode=research_mode,
        )
        for index, order_id in enumerate(admission.cancel_order_ids):
            book.request_cancel(order_id, event_id=f"self-trade:{incoming.order_id}:{index}:{order_id}")
        if admission.status is OmsAdmissionStatus.ACCEPTED:
            book.submit(incoming)
        elif admission.status is OmsAdmissionStatus.RESEARCH_ONLY_INTERNAL_CROSS:
            book.submit(incoming)
        return admission
