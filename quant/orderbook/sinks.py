"""Storage projections for local order book state."""

from __future__ import annotations

from typing import Any

from .local_book import LocalOrderBook


def build_postgres_snapshot_row(
    book: LocalOrderBook,
    *,
    event_type: str,
    storage_tier: str,
    depth_levels: int = 10,
    received_at: str | None = None,
    source: str = "local_orderbook",
) -> dict[str, Any]:
    """Project a local book into the sampled Postgres snapshot contract."""

    payload = book.snapshot_payload(depth_levels=depth_levels)
    payload["source"] = source
    payload["event_type"] = str(event_type)
    payload["storage_tier"] = str(storage_tier)
    return {
        "market_id": payload["market_id"],
        "condition_id": payload["condition_id"],
        "market_slug": book.identity.market_slug,
        "token_id": payload["token_id"],
        "side": book.identity.outcome,
        "token_side": book.identity.outcome,
        "source": source,
        "event_type": str(event_type),
        "book_generation": payload["generation"],
        "snapshot_timestamp": payload["snapshot_timestamp"],
        "fetched_at": received_at or payload["snapshot_timestamp"],
        "received_at": received_at,
        "best_bid": payload["best_bid"],
        "best_ask": payload["best_ask"],
        "spread": payload["spread"],
        "mid": payload["mid"],
        "bid_depth": payload["bid_depth"],
        "ask_depth": payload["ask_depth"],
        "depth_total": payload["depth_total"],
        "imbalance": payload["imbalance"],
        "level_count_bid": len(payload["bids"]),
        "level_count_ask": len(payload["asks"]),
        "book_status": payload["book_status"],
        "storage_tier": str(storage_tier),
        "snapshot_version": payload["snapshot_version"],
        "payload": {
            "bids": payload["bids"],
            "asks": payload["asks"],
            "source": source,
            "market_slug": book.identity.market_slug,
            "condition_id": payload["condition_id"],
            "event_type": str(event_type),
            "storage_tier": str(storage_tier),
        },
    }
