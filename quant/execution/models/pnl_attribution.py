"""PnL confidence tiers and additive attribution."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal

from quant.execution.domain import PnlTier


@dataclass(frozen=True)
class PnlAttribution:
    signal_alpha: Decimal = Decimal("0")
    spread_crossing_cost: Decimal = Decimal("0")
    book_walk_slippage: Decimal = Decimal("0")
    latency_slippage: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    rebates: Decimal = Decimal("0")
    maker_spread_capture: Decimal = Decimal("0")
    adverse_selection: Decimal = Decimal("0")
    settlement_payout: Decimal = Decimal("0")
    merge_convert_benefit: Decimal = Decimal("0")
    capital_lock_cost: Decimal = Decimal("0")
    execution_failure_reversal: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return sum((getattr(self, field.name) for field in fields(self)), Decimal("0"))


@dataclass(frozen=True)
class TieredPnl:
    tier: PnlTier
    amount: Decimal
    in_domain: bool
    confidence: Decimal
    attribution: PnlAttribution

    def __post_init__(self) -> None:
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be within [0, 1]")
