from decimal import Decimal

import pytest

from quant.simulator.liquidity import AllocationOrder, AllocationRequest, LiquidityLevelKey
from quant.simulator.liquidity.overlay_store import SCHEMA_STATEMENTS, _assert_same_request, _order_from_row, _remaining


def _request() -> AllocationRequest:
    return AllocationRequest(
        allocation_id="allocation:one",
        order=AllocationOrder(10, 2, "account:one", "order:one"),
        strategy_id="strategy:one",
        level=LiquidityLevelKey("CLOB", "asset:one", 3, "window:one", "ASK", Decimal("0.5")),
        displayed_size=Decimal("100"),
        requested_size=Decimal("10"),
        source_event_id="book:one",
    )


def test_schema_keeps_generation_scoped_level_and_durable_idempotency_fields() -> None:
    schema = "\n".join(SCHEMA_STATEMENTS)

    for required in (
        "simulator_liquidity_levels",
        "simulator_liquidity_allocations",
        "book_generation",
        "allocation_id TEXT PRIMARY KEY",
        "arrival_ts_ns",
        "deterministic_order_id",
    ):
        assert required in schema


def test_remaining_and_last_order_are_decimal_and_deterministic() -> None:
    row = {
        "displayed_size": Decimal("100"),
        "reserved_size": Decimal("10"),
        "consumed_size": Decimal("60"),
        "released_size": Decimal("5"),
        "last_arrival_ts_ns": 10,
        "last_strategy_priority": 2,
        "last_account_id": "account:one",
        "last_deterministic_order_id": "order:one",
    }

    assert _remaining(row) == Decimal("35")
    assert _order_from_row(row) == AllocationOrder(10, 2, "account:one", "order:one")


def test_reused_allocation_id_with_different_durable_request_is_rejected() -> None:
    request = _request()
    row = {
        "overlay_version": request.overlay_version,
        "venue": request.level.venue,
        "asset_id": request.level.asset_id,
        "book_generation": request.level.book_generation,
        "arrival_window_id": request.level.arrival_window_id,
        "side": request.level.side,
        "price_tick": request.level.price_tick,
        "strategy_id": request.strategy_id,
        "source_event_id": request.source_event_id,
        "arrival_ts_ns": request.order.arrival_ts_ns,
        "strategy_priority": request.order.strategy_priority,
        "account_id": request.order.account_id,
        "deterministic_order_id": "different-order",
    }

    with pytest.raises(ValueError, match="allocation id collision"):
        _assert_same_request(row, request)
