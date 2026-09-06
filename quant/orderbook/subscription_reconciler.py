"""Reconcile registry subscription universe into live order book targets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from quant.market.repository import MarketRegistryRepository, RegistryOutboxPublishSummary

from .local_book import TokenBookIdentity
from .service import RealtimeBookTarget, RealtimeOrderBookService
from .subscriptions import MarketSubscriptionCandidate, SubscriptionDecision, SubscriptionTier, token_shard


@dataclass(frozen=True)
class DesiredSubscription:
    asset_id: str
    market_id: int
    condition_id: str
    market_slug: str | None
    market_title: str | None
    outcome_name: str
    outcome_index: int
    market_state: str | None = None
    execution_eligible: bool = False
    probe_book_status: str | None = None
    probe_book_quality: str | None = None
    last_outbox_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SubscriptionReconcileResult:
    desired_count: int
    total_desired_count: int | None = None
    shard_id: int | None = None
    shard_count: int | None = None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged_count: int = 0
    outbox: RegistryOutboxPublishSummary | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "desired_count": self.desired_count,
            "total_desired_count": self.total_desired_count,
            "shard_id": self.shard_id,
            "shard_count": self.shard_count,
            "added": self.added,
            "removed": self.removed,
            "unchanged_count": self.unchanged_count,
            "outbox": self.outbox.as_meta() if self.outbox else None,
        }


class SubscriptionReconciler:
    """Load desired subscriptions and apply add/remove diffs to a service."""

    def __init__(
        self,
        *,
        max_tokens: int = 1_000,
        outbox_limit: int = 5_000,
        shard_id: int | None = None,
        shard_count: int | None = None,
    ) -> None:
        self.max_tokens = max(0, int(max_tokens))
        self.outbox_limit = max(1, int(outbox_limit))
        self.shard_id = shard_id
        self.shard_count = shard_count
        _validate_shard_args(shard_id=shard_id, shard_count=shard_count)

    def publish_pending_outbox(self, conn: Any) -> RegistryOutboxPublishSummary:
        return MarketRegistryRepository(conn).publish_pending_outbox(limit=self.outbox_limit)

    def load_desired(self, conn: Any) -> list[DesiredSubscription]:
        return load_desired_subscriptions(
            conn,
            limit=self.max_tokens,
            shard_id=self.shard_id,
            shard_count=self.shard_count,
        )

    def build_targets(self, desired: Iterable[DesiredSubscription]) -> list[RealtimeBookTarget]:
        return build_realtime_targets_from_desired(desired)

    def load_stale_actual_asset_ids(self, conn: Any) -> list[str]:
        return load_stale_actual_subscriptions(
            conn,
            shard_id=self.shard_id,
            shard_count=self.shard_count,
        )

    def reconcile(
        self,
        conn: Any,
        service: RealtimeOrderBookService,
        *,
        consume_outbox: bool = True,
    ) -> SubscriptionReconcileResult:
        outbox = self.publish_pending_outbox(conn) if consume_outbox else None
        desired = self.load_desired(conn)
        targets = self.build_targets(desired)
        added, removed = service.replace_targets(targets)
        return SubscriptionReconcileResult(
            desired_count=len(desired),
            total_desired_count=getattr(desired, "total_count", None),
            shard_id=self.shard_id,
            shard_count=self.shard_count,
            added=added,
            removed=removed,
            unchanged_count=max(0, len(targets) - len(added)),
            outbox=outbox,
        )


def load_desired_subscriptions(
    conn: Any,
    *,
    limit: int = 1_000,
    shard_id: int | None = None,
    shard_count: int | None = None,
) -> list[DesiredSubscription]:
    _validate_shard_args(shard_id=shard_id, shard_count=shard_count)
    query_limit = max(0, int(limit))
    # Filter in PostgreSQL so every collector fetches only its own partition.
    # The same first 12 SHA256 hex digits are used by subscriptions.token_shard.
    shard_sql = ""
    query_params: list[Any] = []
    if shard_id is not None and shard_count is not None:
        shard_sql = "AND mod(('x' || substr(encode(digest(t.asset_id, 'sha256'), 'hex'), 1, 12))::bit(48)::bigint, %s) = %s"
        query_params.extend((int(shard_count), int(shard_id)))
    limit_sql = "" if query_limit <= 0 or shard_count is not None else "LIMIT %s"
    if limit_sql:
        query_params.append(query_limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                t.asset_id,
                COALESCE(r.market_id, t.market_id, 0) AS market_id,
                COALESCE(r.condition_id, t.condition_id, '') AS condition_id,
                r.market_slug,
                r.market_title,
                COALESCE(r.outcome_name, 'UNKNOWN') AS outcome_name,
                COALESCE(r.outcome_index, 0) AS outcome_index,
                CASE
                    WHEN COALESCE(r.completion_status, '') = 'OPEN'
                     AND COALESCE(r.closed, FALSE) = FALSE
                     AND COALESCE(r.resolved, FALSE) = FALSE
                     AND (
                            COALESCE(r.archived, FALSE) = TRUE
                         OR COALESCE(r.market_state, '') IN ('RESOLVED', 'ARCHIVED')
                         )
                    THEN 'TRADABLE_PENDING_BOOK'
                    ELSE r.market_state
                END AS market_state,
                COALESCE(r.raw_metadata #>> '{{probe,book_status}}', r.book_status) AS probe_book_status,
                COALESCE(r.raw_metadata #>> '{{probe,book_quality}}', r.book_quality) AS probe_book_quality,
                (
                    COALESCE(r.execution_eligible, FALSE)
                    AND r.market_state = 'LIVE'
                ) AS execution_eligible,
                t.last_outbox_id,
                t.reason,
                t.updated_at,
                COUNT(*) OVER() AS total_desired_count,
                GREATEST(
                    COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                    COALESCE(r.latest_book_at, 'epoch'::timestamptz)
                ) AS latest_book_at
            FROM quant.paper_lob_subscription_targets t
            LEFT JOIN quant.paper_market_registry_tokens r ON r.asset_id = t.asset_id
            WHERE (
                    t.desired_subscribed = TRUE
                 OR (
                        COALESCE(r.market_state, '') = 'CLOSING'
                    AND GREATEST(
                            COALESCE(r.last_transition_at, 'epoch'::timestamptz),
                            COALESCE(r.latest_book_at, 'epoch'::timestamptz),
                            COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                            COALESCE(t.last_l2_event_at, 'epoch'::timestamptz)
                        ) >= now() - INTERVAL '48 hours'
                    )
                 OR (
                        COALESCE(r.active, TRUE) = TRUE
                    AND COALESCE(r.completion_status, '') = 'OPEN'
                    AND COALESCE(r.closed, FALSE) = FALSE
                    AND COALESCE(r.resolved, FALSE) = FALSE
                    AND (
                            COALESCE(r.archived, FALSE) = TRUE
                         OR COALESCE(r.market_state, '') IN ('RESOLVED', 'ARCHIVED')
                        )
                    )
                  )
              AND COALESCE(r.active, TRUE) = TRUE
              AND (
                    COALESCE(r.closed, FALSE) = FALSE
                 OR (
                        COALESCE(r.market_state, '') = 'CLOSING'
                    AND GREATEST(
                            COALESCE(r.last_transition_at, 'epoch'::timestamptz),
                            COALESCE(r.latest_book_at, 'epoch'::timestamptz),
                            COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                            COALESCE(t.last_l2_event_at, 'epoch'::timestamptz)
                        ) >= now() - INTERVAL '48 hours'
                    )
                  )
              AND COALESCE(r.resolved, FALSE) = FALSE
              AND (
                    COALESCE(r.archived, FALSE) = FALSE
                 OR (
                        COALESCE(r.market_state, '') = 'CLOSING'
                    AND GREATEST(
                            COALESCE(r.last_transition_at, 'epoch'::timestamptz),
                            COALESCE(r.latest_book_at, 'epoch'::timestamptz),
                            COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                            COALESCE(t.last_l2_event_at, 'epoch'::timestamptz)
                        ) >= now() - INTERVAL '48 hours'
                    )
                 OR (
                        COALESCE(r.completion_status, '') = 'OPEN'
                    AND COALESCE(r.closed, FALSE) = FALSE
                    AND COALESCE(r.resolved, FALSE) = FALSE
                    )
                  )
              AND COALESCE(r.deprecated, FALSE) = FALSE
              AND (
                    COALESCE(r.market_state, 'TRADABLE_PENDING_BOOK') NOT IN ('RESOLVED', 'ARCHIVED')
                 OR (
                        COALESCE(r.completion_status, '') = 'OPEN'
                    AND COALESCE(r.closed, FALSE) = FALSE
                    AND COALESCE(r.resolved, FALSE) = FALSE
                    )
                  )
              AND t.asset_id IS NOT NULL
              AND t.asset_id <> ''
              {shard_sql}
            ORDER BY
                (
                    COALESCE(r.execution_eligible, FALSE)
                    AND r.market_state = 'LIVE'
                ) DESC,
                CASE COALESCE(r.market_state, '')
                    WHEN 'LIVE' THEN 0
                    WHEN 'TRADABLE_PENDING_BOOK' THEN 1
                    WHEN 'DISCOVERED_PENDING_BOOK' THEN 2
                    ELSE 3
                END,
                GREATEST(
                    COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                    COALESCE(r.latest_book_at, 'epoch'::timestamptz)
                ) DESC,
                t.updated_at DESC,
                t.asset_id
            {limit_sql}
            """,
            tuple(query_params),
        )
        rows = [dict(row) for row in cur.fetchall()]
    desired = [_desired_from_row(row) for row in rows]
    total_count = int(rows[0].get("total_desired_count") or len(desired)) if rows else 0
    if shard_id is None or shard_count is None:
        return _DesiredSubscriptionList(desired, total_count=total_count)
    # Retain a defensive check for custom DB adapters and test doubles.
    sharded = [item for item in desired if token_shard(item.asset_id, shard_count=int(shard_count)) == int(shard_id)]
    if query_limit > 0:
        sharded = sharded[:query_limit]
    return _DesiredSubscriptionList(sharded, total_count=total_count)


