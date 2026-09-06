"""Fail-closed liquidation values when visible exit depth cannot cover a position."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class ExitLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class LiquidityRiskResult:
    requested_quantity: Decimal
    executable_quantity: Decimal
    executable_value: Decimal
    unliquidated_quantity: Decimal
    status: str


def walk_exit_book(
    quantity: Decimal, levels: Iterable[ExitLevel]
) -> LiquidityRiskResult:
    remaining = max(Decimal("0"), Decimal(quantity))
    executable = Decimal("0")
    value = Decimal("0")
    for level in levels:
        take = min(remaining, max(Decimal("0"), Decimal(level.size)))
        executable += take
        value += take * Decimal(level.price)
        remaining -= take
        if remaining == 0:
            break
    return LiquidityRiskResult(
        requested_quantity=Decimal(quantity),
        executable_quantity=executable,
        executable_value=value,
        unliquidated_quantity=remaining,
        status="FULLY_LIQUID" if remaining == 0 else "UNLIQUID_UNMARKED",
    )
