import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.orderbook.local_event_bus import LocalEventEnvelope
from quant.orderbook.subscription_reconciler import (
    build_realtime_targets_from_desired,
)
from quant.paper.live_shadow_service import (
    CausalLifecycleEvent,
    FeedEnvelope,
    LivePaperShadowService,
    _desired,
    _paper_terminal_event_type,
)
from quant.paper.live_shadow_store import LiveWatchTarget, _health_values
from quant.paper.persistent_event_kernel import (
    AppliedKernelEvent,
    KernelAppendResult,
)
from quant.paper.taker_execution import TakerOnlyPaperExecutionEngine


class _RecordingPersistentKernel:
    def __init__(self, *, duplicate_batch_indexes: set[int] | None = None) -> None:
        self.duplicate_batch_indexes = duplicate_batch_indexes or set()
        self.append_batches: list[tuple] = []
        self.applied_batches: list[tuple] = []
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.sequence = 0
        self.journal_hash = ""

    def append_many(self, events, *, worker_id):
        del worker_id
        batch = tuple(events)
        self.append_batches.append(batch)
        self.calls.append(("append_many", tuple(row.event_type for row in batch)))
        return tuple(
            KernelAppendResult(
                event=event,
                processing_state=(
                    "APPLIED" if index in self.duplicate_batch_indexes else "PROCESSING"
                ),
                applied_sequence=(
                    100 + index if index in self.duplicate_batch_indexes else None
                ),
                journal_hash=(
                    f"duplicate-{index}"
                    if index in self.duplicate_batch_indexes
                    else None
                ),
                inserted=index not in self.duplicate_batch_indexes,
            )
            for index, event in enumerate(batch)
        )

    def append(self, event, *, worker_id):
        del worker_id
        self.calls.append(("append", (event.event_type,)))
        return KernelAppendResult(
            event=event,
            processing_state="PROCESSING",
            applied_sequence=None,
            journal_hash=None,
            inserted=True,
        )

    def mark_applied(self, events, *, worker_id):
        del worker_id
        batch = tuple(events)
        self.applied_batches.append(batch)
        self.calls.append(("mark_applied", tuple(row.event_type for row in batch)))
        applied = []
        for event in batch:
            self.sequence += 1
            encoded = json.dumps(
                event.causal_record(sequence=self.sequence),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            self.journal_hash = hashlib.sha256(
                f"{self.journal_hash}|{encoded}".encode()
            ).hexdigest()
            applied.append(
                AppliedKernelEvent(
                    event=event,
                    sequence=self.sequence,
                    journal_hash=self.journal_hash,
                    late_event=False,
                )
            )
        return tuple(applied)


class _FailingPersistentKernel:
    def load_state(self):
        raise RuntimeError("recovery failed")


class _RecordingAuthorityController:
    def __init__(self) -> None:
        self.held = True
        self.token = None
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        self.held = False


def test_feed_consumer_advances_independently_of_control_loop() -> None:
    async def exercise() -> None:
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
        )
        consumer = asyncio.create_task(service._feed_consumer_loop())
        await service._feed_queue.put(FeedEnvelope(source="primary", state="CONNECTED"))
        await asyncio.wait_for(service._feed_queue.join(), timeout=0.5)

        assert service.stats.route_states["primary"] == "CONNECTED"
        assert service.stats.transport_state == "CONNECTED"
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(exercise())


def test_persistent_recovery_failure_releases_authority_lease() -> None:
    async def exercise() -> None:
        authority = _RecordingAuthorityController()
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
            persistent_event_kernel=_FailingPersistentKernel(),
            authority_controller=authority,
        )

        with pytest.raises(RuntimeError, match="recovery failed"):
            await service._restore_persistent_event_kernel()

        assert authority.release_calls == 1
        assert service.stats.authority_state == "RELEASED"

    asyncio.run(exercise())


def test_external_ingress_enqueues_connected_transition_once() -> None:
    async def exercise() -> None:
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
        )
        envelope = LocalEventEnvelope(
            source="primary",
            producer_id="collector",
            kind="events",
            sent_at="2026-08-25T00:00:00+00:00",
            received_ts_ms=0,
            shard_id=0,
            messages=({"event_type": "book"},),
        )

        await service._handle_external_envelope(envelope)
        await service._handle_external_envelope(envelope)

        first = service._feed_queue.get_nowait()
        second = service._feed_queue.get_nowait()
        assert isinstance(first, FeedEnvelope)
        assert isinstance(second, FeedEnvelope)
        assert first.state == "CONNECTED"
        assert second.state is None
        service._feed_queue.task_done()
        service._feed_queue.task_done()

    asyncio.run(exercise())


