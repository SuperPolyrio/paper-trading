"""Projected order admission against event payout and liquidity constraints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import Any

from .event_exposure import EventRiskPosition
from .liquidity_risk import ExitLevel, walk_exit_book
from .outcome_scenarios import OutcomeScenario
from .stress_engine import EventRiskLimits, evaluate_event_risk


@dataclass(frozen=True)
class EventRiskAdmissionInput:
    positions: tuple[EventRiskPosition, ...]
    scenarios: tuple[OutcomeScenario, ...]
    status: str = "READY"
    reasons: tuple[str, ...] = ()
    model_version: str = "paper-event-risk-v1"

    def as_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def evaluate_projected_order_risk(
    risk_input: EventRiskAdmissionInput,
    *,
    intent: Any,
    arrival_checkpoint: Any | None,
    event_id: str,
    category: str,
    limits: EventRiskLimits,
) -> dict[str, Any]:
    if risk_input.status != "READY":
        return {
            "status": "REJECT",
            "breaches": tuple(f"input:{reason}" for reason in risk_input.reasons)
            or ("input:unavailable",),
            "model_version": risk_input.model_version,
            "input": risk_input.as_dict(),
            "limits": _json_value(asdict(limits)),
        }

    projected = _project_positions(
        risk_input.positions,
        intent=intent,
        arrival_checkpoint=arrival_checkpoint,
        event_id=event_id,
        category=category,
    )
    report = evaluate_event_risk(projected, risk_input.scenarios, limits)
    return {
        **report,
        "model_version": risk_input.model_version,
        "input": risk_input.as_dict(),
        "projected_positions": _json_value([asdict(row) for row in projected]),
        "limits": _json_value(asdict(limits)),
    }


def _project_positions(
    positions: tuple[EventRiskPosition, ...],
    *,
    intent: Any,
    arrival_checkpoint: Any | None,
    event_id: str,
    category: str,
) -> tuple[EventRiskPosition, ...]:
    rows = list(positions)
    index = next(
        (
            position_index
            for position_index, position in enumerate(rows)
            if position.asset_id == str(intent.asset_id)
            and position.strategy_id == str(intent.strategy_id)
        ),
        None,
    )
    quantity = _intent_quantity(intent)
    if str(intent.side).upper() == "SELL":
        if index is None:
            return tuple(rows)
        existing = rows[index]
        remaining = max(Decimal(0), Decimal(existing.quantity) - quantity)
        if remaining == 0:
            rows.pop(index)
        else:
            ratio = remaining / Decimal(existing.quantity)
            rows[index] = replace(
                existing,
                quantity=remaining,
                cost_basis=Decimal(existing.cost_basis) * ratio,
                current_liquidation_value=(
                    Decimal(existing.current_liquidation_value) * ratio
                    if existing.current_liquidation_value is not None
                    else None
                ),
                dispute_locked_capital=(
                    Decimal(existing.dispute_locked_capital) * ratio
                ),
            )
        return tuple(rows)

    candidate_cost = _intent_notional(intent)
    candidate_liquidation = _candidate_liquidation_value(quantity, arrival_checkpoint)
    if index is None:
        rows.append(
            EventRiskPosition(
                event_id=str(event_id or intent.condition_id),
                category=str(category or "unknown"),
                strategy_id=str(intent.strategy_id),
                asset_id=str(intent.asset_id),
                quantity=quantity,
                cost_basis=candidate_cost,
                current_liquidation_value=candidate_liquidation,
                condition_id=str(intent.condition_id),
            )
        )
    else:
        existing = rows[index]
        rows[index] = replace(
            existing,
            quantity=Decimal(existing.quantity) + quantity,
            cost_basis=Decimal(existing.cost_basis) + candidate_cost,
            current_liquidation_value=(
                Decimal(existing.current_liquidation_value)
                + Decimal(candidate_liquidation)
                if existing.current_liquidation_value is not None
                and candidate_liquidation is not None
                else None
            ),
        )
    return tuple(rows)


def _candidate_liquidation_value(
    quantity: Decimal, arrival_checkpoint: Any | None
) -> Decimal | None:
    if arrival_checkpoint is None:
        return None
    result = walk_exit_book(
        quantity,
        tuple(
            ExitLevel(Decimal(level.price), Decimal(level.size))
            for level in arrival_checkpoint.bids
        ),
    )
    return result.executable_value if result.unliquidated_quantity == 0 else None


def _intent_quantity(intent: Any) -> Decimal:
    if str(intent.side).upper() == "BUY" and str(intent.amount_unit).upper() == "QUOTE":
        return max(Decimal(0), Decimal(intent.size) / Decimal(intent.limit_price))
    return max(Decimal(0), Decimal(intent.size))


def _intent_notional(intent: Any) -> Decimal:
    if str(intent.side).upper() == "BUY" and str(intent.amount_unit).upper() == "QUOTE":
        return max(Decimal(0), Decimal(intent.size))
    return max(Decimal(0), Decimal(intent.size) * Decimal(intent.limit_price))


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
