"""Explicit void compensation entries; never model a void as an opposite fill."""

from __future__ import annotations

from datetime import datetime

from .finality_model import FillFinalityState, FillFragment, FinalityJournalEntry


def compensation_entries(
    fragment: FillFragment, *, event_prefix: str, event_ts: datetime
) -> tuple[FinalityJournalEntry, ...]:
    """Return the three append-only entries that exactly offset a provisional fill."""
    return (
        FinalityJournalEntry(
            event_id=f"{event_prefix}:cash",
            trade_id=fragment.trade_id,
            event_type="CASH_REVERSAL",
            state=FillFinalityState.REVERSAL_APPLIED,
            event_ts=event_ts,
            cash_delta=-fragment.signed_cash - fragment.fee,
            reason="void_compensating_cash_reversal",
        ),
        FinalityJournalEntry(
            event_id=f"{event_prefix}:position",
            trade_id=fragment.trade_id,
            event_type="POSITION_REVERSAL",
            state=FillFinalityState.REVERSAL_APPLIED,
            event_ts=event_ts,
            shares_delta=-fragment.signed_shares,
            reason="void_compensating_position_reversal",
        ),
        FinalityJournalEntry(
            event_id=f"{event_prefix}:fee",
            trade_id=fragment.trade_id,
            event_type="FEE_REVERSAL",
            state=FillFinalityState.REVERSAL_APPLIED,
            event_ts=event_ts,
            cash_delta=fragment.fee,
            fee_delta=-fragment.fee,
            reason="void_compensating_fee_reversal",
        ),
    )
