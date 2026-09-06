"""Realtime local order book service core.

This module intentionally does not own websocket networking. A thin websocket
wrapper can feed raw Polymarket messages here; this core owns selection,
state-machine application, stale/reset behavior, and sampled snapshot output.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from .local_book import (
    BookMetrics,
    LocalOrderBook,
    OrderBookFrameInvalid,
    OrderBookIdentityMismatch,
    OrderBookNotReady,
    OrderBookOutOfOrder,
    TokenBookIdentity,
)
from .polymarket_adapter import (
    NormalizedBookEvent,
    normalize_polymarket_event,
    normalize_rest_book,
)
from .registry import OrderBookRegistry
from .sinks import build_postgres_snapshot_row
from .storage_policy import SnapshotPersistenceState, should_persist_snapshot
from .subscriptions import (
    MarketSubscriptionCandidate,
    SubscriptionDecision,
    select_subscription_tokens,
)


@dataclass(frozen=True)
class RealtimeBookTarget:
    identity: TokenBookIdentity
    decision: SubscriptionDecision

    @property
    def token_id(self) -> str:
        return self.identity.token_id

    @property
    def focused(self) -> bool:
        return bool(self.decision.candidate.focused_by_user)

    @property
    def strategy_monitored(self) -> bool:
        return bool(self.decision.candidate.active_strategy_target)


@dataclass(frozen=True)
class RealtimeBookOutput:
    token_id: str
    event_type: str
    applied: bool
    persist: bool
    reason: str
    storage_tier: str | None = None
    snapshot_row: dict[str, Any] | None = None
    warning: str = ""


class RealtimeOrderBookService:
    """Maintain selected live books and emit sampled DB-ready snapshots."""

    def __init__(
        self,
        targets: Iterable[RealtimeBookTarget],
        *,
        depth_levels: int = 10,
        sample_interval_ms: int = 5_000,
    ) -> None:
        self.targets = {target.token_id: target for target in targets}
        self.depth_levels = max(1, int(depth_levels))
        self.sample_interval_ms = max(1, int(sample_interval_ms))
        self.registry = OrderBookRegistry()
        self.persistence_state: dict[str, SnapshotPersistenceState | None] = {
            token_id: None for token_id in self.targets
        }

    @classmethod
    def from_candidates(
        cls,
        candidates: Iterable[MarketSubscriptionCandidate],
        *,
        max_tokens: int,
        depth_levels: int = 10,
        sample_interval_ms: int = 5_000,
        shard_id: int | None = None,
        shard_count: int | None = None,
    ) -> RealtimeOrderBookService:
        decisions = select_subscription_tokens(
            candidates,
            max_tokens=max_tokens,
            shard_id=shard_id,
            shard_count=shard_count,
        )
        targets = [
            RealtimeBookTarget(
                identity=TokenBookIdentity(
                    token_id=decision.candidate.token_id,
                    market_id=decision.candidate.market_id,
                    condition_id=decision.candidate.condition_id or "",
                    outcome=decision.candidate.token_side,
                    outcome_index=decision.candidate.outcome_index,
                    market_slug=decision.candidate.market_slug,
                ),
                decision=decision,
            )
            for decision in decisions
        ]
        return cls(targets, depth_levels=depth_levels, sample_interval_ms=sample_interval_ms)

    @property
    def subscribed_token_ids(self) -> list[str]:
        return sorted(self.targets)

    def replace_targets(self, targets: Iterable[RealtimeBookTarget]) -> tuple[list[str], list[str]]:
        """Apply a desired subscription universe and return added/removed token ids."""

        next_targets = {target.token_id: target for target in targets}
        current = set(self.targets)
        incoming = set(next_targets)
        added = sorted(incoming - current)
        removed = sorted(current - incoming)
        self.targets = next_targets
        for token_id in added:
            self.persistence_state[token_id] = None
        for token_id in removed:
            self.persistence_state.pop(token_id, None)
            self.registry.books.pop(token_id, None)
        return added, removed

    def add_targets(self, targets: Iterable[RealtimeBookTarget]) -> list[str]:
        added: list[str] = []
        for target in targets:
            if target.token_id in self.targets:
                self.targets[target.token_id] = target
                continue
            self.targets[target.token_id] = target
            self.persistence_state[target.token_id] = None
            added.append(target.token_id)
        return sorted(added)

    def remove_targets(self, token_ids: Iterable[str]) -> list[str]:
        removed: list[str] = []
        for token_id in sorted({str(token_id) for token_id in token_ids}):
            if token_id not in self.targets:
                continue
            self.targets.pop(token_id, None)
            self.persistence_state.pop(token_id, None)
            self.registry.books.pop(token_id, None)
            removed.append(token_id)
        return removed

    def get_book(self, token_id: str) -> LocalOrderBook | None:
        return self.registry.get(str(token_id))

    def mark_reconnect_stale(self, reason: str = "websocket_reconnect") -> None:
        for book in self.registry.books.values():
            book.mark_gap(reason, gap_id=reason)
        self.persistence_state = {token_id: None for token_id in self.targets}

    def mark_connection_gap(
        self,
        token_ids: Iterable[str] | None = None,
        *,
        reason: str = "connection_gap",
        gap_id: str | None = None,
        received_ts_ms: int | None = None,
        connection_id: str | None = None,
        connection_generation: int | None = None,
    ) -> list[str]:
        """Invalidate affected books without stopping acquisition for other tokens."""

        selected = (
            self.subscribed_token_ids if token_ids is None else sorted({str(item) for item in token_ids})
        )
        marked: list[str] = []
        for token_id in selected:
            book = self.registry.get(token_id)
            if book is None:
                continue
            book.mark_gap(
                reason,
                gap_id=gap_id,
                received_ts_ms=received_ts_ms,
                connection_id=connection_id,
                connection_generation=connection_generation,
            )
            self.persistence_state[token_id] = None
            marked.append(token_id)
        return marked

    def mark_connection_heartbeat(
        self,
        token_ids: Iterable[str] | None = None,
        *,
        received_ts_ms: int,
        connection_id: str | None = None,
        connection_generation: int | None = None,
    ) -> list[str]:
        """Refresh transport coverage for quiet books without changing book clocks."""

        selected = (
            self.subscribed_token_ids if token_ids is None else sorted({str(item) for item in token_ids})
        )
        covered: list[str] = []
        for token_id in selected:
            book = self.registry.get(token_id)
            if book is None:
                continue
            if book.mark_connection_heartbeat(
                received_ts_ms=int(received_ts_ms),
                connection_id=connection_id,
                connection_generation=connection_generation,
            ):
                covered.append(token_id)
        return covered

    def reset_tokens(
        self,
        token_ids: Iterable[str],
        *,
        reason: str = "market_clarification",
    ) -> list[str]:
        """Clear selected books and require a fresh snapshot before deltas apply."""

        reset: list[str] = []
        for token_id in sorted({str(token_id) for token_id in token_ids}):
            book = self.registry.get(token_id)
            if book is None:
                continue
            book.reset(reason)
            self.persistence_state[token_id] = None
            reset.append(token_id)
        return reset

    def process_polymarket_message(
        self,
        message: Mapping[str, Any],
        *,
        received_ts_ms: int | None = None,
    ) -> list[RealtimeBookOutput]:
        """Normalize first, then atomically apply each token's source-frame group."""

        raw = dict(message)
        received = int(received_ts_ms if received_ts_ms is not None else time.time() * 1000)
        try:
            events = normalize_polymarket_event(
                raw,
                received_ts_ms=received,
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            token_ids = _raw_message_token_ids(raw)
            self.mark_connection_gap(
                token_ids,
                reason="invalid_source_frame",
                gap_id=str(raw.get("_raw_group_id") or "invalid_source_frame"),
                received_ts_ms=received,
                connection_id=str(raw.get("_raw_connection_id") or "") or None,
                connection_generation=_int_or_none(raw.get("_raw_connection_generation")),
            )
            return [
                RealtimeBookOutput(
                    token_id=token_ids[0] if token_ids else "",
                    event_type=str(raw.get("event_type") or "unknown"),
                    applied=False,
                    persist=False,
                    reason="invalid_frame",
                    warning=str(exc),
                )
            ]
        groups: dict[str, list[NormalizedBookEvent]] = {}
        for event in events:
            groups.setdefault(event.token_id, []).append(event)
        return [
            self.process_frame(tuple(frame))
            for frame in groups.values()
        ]

    def process_rest_book(
        self,
        *,
        token_id: str,
        payload: Mapping[str, Any],
        event_ts_ms: int | None = None,
        received_ts_ms: int | None = None,
    ) -> RealtimeBookOutput:
        return self.process_event(
            normalize_rest_book(
                token_id,
                dict(payload),
                event_ts_ms=event_ts_ms,
                received_ts_ms=received_ts_ms,
            )
        )

    def process_frame(
        self,
        events: tuple[NormalizedBookEvent, ...],
    ) -> RealtimeBookOutput:
        if not events:
            return RealtimeBookOutput(
                token_id="",
                event_type="unknown",
                applied=False,
                persist=False,
                reason="empty_frame",
            )
        event = events[0]
        target = self.targets.get(event.token_id)
        event_type = _event_type(event)
        if target is None:
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason="not_subscribed",
            )
        try:
            metrics = self.registry.apply_frame(target.identity, events)
        except OrderBookNotReady:
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason="delta_before_snapshot",
                warning="change ignored until a trusted snapshot is available",
            )
        except OrderBookOutOfOrder as exc:
            self.persistence_state[event.token_id] = None
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason="out_of_order",
                warning=str(exc),
            )
        except (OrderBookFrameInvalid, OrderBookIdentityMismatch, ValueError) as exc:
            self.persistence_state[event.token_id] = None
            book = self.registry.get(event.token_id)
            if book is not None and not book.needs_resnapshot:
                book.mark_gap(
                    "invalid_source_frame",
                    gap_id=event.group_id or "invalid_source_frame",
                    received_ts_ms=event.received_ts_ms,
                    connection_id=event.connection_id,
                    connection_generation=event.connection_generation,
                )
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason=(
                    "identity_mismatch"
                    if isinstance(exc, OrderBookIdentityMismatch)
                    else "invalid_frame"
                ),
                warning=str(exc),
            )
        return self._project_applied_event(
            event=event,
            event_type=event_type,
            metrics=metrics,
        )

    def process_event(self, event: NormalizedBookEvent) -> RealtimeBookOutput:
        target = self.targets.get(event.token_id)
        event_type = _event_type(event)
        if target is None:
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason="not_subscribed",
            )
        try:
            metrics = self.registry.apply(target.identity, event)
        except OrderBookNotReady:
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason="delta_before_snapshot",
                warning="change ignored until a trusted snapshot is available",
            )
        except OrderBookOutOfOrder as exc:
            self.persistence_state[event.token_id] = None
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason="out_of_order",
                warning=str(exc),
            )
        except (OrderBookFrameInvalid, OrderBookIdentityMismatch, ValueError) as exc:
            self.persistence_state[event.token_id] = None
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=False,
                persist=False,
                reason=(
                    "identity_mismatch"
                    if isinstance(exc, OrderBookIdentityMismatch)
                    else "invalid_frame"
                ),
                warning=str(exc),
            )
        return self._project_applied_event(
            event=event,
            event_type=event_type,
            metrics=metrics,
        )

    def _project_applied_event(
        self,
        *,
        event: NormalizedBookEvent,
        event_type: Literal["snapshot", "price_change"],
        metrics: BookMetrics,
    ) -> RealtimeBookOutput:
        target = self.targets[event.token_id]
        decision = should_persist_snapshot(
            metrics,
            event_type=event_type,
            previous=self.persistence_state.get(event.token_id),
            now_ms=(event.received_ts_ms or event.event_ts_ms),
            focused=target.focused,
            strategy_monitored=target.strategy_monitored,
            sample_interval_ms=self.sample_interval_ms,
        )
        if not decision.persist:
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=True,
                persist=False,
                reason=decision.reason,
                storage_tier=decision.storage_tier,
            )
        book = self.registry.get(event.token_id)
        if book is None:
            return RealtimeBookOutput(
                token_id=event.token_id,
                event_type=event_type,
                applied=True,
                persist=False,
                reason="book_missing_after_apply",
            )
        row = self.snapshot_row(book, event_type=event_type, storage_tier=decision.storage_tier)
        self.persistence_state[event.token_id] = decision.state
        return RealtimeBookOutput(
            token_id=event.token_id,
            event_type=event_type,
            applied=True,
            persist=True,
            reason=decision.reason,
            storage_tier=decision.storage_tier,
            snapshot_row=row,
        )

    def snapshot_row(self, book: LocalOrderBook, *, event_type: str, storage_tier: str) -> dict[str, Any]:
        return build_postgres_snapshot_row(
            book,
            event_type=event_type,
            storage_tier=storage_tier,
            depth_levels=self.depth_levels,
        )


def _event_type(event: NormalizedBookEvent) -> Literal["snapshot", "price_change"]:
    return cast(
        Literal["snapshot", "price_change"],
        "snapshot" if event.__class__.__name__.endswith("Snapshot") else "price_change",
    )


def _raw_message_token_ids(message: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    direct = str(message.get("asset_id") or "").strip()
    if direct:
        values.append(direct)
    changes = message.get("price_changes")
    if isinstance(changes, list):
        for item in changes:
            if not isinstance(item, Mapping):
                continue
            token_id = str(item.get("asset_id") or "").strip()
            if token_id and token_id not in values:
                values.append(token_id)
    return values


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None
