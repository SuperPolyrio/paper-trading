"""Virtual strategy attribution that reconciles to one shared account balance."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .position_assignment import PositionAssignment, PositionAssignmentBook


@dataclass(frozen=True)
class AttributedFill:
    fill_id: str
    order_id: str
    account_id: str
    asset_id: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        side = str(self.side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("attributed fill side must be BUY or SELL")
        for name in ("fill_id", "order_id", "account_id", "asset_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        price, size, fee = Decimal(self.price), Decimal(self.size), Decimal(self.fee)
        if price < 0 or size <= 0 or fee < 0:
            raise ValueError("attributed fill has invalid amounts")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "fee", fee)


@dataclass(frozen=True)
class StrategyAttribution:
    account_id: str
    strategy_id: str
    asset_id: str
    quantity: Decimal = Decimal("0")
    cost_basis: Decimal = Decimal("0")
    cash_attribution: Decimal = Decimal("0")
    fee_attribution: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


class StrategySubledger:
    """Tracks attribution only; account-level risk remains the primary gate."""

    def __init__(self, assignments: PositionAssignmentBook | None = None) -> None:
        self.assignments = assignments or PositionAssignmentBook()
        self._buckets: dict[tuple[str, str, str], StrategyAttribution] = {}
        self._applied_fill_ids: set[str] = set()

    def register_order(self, assignment: PositionAssignment) -> PositionAssignment:
        return self.assignments.assign(assignment)

    def apply_fill(self, fill: AttributedFill) -> StrategyAttribution:
        assignment = self.assignments.get(fill.order_id)
        if assignment is None:
            raise ValueError("fill has no strategy assignment")
        if assignment.account_id != fill.account_id or assignment.asset_id != fill.asset_id:
            raise ValueError("fill identity does not match strategy assignment")
        key = (assignment.account_id, assignment.strategy_id, assignment.asset_id)
        current = self._buckets.get(key, StrategyAttribution(*key))
        if fill.fill_id in self._applied_fill_ids:
            return current
        if fill.side == "BUY":
            next_bucket = StrategyAttribution(
                *key,
                quantity=current.quantity + fill.size,
                cost_basis=current.cost_basis + fill.price * fill.size + fill.fee,
                cash_attribution=current.cash_attribution - fill.price * fill.size - fill.fee,
                fee_attribution=current.fee_attribution + fill.fee,
                realized_pnl=current.realized_pnl,
            )
        else:
            if fill.size > current.quantity:
                raise ValueError("strategy attribution cannot sell more than assigned quantity")
            unit_cost = current.cost_basis / current.quantity if current.quantity else Decimal("0")
            cost_released = unit_cost * fill.size
            proceeds = fill.price * fill.size - fill.fee
            next_bucket = StrategyAttribution(
                *key,
                quantity=current.quantity - fill.size,
                cost_basis=current.cost_basis - cost_released,
                cash_attribution=current.cash_attribution + proceeds,
                fee_attribution=current.fee_attribution + fill.fee,
                realized_pnl=current.realized_pnl + proceeds - cost_released,
            )
        self._buckets[key] = next_bucket
        self._applied_fill_ids.add(fill.fill_id)
        return next_bucket

    def position(self, *, account_id: str, strategy_id: str, asset_id: str) -> StrategyAttribution:
        return self._buckets.get((account_id, strategy_id, asset_id), StrategyAttribution(account_id, strategy_id, asset_id))

    def account_position(self, *, account_id: str, asset_id: str) -> Decimal:
        return sum(
            (row.quantity for row in self._buckets.values() if row.account_id == account_id and row.asset_id == asset_id),
            Decimal("0"),
        )

    def reconciles_account_position(self, *, account_id: str, asset_id: str, account_quantity: Decimal) -> bool:
        return self.account_position(account_id=account_id, asset_id=asset_id) == Decimal(account_quantity)
