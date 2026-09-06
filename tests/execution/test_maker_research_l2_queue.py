from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
)
from quant.orderbook.local_book import LocalOrderBook, TokenBookIdentity
from quant.orderbook.polymarket_adapter import NormalizedBookDelta
from quant.paper.live_shadow_service import LivePaperShadowService, LiveShadowStats


def _state() -> MakerQueueState:
    return MakerQueueState(
        paper_order_id="paper-1",
        asset_id="asset-1",
        side="BUY",
        price_tick=Decimal("0.40"),
        queue_model_version="research-v1",
        displayed_size_at_accept=Decimal("10"),
        own_orders_ahead=Decimal("0"),
        estimated_external_queue_ahead=Decimal("10"),
        order_size=Decimal("2"),
        book_generation=3,
        last_event_ts_ns=100,
    )


def test_book_decrease_only_advances_probabilistic_cancel_estimate() -> None:
    risk = MakerQueueEngine(QueueModel.RISK_AVERSE_QUEUE).on_book_decrease(
        _state(), decrease=Decimal("4"), event_id="delta-1", event_ts_ns=200
    )
    probabilistic = MakerQueueEngine(
        QueueModel.PROBABILISTIC_QUEUE,
        cancel_ahead_probability=Decimal("0.25"),
    ).on_book_decrease(
        _state(), decrease=Decimal("4"), event_id="delta-1", event_ts_ns=200
    )
    late = MakerQueueEngine(QueueModel.PROBABILISTIC_QUEUE).on_book_decrease(
        probabilistic,
        decrease=Decimal("9"),
        event_id="late-delta",
        event_ts_ns=150,
    )

    assert risk.cumulative_cancel_ahead_estimate == 0
    assert probabilistic.cumulative_cancel_ahead_estimate == Decimal("1")
    assert late == probabilistic


def test_live_book_delta_uses_existing_bookstate_and_only_queues_decrease() -> None:
    service = object.__new__(LivePaperShadowService)
    service._maker_research_levels = {("asset-1", "BUY", Decimal("0.40"))}
    service._maker_research_book_events = deque()
    service._maker_research_book_event_limit = 10
    service.stats = LiveShadowStats(worker_id="worker")
    book = LocalOrderBook(
        TokenBookIdentity(
            token_id="asset-1",
            market_id=1,
            condition_id="condition-1",
            outcome="YES",
        )
    )
    book.apply_snapshot(
        bids=((Decimal("0.40"), Decimal("7")),),
        asks=((Decimal("0.50"), Decimal("8")),),
        event_ts_ms=2000,
    )
    decrease = NormalizedBookDelta(
        token_id="asset-1",
        side="bid",
        price=Decimal("0.40"),
        size=Decimal("7"),
        event_ts_ms=2000,
    )
    increase = NormalizedBookDelta(
        token_id="asset-1",
        side="bid",
        price=Decimal("0.40"),
        size=Decimal("12"),
        event_ts_ms=3000,
    )

    service._queue_maker_research_book_event(
        decrease,
        signature="delta-decrease",
        book=book,
        prior_displayed_size=Decimal("10"),
    )
    service._queue_maker_research_book_event(
        increase,
        signature="delta-increase",
        book=book,
        prior_displayed_size=Decimal("7"),
    )

    assert len(service._maker_research_book_events) == 1
    queued = service._maker_research_book_events[0]
    assert queued.previous_size == Decimal("10")
    assert queued.displayed_size == Decimal("7")


def test_research_book_batch_is_persisted_before_dequeue() -> None:
    observed = []

    class _Store:
        def apply_maker_research_book_events(self, rows):
            observed.extend(rows)
            return {"events": 1, "states": 2, "rebases": 1, "duplicates": 0}

    async def exercise() -> None:
        service = object.__new__(LivePaperShadowService)
        service.store = _Store()
        service._maker_research_book_events = deque([object()])
        service.stats = LiveShadowStats(worker_id="worker")

        async def db_call(callback, *args, **kwargs):
            return callback(*args, **kwargs)

        service._db_call = db_call
        await service._advance_maker_research_book_events()

        assert len(service._maker_research_book_events) == 0
        assert service.stats.maker_research_book_events == 1
        assert service.stats.maker_research_state_updates == 2
        assert service.stats.maker_research_rebases == 1

    asyncio.run(exercise())
