from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import quant.calibration.user_ws_recorder as user_ws_module
from quant.calibration.user_ws_recorder import (
    DurableUserWsEventJournal,
    UserWsRecorder,
)


def test_user_ws_journal_is_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "maker.user-ws.jsonl"
    journal = DurableUserWsEventJournal(path)
    row = {
        "event_key": "userws-event-1",
        "probe_id": "probe-1",
        "event_type": "ORDER",
        "payload": {"id": "order-1", "status": "LIVE"},
    }

    assert journal.append(row) is True
    assert journal.append(row) is False
    original_hash = journal.sha256()

    recovered = DurableUserWsEventJournal(path)
    assert recovered.load_events() == [row]
    assert recovered.append(row) is False
    assert recovered.sha256() == original_hash


def test_submit_watch_cancel_reconnects_without_resubmitting(
    monkeypatch,
) -> None:
    recorder = object.__new__(UserWsRecorder)
    recorder._subscription = lambda _condition_ids: {"markets": ["condition-1"]}
    connections = []

    @asynccontextmanager
    async def connect(_subscription):
        websocket = object()
        connections.append(websocket)
        yield websocket

    recorder._connect = connect
    submissions = []
    cancellations = []
    submission_checkpoints = []
    durable_events = []
    collect_calls = 0

    async def receive_timeout(_websocket):
        raise asyncio.TimeoutError

    async def collect(_websocket, **kwargs):
        nonlocal collect_calls
        collect_calls += 1
        if collect_calls == 1:
            raise RuntimeError("injected ws close")
        payload = {"event_type": "ORDER", "id": "order-1", "status": "CANCELED"}
        sink = kwargs.get("event_sink")
        if sink is not None:
            sink(
                {
                    "event_key": "event-canceled",
                    "probe_id": "probe-1",
                    "event_type": "ORDER",
                    "payload": payload,
                }
            )
        return [payload]

    monkeypatch.setattr(user_ws_module, "_receive_message", receive_timeout)
    monkeypatch.setattr(user_ws_module, "_collect_matching_message", collect)

    def submit():
        submissions.append(True)
        return {"orderID": "order-1"}

    def cancel(order_id):
        cancellations.append(order_id)
        return {"canceled": [order_id]}

    async def exercise():
        return await recorder._submit_watch_cancel(
            probe_id="probe-1",
            condition_ids=["condition-1"],
            submit=submit,
            cancel=cancel,
            resting_seconds=0.1,
            post_cancel_seconds=0.1,
            event_sink=durable_events.append,
            submission_sink=lambda submission, order_id, submitted_at: (
                submission_checkpoints.append(
                    (submission, order_id, isinstance(submitted_at, datetime))
                )
            ),
            max_reconnects=2,
        )

    _, _, capture = asyncio.run(exercise())

    assert len(connections) == 2
    assert len(submissions) == 1
    assert cancellations == ["order-1"]
    assert len(submission_checkpoints) == 1
    assert durable_events[0]["event_key"] == "event-canceled"
    assert capture["status"] == "TERMINAL"
    assert capture["reconnects"] == 1


def test_submit_watch_cancel_cancels_immediately_after_first_partial_fill(
    monkeypatch,
) -> None:
    recorder = object.__new__(UserWsRecorder)
    recorder._subscription = lambda _condition_ids: {"markets": ["condition-1"]}

    @asynccontextmanager
    async def connect(_subscription):
        yield object()

    recorder._connect = connect
    cancellations = []
    payloads = [
        {
            "event_type": "TRADE",
            "status": "MATCHED",
            "maker_orders": [
                {"order_id": "order-1", "matched_amount": "2"}
            ],
        },
        {"event_type": "ORDER", "id": "order-1", "status": "CANCELED"},
    ]

    async def receive_timeout(_websocket):
        raise asyncio.TimeoutError

    async def collect(_websocket, **_kwargs):
        return [payloads.pop(0)]

    monkeypatch.setattr(user_ws_module, "_receive_message", receive_timeout)
    monkeypatch.setattr(user_ws_module, "_collect_matching_message", collect)

    async def exercise():
        return await recorder._submit_watch_cancel(
            probe_id="probe-1",
            condition_ids=["condition-1"],
            submit=lambda: {"orderID": "order-1"},
            cancel=lambda order_id: cancellations.append(order_id) or {"ok": True},
            resting_seconds=60,
            post_cancel_seconds=1,
            event_sink=None,
            submission_sink=None,
            max_reconnects=1,
            cancel_on_first_fill=True,
            original_size="5",
        )

    _, _, capture = asyncio.run(exercise())

    assert cancellations == ["order-1"]
    assert capture["cancel_trigger"] == "FIRST_PARTIAL_FILL"
    assert capture["matched_size_before_cancel"] == "2"
    assert capture["first_positive_match_at"]
    assert capture["status"] == "TERMINAL"


