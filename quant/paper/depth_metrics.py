"""Shared visible-depth metrics for paper and calibration probes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


def visible_executable_depth(
    levels: Iterable[Any],
    *,
    side: str,
    amount_unit: str,
    limit_price: Decimal,
) -> Decimal:
    """Return depth executable at ``limit_price`` in the requested amount unit."""

    side_text = str(side).upper()
    unit = str(amount_unit).upper()
    limit = _decimal(limit_price)
    if side_text not in {"BUY", "SELL"} or unit not in {"QUOTE", "SHARES"}:
        return Decimal("0")
    if limit <= 0:
        return Decimal("0")

    depth = Decimal("0")
    for level in levels:
        if isinstance(level, Mapping):
            price = _decimal(level.get("price"))
            size = _decimal(level.get("size"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _decimal(level[0])
            size = _decimal(level[1])
        else:
            continue
        if price <= 0 or size <= 0:
            continue
        if side_text == "BUY" and price > limit:
            continue
        if side_text == "SELL" and price < limit:
            continue
        depth += price * size if unit == "QUOTE" else size
    return depth


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
