"""Void marker for a matched fragment before its compensation entries."""

from __future__ import annotations

from datetime import datetime

from .finality_model import FillFinalityState, FillFragment, FinalityJournalEntry


def void_entry(
    fragment: FillFragment, *, event_id: str, event_ts: datetime, reason: str
) -> FinalityJournalEntry:
    return FinalityJournalEntry(
        event_id=event_id,
        trade_id=fragment.trade_id,
        event_type="FILL_VOIDED",
        state=FillFinalityState.VOIDED,
        event_ts=event_ts,
        reason=reason,
    )
