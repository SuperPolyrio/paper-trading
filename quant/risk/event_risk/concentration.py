"""Concentration helpers that preserve category, event and strategy dimensions."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .event_exposure import EventRiskPosition


def concentration_by(
    positions: Iterable[EventRiskPosition], attribute: str
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for position in positions:
        key = str(getattr(position, attribute))
        totals[key] = totals.get(key, Decimal("0")) + abs(Decimal(position.cost_basis))
    return totals
