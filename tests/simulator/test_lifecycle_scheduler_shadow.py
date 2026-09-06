from datetime import datetime, timezone

from quant.simulator.kernel.event import SimEvent
from quant.simulator.lifecycle_scheduler_shadow import (
    PaperLifecycleSchedulerShadow,
)


class _Sink:
    def __init__(self) -> None:
        self.rows = {}

    def persist_event(self, event, *, run_id):
        key = (run_id, event.event_id)
        inserted = key not in self.rows
        self.rows[key] = event
        return inserted


def _loader(audit_key):
    event = SimEvent.build(
        event_type="STRATEGY_INTENT",
        event_ts_ns=1,
        source_sequence=1,
        aggregate_key=f"paper:{audit_key}",
        source_event_id=f"source:{audit_key}",
        payload={"audit_key": audit_key},
    )
    return (
        {
            "audit_key": audit_key,
            "created_at": datetime.now(timezone.utc),
        },
        (event,),
    )


def test_scheduler_shadow_is_non_enforcing_deterministic_and_idempotent() -> None:
    sink = _Sink()
    shadow = PaperLifecycleSchedulerShadow(
        artifact_sink=sink,
        run_prefix="paper-worker",
        loader=_loader,
    )

    first = shadow.observe("audit:one")
    replay = shadow.observe("audit:one")

    assert first.agreement is True
    assert first.source_event_count == first.scheduler_event_count == 1
    assert first.live_submission_performed is False
    assert replay == first
    assert len(sink.rows) == 2
