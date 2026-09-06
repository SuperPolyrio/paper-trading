"""Current maker-fill evidence sourced from the paper WS execution process."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection
from quant.paper.paired_probe import OrderFilledEvidenceClient


class PersistedMakerTradeEvidenceClient:
    """Aggregate normalized ``last_trade_price`` events with a coverage gate."""

    source_name = "paper_live_ws_last_trade_price"

    def __init__(
        self,
        connection_factory: Any = postgres_connection,
        *,
        health_tolerance_seconds: int = 120,
    ) -> None:
        self.connection_factory = connection_factory
        self.health_tolerance_seconds = max(1, int(health_tolerance_seconds))

    def summarize_compatible_maker_volume(
        self,
        *,
        asset_id: str,
        maker_side: str,
        limit_price: Decimal,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        side = str(maker_side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("maker side must be BUY or SELL")
        start_utc = _utc(start)
        end_utc = _utc(end)
        if end_utc < start_utc:
            raise ValueError("maker evidence window end precedes start")
        aggressor_side = "SELL" if side == "BUY" else "BUY"
        price_operator = "<=" if side == "BUY" else ">="
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT count(*) AS trade_count,
                       COALESCE(sum(size),0) AS compatible_trade_volume,
                       percentile_disc(0.5) WITHIN GROUP (ORDER BY size)
                           AS median_trade_size,
                       percentile_disc(0.75) WITHIN GROUP (ORDER BY size)
                           AS p75_trade_size,
                       max(event_ts) AS last_trade_at
                FROM quant.paper_live_maker_trade_events
                WHERE asset_id=%s
                  AND event_ts >= %s
                  AND event_ts <= %s
                  AND aggressor_side=%s
                  AND price {price_operator} %s
                """,
                (str(asset_id), start_utc, end_utc, aggressor_side, limit_price),
            )
            aggregate = dict(cur.fetchone() or {})
            coverage = self._coverage(
                cur,
                asset_id=str(asset_id),
                start=start_utc,
                end=end_utc,
            )
        return {
            "source": self.source_name,
            "source_ready": bool(coverage["source_ready"]),
            "trade_count": int(aggregate.get("trade_count") or 0),
            "compatible_trade_volume": str(
                aggregate.get("compatible_trade_volume") or 0
            ),
            "median_trade_size": str(aggregate.get("median_trade_size") or 0),
            "p75_trade_size": str(aggregate.get("p75_trade_size") or 0),
            "last_trade_at": _iso(aggregate.get("last_trade_at")),
            **coverage,
        }

    def _coverage(
        self,
        cur: Any,
        *,
        asset_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        tolerance = timedelta(seconds=self.health_tolerance_seconds)
        cur.execute(
            """
            SELECT worker_id,transport_state,updated_at
            FROM quant.paper_live_shadow_health
            WHERE updated_at >= %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (end - tolerance,),
        )
        health = dict(cur.fetchone() or {})
        worker_id = str(health.get("worker_id") or "")
        if not worker_id or str(health.get("transport_state") or "") != "REDUNDANT":
            return {
                "source_ready": False,
                "coverage_worker_id": worker_id or None,
                "coverage_reason": "maker_trade_stream_not_redundant_or_stale",
                "coverage_stream_event_count": 0,
                "coverage_first_at": None,
                "coverage_last_at": _iso(health.get("updated_at")),
                "coverage_max_gap_seconds": None,
                "asset_assignment_ready": False,
            }
        cur.execute(
            """
            SELECT active,assigned_at,updated_at
            FROM quant.paper_live_target_assignments
            WHERE worker_id=%s AND asset_id=%s
            """,
            (worker_id, str(asset_id)),
        )
        assignment = dict(cur.fetchone() or {})
        assignment_ready = bool(
            assignment.get("active")
            and assignment.get("assigned_at")
            and _utc(assignment["assigned_at"]) <= start + tolerance
        )
        if not assignment_ready:
            return {
                "source_ready": False,
                "coverage_worker_id": worker_id,
                "coverage_reason": "asset_not_assigned_for_full_window",
                "coverage_stream_event_count": 0,
                "coverage_first_at": _iso(assignment.get("assigned_at")),
                "coverage_last_at": _iso(assignment.get("updated_at")),
                "coverage_max_gap_seconds": None,
                "asset_assignment_ready": False,
            }
        cur.execute(
            """
            SELECT count(*) AS stream_event_count
            FROM quant.paper_live_maker_trade_events
            WHERE worker_id=%s
              AND received_at >= %s
              AND received_at <= %s
            """,
            (worker_id, start - tolerance, end + tolerance),
        )
        stream_event_count = int(
            dict(cur.fetchone() or {}).get("stream_event_count") or 0
        )
        cur.execute(
            """
            WITH ordered AS (
                SELECT sampled_at,transport_state,
                       lag(sampled_at) OVER (ORDER BY sampled_at) AS prior_at
                FROM quant.paper_live_shadow_health_samples
                WHERE worker_id=%s
                  AND sampled_at >= %s
                  AND sampled_at <= %s
            )
            SELECT min(sampled_at) AS first_at,
                   max(sampled_at) AS last_at,
                   COALESCE(max(EXTRACT(EPOCH FROM sampled_at-prior_at)),0)
                       AS max_gap_seconds,
                   count(*) FILTER (WHERE transport_state <> 'REDUNDANT')
                       AS degraded_samples
            FROM ordered
            """,
            (worker_id, start - tolerance, end + tolerance),
        )
        window = dict(cur.fetchone() or {})
        first_at = window.get("first_at")
        last_at = window.get("last_at")
        max_gap = Decimal(str(window.get("max_gap_seconds") or 0))
        complete = bool(
            first_at
            and last_at
            and _utc(first_at) <= start + tolerance
            and _utc(last_at) >= end - tolerance
            and int(window.get("degraded_samples") or 0) == 0
            and max_gap <= Decimal(self.health_tolerance_seconds)
            and stream_event_count > 0
            and assignment_ready
        )
        if complete:
            coverage_reason = "redundant_health_window_complete"
        elif stream_event_count <= 0:
            coverage_reason = "maker_trade_stream_has_no_persisted_events"
        else:
            coverage_reason = "redundant_health_window_incomplete"
        return {
            "source_ready": complete,
            "coverage_worker_id": worker_id,
            "coverage_reason": coverage_reason,
            "coverage_stream_event_count": stream_event_count,
            "coverage_first_at": _iso(first_at),
            "coverage_last_at": _iso(last_at),
            "coverage_max_gap_seconds": format(max_gap, "f"),
            "asset_assignment_ready": assignment_ready,
            "asset_assigned_at": _iso(assignment.get("assigned_at")),
        }


class BestAvailableMakerTradeEvidenceClient:
    """Prefer current WS evidence and label delayed warehouse fallback explicitly."""

    def __init__(
        self,
        live_client: Any | None = None,
        delayed_client: Any | None = None,
    ) -> None:
        self.live_client = live_client or PersistedMakerTradeEvidenceClient()
        self.delayed_client = delayed_client or OrderFilledEvidenceClient()

    def summarize_compatible_maker_volume(self, **kwargs: Any) -> dict[str, Any]:
        live_error: str | None = None
        try:
            live = dict(
                self.live_client.summarize_compatible_maker_volume(**kwargs)
            )
            if live.get("source_ready"):
                return live
        except Exception as exc:  # noqa: BLE001
            live = {}
            live_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
        delayed = dict(
            self.delayed_client.summarize_compatible_maker_volume(**kwargs)
        )
        delayed_count = int(delayed.get("trade_count") or 0)
        if delayed_count <= 0 and int(live.get("trade_count") or 0) > 0:
            return {
                **live,
                "source": "paper_live_ws_last_trade_price_incomplete_window",
                "source_ready": False,
                "live_source_ready": False,
                "live_source_error": live_error,
                "coverage_reason": live.get("coverage_reason"),
                "delayed_source_ready": False,
                "delayed_trade_count": 0,
            }
        return {
            **delayed,
            "source": "clickhouse_orderfilled_delayed",
            "source_ready": delayed_count > 0,
            "live_source_ready": bool(live.get("source_ready")),
            "live_source_error": live_error,
            "live_coverage_reason": live.get("coverage_reason"),
        }


class CachedMakerTradeEvidenceClient:
    """Serve one immutable, watermark-checked batch of OrderFilled summaries."""

    source_name = "clickhouse_orderfilled_batch"

    def __init__(self, summaries: dict[str, dict[str, Any]]) -> None:
        self.summaries = {str(key): dict(value) for key, value in summaries.items()}

    def summarize_compatible_maker_volume(
        self,
        *,
        asset_id: str,
        maker_side: str | None = None,
        limit_price: Decimal | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        exact_key = maker_evidence_request_key(
            asset_id=asset_id,
            maker_side=maker_side,
            limit_price=limit_price,
        )
        return dict(
            self.summaries.get(
                exact_key,
                self.summaries.get(
                    str(asset_id),
                    {
                        "source": self.source_name,
                        "source_ready": False,
                        "coverage_reason": "maker_level_missing_from_batch",
                        "trade_count": 0,
                        "compatible_trade_volume": "0",
                        "last_trade_at": None,
                    },
                ),
            )
        )


def maker_evidence_request_key(
    *,
    asset_id: str,
    maker_side: str | None,
    limit_price: Decimal | None,
) -> str:
    """Return the stable cache key for one asset/side/price evidence query."""

    if maker_side is None or limit_price is None:
        return str(asset_id)
    price = Decimal(str(limit_price)).normalize()
    return f"{asset_id}|{str(maker_side).upper()}|{format(price, 'f')}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if value in (None, ""):
        return None
    return str(value)