def load_stale_actual_subscriptions(
    conn: Any,
    *,
    shard_id: int | None = None,
    shard_count: int | None = None,
) -> list[str]:
    """Return actual subscriptions that should be cleared by this collector.

    In sharded mode each process may only clean rows assigned to its own shard;
    otherwise one shard can incorrectly mark another shard's live subscriptions
    inactive. The hash is intentionally the same Python token_shard used for
    selection so cleanup and ownership stay aligned.
    """

    _validate_shard_args(shard_id=shard_id, shard_count=shard_count)
    shard_sql = ""
    query_params: list[Any] = []
    if shard_id is not None and shard_count is not None:
        shard_sql = "AND mod(('x' || substr(encode(digest(t.asset_id, 'sha256'), 'hex'), 1, 12))::bit(48)::bigint, %s) = %s"
        query_params.extend((int(shard_count), int(shard_id)))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.asset_id
            FROM quant.paper_lob_subscription_targets t
            LEFT JOIN quant.paper_market_registry_tokens r ON r.asset_id = t.asset_id
            WHERE t.actual_subscribed = TRUE
              AND (
                    (
                        COALESCE(t.desired_subscribed, FALSE) = FALSE
                        AND NOT (
                            (
                                COALESCE(r.market_state, '') = 'CLOSING'
                                AND GREATEST(
                                        COALESCE(r.last_transition_at, 'epoch'::timestamptz),
                                        COALESCE(r.latest_book_at, 'epoch'::timestamptz),
                                        COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                                        COALESCE(t.last_l2_event_at, 'epoch'::timestamptz)
                                    ) >= now() - INTERVAL '48 hours'
                            )
                            OR (
                                COALESCE(r.active, TRUE) = TRUE
                                AND COALESCE(r.completion_status, '') = 'OPEN'
                                AND COALESCE(r.closed, FALSE) = FALSE
                                AND COALESCE(r.resolved, FALSE) = FALSE
                            )
                        )
                    )
                 OR COALESCE(r.active, TRUE) = FALSE
                 OR (
                        COALESCE(r.closed, FALSE) = TRUE
                    AND NOT (
                            COALESCE(r.market_state, '') = 'CLOSING'
                        AND GREATEST(
                                COALESCE(r.last_transition_at, 'epoch'::timestamptz),
                                COALESCE(r.latest_book_at, 'epoch'::timestamptz),
                                COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                                COALESCE(t.last_l2_event_at, 'epoch'::timestamptz)
                            ) >= now() - INTERVAL '48 hours'
                        )
                    )
                 OR COALESCE(r.resolved, FALSE) = TRUE
                 OR (
                        COALESCE(r.archived, FALSE) = TRUE
                    AND NOT (
                            (
                                COALESCE(r.market_state, '') = 'CLOSING'
                                AND GREATEST(
                                        COALESCE(r.last_transition_at, 'epoch'::timestamptz),
                                        COALESCE(r.latest_book_at, 'epoch'::timestamptz),
                                        COALESCE(t.last_book_update_at, 'epoch'::timestamptz),
                                        COALESCE(t.last_l2_event_at, 'epoch'::timestamptz)
                                    ) >= now() - INTERVAL '48 hours'
                            )
                            OR (
                                COALESCE(r.active, TRUE) = TRUE
                                AND COALESCE(r.completion_status, '') = 'OPEN'
                                AND COALESCE(r.closed, FALSE) = FALSE
                                AND COALESCE(r.resolved, FALSE) = FALSE
                            )
                        )
                    )
                 OR COALESCE(r.deprecated, FALSE) = TRUE
                 OR (
                        COALESCE(r.market_state, 'TRADABLE_PENDING_BOOK') IN ('RESOLVED', 'ARCHIVED')
                    AND NOT (
                            COALESCE(r.active, TRUE) = TRUE
                        AND COALESCE(r.completion_status, '') = 'OPEN'
                        AND COALESCE(r.closed, FALSE) = FALSE
                        AND COALESCE(r.resolved, FALSE) = FALSE
                        )
                    )
              )
              AND t.asset_id IS NOT NULL
              AND t.asset_id <> ''
              {shard_sql}
            ORDER BY t.asset_id
            """,
            tuple(query_params),
        )
        asset_ids = [str(row["asset_id"]) for row in cur.fetchall()]
    if shard_id is None or shard_count is None:
        return asset_ids
    # Retain a defensive check for custom DB adapters and test doubles.
    return [asset_id for asset_id in asset_ids if token_shard(asset_id, shard_count=int(shard_count)) == int(shard_id)]


class _DesiredSubscriptionList(list[DesiredSubscription]):
    def __init__(self, values: Iterable[DesiredSubscription], *, total_count: int) -> None:
        super().__init__(values)
        self.total_count = int(total_count)


def build_realtime_targets_from_desired(desired: Iterable[DesiredSubscription]) -> list[RealtimeBookTarget]:
    targets: list[RealtimeBookTarget] = []
    seen: set[str] = set()
    for item in desired:
        if item.asset_id in seen:
            continue
        seen.add(item.asset_id)
        candidate = MarketSubscriptionCandidate(
            token_id=item.asset_id,
            market_id=item.market_id,
            condition_id=item.condition_id,
            market_slug=item.market_slug,
            market_title=item.market_title,
            token_side=item.outcome_name,
            outcome_index=item.outcome_index,
            focused_by_user=item.execution_eligible,
            active_strategy_target=False,
        )
        targets.append(
            RealtimeBookTarget(
                identity=TokenBookIdentity(
                    token_id=item.asset_id,
                    market_id=item.market_id,
                    condition_id=item.condition_id,
                    outcome=item.outcome_name,
                    outcome_index=item.outcome_index,
                    market_slug=item.market_slug,
                ),
                decision=SubscriptionDecision(
                    candidate=candidate,
                    score=1_000 if item.execution_eligible else 100,
                    tier=_tier_for_desired(item),
                    reason=item.reason or "registry_desired_subscription",
                ),
            )
        )
    return targets


def _desired_from_row(row: Mapping[str, Any]) -> DesiredSubscription:
    return DesiredSubscription(
        asset_id=str(row.get("asset_id") or "").strip(),
        market_id=int(row.get("market_id") or 0),
        condition_id=str(row.get("condition_id") or ""),
        market_slug=_optional_text(row.get("market_slug")),
        market_title=_optional_text(row.get("market_title")),
        outcome_name=str(row.get("outcome_name") or "UNKNOWN").upper(),
        outcome_index=int(row.get("outcome_index") or 0),
        market_state=_optional_text(row.get("market_state")),
        execution_eligible=bool(row.get("execution_eligible")),
        probe_book_status=_optional_text(row.get("probe_book_status")),
        probe_book_quality=_optional_text(row.get("probe_book_quality")),
        last_outbox_id=_optional_int(row.get("last_outbox_id")),
        reason=_optional_text(row.get("reason")),
    )


def _tier_for_desired(item: DesiredSubscription) -> SubscriptionTier:
    if item.execution_eligible:
        return "P0"
    if item.market_state == "LIVE":
        return "P1"
    return "P2"


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_shard_args(*, shard_id: int | None, shard_count: int | None) -> None:
    if shard_id is None and shard_count is None:
        return
    if shard_id is None or shard_count is None:
        raise ValueError("shard_id and shard_count must be provided together")
    if int(shard_count) <= 0:
        raise ValueError("shard_count must be positive")
    if int(shard_id) < 0 or int(shard_id) >= int(shard_count):
        raise ValueError("shard_id must satisfy 0 <= shard_id < shard_count")
