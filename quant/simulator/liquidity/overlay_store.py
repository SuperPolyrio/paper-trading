"""PostgreSQL-backed shared liquidity allocation with deterministic order guards."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, ContextManager, Iterable

from quant.core.db import postgres_connection

from .allocation import AllocationOrder, AllocationRequest, AllocationResult, AllocationStatus, LiquidityLevelKey


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_liquidity_levels (
        overlay_version TEXT NOT NULL, venue TEXT NOT NULL, asset_id TEXT NOT NULL,
        book_generation BIGINT NOT NULL, arrival_window_id TEXT NOT NULL,
        side TEXT NOT NULL, price_tick NUMERIC NOT NULL,
        displayed_size NUMERIC NOT NULL, reserved_size NUMERIC NOT NULL DEFAULT 0,
        consumed_size NUMERIC NOT NULL DEFAULT 0, released_size NUMERIC NOT NULL DEFAULT 0,
        first_source_event_id TEXT NOT NULL, last_source_event_id TEXT NOT NULL,
        last_arrival_ts_ns BIGINT, last_strategy_priority INTEGER,
        last_account_id TEXT, last_deterministic_order_id TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (overlay_version, venue, asset_id, book_generation, arrival_window_id, side, price_tick)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_liquidity_allocations (
        allocation_id TEXT PRIMARY KEY, overlay_version TEXT NOT NULL,
        venue TEXT NOT NULL, asset_id TEXT NOT NULL, book_generation BIGINT NOT NULL,
        arrival_window_id TEXT NOT NULL, side TEXT NOT NULL, price_tick NUMERIC NOT NULL,
        strategy_id TEXT NOT NULL, source_event_id TEXT NOT NULL,
        arrival_ts_ns BIGINT NOT NULL, strategy_priority INTEGER NOT NULL,
        account_id TEXT NOT NULL, deterministic_order_id TEXT NOT NULL,
        status TEXT NOT NULL, allocated_size NUMERIC NOT NULL, remaining_size NUMERIC NOT NULL,
        reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    ALTER TABLE quant.simulator_liquidity_allocations
        ADD COLUMN IF NOT EXISTS strategy_id TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE quant.simulator_liquidity_allocations
        ADD COLUMN IF NOT EXISTS source_event_id TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE quant.simulator_liquidity_allocations
        ADD COLUMN IF NOT EXISTS arrival_ts_ns BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE quant.simulator_liquidity_allocations
        ADD COLUMN IF NOT EXISTS strategy_priority INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE quant.simulator_liquidity_allocations
        ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE quant.simulator_liquidity_allocations
        ADD COLUMN IF NOT EXISTS deterministic_order_id TEXT NOT NULL DEFAULT ''
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_liquidity_allocations_level_idx
        ON quant.simulator_liquidity_allocations (
            overlay_version, venue, asset_id, book_generation, arrival_window_id, side, price_tick
        )
    """,
)


