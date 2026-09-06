"""Sprint 5 paired probes for paper prediction, live lifecycle, and OrderFilled evidence.

This module never submits an exchange order.  It can create a paper prediction
and record externally supplied live-order evidence under one stable probe ID.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from quant.backtest.order_event_collector import events_from_order_payload
from quant.calibration.market_taxonomy import normalize_market_domain
from quant.core.db import ClickHouseClient, safe_identifier
from quant.core.metadata import derive_clickhouse_token_id_hex

from .depth_metrics import visible_executable_depth
from .live_shadow_store import LiveShadowStore
from .paper_ledger import PostgresPaperLedgerSink

NO_SUBMIT = "no-submit"
RECORD_ONLY = "record-only"
PROBE_BOOK_MAX_AGE_SECONDS = 60.0
PAPER_FILL_STATUSES = {"FILLED", "PARTIAL"}
LIVE_FILL_STATUSES = {"FILLED", "PARTIAL_FILLED"}
LIVE_TERMINAL_NO_FILL = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED", "NO_FILL"}
LIVE_TERMINAL = LIVE_FILL_STATUSES | LIVE_TERMINAL_NO_FILL
PAPER_DATA_GATES = {"DATA_NOT_READY", "BOOK_STALE", "BOOK_GAP", "MARKET_NOT_TRADABLE"}


class OrderFilledEvidenceClient:
    """Read a bounded token/time window from the canonical ClickHouse adapter."""

    def __init__(self, client: ClickHouseClient | None = None) -> None:
        self.client = client or ClickHouseClient()

    def fetch_window(
        self,
        *,
        asset_id: str,
        start: datetime,
        end: datetime,
        limit: int = 2_000,
    ) -> list[dict[str, Any]]:
        start_utc = _utc(start)
        end_utc = _utc(end)
        if end_utc < start_utc:
            raise ValueError("OrderFilled window end precedes start")
        table = safe_identifier(self.client.settings.orderfilled_table)
        asset = _ch_escape(
            derive_clickhouse_token_id_hex(str(asset_id)) or str(asset_id)
        )
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return self.client.query_json_rows(
            f"""
            SELECT
                lower(f.tx_hash) AS tx_hash,
                f.log_index,
                f.market_id,
                lower(f.condition_id) AS condition_id,
                f.token_id AS asset_id,
                lower(f.order_hash) AS order_hash,
                lower(f.maker) AS maker,
                lower(f.taker) AS taker,
                f.side_code,
                f.price,
                f.size,
                f.fee,
                f.block_number,
                formatDateTime(bt.block_time, '%Y-%m-%dT%H:%i:%SZ', 'UTC') AS block_time
            FROM {table} AS f
            INNER JOIN block_timestamps AS bt ON bt.block_number=f.block_number
            PREWHERE f.token_id='{asset}'
            WHERE bt.block_time >= toDateTime64('{start_text}', 3, 'UTC')
              AND bt.block_time <= toDateTime64('{end_text}', 3, 'UTC')
            ORDER BY f.block_number, f.log_index, f.tx_hash, f.order_hash
            LIMIT {max(1, int(limit))}
            """,
            timeout_seconds=60,
        )

    def fetch_windows(
        self,
        *,
        asset_ids: Iterable[str],
        start: datetime,
        end: datetime,
        limit: int = 200_000,
    ) -> dict[str, list[dict[str, Any]]]:
        """Read one bounded OrderFilled window and partition it by asset."""

        start_utc = _utc(start)
        end_utc = _utc(end)
        if end_utc < start_utc:
            raise ValueError("OrderFilled window end precedes start")
        lookup: dict[str, str] = {}
        for raw_asset_id in asset_ids:
            asset_id = str(raw_asset_id).strip()
            if not asset_id:
                continue
            asset_key = str(
                derive_clickhouse_token_id_hex(asset_id) or asset_id
            ).lower()
            lookup[asset_key] = asset_id
        if not lookup:
            return {}
        table = safe_identifier(self.client.settings.orderfilled_table)
        assets = ",".join(f"'{_ch_escape(value)}'" for value in sorted(lookup))
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        bounded_limit = max(1, int(limit))
        rows = self.client.query_json_rows(
            f"""
            SELECT
                lower(toString(f.token_id)) AS asset_key,
                lower(f.tx_hash) AS tx_hash,
                f.log_index,
                f.market_id,
                lower(f.condition_id) AS condition_id,
                f.token_id AS asset_id,
                lower(f.order_hash) AS order_hash,
                lower(f.maker) AS maker,
                lower(f.taker) AS taker,
                f.side_code,
                f.price,
                f.size,
                f.fee,
                f.block_number,
                formatDateTime(
                    bt.block_time,
                    '%Y-%m-%dT%H:%i:%SZ',
                    'UTC'
                ) AS block_time
            FROM {table} AS f
            INNER JOIN block_timestamps AS bt ON bt.block_number=f.block_number
            PREWHERE f.token_id IN ({assets})
            WHERE bt.block_time >= toDateTime64('{start_text}', 3, 'UTC')
              AND bt.block_time <= toDateTime64('{end_text}', 3, 'UTC')
            ORDER BY f.block_number, f.log_index, f.tx_hash, f.order_hash
            LIMIT {bounded_limit}
            """,
            timeout_seconds=60,
        )
        if len(rows) >= bounded_limit:
            raise RuntimeError("OrderFilled batch row limit reached")
        partitioned = {asset_id: [] for asset_id in lookup.values()}
        for raw_row in rows:
            row = dict(raw_row)
            asset_key = str(row.pop("asset_key", "")).lower()
            asset_id = lookup.get(asset_key)
            if asset_id is not None:
                partitioned[asset_id].append(row)
        return partitioned

    def coverage_watermark(self) -> dict[str, Any]:
        """Return the latest block whose timestamp is available for time joins."""

        rows = self.client.query_json_rows(
            """
            SELECT
                block_number,
                formatDateTime(
                    block_time,
                    '%Y-%m-%dT%H:%i:%SZ',
                    'UTC'
                ) AS block_time
            FROM block_timestamps
            ORDER BY block_number DESC
            LIMIT 1
            """,
            timeout_seconds=30,
        )
        if not rows:
            return {"block_number": None, "block_time": None}
        row = rows[0]
        return {
            "block_number": int(row["block_number"]),
            "block_time": _parse_time(row["block_time"]),
        }

    def fetch_active_assets(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int = 20_000,
    ) -> dict[str, dict[str, Any]]:
        """Return token activity for a bounded historical selection window."""

        start_utc = _utc(start)
        end_utc = _utc(end)
        if end_utc < start_utc:
            raise ValueError("OrderFilled window end precedes start")
        table = safe_identifier(self.client.settings.orderfilled_table)
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        rows = self.client.query_json_rows(
            f"""
            SELECT
                lower(toString(f.token_id)) AS asset_key,
                count() AS trade_count,
                toString(sum(f.size)) AS trade_volume
            FROM {table} AS f
            INNER JOIN block_timestamps AS bt ON bt.block_number=f.block_number
            WHERE bt.block_time >= toDateTime64('{start_text}', 3, 'UTC')
              AND bt.block_time < toDateTime64('{end_text}', 3, 'UTC')
            GROUP BY f.token_id
            ORDER BY trade_count DESC, f.token_id
            LIMIT {max(1, int(limit))}
            """,
            timeout_seconds=60,
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            asset_key = str(row.get("asset_key") or "").removeprefix("0x")
            try:
                asset_id = str(int(asset_key, 16))
            except ValueError:
                continue
            result[asset_id] = {
                "trade_count": int(row.get("trade_count") or 0),
                "trade_volume": Decimal(str(row.get("trade_volume") or 0)),
            }
        return result

    def fetch_transactions(
        self,
        transaction_hashes: Iterable[str],
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch known live transaction evidence without a timestamp join."""

        hashes = sorted({
            _identity(value)
            for value in transaction_hashes
            if _is_hex_digest(_identity(value), length=64)
        })
        if not hashes:
            return []
        table = safe_identifier(self.client.settings.orderfilled_table)
        values = ",".join(f"'{_ch_escape(value)}'" for value in hashes)
        return self.client.query_json_rows(
            f"""
            SELECT
                lower(f.tx_hash) AS tx_hash,
                f.log_index,
                f.market_id,
                lower(f.condition_id) AS condition_id,
                f.token_id AS asset_id,
                lower(f.order_hash) AS order_hash,
                lower(f.maker) AS maker,
                lower(f.taker) AS taker,
                f.side_code,
                f.price,
                f.size,
                f.fee,
                f.block_number
            FROM {table} AS f
            PREWHERE f.tx_hash IN ({values})
            ORDER BY f.block_number, f.log_index, f.tx_hash, f.order_hash
            LIMIT {max(1, int(limit))}
            """,
            timeout_seconds=60,
        )

    def summarize_compatible_maker_volume(
        self,
        *,
        asset_id: str,
        maker_side: str,
        limit_price: Decimal,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Aggregate pre-decision trades capable of consuming a maker level."""

        side = str(maker_side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("maker side must be BUY or SELL")
        start_utc = _utc(start)
        end_utc = _utc(end)
        if end_utc < start_utc:
            raise ValueError("OrderFilled window end precedes start")
        table = safe_identifier(self.client.settings.orderfilled_table)
        asset = _ch_escape(
            derive_clickhouse_token_id_hex(str(asset_id)) or str(asset_id)
        )
        price = _ch_escape(format(Decimal(limit_price), "f"))
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        side_code = 2 if side == "BUY" else 1
        price_filter = "f.price <=" if side == "BUY" else "f.price >="
        rows = self.client.query_json_rows(
            f"""
            SELECT
                count() AS trade_count,
                toString(sum(f.size)) AS compatible_trade_volume,
                if(
                    count() = 0,
                    '',
                    formatDateTime(max(bt.block_time), '%Y-%m-%dT%H:%i:%SZ', 'UTC')
                ) AS last_trade_at
            FROM {table} AS f
            INNER JOIN block_timestamps AS bt ON bt.block_number=f.block_number
            PREWHERE f.token_id='{asset}'
            WHERE bt.block_time >= toDateTime64('{start_text}', 3, 'UTC')
              AND bt.block_time <= toDateTime64('{end_text}', 3, 'UTC')
              AND f.side_code={side_code}
              AND {price_filter} toDecimal64('{price}', 10)
            """,
            timeout_seconds=60,
        )
        return dict(rows[0]) if rows else {
            "trade_count": 0,
            "compatible_trade_volume": "0",
            "last_trade_at": "",
        }

    def summarize_compatible_maker_volume_batch(
        self,
        requests: Iterable[Mapping[str, Any]],
        *,
        start: datetime,
        end: datetime,
        limit: int = 200_000,
        watermark_tolerance_seconds: int = 120,
    ) -> dict[str, dict[str, Any]]:
        """Read one bounded OrderFilled window for many candidate price levels."""

        start_utc = _utc(start)
        end_utc = _utc(end)
        if end_utc < start_utc:
            raise ValueError("OrderFilled window end precedes start")
        normalized: dict[str, dict[str, Any]] = {}
        lookup: dict[str, list[str]] = {}
        for raw in requests:
            asset_id = str(raw.get("asset_id") or "").strip()
            side = str(raw.get("maker_side") or "").upper()
            price = Decimal(str(raw.get("limit_price") or 0))
            if not asset_id or side not in {"BUY", "SELL"} or not 0 < price < 1:
                continue
            asset_key = str(
                derive_clickhouse_token_id_hex(asset_id) or asset_id
            ).lower()
            request_key = str(raw.get("request_key") or asset_id)
            lookup.setdefault(asset_key, []).append(request_key)
            normalized[request_key] = {
                "asset_id": asset_id,
                "maker_side": side,
                "limit_price": price,
                "trade_count": 0,
                "compatible_trade_volume": Decimal(0),
                "last_trade_at": None,
            }
        if not normalized:
            return {}
        table = safe_identifier(self.client.settings.orderfilled_table)
        assets = ",".join(
            f"'{_ch_escape(value)}'" for value in sorted(lookup)
        )
        start_text = start_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        end_text = end_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        bounded_limit = max(1, int(limit))
        rows = self.client.query_json_rows(
            f"""
            SELECT
                lower(f.token_id) AS asset_key,
                f.side_code,
                toString(f.price) AS price,
                toString(f.size) AS size,
                formatDateTime(
                    bt.block_time,
                    '%Y-%m-%dT%H:%i:%SZ',
                    'UTC'
                ) AS block_time
            FROM {table} AS f
            INNER JOIN block_timestamps AS bt ON bt.block_number=f.block_number
            PREWHERE f.token_id IN ({assets})
            WHERE bt.block_time >= toDateTime64('{start_text}', 3, 'UTC')
              AND bt.block_time <= toDateTime64('{end_text}', 3, 'UTC')
            ORDER BY f.block_number, f.log_index
            LIMIT {bounded_limit}
            """,
            timeout_seconds=60,
        )
        truncated = len(rows) >= bounded_limit
        for row in rows:
            request_keys = lookup.get(
                str(row.get("asset_key") or "").lower(), ()
            )
            if not request_keys:
                continue
            for request_key in request_keys:
                summary = normalized[request_key]
                side_code = int(row.get("side_code") or 0)
                price = Decimal(str(row.get("price") or 0))
                compatible = (
                    summary["maker_side"] == "BUY"
                    and side_code == 2
                    and price <= summary["limit_price"]
                ) or (
                    summary["maker_side"] == "SELL"
                    and side_code == 1
                    and price >= summary["limit_price"]
                )
                if not compatible:
                    continue
                summary["trade_count"] += 1
                summary["compatible_trade_volume"] += Decimal(
                    str(row.get("size") or 0)
                )
                summary["last_trade_at"] = (
                    str(row.get("block_time") or "") or None
                )
        watermark = self.coverage_watermark()
        watermark_at = watermark.get("block_time")
        source_ready = bool(
            isinstance(watermark_at, datetime)
            and watermark_at
            >= end_utc - timedelta(seconds=max(1, int(watermark_tolerance_seconds)))
            and not truncated
        )
        reason = (
            "orderfilled_window_complete"
            if source_ready
            else "orderfilled_batch_row_limit_reached"
            if truncated
            else "orderfilled_watermark_lag"
        )
        return {
            request_key: {
                "source": "clickhouse_orderfilled_batch",
                "source_ready": source_ready,
                "coverage_reason": reason,
                "coverage_watermark_block": watermark.get("block_number"),
                "coverage_watermark_at": (
                    watermark_at.isoformat()
                    if isinstance(watermark_at, datetime)
                    else None
                ),
                "evidence_window_start": start_utc.isoformat(),
                "evidence_window_end": end_utc.isoformat(),
                "trade_count": int(summary["trade_count"]),
                "asset_id": summary["asset_id"],
                "maker_side": summary["maker_side"],
                "limit_price": format(summary["limit_price"], "f"),
                "compatible_trade_volume": format(
                    summary["compatible_trade_volume"], "f"
                ),
                "last_trade_at": summary["last_trade_at"],
            }
            for request_key, summary in normalized.items()
        }


def load_live_probe_candidates(
    store: LiveShadowStore,
    *,
    limit: int = 20,
    max_age_seconds: float = 2.0,
    asset_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return current dual-feed, execution-eligible books already watched by A."""

    with store.connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('quant.paper_market_registry_tokens') AS registry"
        )
        if cur.fetchone()["registry"] is None:
            cur.execute(
                """
                SELECT
                    catalog.asset_id,
                    catalog.market_id,
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
                    TRUE AS rest_book_match,
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
                    catalog.enable_neg_risk,
                    watch.reason AS watch_reason,
                    terms.fee_rate,
                    terms.fee_exponent,
                    COALESCE(terms.fee_taker_only, TRUE) AS fee_taker_only
                FROM quant.paper_execution_market_catalog catalog
                JOIN quant.paper_live_current_books book USING (asset_id)
                JOIN quant.paper_live_watchlist watch USING (asset_id)
                LEFT JOIN quant.paper_market_terms terms USING (asset_id)
                WHERE (
                      (
                          catalog.market_state='LIVE'
                          AND catalog.execution_eligible=TRUE
                      )
                      OR watch.reason='live_probe_candidate'
                  )
                  AND catalog.active=TRUE
                  AND catalog.closed=FALSE
                  AND catalog.resolved=FALSE
                  AND watch.enabled=TRUE
                  AND book.book_status='READY'
                  AND book.coverage_grade IN ('A_PLUS','A')
                  AND book.has_gap=FALSE
                  AND book.redundant_feed_match IS TRUE
                  AND (%s::text IS NULL OR catalog.asset_id=%s::text)
                  AND book.observed_at >= statement_timestamp()
                      - make_interval(secs => %s)
                  AND book.best_bid > 0
                  AND book.best_ask < 1
                  AND book.best_bid < book.best_ask
                ORDER BY book.observed_at DESC, catalog.asset_id
                LIMIT %s
                """,
                (
                    str(asset_id) if asset_id else None,
                    str(asset_id) if asset_id else None,
                    max(0.1, float(max_age_seconds)),
                    max(1, int(limit)),
                ),
            )
            return [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            WITH current_book_sources AS (
                SELECT
                    asset_id,
                    subscription_state,
                    coverage_grade,
                    has_gap,
                    current_best_bid,
                    current_best_ask,
                    rest_reconciled,
                    connection_id,
                    shard_id::text,
                    last_receive_ts,
                    redundant_feed_match,
                    NULL::jsonb AS bids,
                    NULL::jsonb AS asks,
                    1 AS source_priority
                FROM quant.clob_l2_current_coverage
                UNION ALL
                SELECT
                    asset_id,
                    CASE WHEN book_status='READY' THEN 'LIVE_ACTIVE' ELSE book_status END,
                    coverage_grade,
                    has_gap,
                    best_bid,
                    best_ask,
                    TRUE,
                    source_connection_id,
                    'paper-hot',
                    observed_at,
                    redundant_feed_match,
                    bids,
                    asks,
                    0
                FROM quant.paper_live_current_books
            ), current_books AS (
                SELECT DISTINCT ON (asset_id)
                    asset_id,
                    subscription_state,
                    coverage_grade,
                    has_gap,
                    current_best_bid,
                    current_best_ask,
                    rest_reconciled,
                    connection_id,
                    shard_id,
                    last_receive_ts,
                    redundant_feed_match,
                    bids,
                    asks
                FROM current_book_sources
                ORDER BY asset_id, last_receive_ts DESC, source_priority
            )
            SELECT
                r.asset_id,
                COALESCE(r.market_id::text, r.gamma_market_id, r.condition_id) AS market_id,
                r.condition_id,
                r.market_slug,
                r.market_title,
                r.outcome_name,
                COALESCE(r.current_tick_size, 0.001) AS tick_size,
                COALESCE(r.min_order_size, 1) AS min_order_size,
                c.current_best_bid AS best_bid,
                c.current_best_ask AS best_ask,
                c.coverage_grade,
                c.has_gap,
                GREATEST(
                    0,
                    floor(extract(epoch FROM (statement_timestamp() - c.last_receive_ts)) * 1000)
                )::bigint AS book_age_ms,
                c.rest_reconciled AS rest_book_match,
                c.connection_id,
                c.shard_id,
                c.last_receive_ts,
                c.redundant_feed_match,
                c.bids,
                c.asks,
                COALESCE(activity.event_count, 0) AS activity_event_count,
                COALESCE(activity.price_change_count, 0) AS activity_price_change_count,
                r.market_state,
                r.execution_eligible,
                m.raw_metadata,
                em.event_category AS source_category,
                em.event_title,
                em.event_volume,
                tm.end_date,
                w.reason AS watch_reason
            FROM quant.paper_market_registry_tokens r
            JOIN current_books c USING (asset_id)
            JOIN quant.paper_live_watchlist w USING (asset_id)
            LEFT JOIN LATERAL (
                SELECT market.raw_metadata
                FROM quant.paper_market_registry_markets market
                WHERE market.condition_id=r.condition_id
                ORDER BY market.updated_at DESC, market.market_key
                LIMIT 1
            ) m ON TRUE
            LEFT JOIN quant.market_token_metadata tm ON tm.token_id=r.asset_id
            LEFT JOIN LATERAL (
                SELECT coverage.event_count, coverage.price_change_count
                FROM quant.clob_l2_active_active_token_hour_coverage coverage
                WHERE coverage.asset_id=r.asset_id
                ORDER BY coverage.hour_start DESC
                LIMIT 1
            ) activity ON TRUE
            LEFT JOIN LATERAL (
                SELECT metadata.event_category,
                       metadata.event_title,
                       metadata.volume AS event_volume
                FROM quant.market_event_members member
                JOIN quant.market_event_metadata metadata
                  ON metadata.event_slug=member.event_slug
                WHERE member.market_id=r.market_id
                ORDER BY metadata.updated_at DESC, metadata.event_slug
                LIMIT 1
            ) em ON TRUE
            WHERE (
                  (r.market_state='LIVE' AND r.execution_eligible=TRUE)
                  OR w.reason='live_probe_candidate'
              )
              AND r.active=TRUE AND r.closed=FALSE AND r.resolved=FALSE
              AND w.enabled=TRUE
              AND c.subscription_state IN ('SNAPSHOT_CONFIRMED','LIVE_ACTIVE')
              AND c.coverage_grade IN ('A_PLUS','A')
              AND c.has_gap=FALSE
              AND c.redundant_feed_match IS TRUE
              AND (%s::text IS NULL OR r.asset_id=%s::text)
              AND c.last_receive_ts >= statement_timestamp() - make_interval(secs => %s)
              AND c.current_best_bid > 0
              AND c.current_best_ask < 1
              AND c.current_best_bid < c.current_best_ask
            ORDER BY COALESCE(activity.event_count, 0) DESC,
                     c.last_receive_ts DESC,
                     r.asset_id
            LIMIT %s
            """,
            (
                str(asset_id) if asset_id else None,
                str(asset_id) if asset_id else None,
                max(0.1, float(max_age_seconds)),
                max(1, int(limit)),
            ),
        )
        return [dict(row) for row in cur.fetchall()]


def run_paired_probe(
    *,
    store: LiveShadowStore,
    mode: str = NO_SUBMIT,
    strategy_id: str | None = None,
    candidate: Mapping[str, Any] | None = None,
    side: str = "BUY",
    amount: Decimal | str | None = None,
    amount_unit: str = "SHARES",
    order_type: str = "FAK",
    fee_rate: Decimal | str | None = None,
    fee_exponent: Decimal | str | None = None,
    fee_taker_only: bool = True,
    wait_seconds: float = 15.0,
    evidence_client: OrderFilledEvidenceClient | None = None,
    evidence_window_seconds: float = 30.0,
    paper_position_seed: Decimal | str | None = None,
    paper_position_cost_basis: Decimal | str | None = None,
    limit_price: Decimal | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run A and create B/C audit placeholders without any exchange submission."""

    selected_mode = str(mode).strip().lower()
    if selected_mode not in {NO_SUBMIT, RECORD_ONLY}:
        raise ValueError("mode must be no-submit or record-only")
    observed_now = _utc(now or datetime.now(timezone.utc))
    selected = dict(candidate) if candidate is not None else _select_candidate(store, wait_seconds)
    side_text = str(side).upper()
    if side_text not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    unit = str(amount_unit).upper()
    tif = str(order_type).upper()
    if unit not in {"SHARES", "QUOTE"}:
        raise ValueError("amount_unit must be SHARES or QUOTE")
    if tif not in {"FAK", "FOK"}:
        raise ValueError("paired probe order_type must be FAK or FOK")
    if side_text == "SELL" and unit != "SHARES":
        raise ValueError("SELL amount_unit must be SHARES")
    strategy = strategy_id or f"paired-probe-{observed_now.strftime('%Y%m%d')}"
    probe_nonce = observed_now.strftime("%Y%m%dT%H%M%S%fZ")
    client_order_id = f"{probe_nonce}-{side_text.lower()}"
    probe_id = paired_probe_id(strategy, client_order_id)
    size = (
        _decimal(amount)
        if amount is not None
        else _decimal(selected.get("min_order_size"), Decimal("1"))
    )
    limit_price = (
        _decimal(limit_price)
        if limit_price is not None
        else _depth_walk_limit_price(
            selected,
            side=side_text,
            amount=size,
            amount_unit=unit,
        )
    )
    decision_ts = observed_now
    seeded_position = _decimal(paper_position_seed) if paper_position_seed is not None else None
    seeded_cost_basis = (
        _decimal(paper_position_cost_basis)
        if paper_position_cost_basis is not None
        else Decimal("0")
    )
    if side_text == "SELL" and seeded_position is not None:
        PostgresPaperLedgerSink(
            connection_factory=store.connection_factory,
            ensure_schema=False,
        ).seed_calibration_position(
            strategy_id=strategy,
            asset_id=str(selected["asset_id"]),
            market_id=str(selected["market_id"]),
            condition_id=str(selected["condition_id"]),
            quantity=seeded_position,
            cost_basis=seeded_cost_basis,
        )

    paper_intent_id = store.submit(
        strategy_id=strategy,
        client_order_id=client_order_id,
        asset_id=str(selected["asset_id"]),
        side=side_text,
        time_in_force=tif,
        limit_price=limit_price,
        size=size,
        amount_unit=unit,
        fee_rate=_optional_decimal(fee_rate),
        fee_exponent=_optional_decimal(fee_exponent),
        fee_taker_only=bool(fee_taker_only),
        post_only=False,
        decision_ts=decision_ts,
    )
    paper_row = _wait_for_paper(store, paper_intent_id, wait_seconds=wait_seconds)
    paper_prediction = _paper_prediction(paper_row)
    paper_prediction["side"] = side_text
    checkpoint = store.load_book_checkpoint(paper_prediction.get("arrival_checkpoint_id"))
    bucket_context = _bucket_context(
        selected,
        checkpoint=checkpoint,
        side=side_text,
        size=size,
        amount_unit=unit,
        limit_price=limit_price,
        decision_ts=decision_ts,
    )
    lifecycle = (
        _no_submit_lifecycle(probe_id, client_order_id)
        if selected_mode == NO_SUBMIT
        else _awaiting_lifecycle(probe_id, client_order_id)
    )
    evidence = _initial_orderfilled_evidence(selected_mode, decision_ts)
    if evidence_client is not None:
        end = min(
            datetime.now(timezone.utc),
            decision_ts + timedelta(seconds=max(0.0, float(evidence_window_seconds))),
        )
        rows = evidence_client.fetch_window(
            asset_id=str(selected["asset_id"]),
            start=decision_ts,
            end=end,
        )
        evidence = build_ex_self_evidence(
            rows,
            mode=selected_mode,
            window_start=decision_ts,
            window_end=end,
            source_watermark=(
                evidence_client.coverage_watermark().get("block_time")
                if selected_mode == RECORD_ONLY
                else None
            ),
        )
    record = {
        "probe_id": probe_id,
        "strategy_id": strategy,
        "client_order_id": client_order_id,
        "paper_intent_id": paper_intent_id,
        "mode": selected_mode,
        "status": "PENDING",
        "market_id": str(selected["market_id"]),
        "condition_id": str(selected["condition_id"]),
        "asset_id": str(selected["asset_id"]),
        "decision_ts": decision_ts,
        "arrival_ts": _parse_time(paper_prediction.get("arrival_ts")),
        "paper_prediction": paper_prediction,
        "live_lifecycle": lifecycle,
        "orderfilled_ex_self": evidence,
        "bucket_context": bucket_context,
        "audit": {
            "schema_version": "paper_paired_probe_v1",
            "created_at": observed_now,
            "exchange_submit_called": False,
            "order_submitter_present": False,
            "a_route": "paper_live_shadow",
            "b_route": "external_record_only",
            "c_route": "clickhouse_orderfilled_delayed",
            "correlation_key": probe_id,
            "requested_amount": format(size, "f"),
            "amount_unit": unit,
            "order_type": tif,
            "fee_rate": _json_value(fee_rate),
            "fee_exponent": _json_value(fee_exponent),
            "fee_taker_only": bool(fee_taker_only),
            "paper_position_seed": (
                format(seeded_position, "f") if seeded_position is not None else None
            ),
            "paper_position_cost_basis": (
                format(seeded_cost_basis, "f")
                if seeded_position is not None
                else None
            ),
        },
    }
    record["status"] = evaluate_probe_status(record)
    return store.upsert_paired_probe(record)


def attach_live_lifecycle(
    probe: Mapping[str, Any],
    payload: Any,
    *,
    source: str = "micro-live-recorder",
) -> dict[str, Any]:
    """Attach externally collected B events; this function has no submit transport."""

    if str(probe.get("mode")) != RECORD_ONLY:
        raise ValueError("live lifecycle can only be attached to a record-only probe")
    events = events_from_order_payload(payload, source=source)
    errors: list[str] = []
    matched: list[dict[str, Any]] = []
    expected_client_id = str(probe["client_order_id"])
    expected_asset = str(probe["asset_id"])
    for event in events:
        event_payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        event_client_ids = {
            str(value)
            for value in (
                event.get("order_id"),
                event_payload.get("client_order_id"),
                event_payload.get("clientOrderId"),
                event_payload.get("probe_id"),
            )
            if value not in (None, "")
        }
        if expected_client_id not in event_client_ids and str(probe["probe_id"]) not in event_client_ids:
            errors.append("event_correlation_mismatch")
            continue
        event_asset = event.get("token_id") or event_payload.get("asset_id") or event_payload.get("assetId")
        if event_asset not in (None, "") and str(event_asset) != expected_asset:
            errors.append("event_asset_mismatch")
            continue
        matched.append(_json_value(event))
    matched.sort(key=lambda row: str(row.get("event_time") or ""))
    decision_ts = _parse_time(probe.get("decision_ts"))
    if decision_ts and any(
        _parse_time(row.get("event_time")) and _parse_time(row.get("event_time")) < decision_ts
        for row in matched
    ):
        errors.append("event_before_decision")
    statuses = [_event_status(row) for row in matched]
    terminal_statuses = [status for status in statuses if status in LIVE_TERMINAL]
    if len(set(terminal_statuses)) > 1 and set(terminal_statuses) - LIVE_FILL_STATUSES:
        errors.append("conflicting_terminal_states")
    terminal_status = terminal_statuses[-1] if terminal_statuses else None
    lifecycle = {
        "state": "INVALID" if errors else "TERMINAL" if terminal_status else "OPEN" if matched else "AWAITING_IMPORT",
        "terminal_status": terminal_status,
        "event_count": len(matched),
        "events": matched,
        "errors": sorted(set(errors)),
        "external_order_ids": sorted({
            str(row["external_order_id"])
            for row in matched
            if row.get("external_order_id") not in (None, "")
        }),
        "ack_at": _ack_time(matched),
        "fill_at": _first_event_time(matched, LIVE_FILL_STATUSES),
        "live_fill_price": _latest_payload_value(matched, "live_fill_price", "avg_fill_price", "fill_price", "price"),
        "live_fill_size": _latest_payload_value(matched, "live_fill_size", "filled_size", "matched_size", "size"),
        "live_slippage": _latest_payload_value(matched, "live_slippage", "slippage", "slippage_cost"),
        "live_fee": _latest_payload_value(matched, "live_fee", "fee", "fee_cost"),
        "source": source,
        "network_submit_called_by_probe": False,
    }
    updated = dict(probe)
    updated["live_lifecycle"] = lifecycle
    updated["status"] = evaluate_probe_status(updated)
    return updated


def build_ex_self_evidence(
    rows: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    own_addresses: Iterable[str] = (),
    own_order_hashes: Iterable[str] = (),
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    source_watermark: datetime | None = None,
    expected_transaction_hashes: Iterable[str] = (),
) -> dict[str, Any]:
    """Deduplicate OrderFilled rows and remove all known self-generated evidence."""

    addresses = {_identity(value) for value in own_addresses if _identity(value)}
    order_hashes = {_identity(value) for value in own_order_hashes if _identity(value)}
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    duplicate_count = 0
    for raw in rows:
        row = _compact_orderfilled(raw)
        key = (str(row.get("tx_hash") or ""), int(row.get("log_index") or 0), str(row.get("order_hash") or ""))
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        reasons = []
        if _identity(row.get("maker")) in addresses:
            reasons.append("own_maker")
        if _identity(row.get("taker")) in addresses:
            reasons.append("own_taker")
        if _identity(row.get("order_hash")) in order_hashes:
            reasons.append("own_order_hash")
        if reasons:
            excluded.append({**row, "exclusion_reasons": reasons})
        else:
            included.append(row)
    identity_verified = str(mode) == NO_SUBMIT or bool(addresses or order_hashes)
    normalized_window_end = _parse_time(window_end)
    normalized_watermark = _parse_time(source_watermark)
    source_coverage_complete = (
        str(mode) == NO_SUBMIT
        or (
            normalized_window_end is not None
            and normalized_watermark is not None
            and normalized_watermark >= normalized_window_end
        )
    )
    if not identity_verified:
        state = "UNVERIFIED_SELF_IDENTITY"
    elif not source_coverage_complete:
        state = (
            "SOURCE_LAG"
            if normalized_watermark is not None
            else "SOURCE_COVERAGE_UNVERIFIED"
        )
    else:
        state = "VERIFIED"
    expected_transactions = sorted({
        _identity(value)
        for value in expected_transaction_hashes
        if _is_hex_digest(_identity(value), length=64)
    })
    observed_transactions = sorted({
        _identity(row.get("tx_hash"))
        for row in included + excluded
        if _is_hex_digest(_identity(row.get("tx_hash")), length=64)
    })
    missing_transactions = sorted(
        set(expected_transactions) - set(observed_transactions)
    )
    return {
        "state": state,
        "window_start": _json_value(window_start),
        "window_end": _json_value(window_end),
        "source_watermark": _json_value(normalized_watermark),
        "source_coverage_complete": source_coverage_complete,
        "expected_transaction_hashes": expected_transactions,
        "observed_transaction_hashes": observed_transactions,
        "missing_transaction_hashes": missing_transactions,
        "transaction_confirmation_complete": (
            bool(expected_transactions) and not missing_transactions
        ),
        "raw_count": len(included) + len(excluded) + duplicate_count,
        "deduped_count": len(included) + len(excluded),
        "duplicate_count": duplicate_count,
        "excluded_self_count": len(excluded),
        "ex_self_count": len(included),
        "ex_self_volume": format(sum((_decimal(row.get("size"), Decimal("0")) for row in included), Decimal("0")), "f"),
        "events": included[:500],
        "excluded_self_events": excluded[:100],
        "truncated": len(included) > 500 or len(excluded) > 100,
        "self_identity_count": len(addresses) + len(order_hashes),
        "used_for_actual_live_outcome": False,
        "prediction": "TRADE_EVIDENCE_PRESENT" if included else "NO_TRADE_EVIDENCE",
    }


def attach_orderfilled_evidence(
    probe: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    own_addresses: Iterable[str] = (),
    own_order_hashes: Iterable[str] = (),
    window_end: datetime | None = None,
    source_watermark: datetime | None = None,
    expected_transaction_hashes: Iterable[str] = (),
) -> dict[str, Any]:
    lifecycle = probe.get("live_lifecycle") if isinstance(probe.get("live_lifecycle"), Mapping) else {}
    inferred_hashes = set(own_order_hashes)
    inferred_hashes.update(str(value) for value in lifecycle.get("external_order_ids") or [])
    updated = dict(probe)
    updated["orderfilled_ex_self"] = build_ex_self_evidence(
        rows,
        mode=str(probe.get("mode") or RECORD_ONLY),
        own_addresses=own_addresses,
        own_order_hashes=inferred_hashes,
        window_start=_parse_time(probe.get("decision_ts")),
        window_end=window_end,
        source_watermark=source_watermark,
        expected_transaction_hashes=expected_transaction_hashes,
    )
    updated["status"] = evaluate_probe_status(updated)
    return updated


def evaluate_probe_status(probe: Mapping[str, Any]) -> str:
    paper = probe.get("paper_prediction") if isinstance(probe.get("paper_prediction"), Mapping) else {}
    lifecycle = probe.get("live_lifecycle") if isinstance(probe.get("live_lifecycle"), Mapping) else {}
    evidence = probe.get("orderfilled_ex_self") if isinstance(probe.get("orderfilled_ex_self"), Mapping) else {}
    if not paper or str(paper.get("queue_status") or "") not in {"COMPLETED", "FAILED"}:
        return "PENDING_PAPER"
    if str(paper.get("queue_status")) == "FAILED" or str(paper.get("status")) == "FAILED":
        return "PAPER_FAILED"
    if str(paper.get("status") or "") in PAPER_DATA_GATES:
        return "SHADOW_BLOCKED_DATA"
    if str(probe.get("mode")) == NO_SUBMIT:
        return "SHADOW_READY" if lifecycle.get("state") == "NOT_SUBMITTED" else "INVALID"
    if lifecycle.get("state") == "INVALID":
        return "INVALID"
    if lifecycle.get("state") != "TERMINAL":
        return "PENDING_LIVE"
    audit = probe.get("audit") if isinstance(probe.get("audit"), Mapping) else {}
    external_live = (
        audit.get("external_live")
        if isinstance(audit.get("external_live"), Mapping)
        else {}
    )
    if external_live and str(external_live.get("probe_state") or "") != "CALIBRATABLE":
        return "LIVE_NOT_CALIBRATABLE"
    if evidence.get("state") != "VERIFIED":
        return "PENDING_ORDERFILLED_EX_SELF"
    return "CALIBRATION_READY"


def build_paired_probe_report(
    probes: Sequence[Mapping[str, Any]],
    *,
    max_false_positive_rate_pct: Decimal = Decimal("10"),
    max_avg_price_error_ticks: Decimal = Decimal("1"),
) -> dict[str, Any]:
    rows = [dict(row) for row in probes]
    calibratable = [row for row in rows if row.get("status") == "CALIBRATION_READY"]
    non_calibratable = [
        row for row in rows if row.get("status") == "LIVE_NOT_CALIBRATABLE"
    ]
    metrics = _metrics(calibratable)
    bucket_dimensions = (
        "category", "source_category", "outcome_name", "price_bucket", "spread_bucket", "depth_bucket",
        "activity_bucket", "time_to_resolution_bucket", "coverage_grade",
        "connection", "shard", "size_depth_ratio_bucket",
    )
    buckets: dict[str, dict[str, Any]] = {}
    for dimension in bucket_dimensions:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in calibratable:
            context = row.get("bucket_context") if isinstance(row.get("bucket_context"), Mapping) else {}
            grouped[str(context.get(dimension) or "unknown")].append(row)
        buckets[dimension] = {name: _metrics(items) for name, items in sorted(grouped.items())}
    status_counts = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
    if any(row.get("status") == "INVALID" for row in rows):
        status = "FAIL"
        reason = "one or more probes contain invalid lifecycle evidence"
    elif calibratable:
        false_positive = _decimal(metrics.get("false_positive_rate_pct"), Decimal("0"))
        price_error = _decimal(metrics.get("avg_price_error_ticks"), Decimal("0"))
        within_thresholds = (
            false_positive <= max_false_positive_rate_pct
            and price_error <= max_avg_price_error_ticks
        )
        pending_count = sum(
            1 for row in rows if str(row.get("status") or "").startswith("PENDING_")
        )
        if not within_thresholds:
            status = "REVIEW"
            reason = "paired calibration exceeds configured thresholds"
        elif pending_count:
            status = "PASS_WITH_PENDING"
            reason = (
                "completed paired samples are within thresholds; "
                "one or more delayed evidence windows are still pending"
            )
        else:
            status = "PASS"
            reason = "paired calibration is within configured thresholds"
    elif any(row.get("status") == "SHADOW_READY" for row in rows):
        status = "SHADOW_READY"
        reason = "A route is verified; B was intentionally not submitted, so live calibration is not claimed"
    else:
        status = "PENDING"
        reason = "paired probe evidence is incomplete"
    return _json_value({
        "schema_version": "paper_paired_probe_report_v1",
        "generated_at": datetime.now(timezone.utc),
        "status": status,
        "reason": reason,
        "probe_count": len(rows),
        "calibratable_count": len(calibratable),
        "non_calibratable_count": len(non_calibratable),
        "pending_count": sum(
            1 for row in rows if str(row.get("status") or "").startswith("PENDING_")
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "safety": {
            "exchange_submit_calls_by_this_module": 0,
            "no_submit_probe_count": sum(1 for row in rows if row.get("mode") == NO_SUBMIT),
            "record_only_probe_count": sum(1 for row in rows if row.get("mode") == RECORD_ONLY),
            "self_contamination_excluded": sum(int((row.get("orderfilled_ex_self") or {}).get("excluded_self_count") or 0) for row in rows),
        },
        "thresholds": {
            "max_false_positive_rate_pct": max_false_positive_rate_pct,
            "max_avg_price_error_ticks": max_avg_price_error_ticks,
        },
        "metrics": metrics,
        "buckets": buckets,
        "probes": rows,
    })


def paired_probe_id(strategy_id: str, client_order_id: str) -> str:
    digest = hashlib.sha256(f"{strategy_id}|{client_order_id}".encode("utf-8")).hexdigest()
    return f"pp-{digest[:24]}"


def _select_candidate(store: LiveShadowStore, wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        rows = load_live_probe_candidates(
            store,
            limit=20,
            max_age_seconds=PROBE_BOOK_MAX_AGE_SECONDS,
        )
        if rows:
            return rows[0]
        if time.monotonic() >= deadline:
            raise RuntimeError("no fresh dual-feed paper probe candidate")
        time.sleep(0.25)


def _wait_for_paper(store: LiveShadowStore, intent_id: int, *, wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(wait_seconds))
    row: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        row = store.load_intent(intent_id)
        if row and row.get("status") in {"COMPLETED", "FAILED"}:
            return row
        time.sleep(0.1)
    return row or {"intent_id": intent_id, "status": "MISSING", "last_error": "paper intent not found"}


def _paper_prediction(row: Mapping[str, Any]) -> dict[str, Any]:
    result = row.get("result") if isinstance(row.get("result"), Mapping) else {}
    return {
        "queue_status": row.get("status"),
        "intent_id": row.get("intent_id"),
        "status": result.get("status") or ("FAILED" if row.get("status") == "FAILED" else None),
        "reason": result.get("reason") or row.get("last_error"),
        "decision_ts": row.get("decision_ts"),
        "arrival_ts": result.get("arrival_ts"),
        "decision_checkpoint_id": result.get("decision_checkpoint_id"),
        "arrival_checkpoint_id": result.get("arrival_checkpoint_id"),
        "coverage_grade": result.get("coverage_grade"),
        "book_age_ms": result.get("book_age_ms"),
        "filled_size": result.get("filled_size"),
        "remaining_size": result.get("remaining_size"),
        "requested_amount": result.get("requested_amount"),
        "amount_unit": result.get("amount_unit"),
        "filled_notional": result.get("filled_notional"),
        "remaining_amount": result.get("remaining_amount"),
        "avg_fill_price": result.get("avg_fill_price"),
        "total_fee": result.get("total_fee"),
        "slippage": result.get("slippage"),
        "fills": result.get("fills") or [],
        "audit_key": result.get("audit_key") or row.get("result_audit_key"),
        "model_version": result.get("model_version"),
        "config_hash": result.get("config_hash"),
    }


def _depth_walk_limit_price(
    candidate: Mapping[str, Any],
    *,
    side: str,
    amount: Decimal,
    amount_unit: str,
) -> Decimal:
    side_text = str(side).upper()
    unit = str(amount_unit).upper()
    fallback = _decimal(
        candidate.get("best_ask") if side_text == "BUY" else candidate.get("best_bid")
    )
    raw_levels = candidate.get("asks") if side_text == "BUY" else candidate.get("bids")
    if not isinstance(raw_levels, (list, tuple)):
        return fallback
    levels: list[tuple[Decimal, Decimal]] = []
    for raw in raw_levels:
        if isinstance(raw, Mapping):
            price = _decimal(raw.get("price"))
            size = _decimal(raw.get("size"))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price = _decimal(raw[0])
            size = _decimal(raw[1])
        else:
            continue
        if price > 0 and size > 0:
            levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=side_text == "SELL")
    remaining = Decimal(amount)
    for price, available_size in levels:
        remaining -= (
            price * available_size
            if side_text == "BUY" and unit == "QUOTE"
            else available_size
        )
        if remaining <= 0:
            return price
    return fallback


def _bucket_context(
    candidate: Mapping[str, Any],
    *,
    checkpoint: Mapping[str, Any] | None,
    side: str,
    size: Decimal,
    amount_unit: str,
    limit_price: Decimal,
    decision_ts: datetime,
) -> dict[str, Any]:
    tick = _decimal(candidate.get("tick_size"), Decimal("0.001"))
    bid = _decimal(candidate.get("best_bid"), Decimal("0"))
    ask = _decimal(candidate.get("best_ask"), Decimal("0"))
    spread_ticks = (ask - bid) / tick if tick > 0 else Decimal("0")
    levels = []
    if checkpoint:
        levels = checkpoint.get("asks" if side == "BUY" else "bids") or []
    visible_depth = visible_executable_depth(
        levels,
        side=side,
        amount_unit=amount_unit,
        limit_price=limit_price,
    )
    ratio = size / visible_depth if visible_depth > 0 else None
    raw_meta = candidate.get("raw_metadata") if isinstance(candidate.get("raw_metadata"), Mapping) else {}
    end_date = _parse_time(candidate.get("end_date") or raw_meta.get("endDate") or raw_meta.get("end_date"))
    source_category = (
        candidate.get("source_category")
        or candidate.get("category")
        or raw_meta.get("category")
        or "unknown"
    )
    return {
        "category": normalize_market_domain(
            source_category,
            market_title=candidate.get("market_title"),
            event_title=candidate.get("event_title"),
            market_slug=candidate.get("market_slug"),
        ),
        "source_category": source_category,
        "price_bucket": _price_bucket(limit_price),
        "spread_bucket": _spread_bucket(spread_ticks),
        "depth_bucket": _depth_bucket(visible_depth),
        "activity_bucket": _activity_bucket(_optional_decimal(candidate.get("event_volume"))),
        "time_to_resolution_bucket": _time_bucket(end_date - decision_ts if end_date else None),
        "coverage_grade": candidate.get("coverage_grade") or "unknown",
        "connection": candidate.get("connection_id") or (checkpoint or {}).get("source_connection_id") or "unknown",
        "shard": str(candidate.get("shard_id")) if candidate.get("shard_id") is not None else "unknown",
        "size_depth_ratio_bucket": _ratio_bucket(ratio),
        "tick_size": tick,
        "best_bid": bid,
        "best_ask": ask,
        "spread_ticks": spread_ticks,
        "visible_opposite_depth": visible_depth,
        "order_size": size,
        "amount_unit": amount_unit,
        "order_size_depth_ratio": ratio,
        "market_slug": candidate.get("market_slug"),
        "outcome_name": candidate.get("outcome_name"),
        "redundant_feed_match": bool(candidate.get("redundant_feed_match")),
        "last_receive_ts": candidate.get("last_receive_ts"),
    }


def _no_submit_lifecycle(probe_id: str, client_order_id: str) -> dict[str, Any]:
    return {
        "state": "NOT_SUBMITTED",
        "terminal_status": None,
        "event_count": 0,
        "events": [],
        "probe_id": probe_id,
        "client_order_id": client_order_id,
        "network_submit_called_by_probe": False,
        "reason": "safe default; no exchange order was sent",
    }


def _awaiting_lifecycle(probe_id: str, client_order_id: str) -> dict[str, Any]:
    return {
        "state": "AWAITING_IMPORT",
        "terminal_status": None,
        "event_count": 0,
        "events": [],
        "probe_id": probe_id,
        "client_order_id": client_order_id,
        "network_submit_called_by_probe": False,
        "reason": "record-only mode requires externally collected lifecycle events",
    }


def _initial_orderfilled_evidence(mode: str, decision_ts: datetime) -> dict[str, Any]:
    return {
        "state": "VERIFIED" if mode == NO_SUBMIT else "PENDING",
        "window_start": decision_ts,
        "window_end": None,
        "raw_count": 0,
        "deduped_count": 0,
        "duplicate_count": 0,
        "excluded_self_count": 0,
        "ex_self_count": 0,
        "events": [],
        "used_for_actual_live_outcome": False,
        "prediction": "PENDING",
        "reason": "no self contamination is possible in no-submit mode" if mode == NO_SUBMIT else "awaiting bounded OrderFilled ex-self join",
    }


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    false_positive = 0
    false_negative = 0
    class_matches = 0
    price_errors: list[Decimal] = []
    size_errors: list[Decimal] = []
    slippage_errors: list[Decimal] = []
    fee_errors: list[Decimal] = []
    latency_errors: list[Decimal] = []
    confusion: Counter[str] = Counter()
    for row in rows:
        paper = row.get("paper_prediction") if isinstance(row.get("paper_prediction"), Mapping) else {}
        live = row.get("live_lifecycle") if isinstance(row.get("live_lifecycle"), Mapping) else {}
        context = row.get("bucket_context") if isinstance(row.get("bucket_context"), Mapping) else {}
        paper_filled = str(paper.get("status")) in PAPER_FILL_STATUSES
        live_filled = str(live.get("terminal_status")) in LIVE_FILL_STATUSES
        confusion[f"{_paper_outcome_class(paper)}->{_live_outcome_class(live)}"] += 1
        class_matches += int(paper_filled == live_filled)
        false_positive += int(paper_filled and not live_filled)
        false_negative += int(not paper_filled and live_filled)
        paper_price = _optional_decimal(paper.get("avg_fill_price"))
        live_price = _optional_decimal(live.get("live_fill_price"))
        tick = _optional_decimal(context.get("tick_size"))
        if paper_price is not None and live_price is not None and tick and tick > 0:
            price_errors.append(abs(paper_price - live_price) / tick)
        paper_size = _optional_decimal(paper.get("filled_size"))
        live_size = _optional_decimal(live.get("live_fill_size"))
        if paper_size is not None and live_size is not None:
            size_errors.append(abs(paper_size - live_size))
        paper_slippage = _optional_decimal(paper.get("slippage"))
        live_slippage = _optional_decimal(live.get("live_slippage"))
        if paper_slippage is not None and live_slippage is not None:
            slippage_errors.append(abs(paper_slippage - live_slippage))
        paper_fee = _optional_decimal(paper.get("total_fee"))
        live_fee = _optional_decimal(live.get("live_fee"))
        if paper_fee is not None and live_fee is not None:
            fee_errors.append(abs(paper_fee - live_fee))
        decision_at = _parse_time(row.get("decision_ts"))
        paper_arrival_at = _parse_time(paper.get("arrival_ts"))
        live_ack_at = _parse_time(live.get("ack_at"))
        live_fill_at = _parse_time(live.get("fill_at"))
        if decision_at and paper_arrival_at and live_ack_at and live_fill_at:
            paper_latency = Decimal(str(max(0.0, (paper_arrival_at - decision_at).total_seconds())))
            live_latency = Decimal(str(max(0.0, (live_fill_at - live_ack_at).total_seconds())))
            latency_errors.append(abs(paper_latency - live_latency))
    count = len(rows)
    predicted_fills = sum(1 for row in rows if str((row.get("paper_prediction") or {}).get("status")) in PAPER_FILL_STATUSES)
    actual_fills = sum(1 for row in rows if str((row.get("live_lifecycle") or {}).get("terminal_status")) in LIVE_FILL_STATUSES)
    return {
        "sample_count": count,
        "classification_accuracy_pct": _pct(class_matches, count),
        "false_positive_count": false_positive,
        "false_positive_rate_pct": _pct(false_positive, predicted_fills),
        "false_negative_count": false_negative,
        "false_negative_rate_pct": _pct(false_negative, actual_fills),
        "avg_price_error_ticks": _average_text(price_errors),
        "avg_fill_size_error": _average_text(size_errors),
        "avg_slippage_error": _average_text(slippage_errors),
        "avg_fee_error": _average_text(fee_errors),
        "avg_ack_to_fill_latency_error_seconds": _average_text(latency_errors),
        "partial_full_reject_confusion_matrix": dict(sorted(confusion.items())),
    }


def _event_status(event: Mapping[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    for value in (
        payload.get("live_status"), payload.get("status"), event.get("api_order_status"),
        event.get("clob_order_status"), event.get("chain_order_status"),
        event.get("cancel_status"), event.get("accepted_status"), event.get("submit_status"),
    ):
        status = _canonical_status(value)
        if status != "UNKNOWN":
            return status
    return "UNKNOWN"


def _canonical_status(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not text:
        return "UNKNOWN"
    if "PARTIAL" in text:
        return "PARTIAL_FILLED"
    if "FILL" in text or "MATCH" in text:
        return "FILLED"
    if "CANCEL" in text:
        return "CANCELED"
    if "REJECT" in text:
        return "REJECTED"
    if "EXPIRE" in text:
        return "EXPIRED"
    if text in {"OPEN", "ACCEPTED", "SUBMITTED", "PENDING", "FAILED", "NO_FILL"}:
        return text
    return text


def _first_event_time(events: Sequence[Mapping[str, Any]], statuses: set[str]) -> str | None:
    for event in events:
        if _event_status(event) in statuses:
            value = event.get("event_time") or event.get("accepted_at") or event.get("submit_at")
            return str(value) if value not in (None, "") else None
    return None


def _latest_payload_value(events: Sequence[Mapping[str, Any]], *keys: str) -> Any:
    for event in reversed(events):
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        for key in keys:
            if payload.get(key) not in (None, ""):
                return payload.get(key)
    return None


def _ack_time(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in events:
        if event.get("accepted_at") not in (None, ""):
            return str(event["accepted_at"])
    return _first_event_time(events, {"OPEN", "ACCEPTED", "SUBMITTED", "PENDING"})


def _paper_outcome_class(paper: Mapping[str, Any]) -> str:
    status = str(paper.get("status") or "UNKNOWN")
    if status == "FILLED":
        return "FULL"
    if status == "PARTIAL":
        return "PARTIAL"
    if status == "REJECTED":
        return "REJECT"
    return "NO_FILL"


def _live_outcome_class(live: Mapping[str, Any]) -> str:
    status = str(live.get("terminal_status") or "UNKNOWN")
    if status == "FILLED":
        return "FULL"
    if status == "PARTIAL_FILLED":
        return "PARTIAL"
    if status in {"REJECTED", "FAILED"}:
        return "REJECT"
    return "NO_FILL"


def _compact_orderfilled(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "tx_hash", "log_index", "market_id", "condition_id", "asset_id",
        "order_hash", "maker", "taker", "side_code", "price", "size", "fee",
        "block_number", "block_time",
    )
    return _json_value({key: row.get(key) for key in keys if row.get(key) not in (None, "")})


def _price_bucket(price: Decimal) -> str:
    if price < Decimal("0.2"):
        return "0.00-0.20"
    if price < Decimal("0.4"):
        return "0.20-0.40"
    if price < Decimal("0.6"):
        return "0.40-0.60"
    if price < Decimal("0.8"):
        return "0.60-0.80"
    return "0.80-1.00"


def _spread_bucket(spread_ticks: Decimal) -> str:
    if spread_ticks <= 1:
        return "1_tick"
    if spread_ticks <= 3:
        return "2-3_ticks"
    if spread_ticks <= 10:
        return "4-10_ticks"
    return "gt_10_ticks"


def _depth_bucket(depth: Decimal) -> str:
    if depth <= 0:
        return "empty"
    if depth < 10:
        return "lt_10"
    if depth < 100:
        return "10-100"
    if depth < 1_000:
        return "100-1000"
    return "gte_1000"


def _activity_bucket(volume: Decimal | None) -> str:
    if volume is None:
        return "unknown"
    if volume < Decimal("1000"):
        return "low"
    if volume < Decimal("10000"):
        return "medium"
    return "high"


def _ratio_bucket(ratio: Decimal | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio <= Decimal("0.01"):
        return "lte_1pct"
    if ratio <= Decimal("0.1"):
        return "1-10pct"
    if ratio <= Decimal("0.5"):
        return "10-50pct"
    return "gt_50pct"


def _time_bucket(delta: timedelta | None) -> str:
    if delta is None:
        return "unknown"
    seconds = delta.total_seconds()
    if seconds <= 3_600:
        return "lte_1h"
    if seconds <= 86_400:
        return "1h-1d"
    if seconds <= 604_800:
        return "1d-7d"
    return "gt_7d"


def _pct(numerator: int, denominator: int) -> str | None:
    if denominator <= 0:
        return None
    return format((Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(Decimal("0.01")), "f")


def _average_text(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return format((sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001")), "f")


def _decimal(value: Any, default: Decimal | None = None) -> Decimal:
    if value in (None, ""):
        if default is None:
            raise ValueError("decimal value is required")
        return default
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _identity(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized[2:] if normalized.startswith("0x") else normalized


def _is_hex_digest(value: str, *, length: int) -> bool:
    return len(value) == length and all(ch in "0123456789abcdef" for ch in value)


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _utc(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _ch_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def report_to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2) + "\n"
