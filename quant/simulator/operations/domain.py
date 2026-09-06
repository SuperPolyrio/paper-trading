"""Stateful position-operation contracts kept separate from CLOB orders."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import Enum


class PositionOperationType(str, Enum):
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    REDEEM = "REDEEM"
    NEG_RISK_CONVERT = "NEG_RISK_CONVERT"


class PositionOperationState(str, Enum):
    CREATED = "CREATED"
    ALLOWANCE_CHECKED = "ALLOWANCE_CHECKED"
    NONCE_RESERVED = "NONCE_RESERVED"
    SUBMITTED = "SUBMITTED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    RECONCILED = "RECONCILED"


@dataclass(frozen=True)
class PositionOperationIntent:
    event_id: str
    operation_type: PositionOperationType
    account_id: str
    strategy_id: str
    condition_id: str
    amount: Decimal
    decision_ts: datetime
    collateral_delta: Decimal
    token_deltas: Mapping[str, Decimal] = field(default_factory=dict)
    token_decimals: int = 6
    market_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "account_id", "strategy_id", "condition_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        amount = Decimal(self.amount)
        if amount <= 0:
            raise ValueError("operation amount must be positive")
        if int(self.token_decimals) < 0 or int(self.token_decimals) > 18:
            raise ValueError("token decimals must be between 0 and 18")
        deltas = {
            str(asset): _round_down(Decimal(value), int(self.token_decimals))
            for asset, value in self.token_deltas.items()
        }
        if not deltas:
            raise ValueError("operation requires explicit token deltas")
        if any(value == 0 for value in deltas.values()):
            raise ValueError("operation token deltas must be non-zero")
        collateral = _round_down(
            Decimal(self.collateral_delta), int(self.token_decimals)
        )
        positive = any(value > 0 for value in deltas.values())
        negative = any(value < 0 for value in deltas.values())
        if self.operation_type is PositionOperationType.SPLIT and not (
            collateral < 0 and positive and not negative
        ):
            raise ValueError("split requires collateral debit and token credits")
        if self.operation_type is PositionOperationType.MERGE and not (
            collateral > 0 and negative and not positive
        ):
            raise ValueError("merge requires token debits and collateral credit")
        if self.operation_type is PositionOperationType.REDEEM and not (
            collateral >= 0 and negative and not positive
        ):
            raise ValueError("redeem requires token debits and non-negative payout")
        if self.operation_type is PositionOperationType.NEG_RISK_CONVERT and not (
            positive and negative
        ):
            raise ValueError(
                "negative-risk conversion requires token debits and credits"
            )
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "collateral_delta", collateral)
        object.__setattr__(self, "token_deltas", deltas)
        object.__setattr__(
            self,
            "market_id",
            str(self.market_id) if self.market_id else str(self.condition_id),
        )


def _round_down(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
