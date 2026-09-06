"""Portfolio-level scenario, liquidity, lock-capital and concentration gate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .concentration import concentration_by
from .event_exposure import (
    EventRiskPosition,
    evaluate_event_scenarios,
)
from .outcome_scenarios import OutcomeScenario


@dataclass(frozen=True)
class EventRiskLimits:
    max_event_worst_case_loss: Decimal
    max_category_worst_case_loss: Decimal
    max_illiquid_position_value: Decimal
    max_dispute_locked_capital: Decimal
    max_strategy_concentration: Decimal


def evaluate_event_risk(
    positions: Iterable[EventRiskPosition],
    scenarios: Iterable[OutcomeScenario],
    limits: EventRiskLimits,
) -> dict[str, object]:
    rows = tuple(positions)
    results = evaluate_event_scenarios(rows, scenarios)
    per_condition: dict[tuple[str, str], Decimal] = {}
    for result in results:
        key = (result.event_id, result.condition_id or "__EVENT__")
        per_condition[key] = (
            min(per_condition[key], result.pnl) if key in per_condition else result.pnl
        )
    per_event: dict[str, Decimal] = {}
    for (event_id, _condition_id), loss in per_condition.items():
        per_event[event_id] = per_event.get(event_id, Decimal("0")) + loss
    per_category: dict[str, Decimal] = {}
    for event_id, loss in per_event.items():
        category = next(
            (row.category for row in rows if row.event_id == event_id), "unknown"
        )
        per_category[category] = per_category.get(category, Decimal("0")) + loss
    illiquid = sum(
        (
            Decimal(row.cost_basis)
            for row in rows
            if row.current_liquidation_value is None
        ),
        Decimal("0"),
    )
    locked = sum((Decimal(row.dispute_locked_capital) for row in rows), Decimal("0"))
    strategies = concentration_by(rows, "strategy_id")
    breaches = []
    breaches.extend(
        f"event:{event_id}"
        for event_id, loss in per_event.items()
        if -loss > limits.max_event_worst_case_loss
    )
    breaches.extend(
        f"category:{category}"
        for category, loss in per_category.items()
        if -loss > limits.max_category_worst_case_loss
    )
    if illiquid > limits.max_illiquid_position_value:
        breaches.append("illiquid_position_value")
    if locked > limits.max_dispute_locked_capital:
        breaches.append("dispute_locked_capital")
    breaches.extend(
        f"strategy:{strategy}"
        for strategy, total in strategies.items()
        if total > limits.max_strategy_concentration
    )
    return {
        "status": "PASS" if not breaches else "REJECT",
        "breaches": tuple(breaches),
        "scenario_results": tuple(results),
        "max_loss_at_resolution": min(per_event.values(), default=Decimal("0")),
        "max_gain_at_resolution": max(per_event.values(), default=Decimal("0")),
        "event_worst_case": per_event,
        "category_worst_case": per_category,
        "illiquid_position_value": illiquid,
        "dispute_locked_capital": locked,
        "strategy_concentration": strategies,
    }
