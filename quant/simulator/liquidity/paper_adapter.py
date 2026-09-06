"""Paper-engine adapters for durable global counterfactual liquidity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from quant.execution.overlay.counterfactual_book import CounterfactualLiquidityOverlay
from quant.simulator.kernel.deterministic_id import deterministic_id

from .allocation import (
    AllocationOrder,
    AllocationRequest,
    AllocationResult,
    AllocationStatus,
    LiquidityLevelKey,
)
from .overlay_store import PostgresLiquidityOverlayStore


@dataclass(frozen=True)
class LiquidityAllocationComparison:
    allocation_id: str
    deterministic_order_id: str
    baseline_size: Decimal
    durable_size: Decimal
    agreement: bool
    durable_status: str
    durable_reason: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("baseline_size", "durable_size"):
            payload[key] = format(payload[key], "f")
        return payload


class DurablePaperLiquidityOverlay:
    """Translate paper fill requests into durable terminal allocations."""

    def __init__(
        self,
        *,
        store: PostgresLiquidityOverlayStore,
        overlay_version: str,
        venue: str = "POLYMARKET_CLOB",
        arrival_window_ns: int = 1_000_000,
    ) -> None:
        if not str(overlay_version).strip() or int(arrival_window_ns) <= 0:
            raise ValueError("overlay_version and positive arrival_window_ns are required")
        self.store = store
        self.overlay_version = str(overlay_version)
        self.venue = str(venue)
        self.arrival_window_ns = int(arrival_window_ns)
        self._results: dict[str, AllocationResult] = {}
        self._order_allocations: dict[str, list[str]] = {}
        self._last_result: AllocationResult | None = None

    def available(self, **kwargs: Any) -> Decimal:
        # Transactional allocation is authoritative. This is only a candidate
        # bound and never mutates the source BookState.
        return max(Decimal("0"), Decimal(kwargs["displayed_size"]))

    def on_level_update(self, **_kwargs: Any) -> None:
        # Generations and source event IDs are carried by every allocation.
        return None

    def consume(
        self,
        *,
        asset_id: str,
        book_generation: int,
        side: str,
        price_tick: Decimal,
        displayed_size: Decimal,
        requested_size: Decimal,
        event_id: str,
        strategy_id: str,
        account_id: str,
        arrival_ts_ns: int,
        deterministic_order_id: str,
        strategy_priority: int = 0,
        intent_sequence: int = 0,
    ) -> Decimal:
        if not all(
            str(value).strip()
            for value in (strategy_id, account_id, deterministic_order_id)
        ):
            raise ValueError("durable liquidity allocation requires paper order identity")
        level = LiquidityLevelKey(
            venue=self.venue,
            asset_id=str(asset_id),
            book_generation=int(book_generation),
            arrival_window_id=self._arrival_window(arrival_ts_ns),
            side=str(side),
            price_tick=Decimal(price_tick),
        )
        allocation_id = deterministic_id(
            "paper-liquidity-allocation",
            {
                "overlay_version": self.overlay_version,
                "level": {
                    "venue": level.venue,
                    "asset_id": level.asset_id,
                    "book_generation": level.book_generation,
                    "arrival_window_id": level.arrival_window_id,
                    "side": level.side,
                    "price_tick": format(level.price_tick, "f"),
                },
                "strategy_id": strategy_id,
                "account_id": account_id,
                "arrival_ts_ns": int(arrival_ts_ns),
                "deterministic_order_id": deterministic_order_id,
                "intent_sequence": int(intent_sequence),
            },
        )
        request = AllocationRequest(
            allocation_id=allocation_id,
            order=AllocationOrder(
                arrival_ts_ns=int(arrival_ts_ns),
                strategy_priority=int(strategy_priority),
                account_id=str(account_id),
                deterministic_order_id=str(deterministic_order_id),
            ),
            strategy_id=str(strategy_id),
            level=level,
            displayed_size=Decimal(displayed_size),
            requested_size=Decimal(requested_size),
            source_event_id=str(event_id),
            overlay_version=self.overlay_version,
        )
        result = self.store.allocate(request)
        self._results[allocation_id] = result
        self._last_result = result
        allocations = self._order_allocations.setdefault(
            str(deterministic_order_id), []
        )
        if allocation_id not in allocations:
            allocations.append(allocation_id)
        return (
            result.allocated_size
            if result.status is AllocationStatus.ALLOCATED
            else Decimal("0")
        )

    def release_order(self, deterministic_order_id: str, *, reason: str) -> int:
        released = 0
        for allocation_id in self._order_allocations.get(
            str(deterministic_order_id), ()
        ):
            released += int(self.store.release(allocation_id, reason=reason))
        return released

    def allocation_results(self) -> tuple[AllocationResult, ...]:
        return tuple(self._results[key] for key in sorted(self._results))

    @property
    def last_result(self) -> AllocationResult:
        if self._last_result is None:
            raise RuntimeError("no durable liquidity allocation has completed")
        return self._last_result

    def _arrival_window(self, arrival_ts_ns: int) -> str:
        return str(int(arrival_ts_ns) // self.arrival_window_ns)


class ShadowComparingPaperLiquidityOverlay:
    """Record durable allocations while preserving the current paper result."""

    def __init__(
        self,
        durable: DurablePaperLiquidityOverlay,
        *,
        baseline: CounterfactualLiquidityOverlay | None = None,
    ) -> None:
        self.durable = durable
        self.baseline = baseline or CounterfactualLiquidityOverlay()
        self.comparisons: list[LiquidityAllocationComparison] = []

    def available(self, **kwargs: Any) -> Decimal:
        return self.baseline.available(**kwargs)

    def on_level_update(self, **kwargs: Any) -> Any:
        return self.baseline.on_level_update(**kwargs)

    def consume(self, **kwargs: Any) -> Decimal:
        baseline_size = self.baseline.consume(**kwargs)
        durable_size = self.durable.consume(**kwargs)
        result = self.durable.last_result
        self.comparisons.append(
            LiquidityAllocationComparison(
                allocation_id=result.allocation_id,
                deterministic_order_id=str(kwargs["deterministic_order_id"]),
                baseline_size=baseline_size,
                durable_size=durable_size,
                agreement=baseline_size == durable_size,
                durable_status=result.status.value,
                durable_reason=result.reason,
            )
        )
        return baseline_size
