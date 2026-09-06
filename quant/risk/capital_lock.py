"""Capital-lock cost used separately from execution alpha."""

from __future__ import annotations

from decimal import Decimal


def capital_lock_cost(
    principal: Decimal,
    *,
    locked_seconds: int,
    annual_rate: Decimal,
) -> Decimal:
    seconds_per_year = Decimal("31536000")
    return (
        max(Decimal("0"), principal)
        * max(Decimal("0"), annual_rate)
        * (Decimal(max(0, locked_seconds)) / seconds_per_year)
    )
