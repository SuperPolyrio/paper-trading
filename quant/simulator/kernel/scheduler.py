"""Single-threaded deterministic scheduler for causal simulation episodes."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .causal_barrier import CausalBarrier
from .event import SimEvent
from .event_journal import EventJournal
from .simulation_clock import SimulationClock

EventHandler = Callable[[SimEvent], Iterable[SimEvent] | None]


@dataclass(frozen=True)
class SchedulerSnapshot:
    current_ts_ns: int | None
    processed_event_count: int
    queued_event_count: int
    journal_hash: str
    processed_by_type: Mapping[str, int]


@dataclass
class DeterministicScheduler:
    """Schedule and process events without depending on coroutine completion order.

    Handlers run synchronously and may emit descendants only after the parent in
    the fixed causal order. Live adapters remain outside this component and
    translate their input into ``SimEvent`` instances at their boundary.
    """

    clock: SimulationClock = field(default_factory=SimulationClock)
    journal: EventJournal = field(default_factory=EventJournal)
    _queue: list[tuple[tuple[int, int, int, str], str]] = field(default_factory=list, init=False)
    _queued_by_id: dict[str, SimEvent] = field(default_factory=dict, init=False)
    _sort_keys: dict[tuple[int, int, int, str], str] = field(default_factory=dict, init=False)
    _processed_by_id: dict[str, SimEvent] = field(default_factory=dict, init=False)
    _handlers: dict[str, list[EventHandler]] = field(default_factory=dict, init=False)
    _processed_by_type: dict[str, int] = field(default_factory=dict, init=False)
    _current_event: SimEvent | None = field(default=None, init=False)
    _last_processed_key: tuple[int, int, int, str] | None = field(default=None, init=False)

    def register(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(str(event_type).strip().upper(), []).append(handler)

    def schedule(self, event: SimEvent, *, parent: SimEvent | None = None) -> bool:
        """Schedule an initial event or a handler-emitted descendant.

        Repeating an identical event is idempotent. Reusing either an event ID
        or deterministic sort key for a different event is a hard error rather
        than a hidden dependency on insertion order.
        """
        causal_parent = parent or self._current_event
        if causal_parent is not None:
            CausalBarrier.validate(causal_parent, event)
        elif self._last_processed_key is not None and event.sort_key <= self._last_processed_key:
            raise ValueError("cannot schedule an event at or before processed simulator time")

        existing = self._processed_by_id.get(event.event_id) or self._queued_by_id.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ValueError(f"event_id reused for different event: {event.event_id}")
            return False
        other_id = self._sort_keys.get(event.sort_key)
        if other_id is not None:
            other = self._processed_by_id.get(other_id) or self._queued_by_id.get(other_id)
            if other != event:
                raise ValueError(
                    "distinct events share deterministic sort key; provide a stable unique tiebreaker"
                )
            return False
        self._queued_by_id[event.event_id] = event
        self._sort_keys[event.sort_key] = event.event_id
        heapq.heappush(self._queue, (event.sort_key, event.event_id))
        return True

    def run(self, *, max_events: int | None = None) -> tuple[SimEvent, ...]:
        processed = 0
        while self._queue and (max_events is None or processed < max_events):
            _, event_id = heapq.heappop(self._queue)
            event = self._queued_by_id.pop(event_id)
            self.clock.advance_to(event.event_ts_ns)
            self._current_event = event
            self.journal.append(event)
            self._processed_by_id[event.event_id] = event
            self._last_processed_key = event.sort_key
            self._processed_by_type[event.event_type] = self._processed_by_type.get(event.event_type, 0) + 1
            try:
                for handler in self._handlers.get(event.event_type, ()):
                    for emitted in handler(event) or ():
                        self.schedule(emitted, parent=event)
            finally:
                self._current_event = None
            processed += 1
        return self.journal.events

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            current_ts_ns=self.clock.current_ts_ns,
            processed_event_count=self.journal.event_count,
            queued_event_count=len(self._queue),
            journal_hash=self.journal.journal_hash,
            processed_by_type=dict(sorted(self._processed_by_type.items())),
        )

    @property
    def pending_events(self) -> tuple[SimEvent, ...]:
        return tuple(
            self._queued_by_id[event_id]
            for _, event_id in sorted(self._queue)
            if event_id in self._queued_by_id
        )
