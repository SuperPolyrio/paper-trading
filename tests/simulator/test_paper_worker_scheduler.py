from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quant.simulator.paper_worker_scheduler import (
    DeterministicPaperIntentScheduler,
)

NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Pending:
    row: object
    arrival: datetime


def _pending(intent_id: int, *, arrival: datetime) -> _Pending:
    return _Pending(
        row=SimpleNamespace(
            intent_id=intent_id,
            intent=SimpleNamespace(
                strategy_id="strategy",
                client_order_id=f"client-{intent_id}",
                asset_id="asset",
                decision_ts=NOW,
            ),
        ),
        arrival=arrival,
    )


def test_due_order_is_independent_of_input_insertion_order() -> None:
    scheduler = DeterministicPaperIntentScheduler()
    first = _pending(10, arrival=NOW)
    second = _pending(20, arrival=NOW + timedelta(milliseconds=1))

    forward = scheduler.order_due((first, second), arrival_ts=lambda item: item.arrival)
    reverse = scheduler.order_due((second, first), arrival_ts=lambda item: item.arrival)

    assert forward.intent_ids == (10, 20)
    assert reverse.intent_ids == forward.intent_ids
    assert reverse.journal_hash == forward.journal_hash
    assert tuple(event.source_sequence for event in forward.events) == (10, 20)
    assert forward.event_count == len(forward.events)


def test_intent_id_breaks_equal_arrival_ties_deterministically() -> None:
    schedule = DeterministicPaperIntentScheduler().order_due(
        (_pending(20, arrival=NOW), _pending(10, arrival=NOW)),
        arrival_ts=lambda item: item.arrival,
    )

    assert schedule.intent_ids == (10, 20)


def test_artifact_identity_is_stable_within_run_and_isolated_across_runs() -> None:
    item = _pending(10, arrival=NOW)

    first = DeterministicPaperIntentScheduler(
        event_namespace="paper-run-one"
    ).order_due((item,), arrival_ts=lambda row: row.arrival)
    replay = DeterministicPaperIntentScheduler(
        event_namespace="paper-run-one"
    ).order_due((item,), arrival_ts=lambda row: row.arrival)
    recovered = DeterministicPaperIntentScheduler(
        event_namespace="paper-run-two"
    ).order_due((item,), arrival_ts=lambda row: row.arrival)

    assert replay.events[0].event_id == first.events[0].event_id
    assert replay.journal_hash == first.journal_hash
    assert recovered.events[0].event_id != first.events[0].event_id
    assert recovered.events[0].payload["event_namespace"] == "paper-run-two"


def test_duplicate_intent_is_rejected() -> None:
    item = _pending(10, arrival=NOW)

    with pytest.raises(ValueError, match="duplicate due paper intent_id"):
        DeterministicPaperIntentScheduler().order_due(
            (item, item),
            arrival_ts=lambda row: row.arrival,
        )


def test_naive_arrival_timestamp_is_rejected() -> None:
    item = _pending(10, arrival=NOW.replace(tzinfo=None))

    with pytest.raises(ValueError, match="timezone-aware"):
        DeterministicPaperIntentScheduler().order_due(
            (item,),
            arrival_ts=lambda row: row.arrival,
        )
