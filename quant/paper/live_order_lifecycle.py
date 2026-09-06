"""Durable internal lifecycle transitions for live paper order results."""

from __future__ import annotations

from dataclasses import dataclass

from .taker_execution import PaperExecutionResult


@dataclass(frozen=True)
class PaperOrderTransition:
    event_type: str
    to_state: str
    reason: str


def result_transitions(
    result: PaperExecutionResult,
) -> tuple[PaperOrderTransition, ...]:
    working = (
        result.intent.order_type in {"GTC", "GTD"}
        and result.status in {"WORKING", "PARTIAL"}
        and result.remaining_size > 0
    )
    transitions: list[PaperOrderTransition] = []
    if result.filled_size > 0:
        matched_state = (
            "PARTIALLY_MATCHED_PROVISIONAL"
            if result.remaining_size > 0
            else "MATCHED_PROVISIONAL"
        )
        transitions.extend(
            (
                PaperOrderTransition(matched_state, matched_state, result.reason),
                PaperOrderTransition(
                    "SETTLEMENT_PENDING",
                    "SETTLEMENT_PENDING",
                    "paper_fill_pending_synthetic_confirmation",
                ),
                PaperOrderTransition(
                    "CONFIRMED",
                    "CONFIRMED",
                    "paper_fill_synthetically_confirmed",
                ),
            )
        )
        if working:
            transitions.append(
                PaperOrderTransition(
                    "WORKING",
                    "WORKING",
                    "confirmed_partial_fill_with_resting_remainder",
                )
            )
        return tuple(transitions)

    if working:
        return (PaperOrderTransition("WORKING", "WORKING", result.reason),)

    status = str(result.status).upper()
    terminal_state = (
        "CANCELED"
        if status in {"CANCELLED", "CANCELED"}
        else "EXPIRED"
        if status == "EXPIRED"
        else "REJECTED"
    )
    return (
        PaperOrderTransition(terminal_state, terminal_state, result.reason),
    )
