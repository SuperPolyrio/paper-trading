"""Provisional match finality and auditable failed-trade reversal."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ProvisionalTrade:
    trade_id: str
    asset_id: str
    side: str
    size: Decimal
    price: Decimal
    fee: Decimal
    state: str
    matched_at: datetime
    finalized_at: datetime | None = None


@dataclass(frozen=True)
class FinalityJournal:
    journal_key: str
    event_type: str
    cash_delta: Decimal
    shares_delta: Decimal
    inventory_value_delta: Decimal
    receivable_delta: Decimal
    fee_delta: Decimal
    reason: str

    @property
    def trial_balance(self) -> Decimal:
        return (
            self.cash_delta
            + self.inventory_value_delta
            + self.receivable_delta
            + self.fee_delta
        )


class SettlementFinalityModel:
    def provisional(
        self, trade: ProvisionalTrade
    ) -> tuple[ProvisionalTrade, FinalityJournal]:
        if trade.state not in {
            "TRADE_ID_ASSIGNED",
            "MATCHED_NOT_BROADCASTED",
            "MATCHED",
        }:
            raise ValueError(f"cannot provisionally match state {trade.state}")
        notional = trade.size * trade.price
        direction = Decimal("1") if trade.side.upper() == "BUY" else Decimal("-1")
        journal = FinalityJournal(
            journal_key=f"provisional:{trade.trade_id}",
            event_type="PROVISIONAL_MATCH",
            cash_delta=Decimal("0"),
            shares_delta=direction * trade.size,
            inventory_value_delta=direction * notional,
            receivable_delta=-(direction * notional),
            fee_delta=Decimal("0"),
            reason="offchain_match_pending_onchain_confirmation",
        )
        return replace(trade, state="SETTLEMENT_PENDING"), journal

    def confirm(
        self,
        trade: ProvisionalTrade,
        *,
        confirmed_at: datetime,
    ) -> tuple[ProvisionalTrade, FinalityJournal]:
        if trade.state != "SETTLEMENT_PENDING":
            raise ValueError("only pending trade can confirm")
        direction = Decimal("1") if trade.side.upper() == "BUY" else Decimal("-1")
        notional = trade.size * trade.price
        journal = FinalityJournal(
            journal_key=f"confirm:{trade.trade_id}",
            event_type="TRADE_CONFIRMATION",
            cash_delta=-(direction * notional) - trade.fee,
            shares_delta=Decimal("0"),
            inventory_value_delta=Decimal("0"),
            receivable_delta=direction * notional,
            fee_delta=trade.fee,
            reason="onchain_trade_confirmed",
        )
        return replace(trade, state="CONFIRMED", finalized_at=confirmed_at), journal

    def fail_and_reverse(
        self,
        trade: ProvisionalTrade,
        *,
        failed_at: datetime,
    ) -> tuple[ProvisionalTrade, FinalityJournal]:
        if trade.state != "SETTLEMENT_PENDING":
            raise ValueError("only pending trade can fail")
        direction = Decimal("1") if trade.side.upper() == "BUY" else Decimal("-1")
        notional = trade.size * trade.price
        journal = FinalityJournal(
            journal_key=f"reversal:{trade.trade_id}",
            event_type="FAILED_TRADE_REVERSAL",
            cash_delta=Decimal("0"),
            shares_delta=-(direction * trade.size),
            inventory_value_delta=-(direction * notional),
            receivable_delta=direction * notional,
            fee_delta=Decimal("0"),
            reason="matched_trade_failed_before_confirmation",
        )
        return replace(trade, state="FAILED_REVERSED", finalized_at=failed_at), journal

    @staticmethod
    def conservative_value(
        trade: ProvisionalTrade,
        *,
        mark_price: Decimal,
        pending_haircut: Decimal = Decimal("0.5"),
    ) -> Decimal:
        if trade.state == "CONFIRMED":
            return trade.size * mark_price
        if trade.state == "SETTLEMENT_PENDING":
            haircut = min(Decimal("1"), max(Decimal("0"), pending_haircut))
            return trade.size * mark_price * haircut
        return Decimal("0")
