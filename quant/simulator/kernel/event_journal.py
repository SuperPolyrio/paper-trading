"""Append-only in-memory event journal with stable serialization and hashing."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .deterministic_id import canonical_json, stable_hash
from .event import SimEvent


class EventJournal:
    """Journal processed events in the same order as the simulator clock."""

    def __init__(self, events: Iterable[SimEvent] = ()) -> None:
        self._events: list[SimEvent] = []
        self._by_id: dict[str, SimEvent] = {}
        for event in events:
            self.append(event)

    def append(self, event: SimEvent) -> bool:
        """Append once; a repeated identical event is idempotent."""
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ValueError(f"event_id reused for different event: {event.event_id}")
            return False
        if self._events and event.sort_key <= self._events[-1].sort_key:
            raise ValueError("journal events must be strictly causally ordered")
        self._events.append(event)
        self._by_id[event.event_id] = event
        return True

    @property
    def events(self) -> tuple[SimEvent, ...]:
        return tuple(self._events)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def journal_hash(self) -> str:
        return stable_hash([event.to_dict() for event in self._events])

    def to_jsonl(self) -> str:
        return "".join(canonical_json(event.to_dict()) + "\n" for event in self._events)

    def write_jsonl(self, path: Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_jsonl(), encoding="utf-8")
        return destination

    @classmethod
    def read_jsonl(cls, path: Path) -> "EventJournal":
        import json

        events: list[SimEvent] = []
        for line_number, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not raw_line.strip():
                continue
            try:
                events.append(SimEvent.from_dict(json.loads(raw_line)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid event journal line {line_number}: {exc}") from exc
        return cls(events)