def test_submit_watch_cancel_stops_on_full_without_partial_cancel_mode(
    monkeypatch,
) -> None:
    recorder = object.__new__(UserWsRecorder)
    recorder._subscription = lambda _condition_ids: {"markets": ["condition-1"]}

    @asynccontextmanager
    async def connect(_subscription):
        yield object()

    recorder._connect = connect
    cancellations = []
    payloads = [
        {
            "event_type": "ORDER",
            "id": "order-1",
            "status": "FILLED",
            "size_matched": "5",
        },
        {
            "event_type": "ORDER",
            "id": "order-1",
            "status": "FILLED",
            "size_matched": "5",
        },
    ]

    async def receive_timeout(_websocket):
        raise asyncio.TimeoutError

    async def collect(_websocket, **_kwargs):
        return [payloads.pop(0)]

    monkeypatch.setattr(user_ws_module, "_receive_message", receive_timeout)
    monkeypatch.setattr(user_ws_module, "_collect_matching_message", collect)

    async def exercise():
        return await recorder._submit_watch_cancel(
            probe_id="probe-1",
            condition_ids=["condition-1"],
            submit=lambda: {"orderID": "order-1"},
            cancel=lambda order_id: cancellations.append(order_id),
            resting_seconds=60,
            post_cancel_seconds=1,
            event_sink=None,
            submission_sink=None,
            max_reconnects=1,
            cancel_on_first_fill=False,
            original_size="5",
        )

    _, _, capture = asyncio.run(exercise())

    assert cancellations == []
    assert capture["cancel_attempted"] is False
    assert capture["cancel_trigger"] == "FULL_BEFORE_CANCEL"
    assert capture["matched_size_before_cancel"] == "5"
    assert capture["status"] == "TERMINAL"


def test_long_running_collector_uses_stable_identity_and_multiple_order_filter(
    monkeypatch,
) -> None:
    recorder = object.__new__(UserWsRecorder)
    recorder._subscription = lambda condition_ids: {"markets": condition_ids}

    @asynccontextmanager
    async def connect(_subscription):
        yield object()

    recorder._connect = connect
    payload = {
        "event_type": "ORDER",
        "id": "order-1",
        "status": "CANCELED",
        "timestamp": "1787712000000",
        "maker_orders": [{"order_id": "order-2"}],
    }

    async def receive(_websocket):
        return payload

    monkeypatch.setattr(user_ws_module, "_receive_message", receive)
    events = []

    result = asyncio.run(
        recorder._record_orders(
            collector_id="collector-cycle-1",
            condition_ids=["condition-1"],
            order_ids=["order-1", "order-2"],
            timeout_seconds=1,
            event_sink=events.append,
            identity_scope="maker-account-1",
            max_reconnects=1,
        )
    )
    first = recorder.normalize_event(
        "collector-cycle-1", payload, identity_scope="maker-account-1"
    )
    second = recorder.normalize_event(
        "collector-cycle-2", payload, identity_scope="maker-account-1"
    )

    assert result["status"] == "ALL_TERMINAL"
    assert result["terminal_order_ids"] == ["order-1", "order-2"]
    assert events[0]["matched_order_ids"] == ["order-1", "order-2"]
    assert first["event_key"] == second["event_key"]
