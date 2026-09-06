"""Scheduler-owned offline bridge for existing simulator gateway lifecycles.

The worker intentionally receives already-offline ``VenueGateway`` evidence.
It provides a safe migration path: all translated lifecycle events cross the
deterministic scheduler and can be persisted as a run artifact before a future
live-paper worker adopts the same command model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .kernel.deterministic_id import stable_hash
from .kernel.event import SimEvent
from .kernel.scheduler import DeterministicScheduler
from .venue.gateway import VenueGateway
from .venue.inflight_queue import InFlightCommand


class ShadowArtifactSink(Protocol):
    def persist_event(self, event: SimEvent, *, run_id: str) -> bool: ...

    def persist_inflight(self, item: InFlightCommand, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class ShadowWorkerResult:
    run_id: str
    journal_hash: str
    result_hash: str
    event_count: int
    journal_event_count: int
    command_count: int
    persisted_event_count: int
    live_submission_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class SchedulerOwnedShadowWorker:
    """Replay offline events through one deterministic journal and optional sink."""

    def __init__(self, artifact_sink: ShadowArtifactSink | None = None) -> None:
        self.artifact_sink = artifact_sink

    def replay_events(self, *, run_id: str, events: Iterable[SimEvent]) -> ShadowWorkerResult:
        scheduler = DeterministicScheduler()
        for event in sorted(tuple(events), key=lambda row: row.sort_key):
            scheduler.schedule(event)
        processed = scheduler.run()
        persisted = 0
        if self.artifact_sink is not None:
            for event in processed:
                persisted += int(self.artifact_sink.persist_event(event, run_id=str(run_id)))
        snapshot = scheduler.snapshot()
        result_hash = stable_hash(
            {
                "run_id": str(run_id),
                "journal_hash": snapshot.journal_hash,
                "event_count": snapshot.processed_event_count,
                "processed_by_type": dict(snapshot.processed_by_type),
            }
        )
        return ShadowWorkerResult(
            run_id=str(run_id),
            journal_hash=snapshot.journal_hash,
            result_hash=result_hash,
            event_count=snapshot.processed_event_count,
            journal_event_count=snapshot.processed_event_count,
            command_count=0,
            persisted_event_count=persisted,
        )

    def replay_gateway(self, *, run_id: str, gateway: VenueGateway) -> ShadowWorkerResult:
        """Persist gateway command state, then replay its lifecycle through the kernel."""

        if self.artifact_sink is not None:
            for item in gateway.commands:
                self.artifact_sink.persist_inflight(item)
        base = self.replay_events(run_id=run_id, events=gateway.lifecycle_sim_events())
        return ShadowWorkerResult(
            run_id=base.run_id,
            journal_hash=base.journal_hash,
            result_hash=base.result_hash,
            event_count=base.event_count,
            journal_event_count=base.journal_event_count,
            command_count=len(gateway.commands),
            persisted_event_count=base.persisted_event_count,
        )
