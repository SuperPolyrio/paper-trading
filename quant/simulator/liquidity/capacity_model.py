"""Adapter from existing calibrated capacity limits to simulator-facing labels."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant.execution.domain import CapacityStatus
from quant.risk.capacity_gate import CapacityDecision, CapacityGate, CapacitySnapshot


@dataclass(frozen=True)
class CapacityContext:
    top1_depth: Decimal
    top5_depth: Decimal
    visible_depth: Decimal
    trailing_real_volume: Decimal
    strategy_participation: Decimal
    account_participation: Decimal
    event_notional: Decimal
    has_market_impact_model: bool = False


def evaluate_capacity(context: CapacityContext, *, gate: CapacityGate | None = None) -> CapacityDecision | None:
    """Return None for unmodeled impact rather than labeling its PnL trusted."""
    if context.has_market_impact_model:
        return None
    safe = Decimal("0.00000001")
    snapshot = CapacitySnapshot(
        order_to_top1_depth=context.event_notional / max(safe, context.top1_depth),
        order_to_top5_depth=context.event_notional / max(safe, context.top5_depth),
        order_to_visible_eligible_depth=context.event_notional / max(safe, context.visible_depth),
        order_to_trailing_real_volume=context.event_notional / max(safe, context.trailing_real_volume),
        strategy_market_window_participation=context.strategy_participation,
        account_market_window_participation=context.account_participation,
        event_level_notional=context.event_notional,
    )
    return (gate or CapacityGate()).evaluate(snapshot)


def is_trusted_capacity(decision: CapacityDecision | None) -> bool:
    return decision is not None and decision.status is CapacityStatus.IN_DOMAIN_CALIBRATED
