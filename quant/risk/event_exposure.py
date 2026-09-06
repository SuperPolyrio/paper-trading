"""Condition, event, neg-risk, category, and pending-settlement risk."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class ExposureLimits:
    max_per_condition: Decimal
    max_per_event: Decimal
    max_per_neg_risk_group: Decimal
    max_correlated_category: Decimal
    max_unconfirmed_receivable: Decimal
    max_resolution_pending_capital: Decimal


@dataclass(frozen=True)
class ExposurePosition:
    condition_id: str
    event_id: str
    category: str
    notional: Decimal
    neg_risk_group: str | None = None
    unconfirmed_receivable: Decimal = Decimal("0")
    resolution_pending_capital: Decimal = Decimal("0")


def evaluate_exposure(
    positions: Iterable[ExposurePosition],
    limits: ExposureLimits,
) -> dict[str, object]:
    aggregates: dict[str, dict[str, Decimal]] = {
        "condition": {},
        "event": {},
        "neg_risk": {},
        "category": {},
    }
    unconfirmed = Decimal("0")
    pending = Decimal("0")
    for position in positions:
        _add(aggregates["condition"], position.condition_id, position.notional)
        _add(aggregates["event"], position.event_id, position.notional)
        _add(aggregates["category"], position.category, position.notional)
        if position.neg_risk_group:
            _add(aggregates["neg_risk"], position.neg_risk_group, position.notional)
        unconfirmed += position.unconfirmed_receivable
        pending += position.resolution_pending_capital
    breaches: list[str] = []
    for dimension, maximum in (
        ("condition", limits.max_per_condition),
        ("event", limits.max_per_event),
        ("neg_risk", limits.max_per_neg_risk_group),
        ("category", limits.max_correlated_category),
    ):
        breaches.extend(
            f"{dimension}:{key}"
            for key, value in aggregates[dimension].items()
            if value > maximum
        )
    if unconfirmed > limits.max_unconfirmed_receivable:
        breaches.append("unconfirmed_settlement_receivable")
    if pending > limits.max_resolution_pending_capital:
        breaches.append("resolution_pending_capital")
    return {
        "status": "PASS" if not breaches else "REJECT",
        "breaches": breaches,
        "aggregates": {
            dimension: {key: format(value, "f") for key, value in values.items()}
            for dimension, values in aggregates.items()
        },
        "unconfirmed_receivable": format(unconfirmed, "f"),
        "resolution_pending_capital": format(pending, "f"),
    }


def _add(target: dict[str, Decimal], key: str, amount: Decimal) -> None:
    target[key] = target.get(key, Decimal("0")) + amount
