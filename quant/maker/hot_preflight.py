"""Bounded current-book proof for controlled Maker calibration probes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from quant.adapters.polymarket_market_ws_client import PolymarketMarketWsClient
from quant.orderbook.polymarket_adapter import (
    NormalizedBookSnapshot,
    normalize_polymarket_event,
)


def load_hot_preflight_candidate(
    store: Any,
    *,
    asset_id: str,
    market_id: str,
    max_age_seconds: float,
) -> dict[str, Any] | None:
    """Load one current GCP Paper book without asserting historical continuity."""

    with store.connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                catalog.asset_id,
                catalog.market_id::text AS market_id,
                catalog.condition_id,
                catalog.market_slug,
                catalog.market_title,
                catalog.outcome_name,
                COALESCE(catalog.current_tick_size, 0.001) AS tick_size,
                COALESCE(catalog.min_order_size, 1) AS min_order_size,
                book.best_bid,
                book.best_ask,
                book.coverage_grade,
                book.has_gap,
                GREATEST(
                    0,
                    floor(extract(epoch FROM (
                        statement_timestamp() - book.observed_at
                    )) * 1000)
                )::bigint AS book_age_ms,
                FALSE AS rest_book_match,
                book.source_connection_id AS connection_id,
                'paper-hot'::text AS shard_id,
                book.observed_at AS last_receive_ts,
                book.redundant_feed_match,
                book.bids,
                book.asks,
                0::bigint AS activity_event_count,
                0::bigint AS activity_price_change_count,
                catalog.market_state,
                catalog.execution_eligible,
                catalog.raw_metadata,
                catalog.category AS source_category,
                catalog.event_title,
                catalog.event_volume,
                catalog.end_date,
                catalog.enable_neg_risk AS neg_risk,
                terms.fee_rate,
                terms.fee_exponent,
                COALESCE(terms.fee_taker_only, TRUE) AS fee_taker_only,
                book.book_status,
                book.generation,
                book.book_fingerprint,
                book.transport_state
            FROM quant.paper_execution_market_catalog catalog
            JOIN quant.paper_live_current_books book USING (asset_id)
            JOIN quant.paper_live_watchlist watch USING (asset_id)
            LEFT JOIN quant.paper_market_terms terms USING (asset_id)
            WHERE catalog.asset_id=%s
              AND catalog.market_id::text=%s
              AND catalog.market_state='LIVE'
              AND catalog.execution_eligible=TRUE
              AND catalog.active=TRUE
              AND catalog.closed=FALSE
              AND catalog.resolved=FALSE
              AND catalog.archived=FALSE
              AND catalog.deprecated=FALSE
              AND watch.enabled=TRUE
              AND book.book_status='READY'
              AND book.observed_at >= statement_timestamp()
                  - make_interval(secs => %s)
              AND book.best_bid > 0
              AND book.best_ask < 1
              AND book.best_bid < book.best_ask
            ORDER BY book.observed_at DESC
            LIMIT 1
            """,
            (
                str(asset_id),
                str(market_id),
                max(0.1, float(max_age_seconds)),
            ),
        )
        row = cur.fetchone()
    return dict(row) if row else None