def test_external_routes_recover_from_older_authoritative_snapshots() -> None:
    service = LivePaperShadowService(
        store=object(),
        client=None,
        external_event_socket=Path("/tmp/paper-events-unused.sock"),
        engine=TakerOnlyPaperExecutionEngine(),
    )
    asset_id = "asset-1"
    target = LiveWatchTarget(
        asset_id=asset_id,
        market_id="1",
        condition_id="condition-1",
        market_slug="market-1",
        outcome_name="YES",
        outcome_index=0,
        market_state="LIVE",
        execution_eligible=True,
        coverage_grade="D",
        has_gap=True,
    )
    realtime_targets = build_realtime_targets_from_desired([_desired(target)])
    service.targets[asset_id] = target
    service.service.replace_targets(realtime_targets)
    service.stats.route_states = {"primary": "CONNECTED", "secondary": "CONNECTED"}
    for route in service.route_services.values():
        route.replace_targets(realtime_targets)

    rest_payload = {
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.60", "size": "10"}],
    }
    for book_service in (service.service, *service.route_services.values()):
        book_service.process_rest_book(
            token_id=asset_id,
            payload=rest_payload,
            event_ts_ms=2_000,
            received_ts_ms=2_000,
        )
    for source, route in service.route_services.items():
        route.get_book(asset_id).mark_stale(f"{source}_connection_gap")
        service.route_gap_assets[source].add(asset_id)
    service.resyncing_assets.add(asset_id)

    snapshot = {
        "event_type": "book",
        "asset_id": asset_id,
        "timestamp": "1",
        "bids": rest_payload["bids"],
        "asks": rest_payload["asks"],
        "_raw_received_wall_ns": 3_000_000_000,
    }
    observed_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    service._apply_message("primary", snapshot, observed_at=observed_at)

    assert asset_id not in service.route_gap_assets["primary"]
    assert asset_id in service.route_gap_assets["secondary"]
    assert asset_id not in service.resyncing_assets
    assert service._effective_target(asset_id, target).coverage_grade == "B"
    assert asset_id not in service.redundant_ready_assets

    service._apply_message("secondary", snapshot, observed_at=observed_at)

    assert asset_id not in service.route_gap_assets["secondary"]
    assert service._effective_target(asset_id, target).coverage_grade == "A"
    assert asset_id in service.redundant_ready_assets


def test_external_watchlist_publishes_exact_collector_subscription(tmp_path) -> None:
    async def exercise() -> None:
        subscription_path = tmp_path / "subscriptions.json"
        service = LivePaperShadowService(
            store=object(),
            client=None,
            external_event_socket=tmp_path / "events.sock",
            external_subscription_file=subscription_path,
            engine=TakerOnlyPaperExecutionEngine(),
        )
        target = LiveWatchTarget(
            asset_id="asset-1",
            market_id="17",
            condition_id="condition-1",
            market_slug="market-1",
            outcome_name="YES",
            outcome_index=0,
            market_state="LIVE",
            execution_eligible=True,
            coverage_grade="D",
            has_gap=True,
            refresh_nonce="2026-09-01T00:00:00+00:00",
        )

        await service._apply_watch_targets([target])
        first_syncs = service.stats.external_subscription_syncs
        await service._apply_watch_targets([target])

        refreshed_target = replace(
            target,
            refresh_nonce="2026-09-01T00:01:00+00:00",
        )
        await service._apply_watch_targets([refreshed_target])

        payload = json.loads(subscription_path.read_text(encoding="utf-8"))
        assert payload["token_count"] == 1
        assert payload["tokens"][0]["asset_id"] == "asset-1"
        assert payload["tokens"][0]["market_id"] == 17
        assert payload["tokens"][0]["execution_eligible"] is True
        assert service.stats.external_subscription_count == 1
        assert service.stats.external_subscription_sha256
        assert first_syncs == 1
        assert service.stats.external_subscription_syncs == 2
        assert service.stats.external_subscription_failures == 0

    asyncio.run(exercise())


def test_external_watchlist_streams_selected_stale_exposure(tmp_path) -> None:
    async def exercise() -> None:
        subscription_path = tmp_path / "subscriptions.json"
        service = LivePaperShadowService(
            store=object(),
            client=None,
            external_event_socket=tmp_path / "events.sock",
            external_subscription_file=subscription_path,
            engine=TakerOnlyPaperExecutionEngine(),
        )
        target = LiveWatchTarget(
            asset_id="asset-stale",
            market_id="18",
            condition_id="condition-stale",
            market_slug="market-stale",
            outcome_name="YES",
            outcome_index=0,
            market_state="STALE",
            execution_eligible=False,
            coverage_grade="D",
            has_gap=True,
        )

        await service._apply_watch_targets([target])

        payload = json.loads(subscription_path.read_text(encoding="utf-8"))
        assert payload["tokens"][0]["market_state"] == "STALE"
        assert payload["tokens"][0]["execution_eligible"] is True

    asyncio.run(exercise())


