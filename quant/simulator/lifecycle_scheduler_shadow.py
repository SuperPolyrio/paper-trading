"""Non-enforcing deterministic comparison for completed paper lifecycles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from .kernel.deterministic_id import deterministic_id
from .kernel.event import SimEvent
from .kernel.event_journal import EventJournal
from .paper_episode import load_paper_lifecycle_events
from .shadow_worker import SchedulerOwnedShadowWorker


class LifecycleShadowArtifactSink(Protocol):
    def persist_event(self, event: SimEvent, *, run_id: str) -> bool: ...


@dataclass(frozen=True)
class LifecycleSchedulerShadowResult:
    audit_key: str
    run_id: str
    source_event_count: int
    scheduler_event_count: int
    source_journal_hash: str
    scheduler_journal_hash: str
    agreement: bool
    comparison_event_persisted: bool
    live_submission_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class PaperLifecycleSchedulerShadow:
    """Replay an already-recorded paper lifecycle without controlling execution."""

    MODEL_VERSION = "paper-lifecycle-scheduler-shadow-v1"

    def __init__(
        self,
        *,
        artifact_sink: LifecycleShadowArtifactSink,
        run_prefix: str,
        loader=load_paper_lifecycle_events,
    ) -> None:
        if not str(run_prefix).strip():
            raise ValueError("run_prefix is required")
        self.artifact_sink = artifact_sink
        self.run_prefix = str(run_prefix)
        self.loader = loader
        self._results: dict[str, LifecycleSchedulerShadowResult] = {}

    def observe(self, audit_key: str) -> LifecycleSchedulerShadowResult:
        key = str(audit_key)
        if not key.strip():
            raise ValueError("audit_key is required")
        cached = self._results.get(key)
        if cached is not None:
            return cached
        loaded = self.loader(key)
        if loaded is None:
            raise ValueError(f"paper lifecycle not found: {key}")
        _audit, events = loaded
        source = EventJournal(events)
        run_id = f"{self.run_prefix}:{deterministic_id('paper-audit', key)}"
        scheduler = SchedulerOwnedShadowWorker(self.artifact_sink).replay_events(
            run_id=run_id,
            events=events,
        )
        agreement = (
            scheduler.event_count == source.event_count
            and scheduler.journal_hash == source.journal_hash
        )
        event_ts_ns = max((event.event_ts_ns for event in events), default=0)
        comparison = SimEvent.build(
            event_type="REPORTING",
            event_ts_ns=event_ts_ns,
            source_sequence=120,
            aggregate_key=f"paper-audit:{key}",
            source_event_id=f"paper-lifecycle-shadow:{key}",
            model_version=self.MODEL_VERSION,
            payload={
                "audit_key": key,
                "source_event_count": source.event_count,
                "scheduler_event_count": scheduler.event_count,
                "source_journal_hash": source.journal_hash,
                "scheduler_journal_hash": scheduler.journal_hash,
                "agreement": agreement,
                "non_enforcing": True,
                "live_submission_performed": False,
            },
        )
        comparison_persisted = self.artifact_sink.persist_event(
            comparison,
            run_id=run_id,
        )
        result = LifecycleSchedulerShadowResult(
            audit_key=key,
            run_id=run_id,
            source_event_count=source.event_count,
            scheduler_event_count=scheduler.event_count,
            source_journal_hash=source.journal_hash,
            scheduler_journal_hash=scheduler.journal_hash,
            agreement=agreement,
            comparison_event_persisted=comparison_persisted,
        )
        self._results[key] = result
        return result
