"""Event-level resolution PnL across mutually exclusive outcome scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .outcome_scenarios import OutcomeScenario


@dataclass(frozen=True)
class EventRiskPosition:
    event_id: str
    category: str
    strategy_id: str
    asset_id: str
    quantity: Decimal
    cost_basis: Decimal
    current_liquidation_value: Decimal | None = None
    dispute_locked_capital: Decimal = Decimal("0")
    condition_id: str = ""


@dataclass(frozen=True)
class EventScenarioResult:
    scenario_id: str
    event_id: str
    payout: Decimal
    pnl: Decimal
    condition_id: str = ""


def evaluate_event_scenarios(
    positions: Iterable[EventRiskPosition],
    scenarios: Iterable[OutcomeScenario],
) -> tuple[EventScenarioResult, ...]:
    by_event: dict[str, list[EventRiskPosition]] = {}
    for position in positions:
        by_event.setdefault(position.event_id, []).append(position)
    results: list[EventScenarioResult] = []
    for scenario in scenarios:
        rows = by_event.get(scenario.event_id, [])
        if scenario.condition_id:
            rows = [row for row in rows if row.condition_id == scenario.condition_id]
        payout = sum(
            (Decimal(row.quantity) * scenario.payout_for(row.asset_id) for row in rows),
            Decimal("0"),
        )
        cost = sum((Decimal(row.cost_basis) for row in rows), Decimal("0"))
        results.append(
            EventScenarioResult(
                scenario.scenario_id,
                scenario.event_id,
                payout,
                payout - cost,
                scenario.condition_id,
            )
        )
    return tuple(results)