def test_command_checkpoint_is_serialized_after_prior_feed_work() -> None:
    async def exercise() -> None:
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
        )
        observed_at = datetime(2026, 8, 6, tzinfo=timezone.utc)
        marker = SimpleNamespace(observed_at=observed_at)

        async def apply(_envelope):
            service.history["asset"].append(marker)

        service._apply_feed_envelope_cooperatively = apply
        consumer = asyncio.create_task(service._feed_consumer_loop())
        service._feed_tasks["consumer"] = consumer
        await service._feed_queue.put(
            FeedEnvelope(source="primary", received_at=observed_at)
        )

        checkpoint = await service._causal_checkpoint(
            intent_id=1,
            asset_id="asset",
            as_of=observed_at,
        )

        assert checkpoint is marker
        assert service.stats.causal_checkpoint_requests == 1
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(exercise())


def test_market_strategy_fill_and_accounting_share_one_causal_fifo() -> None:
    async def exercise() -> None:
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
        )
        observed_at = datetime(2026, 8, 6, 1, 2, 3, tzinfo=timezone.utc)
        service._apply_message = lambda *_args, **_kwargs: None
        consumer = asyncio.create_task(service._feed_consumer_loop())
        service._feed_tasks["consumer"] = consumer
        await service._feed_queue.put(
            FeedEnvelope(
                source="primary",
                messages=({"event_type": "book", "asset_id": "asset"},),
                received_at=observed_at,
            )
        )
        await service._causal_checkpoint(
            intent_id=7,
            asset_id="asset",
            as_of=observed_at,
            event_types=("STRATEGY_OBSERVATION", "STRATEGY_INTENT"),
        )
        match_sequence = await service._causal_lifecycle_event(
            "PAPER_MATCH",
            intent_id=7,
            asset_id="asset",
            event_ts=observed_at,
        )
        accounting_sequence = await service._causal_lifecycle_event(
            "ACCOUNTING_MARK",
            intent_id=7,
            asset_id="asset",
            event_ts=observed_at,
        )

        event_types = [
            row["event_type"] for row in service.stats.causal_kernel_recent_events
        ]
        assert event_types == [
            "BOOK_SNAPSHOT",
            "STRATEGY_OBSERVATION",
            "STRATEGY_INTENT",
            "PAPER_MATCH",
            "ACCOUNTING_MARK",
        ]
        assert match_sequence == 4
        assert accounting_sequence == 5
        assert service.stats.causal_kernel_last_sequence == 5
        assert service.stats.causal_kernel_failures == 0
        assert [
            row["event_type"]
            for row in service.stats.causal_kernel_recent_lifecycle_events
        ] == [
            "STRATEGY_OBSERVATION",
            "STRATEGY_INTENT",
            "PAPER_MATCH",
            "ACCOUNTING_MARK",
        ]
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(exercise())


def test_causal_kernel_hash_is_deterministic_for_same_event_sequence() -> None:
    event_ts = datetime(2026, 8, 6, 1, 2, 3, tzinfo=timezone.utc)
    services = [
        LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
        )
        for _ in range(2)
    ]
    for service in services:
        service._record_causal_kernel_event(
            "BOOK_SNAPSHOT",
            asset_id="asset",
            event_ts=event_ts,
            payload={"source": "primary"},
        )
        service._record_causal_kernel_event(
            "STRATEGY_INTENT",
            intent_id=7,
            asset_id="asset",
            event_ts=event_ts,
        )

    assert (
        services[0].stats.causal_kernel_journal_hash
        == services[1].stats.causal_kernel_journal_hash
    )


def test_large_feed_batch_yields_without_reordering_messages() -> None:
    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        applied: list[int] = []
        service._apply_feed_envelope_header = lambda _envelope: object()
        service._apply_message = lambda _source, message, **_kwargs: applied.append(
            message["sequence"]
        )

        peer_ticks = 0

        async def peer() -> None:
            nonlocal peer_ticks
            for _ in range(4):
                await asyncio.sleep(0)
                peer_ticks += 1

        peer_task = asyncio.create_task(peer())
        await service._apply_feed_envelope_cooperatively(
            FeedEnvelope(
                source="primary",
                messages=tuple({"sequence": value} for value in range(300)),
            )
        )
        await peer_task

        assert applied == list(range(300))
        assert peer_ticks == 4

    asyncio.run(exercise())


