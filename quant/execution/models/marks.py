"""Research, liquidation, and conservative mark hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class MarkQuality(str, Enum):
    TWO_SIDED_MID = "TWO_SIDED_MID"
    LIQUIDATION_BID_ASK = "LIQUIDATION_BID_ASK"
    LAST_TRADE = "LAST_TRADE"
    ONE_SIDED_BOUND = "ONE_SIDED_BOUND"
    MODEL_FAIR_VALUE = "MODEL_FAIR_VALUE"
    SETTLEMENT_PAYOUT = "SETTLEMENT_PAYOUT"
    STALE_UNMARKABLE = "STALE_UNMARKABLE"


@dataclass(frozen=True)
class MarkInput:
    as_of: datetime
    side: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    last_trade: Decimal | None = None
    model_fair_value: Decimal | None = None
    settlement_payout: Decimal | None = None
    source_event_at: datetime | None = None
    stale_after_ms: int = 30_000


@dataclass(frozen=True)
class MarkResult:
    research_mark: Decimal | None
    liquidation_mark: Decimal | None
    conservative_mark: Decimal | None
    mark_quality: MarkQuality
    mark_age_ms: int | None


def build_marks(source: MarkInput) -> MarkResult:
    age = (
        max(0, int((source.as_of - source.source_event_at).total_seconds() * 1000))
        if source.source_event_at
        else None
    )
    if source.settlement_payout is not None:
        value = _bound(source.settlement_payout)
        return MarkResult(value, value, value, MarkQuality.SETTLEMENT_PAYOUT, age)
    if age is not None and age > source.stale_after_ms:
        return MarkResult(None, None, None, MarkQuality.STALE_UNMARKABLE, age)
    if source.best_bid is not None and source.best_ask is not None:
        mid = _bound((source.best_bid + source.best_ask) / 2)
        liquidation = _bound(
            source.best_bid if source.side.upper() == "LONG" else source.best_ask
        )
        conservative = (
            min(mid, liquidation)
            if source.side.upper() == "LONG"
            else max(mid, liquidation)
        )
        return MarkResult(
            mid, liquidation, conservative, MarkQuality.TWO_SIDED_MID, age
        )
    one_sided = source.best_bid if source.best_bid is not None else source.best_ask
    if one_sided is not None:
        value = _bound(one_sided)
        conservative = value if source.best_bid is not None else Decimal("0")
        return MarkResult(value, value, conservative, MarkQuality.ONE_SIDED_BOUND, age)
    if source.last_trade is not None:
        value = _bound(source.last_trade)
        return MarkResult(value, value, value, MarkQuality.LAST_TRADE, age)
    if source.model_fair_value is not None:
        value = _bound(source.model_fair_value)
        return MarkResult(value, None, value, MarkQuality.MODEL_FAIR_VALUE, age)
    return MarkResult(None, None, None, MarkQuality.STALE_UNMARKABLE, age)


def _bound(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))
