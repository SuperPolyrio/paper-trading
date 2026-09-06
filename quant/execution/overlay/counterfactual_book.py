"""Deterministic displayed-depth consumption for exogenous L2 replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from threading import RLock
from typing import Any


class LevelRefreshPolicy(str, Enum):
    RESET = "RESET"
    PRESERVE_CONSUMED = "PRESERVE_CONSUMED"
    DECAY_CONSUMED = "DECAY_CONSUMED"


@dataclass
class CounterfactualLevel:
    asset_id: str
    book_generation: int
    price_tick: Decimal
    side: str
    displayed_size: Decimal
    consumed_size: Decimal = Decimal("0")
    refreshed_at_event_id: str = ""

    @property
    def available_size(self) -> Decimal:
        return max(Decimal("0"), self.displayed_size - self.consumed_size)


class CounterfactualLiquidityOverlay:
    """A process-global depth ledger with deterministic intent allocation."""

    def __init__(
        self,
        *,
        refresh_policy: LevelRefreshPolicy = LevelRefreshPolicy.RESET,
        decay_fraction: Decimal = Decimal("0.5"),
    ) -> None:
        self.refresh_policy = refresh_policy
        self.decay_fraction = min(Decimal("1"), max(Decimal("0"), decay_fraction))
        self._levels: dict[tuple[str, int, str, Decimal], CounterfactualLevel] = {}
        self._allocations: dict[tuple[int, str, int, str, Decimal], Decimal] = {}
        self._last_intent_sequence = -1
        self._lock = RLock()

    def available(
        self,
        *,
        asset_id: str,
        book_generation: int,
        side: str,
        price_tick: Decimal,
        displayed_size: Decimal,
        event_id: str,
    ) -> Decimal:
        with self._lock:
            level = self._refresh(
                asset_id=asset_id,
                book_generation=book_generation,
                side=side,
                price_tick=price_tick,
                displayed_size=displayed_size,
                event_id=event_id,
            )
            return level.available_size

    def consume(
        self,
        *,
        intent_sequence: int,
        asset_id: str,
        book_generation: int,
        side: str,
        price_tick: Decimal,
        displayed_size: Decimal,
        requested_size: Decimal,
        event_id: str,
        strategy_id: str = "",
        account_id: str = "",
        arrival_ts_ns: int = 0,
        deterministic_order_id: str = "",
        strategy_priority: int = 0,
    ) -> Decimal:
        if requested_size <= 0:
            return Decimal("0")
        allocation_key = (
            int(intent_sequence),
            str(asset_id),
            int(book_generation),
            str(side).upper(),
            Decimal(price_tick),
        )
        with self._lock:
            if allocation_key in self._allocations:
                return self._allocations[allocation_key]
            if int(intent_sequence) < self._last_intent_sequence:
                raise ValueError("intent_sequence must be globally nondecreasing")
            level = self._refresh(
                asset_id=asset_id,
                book_generation=book_generation,
                side=side,
                price_tick=price_tick,
                displayed_size=displayed_size,
                event_id=event_id,
            )
            consumed = min(max(Decimal("0"), requested_size), level.available_size)
            level.consumed_size += consumed
            self._allocations[allocation_key] = consumed
            self._last_intent_sequence = max(
                self._last_intent_sequence, int(intent_sequence)
            )
            return consumed

    def on_level_update(
        self,
        *,
        asset_id: str,
        book_generation: int,
        side: str,
        price_tick: Decimal,
        displayed_size: Decimal,
        event_id: str,
    ) -> CounterfactualLevel:
        with self._lock:
            return self._refresh(
                asset_id=asset_id,
                book_generation=book_generation,
                side=side,
                price_tick=price_tick,
                displayed_size=displayed_size,
                event_id=event_id,
                explicit_refresh=True,
            )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    **asdict(level),
                    "price_tick": format(level.price_tick, "f"),
                    "displayed_size": format(level.displayed_size, "f"),
                    "consumed_size": format(level.consumed_size, "f"),
                    "available_size": format(level.available_size, "f"),
                }
                for _, level in sorted(self._levels.items(), key=lambda item: item[0])
            ]

    def _refresh(
        self,
        *,
        asset_id: str,
        book_generation: int,
        side: str,
        price_tick: Decimal,
        displayed_size: Decimal,
        event_id: str,
        explicit_refresh: bool = False,
    ) -> CounterfactualLevel:
        key = (
            str(asset_id),
            int(book_generation),
            str(side).upper(),
            Decimal(price_tick),
        )
        level = self._levels.get(key)
        if level is None:
            level = CounterfactualLevel(
                asset_id=key[0],
                book_generation=key[1],
                side=key[2],
                price_tick=key[3],
                displayed_size=max(Decimal("0"), Decimal(displayed_size)),
                refreshed_at_event_id=str(event_id),
            )
            self._levels[key] = level
            return level
        displayed_changed = level.displayed_size != max(
            Decimal("0"), Decimal(displayed_size)
        )
        if explicit_refresh or displayed_changed:
            if self.refresh_policy == LevelRefreshPolicy.RESET:
                level.consumed_size = Decimal("0")
            elif self.refresh_policy == LevelRefreshPolicy.DECAY_CONSUMED:
                level.consumed_size *= self.decay_fraction
        level.displayed_size = max(Decimal("0"), Decimal(displayed_size))
        level.refreshed_at_event_id = str(event_id)
        return level
