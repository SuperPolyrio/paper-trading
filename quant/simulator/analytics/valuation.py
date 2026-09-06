"""Multi-view NAV that refuses to price uncovered depth as executable liquidity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Iterable

from quant.risk.event_risk.liquidity_risk import (
    ExitLevel,
    LiquidityRiskResult,
    walk_exit_book,
)


@dataclass(frozen=True)
class NavPositionInput:
    asset_id: str
    quantity: Decimal
    mid_mark: Decimal | None
    exit_levels: tuple[ExitLevel, ...]
    accounting_value: Decimal = Decimal("0")
    expected_payout: Decimal | None = None
    worst_case_payout: Decimal | None = None
    provisional_value: Decimal | None = None
    confirmed_value: Decimal | None = None


@dataclass(frozen=True)
class NavSnapshot:
    accounting_nav: Decimal
    mid_mark_nav: Decimal | None
    bbo_liquidation_nav: Decimal | None
    walk_book_liquidation_nav: Decimal
    resolution_expected_nav: Decimal | None
    worst_case_resolution_nav: Decimal | None
    provisional_nav: Decimal | None
    confirmed_nav: Decimal | None
    unliquidated_quantity: Decimal
    mark_status: str
    valuation_model_version: str = "paper-multiview-nav-v1"

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


def build_nav_snapshot(
    *, accounting_cash: Decimal, positions: Iterable[NavPositionInput]
) -> NavSnapshot:
    rows = tuple(positions)
    mid_values = _values(rows, "mid_mark")
    expected_values = _values(rows, "expected_payout")
    worst_values = _values(rows, "worst_case_payout")
    provisional_values = _raw_values(rows, "provisional_value")
    confirmed_values = _raw_values(rows, "confirmed_value")
    exits: list[LiquidityRiskResult] = [
        walk_exit_book(row.quantity, row.exit_levels) for row in rows
    ]
    walk_value = sum((result.executable_value for result in exits), Decimal("0"))
    unliquidated = sum((result.unliquidated_quantity for result in exits), Decimal("0"))
    bbo_values: list[Decimal] = []
    bbo_complete = True
    for row, result in zip(rows, exits, strict=True):
        if result.unliquidated_quantity > 0 or not row.exit_levels:
            bbo_complete = False
            continue
        bbo_values.append(row.quantity * row.exit_levels[0].price)
    cash = Decimal(accounting_cash)
    return NavSnapshot(
        accounting_nav=cash
        + sum((Decimal(row.accounting_value) for row in rows), Decimal("0")),
        mid_mark_nav=None if mid_values is None else cash + mid_values,
        bbo_liquidation_nav=None
        if not bbo_complete
        else cash + sum(bbo_values, Decimal("0")),
        walk_book_liquidation_nav=cash + walk_value,
        resolution_expected_nav=None
        if expected_values is None
        else cash + expected_values,
        worst_case_resolution_nav=None if worst_values is None else cash + worst_values,
        provisional_nav=None
        if provisional_values is None
        else cash + provisional_values,
        confirmed_nav=None if confirmed_values is None else cash + confirmed_values,
        unliquidated_quantity=unliquidated,
        mark_status="UNLIQUID_UNMARKED" if unliquidated > 0 else "FULLY_EXECUTABLE",
    )


def _values(rows: tuple[NavPositionInput, ...], attribute: str) -> Decimal | None:
    values = []
    for row in rows:
        value = getattr(row, attribute)
        if value is None:
            return None
        values.append(Decimal(row.quantity) * Decimal(value))
    return sum(values, Decimal("0"))


def _raw_values(rows: tuple[NavPositionInput, ...], attribute: str) -> Decimal | None:
    values = [getattr(row, attribute) for row in rows]
    if any(value is None for value in values):
        return None
    return sum((Decimal(value) for value in values), Decimal("0"))


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