async def capture_targeted_ws_book(
    *,
    asset_id: str,
    proxy_url: str | None,
    timeout_seconds: float,
    activity_observation_seconds: float = 0,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return a native WS baseline plus bounded current activity evidence."""

    ws = client or PolymarketMarketWsClient(
        proxy_url=proxy_url,
        application_ping_interval=None,
        open_timeout=min(15.0, max(1.0, float(timeout_seconds))),
    )
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    baseline: dict[str, Any] | None = None
    activity_deadline: float | None = None
    activity_counts = {
        "price_change": 0,
        "best_bid_ask": 0,
        "last_trade_price": 0,
    }
    activity_hashes: list[str] = []

    def completed() -> dict[str, Any]:
        if baseline is None:
            raise RuntimeError("targeted market WS baseline is unavailable")
        return {
            **baseline,
            "activity_observation_seconds": format(
                max(0.0, float(activity_observation_seconds)), ".3f"
            ),
            "activity_counts": activity_counts,
            "activity_payload_sha256": activity_hashes,
        }
    try:
        await ws.connect()
        await ws.subscribe([str(asset_id)], initial=True, initial_dump=True)
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if baseline is not None and activity_deadline is not None:
                remaining = min(remaining, activity_deadline - now)
            if remaining <= 0:
                if baseline is not None:
                    return completed()
                raise TimeoutError("targeted market WS did not provide a book baseline")
            try:
                messages = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                if baseline is not None:
                    return completed()
                raise
            for message in messages:
                event_type = str(message.get("event_type") or "")
                if event_type in activity_counts:
                    activity_counts[event_type] += 1
                    if len(activity_hashes) < 32:
                        activity_hashes.append(_payload_sha256(message))
                for event in normalize_polymarket_event(message):
                    if not isinstance(event, NormalizedBookSnapshot):
                        continue
                    if event.token_id != str(asset_id):
                        continue
                    if not event.bids or not event.asks:
                        raise RuntimeError(
                            "targeted market WS baseline is not two-sided"
                        )
                    bids = sorted(event.bids, key=lambda level: level[0], reverse=True)
                    asks = sorted(event.asks, key=lambda level: level[0])
                    received_at = datetime.now(timezone.utc)
                    baseline = {
                        "asset_id": event.token_id,
                        "event_ts_ms": event.event_ts_ms,
                        "received_at": received_at.isoformat(),
                        "source_hash": event.source_hash,
                        "payload_sha256": _payload_sha256(message),
                        "proxy_url": proxy_url,
                        "best_bid": format(bids[0][0], "f"),
                        "best_ask": format(asks[0][0], "f"),
                        "bids": [
                            [format(price, "f"), format(size, "f")]
                            for price, size in bids
                        ],
                        "asks": [
                            [format(price, "f"), format(size, "f")]
                            for price, size in asks
                        ],
                    }
                    activity_deadline = min(
                        deadline,
                        time.monotonic()
                        + max(0.0, float(activity_observation_seconds)),
                    )
            if baseline is not None and (
                activity_deadline is None
                or time.monotonic() >= activity_deadline
                or sum(activity_counts.values()) > 0
            ):
                return completed()
    finally:
        await ws.close()


def capture_targeted_ws_book_sync(
    *,
    asset_id: str,
    proxy_url: str | None,
    timeout_seconds: float,
    activity_observation_seconds: float = 0,
) -> dict[str, Any]:
    """Synchronous boundary used by the existing probe command."""

    return asyncio.run(
        capture_targeted_ws_book(
            asset_id=asset_id,
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
            activity_observation_seconds=activity_observation_seconds,
        )
    )


def align_hot_preflight_candidate(
    candidate: Mapping[str, Any],
    ws_book: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a current WS candidate while retaining the prior GCP book as audit."""

    if str(candidate.get("asset_id") or "") != str(ws_book.get("asset_id") or ""):
        raise ValueError("hot preflight asset identity mismatch")
    local_bid = _decimal(candidate.get("best_bid"))
    local_ask = _decimal(candidate.get("best_ask"))
    ws_bid = _decimal(ws_book.get("best_bid"))
    ws_ask = _decimal(ws_book.get("best_ask"))
    if min(local_bid, local_ask, ws_bid, ws_ask) <= 0:
        raise ValueError("hot preflight book is incomplete")
    if ws_bid >= ws_ask:
        raise ValueError("hot preflight book is crossed")
    gcp_bbo_match = local_bid == ws_bid and local_ask == ws_ask

    result = dict(candidate)
    result.update(
        {
            "registry_coverage_grade": candidate.get("coverage_grade"),
            "registry_has_gap": bool(candidate.get("has_gap", True)),
            "registry_redundant_feed_match": bool(
                candidate.get("redundant_feed_match")
            ),
            "best_bid": ws_book["best_bid"],
            "best_ask": ws_book["best_ask"],
            "bids": list(ws_book.get("bids") or ()),
            "asks": list(ws_book.get("asks") or ()),
            "coverage_grade": "A",
            "has_gap": False,
            # The second current-price source is CLOB REST and is checked by
            # the runner immediately after this native WS baseline.
            "redundant_feed_match": False,
            "rest_book_match": False,
            "book_age_ms": 0,
            "last_receive_ts": ws_book.get("received_at"),
            "shadow_checkpoint_id": ws_book.get("payload_sha256"),
            "shadow_generation": candidate.get("generation"),
            "shadow_observed_at": ws_book.get("received_at"),
            "shadow_transport_state": "TARGETED_HOT_PREFLIGHT",
            "book_validation_mode": "GCP_REGISTRY_PLUS_TARGETED_NATIVE_WS",
            "hot_preflight": {
                "schema_version": "maker_hot_preflight_v1",
                "scope": "CURRENT_PROBE_ONLY",
                "historical_continuity_claimed": False,
                "gcp_current_book": {
                    "observed_at": str(candidate.get("last_receive_ts") or ""),
                    "best_bid": format(local_bid, "f"),
                    "best_ask": format(local_ask, "f"),
                    "coverage_grade": candidate.get("coverage_grade"),
                    "has_gap": bool(candidate.get("has_gap", True)),
                    "redundant_feed_match": bool(
                        candidate.get("redundant_feed_match")
                    ),
                    "book_fingerprint": candidate.get("book_fingerprint"),
                    "transport_state": candidate.get("transport_state"),
                    "used_for_current_price": gcp_bbo_match,
                },
                "targeted_ws_book": dict(ws_book),
                "gcp_ws_bbo_match": gcp_bbo_match,
                "rest_bbo_match": False,
            },
        }
    )
    return result


def acquire_hot_preflight_candidate(
    store: Any,
    *,
    asset_id: str,
    market_id: str,
    proxy_url: str | None,
    timeout_seconds: float,
    max_book_age_seconds: float,
    identity_book_grace_seconds: float = 3600.0,
    activity_observation_seconds: float = 0,
    capture: Callable[..., dict[str, Any]] = capture_targeted_ws_book_sync,
) -> dict[str, Any]:
    """Acquire a bounded, current-only two-source book proof."""

    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    last_error = "hot preflight did not start"
    while True:
        candidate = load_hot_preflight_candidate(
            store,
            asset_id=asset_id,
            market_id=market_id,
            max_age_seconds=max_book_age_seconds,
        )
        identity_only = False
        if candidate is None:
            candidate = load_hot_preflight_candidate(
                store,
                asset_id=asset_id,
                market_id=market_id,
                max_age_seconds=max(
                    float(max_book_age_seconds),
                    float(identity_book_grace_seconds),
                ),
            )
            identity_only = candidate is not None
        if candidate is None:
            last_error = "GCP Paper current book is unavailable or market is not LIVE"
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                capture_kwargs = {
                    "asset_id": asset_id,
                    "proxy_url": proxy_url,
                    "timeout_seconds": remaining,
                }
                if activity_observation_seconds > 0:
                    capture_kwargs["activity_observation_seconds"] = (
                        activity_observation_seconds
                    )
                ws_book = capture(**capture_kwargs)
                refreshed = load_hot_preflight_candidate(
                    store,
                    asset_id=asset_id,
                    market_id=market_id,
                    max_age_seconds=(
                        max(
                            float(max_book_age_seconds),
                            float(identity_book_grace_seconds),
                        )
                        if identity_only
                        else max_book_age_seconds
                    ),
                )
                if refreshed is None:
                    raise RuntimeError(
                        "GCP Paper current book disappeared during preflight"
                    )
                aligned = align_hot_preflight_candidate(refreshed, ws_book)
                if identity_only:
                    aligned["hot_preflight"]["gcp_current_book"].update(
                        {
                            "identity_only": True,
                            "used_for_current_price": False,
                            "freshness_policy_seconds": format(
                                max(0.1, float(max_book_age_seconds)), ".3f"
                            ),
                            "identity_grace_seconds": format(
                                max(
                                    float(max_book_age_seconds),
                                    float(identity_book_grace_seconds),
                                ),
                                ".3f",
                            ),
                        }
                    )
                    aligned["hot_preflight"]["historical_continuity_claimed"] = (
                        False
                    )
                return aligned
            except (RuntimeError, TimeoutError, ValueError) as exc:
                last_error = str(exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    raise RuntimeError(f"targeted hot preflight failed: {last_error}")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid book price: {value!r}") from exc
