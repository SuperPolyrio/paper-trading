"""Deterministic ordering authority for due live-paper intents."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from .kernel.event import SimEvent
from .kernel.scheduler import DeterministicScheduler

T = TypeVar("T")


@dataclass(frozen=True)
class PaperIntentSchedule(Generic[T]):
    ordered_items: tuple[T, ...]
    intent_ids: tuple[int, ...]
    events: tuple[SimEvent, ...]
    journal_hash: str
    event_count: int


class DeterministicPaperIntentScheduler:
    """Order one due set independently of claim or dictionary insertion order."""

    MODEL_VERSION = "paper-worker-intent-scheduler-v1"

    def __init__(self, *, event_namespace: str = "paper-worker") -> None:
        namespace = str(event_namespace).strip()
        if not namespace:
            raise ValueError("paper worker event namespace is required")
        self.event_namespace = namespace

    def order_due(
        self,
        items: Iterable[T],
        *,
        arrival_ts: Callable[[T], datetime],
    ) -> PaperIntentSchedule[T]:
        values = tuple(items)
        by_intent_id: dict[int, T] = {}
        scheduler = DeterministicScheduler()
        for item in values:
            row = item.row
            intent_id = int(row.intent_id)
            if intent_id in by_intent_id:
                raise ValueError(f"duplicate due paper intent_id: {intent_id}")
            intent = row.intent
            event = SimEvent.build(
                event_type="PAPER_COMMAND_ARRIVAL",
                event_ts_ns=_datetime_ns(arrival_ts(item)),
                source_sequence=intent_id,
                aggregate_key=f"paper-intent:{intent_id}",
                source_event_id=(
                    f"paper-worker-due:{self.event_namespace}:{intent_id}"
                ),
                model_version=self.MODEL_VERSION,
                payload={
                    "event_namespace": self.event_namespace,
                    "intent_id": intent_id,
                    "strategy_id": str(intent.strategy_id),
                    "client_order_id": str(intent.client_order_id),
                    "asset_id": str(intent.asset_id),
                    "decision_ts": intent.decision_ts.isoformat(),
                },
            )
            by_intent_id[intent_id] = item
            scheduler.schedule(event)
        processed = scheduler.run()
        intent_ids = tuple(int(event.source_sequence) for event in processed)
        if set(intent_ids) != set(by_intent_id):
            raise RuntimeError("deterministic scheduler lost a due paper intent")
        snapshot = scheduler.snapshot()
        return PaperIntentSchedule(
            ordered_items=tuple(by_intent_id[intent_id] for intent_id in intent_ids),
            intent_ids=intent_ids,
            events=processed,
            journal_hash=snapshot.journal_hash,
            event_count=snapshot.processed_event_count,
        )


def _datetime_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("paper intent arrival timestamp must be timezone-aware")
    seconds = int(value.timestamp())
    return seconds * 1_000_000_000 + value.microsecond * 1_000
