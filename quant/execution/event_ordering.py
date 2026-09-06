"""Deterministic event ordering used by online shadow and offline replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

EVENT_PRIORITY = {
    "BOOK": 10,
    "PRICE_CHANGE": 20,
    "TRADE": 30,
    "ORDER_ARRIVAL": 40,
    "CANCEL_ARRIVAL": 45,
    "EXPIRE": 50,
    "ORDER_EVENT": 60,
    "ACCOUNT_EVENT": 70,
    "STRATEGY": 80,
}


@dataclass(frozen=True, order=True)
class OrderedExecutionEvent:
    event_ts: datetime
    event_priority: int
    source_sequence: int
    ingest_id: str
    event_type: str = field(compare=False)
    payload: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.event_ts.tzinfo is None:
            object.__setattr__(
                self, "event_ts", self.event_ts.replace(tzinfo=timezone.utc)
            )

    @classmethod
    def build(
        cls,
        *,
        event_ts: datetime,
        event_type: str,
        source_sequence: int,
        ingest_id: str,
        payload: Mapping[str, Any] | None = None,
        event_priority: int | None = None,
    ) -> "OrderedExecutionEvent":
        kind = str(event_type).upper()
        priority = (
            EVENT_PRIORITY.get(kind, 100)
            if event_priority is None
            else int(event_priority)
        )
        return cls(
            event_ts=event_ts,
            event_priority=priority,
            source_sequence=int(source_sequence),
            ingest_id=str(ingest_id),
            event_type=kind,
            payload=dict(payload or {}),
        )


def event_sort_key(event: OrderedExecutionEvent) -> tuple[datetime, int, int, str]:
    return (
        event.event_ts,
        event.event_priority,
        event.source_sequence,
        event.ingest_id,
    )


def deterministic_order(
    events: Iterable[OrderedExecutionEvent],
) -> list[OrderedExecutionEvent]:
    return sorted(events, key=event_sort_key)
