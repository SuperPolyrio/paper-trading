"""Small deterministic causal event loop for execution-model tests and replay."""

from __future__ import annotations

import heapq
from typing import Callable

from .event_ordering import OrderedExecutionEvent

EventHandler = Callable[[OrderedExecutionEvent], list[OrderedExecutionEvent] | None]


class CausalExecutionKernel:
    def __init__(self) -> None:
        self._queue: list[OrderedExecutionEvent] = []
        self._handlers: dict[str, list[EventHandler]] = {}
        self.processed: list[OrderedExecutionEvent] = []

    def register(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(str(event_type).upper(), []).append(handler)

    def schedule(self, event: OrderedExecutionEvent) -> None:
        heapq.heappush(self._queue, event)

    def run(self) -> list[OrderedExecutionEvent]:
        while self._queue:
            event = heapq.heappop(self._queue)
            self.processed.append(event)
            for handler in self._handlers.get(event.event_type, ()):
                for emitted in handler(event) or ():
                    if emitted.event_ts < event.event_ts:
                        raise ValueError("handler emitted an event into the past")
                    self.schedule(emitted)
        return list(self.processed)
