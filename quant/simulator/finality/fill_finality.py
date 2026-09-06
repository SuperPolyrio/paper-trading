"""Idempotent fragment finality machine with append-only compensation journal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .fill_void import void_entry
from .finality_model import (
    FillFinalityState,
    FillFragment,
    FinalityJournalEntry,
    FinalityTrade,
)
from .ledger_compensation import compensation_entries
from .position_rebuild import FinalityNav, rebuild_nav


class FillFinalityLedger:
    def __init__(self) -> None:
        self._trades: dict[str, FinalityTrade] = {}
        self._journal: dict[str, FinalityJournalEntry] = {}

    def record_match(self, fragment: FillFragment, *, event_id: str) -> FinalityTrade:
        existing = self._trades.get(fragment.trade_id)
        if existing is not None:
            if existing.fragment != fragment:
                raise ValueError("trade id collision with different fragment")
            return existing
        trade = FinalityTrade(fragment, FillFinalityState.MATCHED_PROVISIONAL)
        self._trades[fragment.trade_id] = trade
        self._append(
            FinalityJournalEntry(
                event_id=event_id,
                trade_id=fragment.trade_id,
                event_type="PROVISIONAL_FILL",
                state=FillFinalityState.MATCHED_PROVISIONAL,
                event_ts=fragment.matched_at,
                cash_delta=fragment.signed_cash,
                shares_delta=fragment.signed_shares,
                fee_delta=fragment.fee,
                reason="venue_match_not_economic_finality",
            )
        )
        return trade

    def mark_retrying(
        self, trade_id: str, *, event_id: str, event_ts: datetime
    ) -> FinalityTrade:
        trade = self._require_retryable(trade_id)
        if trade.state is FillFinalityState.RETRYING:
            return trade
        updated = FinalityTrade(trade.fragment, FillFinalityState.RETRYING, event_ts)
        self._trades[trade_id] = updated
        self._append(
            FinalityJournalEntry(
                event_id,
                trade_id,
                "FINALITY_RETRYING",
                FillFinalityState.RETRYING,
                event_ts,
                reason="venue_confirmation_retry",
            )
        )
        return updated

    def confirm(
        self, trade_id: str, *, event_id: str, event_ts: datetime
    ) -> FinalityTrade:
        trade = self._require_confirmable(trade_id)
        if trade.state is FillFinalityState.CONFIRMED_FINAL:
            return trade
        updated = FinalityTrade(
            trade.fragment, FillFinalityState.CONFIRMED_FINAL, event_ts
        )
        self._trades[trade_id] = updated
        self._append(
            FinalityJournalEntry(
                event_id,
                trade_id,
                "FILL_CONFIRMED",
                FillFinalityState.CONFIRMED_FINAL,
                event_ts,
                reason="economic_finality_confirmed",
            )
        )
        return updated

    def fail_and_void(
        self, trade_id: str, *, event_id: str, event_ts: datetime, reason: str
    ) -> FinalityTrade:
        trade = self._require_voidable(trade_id)
        if trade.state is FillFinalityState.REVERSAL_APPLIED:
            return trade
        self._append(
            FinalityJournalEntry(
                event_id=f"{event_id}:failed",
                trade_id=trade_id,
                event_type="FINALITY_FAILED",
                state=FillFinalityState.FAILED_FINAL,
                event_ts=event_ts,
                reason=reason,
            )
        )
        self._append(
            void_entry(
                trade.fragment,
                event_id=f"{event_id}:void",
                event_ts=event_ts,
                reason=reason,
            )
        )
        for entry in compensation_entries(
            trade.fragment, event_prefix=f"{event_id}:reversal", event_ts=event_ts
        ):
            self._append(entry)
        updated = FinalityTrade(
            trade.fragment, FillFinalityState.REVERSAL_APPLIED, event_ts
        )
        self._trades[trade_id] = updated
        return updated

    def provisional_nav(self) -> FinalityNav:
        return rebuild_nav(self._trades.values(), confirmed_only=False)

    def confirmed_nav(self) -> FinalityNav:
        return rebuild_nav(self._trades.values(), confirmed_only=True)

    def trade(self, trade_id: str) -> FinalityTrade | None:
        return self._trades.get(trade_id)

    def journal(self) -> tuple[FinalityJournalEntry, ...]:
        return tuple(self._journal.values())

    def snapshot(self) -> dict[str, Any]:
        return {"trades": tuple(self._trades.values()), "journal": self.journal()}

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> FillFinalityLedger:
        ledger = cls()
        ledger._trades = {
            trade.fragment.trade_id: trade for trade in snapshot.get("trades", ())
        }
        ledger._journal = {
            entry.event_id: entry for entry in snapshot.get("journal", ())
        }
        return ledger

    def _require_retryable(self, trade_id: str) -> FinalityTrade:
        trade = self._trades.get(trade_id)
        if trade is None:
            raise KeyError(f"unknown finality trade: {trade_id}")
        if trade.state in {
            FillFinalityState.MATCHED_PROVISIONAL,
            FillFinalityState.RETRYING,
        }:
            return trade
        raise ValueError(f"trade is not retryable: {trade.state.value}")

    def _require_confirmable(self, trade_id: str) -> FinalityTrade:
        trade = self._trades.get(trade_id)
        if trade is None:
            raise KeyError(f"unknown finality trade: {trade_id}")
        if trade.state in {
            FillFinalityState.MATCHED_PROVISIONAL,
            FillFinalityState.RETRYING,
            FillFinalityState.CONFIRMED_FINAL,
        }:
            return trade
        raise ValueError(f"trade is not confirmable: {trade.state.value}")

    def _require_voidable(self, trade_id: str) -> FinalityTrade:
        trade = self._trades.get(trade_id)
        if trade is None:
            raise KeyError(f"unknown finality trade: {trade_id}")
        if trade.state in {
            FillFinalityState.MATCHED_PROVISIONAL,
            FillFinalityState.RETRYING,
            FillFinalityState.CONFIRMED_FINAL,
            FillFinalityState.REVERSAL_APPLIED,
        }:
            return trade
        raise ValueError(f"trade is not voidable: {trade.state.value}")

    def _append(self, entry: FinalityJournalEntry) -> None:
        existing = self._journal.get(entry.event_id)
        if existing is not None and existing != entry:
            raise ValueError("finality journal event id collision")
        self._journal[entry.event_id] = entry
