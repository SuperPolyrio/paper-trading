"""Append-only state and journal contracts for paper fill economic finality."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class FillFinalityState(str, Enum):
    MATCHED_PROVISIONAL = "MATCHED_PROVISIONAL"
    RETRYING = "RETRYING"
    CONFIRMED_FINAL = "CONFIRMED_FINAL"
    FAILED_FINAL = "FAILED_FINAL"
    VOIDED = "VOIDED"
    REVERSAL_APPLIED = "REVERSAL_APPLIED"


@dataclass(frozen=True)
class FillFragment:
    trade_id: str
    account_id: str
    strategy_id: str
    asset_id: str
    side: str
    size: Decimal
    price: Decimal
    fee: Decimal
    matched_at: datetime

    def __post_init__(self) -> None:
        for name in ("trade_id", "account_id", "strategy_id", "asset_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        side = str(self.side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("fill fragment side must be BUY or SELL")
        size, price, fee = Decimal(self.size), Decimal(self.price), Decimal(self.fee)
        if size <= 0 or not Decimal(0) < price < Decimal(1) or fee < 0:
            raise ValueError("fill fragment economics are invalid")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fee", fee)

    @property
    def signed_shares(self) -> Decimal:
        return self.size if self.side == "BUY" else -self.size

    @property
    def signed_cash(self) -> Decimal:
        notional = self.size * self.price
        return -notional - self.fee if self.side == "BUY" else notional - self.fee


@dataclass(frozen=True)
class FinalityJournalEntry:
    event_id: str
    trade_id: str
    event_type: str
    state: FillFinalityState
    event_ts: datetime
    cash_delta: Decimal = Decimal(0)
    shares_delta: Decimal = Decimal(0)
    fee_delta: Decimal = Decimal(0)
    reason: str = ""


@dataclass(frozen=True)
class FinalityTrade:
    fragment: FillFragment
    state: FillFinalityState
    finality_ts: datetime | None = None


@dataclass(frozen=True)
class FinalityReconciliationCandidate:
    trade_id: str
    outcome: str
    event_ts: datetime
    reason: str