class PostgresLiquidityOverlayStore:
    """Transactional allocation store shared by independent simulator workers.

    The source book is not mutated. Consumption is attributed to a source-book
    generation and a deterministic allocation order. A late request is recorded
    as deferred, rather than being allowed to consume depth retroactively.
    """

    def __init__(self, connection_factory: Callable[..., ContextManager[Any]] = postgres_connection) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA_STATEMENTS:
                    cur.execute(statement)

    def allocate(self, request: AllocationRequest) -> AllocationResult:
        with self.connection_factory(readonly=False) as conn:
            with conn.cursor() as cur:
                existing = self._existing(cur, request)
                if existing is not None:
                    return existing
                key = request.level
                cur.execute(
                    """
                    INSERT INTO quant.simulator_liquidity_levels (
                        overlay_version, venue, asset_id, book_generation, arrival_window_id, side, price_tick,
                        displayed_size, first_source_event_id, last_source_event_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (*_key_values(key, request.overlay_version), request.displayed_size, request.source_event_id, request.source_event_id),
                )
                cur.execute(
                    """
                    SELECT * FROM quant.simulator_liquidity_levels
                    WHERE overlay_version=%s AND venue=%s AND asset_id=%s AND book_generation=%s
                      AND arrival_window_id=%s AND side=%s AND price_tick=%s
                    FOR UPDATE
                    """,
                    _key_values(key, request.overlay_version),
                )
                level = cur.fetchone()
                if level is None:
                    raise RuntimeError("liquidity level was not available after upsert")
                existing = self._existing(cur, request)
                if existing is not None:
                    return existing
                if Decimal(level["displayed_size"]) != request.displayed_size:
                    raise ValueError("same book generation cannot carry two displayed sizes")
                last = _order_from_row(level)
                if last is not None and request.order < last:
                    return self._persist_result(
                        cur,
                        request,
                        AllocationResult(
                            request.allocation_id,
                            AllocationStatus.DEFERRED_OUT_OF_ORDER,
                            Decimal("0"),
                            _remaining(level),
                            "arrival_order_precedes_already_allocated_request",
                            key,
                        ),
                    )
                if last == request.order:
                    raise ValueError("distinct allocations cannot share one allocation order on a level")
                allocated = min(request.requested_size, _remaining(level))
                remaining = _remaining(level) - allocated
                cur.execute(
                    """
                    UPDATE quant.simulator_liquidity_levels
                    SET consumed_size=consumed_size+%s, last_source_event_id=%s,
                        last_arrival_ts_ns=%s, last_strategy_priority=%s,
                        last_account_id=%s, last_deterministic_order_id=%s, updated_at=clock_timestamp()
                    WHERE overlay_version=%s AND venue=%s AND asset_id=%s AND book_generation=%s
                      AND arrival_window_id=%s AND side=%s AND price_tick=%s
                    """,
                    (
                        allocated,
                        request.source_event_id,
                        request.order.arrival_ts_ns,
                        request.order.strategy_priority,
                        request.order.account_id,
                        request.order.deterministic_order_id,
                        *_key_values(key, request.overlay_version),
                    ),
                )
                return self._persist_result(
                    cur,
                    request,
                    AllocationResult(
                        request.allocation_id,
                        AllocationStatus.ALLOCATED,
                        allocated,
                        remaining,
                        "allocated" if allocated == request.requested_size else "visible_depth_exhausted",
                        key,
                    ),
                )

    def allocate_many(self, requests: Iterable[AllocationRequest]) -> tuple[AllocationResult, ...]:
        """Dispatch a worker-owned batch in the canonical level/order sequence."""
        return tuple(self.allocate(request) for request in sorted(requests, key=lambda row: (_level_sort_key(row.level), row.order)))

    def release(self, allocation_id: str, *, reason: str) -> bool:
        """Return a terminal allocation to its level exactly once."""

        if not str(allocation_id).strip() or not str(reason).strip():
            raise ValueError("allocation_id and reason are required")
        with self.connection_factory(readonly=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM quant.simulator_liquidity_allocations
                    WHERE allocation_id=%s FOR UPDATE
                    """,
                    (str(allocation_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"unknown liquidity allocation: {allocation_id}")
                status = AllocationStatus(str(row["status"]))
                if status is AllocationStatus.RELEASED:
                    return False
                allocated = Decimal(row["allocated_size"])
                if status is AllocationStatus.ALLOCATED and allocated > 0:
                    cur.execute(
                        """
                        UPDATE quant.simulator_liquidity_levels
                        SET released_size=released_size+%s,
                            updated_at=clock_timestamp()
                        WHERE overlay_version=%s AND venue=%s AND asset_id=%s
                          AND book_generation=%s AND arrival_window_id=%s
                          AND side=%s AND price_tick=%s
                        """,
                        (
                            allocated,
                            row["overlay_version"],
                            row["venue"],
                            row["asset_id"],
                            row["book_generation"],
                            row["arrival_window_id"],
                            row["side"],
                            row["price_tick"],
                        ),
                    )
                    if int(cur.rowcount or 0) != 1:
                        raise RuntimeError("liquidity level missing during release")
                cur.execute(
                    """
                    UPDATE quant.simulator_liquidity_allocations
                    SET status=%s,reason=%s
                    WHERE allocation_id=%s
                    """,
                    (
                        AllocationStatus.RELEASED.value,
                        f"released:{reason}",
                        str(allocation_id),
                    ),
                )
                return True

    def _persist_result(self, cursor: Any, request: AllocationRequest, result: AllocationResult) -> AllocationResult:
        cursor.execute(
            """
            INSERT INTO quant.simulator_liquidity_allocations (
                allocation_id, overlay_version, venue, asset_id, book_generation, arrival_window_id,
                side, price_tick, strategy_id, source_event_id,
                arrival_ts_ns, strategy_priority, account_id, deterministic_order_id,
                status, allocated_size, remaining_size, reason
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (allocation_id) DO NOTHING
            RETURNING allocation_id
            """,
            (
                result.allocation_id,
                *_key_values(result.level, request.overlay_version),
                request.strategy_id,
                request.source_event_id,
                request.order.arrival_ts_ns,
                request.order.strategy_priority,
                request.order.account_id,
                request.order.deterministic_order_id,
                result.status.value,
                result.allocated_size,
                result.remaining_size,
                result.reason,
            ),
        )
        if cursor.fetchone() is not None:
            return result
        existing = self._existing(cursor, request)
        if existing is None:
            raise RuntimeError("allocation insert conflicted but no durable row was found")
        return existing

    @staticmethod
    def _existing(cursor: Any, request: AllocationRequest) -> AllocationResult | None:
        cursor.execute("SELECT * FROM quant.simulator_liquidity_allocations WHERE allocation_id=%s", (request.allocation_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        _assert_same_request(row, request)
        key = LiquidityLevelKey(
            str(row["venue"]),
            str(row["asset_id"]),
            int(row["book_generation"]),
            str(row["arrival_window_id"]),
            str(row["side"]),
            Decimal(row["price_tick"]),
        )
        return AllocationResult(
            str(row["allocation_id"]),
            AllocationStatus(str(row["status"])),
            Decimal(row["allocated_size"]),
            Decimal(row["remaining_size"]),
            str(row["reason"]),
            key,
        )


def _key_values(key: LiquidityLevelKey, version: str) -> tuple[object, ...]:
    return (version, key.venue, key.asset_id, key.book_generation, key.arrival_window_id, key.side, key.price_tick)


def _remaining(row: Any) -> Decimal:
    return max(Decimal("0"), Decimal(row["displayed_size"]) - Decimal(row["reserved_size"]) - Decimal(row["consumed_size"]) + Decimal(row["released_size"]))


def _order_from_row(row: Any) -> AllocationOrder | None:
    if row["last_arrival_ts_ns"] is None:
        return None
    return AllocationOrder(
        int(row["last_arrival_ts_ns"]),
        int(row["last_strategy_priority"]),
        str(row["last_account_id"]),
        str(row["last_deterministic_order_id"]),
    )


def _level_sort_key(key: LiquidityLevelKey) -> tuple[str, str, int, str, str, Decimal]:
    return (key.venue, key.asset_id, key.book_generation, key.arrival_window_id, key.side, key.price_tick)


def _assert_same_request(row: Any, request: AllocationRequest) -> None:
    expected = {
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
        "deterministic_order_id": request.order.deterministic_order_id,
    }
    for field, value in expected.items():
        actual = row[field]
        if field == "price_tick":
            matches = Decimal(actual) == Decimal(value)
        else:
            matches = actual == value
        if not matches:
            raise ValueError(f"allocation id collision: stored {field} does not match request")