def test_persistent_feed_batch_keeps_event_granularity_and_skips_duplicates() -> None:
    async def exercise() -> None:
        kernel = _RecordingPersistentKernel(duplicate_batch_indexes={1})
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
            persistent_event_kernel=kernel,
        )
        applied: list[int] = []
        service._apply_message = lambda _source, message, **_kwargs: applied.append(
            message["sequence"]
        )
        observed_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)

        await service._apply_plain_feed_batch(
            tuple(
                FeedEnvelope(
                    source="primary",
                    messages=({"event_type": "book", "sequence": sequence},),
                    received_at=observed_at,
                )
                for sequence in (1, 2, 3)
            )
        )

        assert len(kernel.append_batches) == 1
        assert len(kernel.append_batches[0]) == 3
        assert [row.source_sequence for row in kernel.append_batches[0]] == [1, 2, 3]
        assert [row.record_payload["message_count"] for row in kernel.append_batches[0]] == [
            1,
            1,
            1,
        ]
        assert applied == [1, 3]
        assert len(kernel.applied_batches) == 1
        assert [row.source_sequence for row in kernel.applied_batches[0]] == [1, 3]
        assert service.stats.persistent_kernel_duplicate_events == 1
        assert service.stats.persistent_kernel_feed_batches == 1
        assert service.stats.persistent_kernel_feed_envelopes == 3
        assert service.stats.persistent_kernel_feed_max_batch_size == 3
        assert service.stats.persistent_kernel_feed_db_transactions_avoided == 3

    asyncio.run(exercise())


def test_feed_batch_does_not_cross_lifecycle_boundary() -> None:
    async def exercise() -> None:
        kernel = _RecordingPersistentKernel()
        service = LivePaperShadowService(
            store=object(),
            client=object(),
            engine=TakerOnlyPaperExecutionEngine(),
            persistent_event_kernel=kernel,
        )
        applied: list[int] = []
        service._apply_message = lambda _source, message, **_kwargs: applied.append(
            message["sequence"]
        )
        observed_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
        lifecycle_result = asyncio.get_running_loop().create_future()
        for sequence in (1, 2):
            await service._feed_queue.put(
                FeedEnvelope(
                    source="primary",
                    messages=({"event_type": "book", "sequence": sequence},),
                    received_at=observed_at,
                )
            )
        await service._feed_queue.put(
            CausalLifecycleEvent(
                intent_id=7,
                event_type="PAPER_CANCEL",
                event_ts=observed_at,
                asset_id="asset",
                payload={},
                result=lifecycle_result,
            )
        )
        await service._feed_queue.put(
            FeedEnvelope(
                source="primary",
                messages=({"event_type": "book", "sequence": 3},),
                received_at=observed_at,
            )
        )

        consumer = asyncio.create_task(service._feed_consumer_loop())
        await asyncio.wait_for(service._feed_queue.join(), timeout=1.0)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

        assert applied == [1, 2, 3]
        assert lifecycle_result.done()
        assert [name for name, _event_types in kernel.calls] == [
            "append_many",
            "mark_applied",
            "append",
            "mark_applied",
            "append_many",
            "mark_applied",
        ]
        assert [len(batch) for batch in kernel.append_batches] == [2, 1]
        assert kernel.calls[2] == ("append", ("PAPER_CANCEL",))

    asyncio.run(exercise())


def test_health_values_persist_backpressure_state() -> None:
    values = _health_values(
        "worker-1",
        {
            "transport_state": "REDUNDANT",
            "watched_assets": 250,
            "ready_books": 247,
            "execution_watched_assets": 200,
            "execution_ready_books": 199,
            "execution_fresh_books": 12,
            "fresh_books": 12,
            "stale_books": 0,
            "queued_intents": 0,
            "processing_intents": 0,
            "completed_intents": 3,
            "rejected_intents": 1,
            "websocket_messages": 100,
            "reconnects": 2,
            "backpressure_status": "ACCEPT",
            "backpressure_reasons": [],
        },
    )

    assert len(values) == 25
    assert values[4:7] == (200, 199, 12)
    assert values[21] == "ACCEPT"
    assert json.loads(values[22]) == []


def test_working_order_is_not_journaled_as_rejected() -> None:
    assert _paper_terminal_event_type("WORKING") == "PAPER_WORKING"
    assert _paper_terminal_event_type("CANCELLED") == "PAPER_CANCEL"
    assert _paper_terminal_event_type("REJECTED") == "PAPER_REJECT"
