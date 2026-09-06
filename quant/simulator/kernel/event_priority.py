"""Stable priority classes for causal simulator events."""

from __future__ import annotations

from enum import IntEnum


class EventPriority(IntEnum):
    """Lower values are processed first when timestamps are equal."""

    VENUE_STATE = 10
    MARKET_DATA = 20
    EXTERNAL_TRADE = 30
    REAL_ORDER_LIFECYCLE = 40
    PAPER_COMMAND_ARRIVAL = 50
    PAPER_VENUE_ACTION = 60
    POSITION_OPERATION = 70
    STRATEGY_OBSERVATION = 80
    STRATEGY_INTENT = 90
    ACCOUNTING = 100


EVENT_PRIORITIES: dict[str, EventPriority] = {
    "VENUE_STATE_CHANGED": EventPriority.VENUE_STATE,
    "MARKET_TRADING_STATUS": EventPriority.VENUE_STATE,
    "MARKET_CLOSED": EventPriority.VENUE_STATE,
    "BOOK_SNAPSHOT": EventPriority.MARKET_DATA,
    "BOOK_DELTA": EventPriority.MARKET_DATA,
    "MARKET_DATA_BATCH": EventPriority.MARKET_DATA,
    "TICK_SIZE_CHANGE": EventPriority.MARKET_DATA,
    "EXTERNAL_TRADE": EventPriority.EXTERNAL_TRADE,
    "ORDERFILLED_EVIDENCE": EventPriority.EXTERNAL_TRADE,
    "REAL_ORDER_LIFECYCLE": EventPriority.REAL_ORDER_LIFECYCLE,
    "FILL_FINALITY": EventPriority.REAL_ORDER_LIFECYCLE,
    "PAPER_COMMAND_ARRIVAL": EventPriority.PAPER_COMMAND_ARRIVAL,
    "VENUE_ADMISSION": EventPriority.PAPER_COMMAND_ARRIVAL,
    "PAPER_MATCH": EventPriority.PAPER_VENUE_ACTION,
    "PAPER_WORKING": EventPriority.PAPER_VENUE_ACTION,
    "PAPER_CANCEL": EventPriority.PAPER_VENUE_ACTION,
    "PAPER_EXPIRE": EventPriority.PAPER_VENUE_ACTION,
    "PAPER_REJECT": EventPriority.PAPER_VENUE_ACTION,
    "POSITION_OPERATION_CONFIRMATION": EventPriority.POSITION_OPERATION,
    "STRATEGY_OBSERVATION": EventPriority.STRATEGY_OBSERVATION,
    "STRATEGY_INTENT": EventPriority.STRATEGY_INTENT,
    "RISK_DECISION": EventPriority.STRATEGY_INTENT,
    "ACCOUNTING_MARK": EventPriority.ACCOUNTING,
    "REPORTING": EventPriority.ACCOUNTING,
}


def priority_for(event_type: str) -> int:
    """Return the contract priority, defaulting unknown event types to reporting."""
    return int(EVENT_PRIORITIES.get(str(event_type).upper(), EventPriority.ACCOUNTING))
