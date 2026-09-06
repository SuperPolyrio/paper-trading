from datetime import datetime, timedelta, timezone

from quant.simulator.paper_episode import lifecycle_rows_to_sim_events


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _audit() -> dict[str, object]:
    return {
        "audit_key": "audit-content-hash",
        "strategy_id": "paper-test",
        "client_order_id": "client-1",
        "asset_id": "asset-1",
        "decision_ts": NOW,
        "arrival_ts": NOW + timedelta(milliseconds=10),
        "created_at": NOW + timedelta(milliseconds=20),
        "status": "FILLED",
        "reason": "arrival_book_walk_complete",
    }


def _row(
    event_type: str,
    event_ts: datetime,
    *,
    idempotency_key: str,
    from_state: str | None = None,
    to_state: str | None = None,
) -> dict[str, object]:
    return {
        "idempotency_key": idempotency_key,
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state or event_type,
        "reason": "recorded",
        "checkpoint_id": "checkpoint-1",
        "event_ts": event_ts,
    }


def test_lifecycle_adapter_uses_causal_event_types_and_business_tiebreakers() -> None:
    arrival = NOW + timedelta(milliseconds=10)
    rows = (
        _row("MATCHED_PROVISIONAL", arrival, idempotency_key="paper-order:955:matched"),
        _row("CREATED", NOW, idempotency_key="paper-order:955:created"),
        _row("VENUE_ACCEPTED", arrival, idempotency_key="paper-order:955:accepted"),
        _row("CONFIRMED", arrival, idempotency_key="paper-order:955:confirmed"),
    )

    events = lifecycle_rows_to_sim_events(_audit(), rows)

    assert [event.event_type for event in events] == [
        "STRATEGY_INTENT",
        "PAPER_COMMAND_ARRIVAL",
        "PAPER_MATCH",
        "POSITION_OPERATION_CONFIRMATION",
    ]
    assert [event.priority for event in events[1:]] == [50, 60, 70]
    assert all("955" not in event.deterministic_tiebreaker for event in events)


def test_lifecycle_adapter_does_not_use_database_shaped_source_ids_for_sorting() -> None:
    row_a = _row("MATCHED_PROVISIONAL", NOW, idempotency_key="paper-order:1:result")
    row_b = _row("MATCHED_PROVISIONAL", NOW, idempotency_key="paper-order:999999:result")

    first = lifecycle_rows_to_sim_events(_audit(), (row_a,))[0]
    second = lifecycle_rows_to_sim_events(_audit(), (row_b,))[0]

    assert first.source_event_id != second.source_event_id
    assert first.deterministic_tiebreaker == second.deterministic_tiebreaker
    assert first.sort_key == second.sort_key


def test_lifecycle_adapter_has_honest_audit_only_fallback() -> None:
    events = lifecycle_rows_to_sim_events(_audit(), ())

    assert [event.event_type for event in events] == [
        "STRATEGY_INTENT",
        "PAPER_COMMAND_ARRIVAL",
        "ACCOUNTING_MARK",
    ]
    assert events[0].event_ts_ns < events[1].event_ts_ns <= events[2].event_ts_ns
