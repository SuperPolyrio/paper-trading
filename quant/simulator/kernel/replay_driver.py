"""Episode replay and repeat-determinism verification for the simulation kernel."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from typing import Any

from .deterministic_id import stable_hash
from .event import SimEvent
from .scheduler import DeterministicScheduler, EventHandler

HandlerMap = Mapping[str, Iterable[EventHandler]]
StateSnapshot = Callable[[DeterministicScheduler], Mapping[str, Any] | None]


def replay_events(
    events: Iterable[SimEvent],
    *,
    handlers: HandlerMap | None = None,
    state_snapshot: StateSnapshot | None = None,
) -> dict[str, Any]:
    """Replay initial events and return a canonical event-journal result."""
    scheduler = DeterministicScheduler()
    for event_type, callbacks in (handlers or {}).items():
        for callback in callbacks:
            scheduler.register(event_type, callback)
    for event in sorted(events, key=lambda item: item.sort_key):
        scheduler.schedule(event)
    scheduler.run()
    snapshot = scheduler.snapshot()
    state = dict(state_snapshot(scheduler) or {}) if state_snapshot else {}
    result = {
        "journal_hash": snapshot.journal_hash,
        "event_count": snapshot.processed_event_count,
        "current_ts_ns": snapshot.current_ts_ns,
        "processed_by_type": dict(snapshot.processed_by_type),
        "events": [event.to_dict() for event in scheduler.journal.events],
        "state": state,
    }
    result["result_hash"] = stable_hash(result)
    return result


def verify_repeat_determinism(
    events: Iterable[SimEvent],
    *,
    runs: int = 100,
    handlers_factory: Callable[[], HandlerMap] | None = None,
    state_snapshot_factory: Callable[[], StateSnapshot | None] | None = None,
) -> dict[str, Any]:
    """Run the same episode repeatedly and compare its complete result hash."""
    materialized_events = tuple(events)
    if int(runs) < 2:
        raise ValueError("runs must be at least 2")
    results: list[dict[str, Any]] = []
    for _ in range(int(runs)):
        results.append(
            replay_events(
                materialized_events,
                handlers=handlers_factory() if handlers_factory else None,
                state_snapshot=state_snapshot_factory() if state_snapshot_factory else None,
            )
        )
    baseline = results[0]
    mismatches = [
        {
            "run": index + 1,
            "result_hash": result["result_hash"],
            "journal_hash": result["journal_hash"],
        }
        for index, result in enumerate(results[1:], 1)
        if result["result_hash"] != baseline["result_hash"]
    ]
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "runs": int(runs),
        "result_hash": baseline["result_hash"],
        "journal_hash": baseline["journal_hash"],
        "event_count": baseline["event_count"],
        "mismatches": mismatches,
        "snapshot": asdict_result(baseline),
        "live_submission_performed": False,
    }


def asdict_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Small human-readable summary; full events stay in replay artifacts."""
    return {
        "event_count": result["event_count"],
        "current_ts_ns": result["current_ts_ns"],
        "processed_by_type": result["processed_by_type"],
        "state": result["state"],
    }
