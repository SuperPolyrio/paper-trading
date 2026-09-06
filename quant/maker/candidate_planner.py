"""Read-only maker holdout candidate selection by expected queue outcome."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from quant.core.db import postgres_connection
from quant.market.api_client import book_probe_from_payload

from .live_probe_runner import (
    maker_limit_price,
    maker_predictions,
    maker_trade_forecast,
    resolved_maker_placement,
)

OUTCOMES = ("NO_FILL", "PARTIAL", "FULL")
CAMPAIGN_SIDES = ("BUY", "SELL")
CAMPAIGN_PLACEMENTS = (
    "AT_BEST",
    "ONE_TICK_BEHIND",
    "ONE_TICK_INSIDE_SPREAD",
)
CAMPAIGN_HORIZONS_SECONDS = (30, 120, 300, 900)


def enrich_candidates_with_recent_public_trades(
    candidates: Iterable[Mapping[str, Any]],
    trades: Iterable[Mapping[str, Any]],
    *,
    observed_at: datetime | None = None,
    lookback_seconds: int = 1800,
) -> list[dict[str, Any]]:
    """Attach bounded official taker tape without treating it as own-order truth."""

    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(seconds=max(1, int(lookback_seconds)))
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for source in trades:
        asset_id = str(source.get("asset") or "").strip()
        try:
            event_at = datetime.fromtimestamp(
                int(source.get("timestamp") or 0),
                tz=timezone.utc,
            )
            price = _nonnegative(source.get("price") or 0)
            size = _nonnegative(source.get("size") or 0)
        except (OSError, TypeError, ValueError):
            continue
        side = str(source.get("side") or "").upper()
        if (
            not asset_id
            or side not in {"BUY", "SELL"}
            or event_at < cutoff
            or event_at > now + timedelta(seconds=5)
            or price <= 0
            or size <= 0
        ):
            continue
        by_asset.setdefault(asset_id, []).append(
            {
                "side": side,
                "price": price,
                "size": size,
                "event_at": event_at,
                "transaction_hash": str(source.get("transactionHash") or ""),
            }
        )

    result: list[dict[str, Any]] = []
    for source in candidates:
        candidate = dict(source)
        rows = sorted(
            by_asset.get(str(candidate.get("asset_id") or ""), ()),
            key=lambda row: row["event_at"],
            reverse=True,
        )
        candidate.update(
            {
                "recent_public_trades": rows[:500],
                "recent_public_trade_count": len(rows),
                "recent_public_trade_last_at": (
                    rows[0]["event_at"] if rows else None
                ),
                "recent_public_trade_lookback_seconds": max(
                    1, int(lookback_seconds)
                ),
                "recent_public_trade_is_own_order_truth": False,
            }
        )
        result.append(candidate)
    return result


def enrich_candidates_with_gamma_activity(
    candidates: Iterable[Mapping[str, Any]],
    gamma_markets: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach current official discovery metrics without treating them as fills."""

    by_market_id: dict[str, Mapping[str, Any]] = {}
    by_condition_id: dict[str, Mapping[str, Any]] = {}
    for market in gamma_markets:
        market_id = str(market.get("id") or "").strip()
        condition_id = str(
            market.get("conditionId") or market.get("condition_id") or ""
        ).strip()
        if market_id:
            by_market_id[market_id] = market
        if condition_id:
            by_condition_id[condition_id.lower()] = market

    enriched: list[dict[str, Any]] = []
    for source in candidates:
        candidate = dict(source)
        market = by_market_id.get(str(candidate.get("market_id") or ""))
        if market is None:
            market = by_condition_id.get(
                str(candidate.get("condition_id") or "").lower()
            )
        if market is None:
            candidate.update(
                gamma_activity_matched=False,
                gamma_volume_24h=Decimal(0),
                gamma_liquidity=Decimal(0),
            )
            enriched.append(candidate)
            continue
        volume = _nonnegative(
            market.get("volume24hrClob")
            or market.get("volume24hr")
            or market.get("volumeNum")
            or market.get("volume")
            or 0
        )
        liquidity = _nonnegative(
            market.get("liquidityClob")
            or market.get("liquidityNum")
            or market.get("liquidity")
            or 0
        )
        eligible = bool(
            market.get("active") is not False
            and market.get("closed") is not True
            and market.get("enableOrderBook") is not False
            and market.get("acceptingOrders") is not False
        )
        candidate.update(
            gamma_activity_matched=True,
            gamma_candidate_eligible=eligible,
            gamma_volume_24h=volume,
            gamma_liquidity=liquidity,
            gamma_accepting_orders=market.get("acceptingOrders"),
            gamma_enable_order_book=market.get("enableOrderBook"),
            gamma_raw_payload_hash=market.get("_raw_payload_hash"),
        )
        enriched.append(candidate)
    return enriched


def reconcile_candidates_with_rest_books(
    candidates: Iterable[Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
    *,
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Overlay authoritative CLOB book parameters before maker ranking."""

    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reconciled: list[dict[str, Any]] = []
    for source in candidates:
        candidate = dict(source)
        asset_id = str(candidate.get("asset_id") or "")
        payload = books.get(asset_id)
        if not isinstance(payload, Mapping):
            candidate["rest_reconciliation_error"] = "rest_book_missing"
            reconciled.append(candidate)
            continue
        probe = book_probe_from_payload(asset_id, payload, observed_at=now)
        if not probe.ok or probe.best_bid is None or probe.best_ask is None:
            candidate["rest_reconciliation_error"] = (
                f"rest_book_not_two_sided:{probe.book_status}"
            )
            reconciled.append(candidate)
            continue
        tick_size = payload.get("tick_size") or payload.get("tickSize")
        minimum_size = payload.get("min_order_size") or payload.get("minOrderSize")
        if tick_size in (None, "", "0", 0) or minimum_size in (None, "", "0", 0):
            candidate["rest_reconciliation_error"] = (
                "rest_market_parameters_missing"
            )
            reconciled.append(candidate)
            continue
        candidate.update(
            {
                "local_best_bid": candidate.get("best_bid"),
                "local_best_ask": candidate.get("best_ask"),
                "local_tick_size": candidate.get("tick_size"),
                "local_min_order_size": candidate.get("min_order_size"),
                "best_bid": probe.best_bid,
                "best_ask": probe.best_ask,
                "bids": list(payload.get("bids") or ()),
                "asks": list(payload.get("asks") or ()),
                "tick_size": tick_size,
                "min_order_size": minimum_size,
                "rest_book_hash": str(payload.get("hash") or ""),
                "last_trade_price": payload.get("last_trade_price")
                or payload.get("lastTradePrice"),
                "rest_book_observed_at": now,
                "rest_reconciled": True,
            }
        )
        reconciled.append(candidate)
    return reconciled


def load_maker_candidate_pool(
    *,
    limit: int = 20,
    max_age_seconds: float = 60.0,
    connection_factory: Any = postgres_connection,
) -> list[dict[str, Any]]:
    """Load fresh, redundant books from the authoritative paper watchlist."""

    with connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                book.asset_id,
                book.market_id,
                book.condition_id,
                registry.market_slug,
                registry.market_title,
                registry.outcome_name,
                COALESCE(registry.current_tick_size, 0.001) AS tick_size,
                COALESCE(registry.min_order_size, 1) AS min_order_size,
                book.best_bid,
                book.best_ask,
                book.bids,
                book.asks,
                book.observed_at,
                book.coverage_grade,
                book.transport_state,
                book.redundant_feed_match
            FROM quant.paper_live_current_books book
            JOIN quant.paper_execution_market_catalog registry
              ON registry.asset_id=book.asset_id
            JOIN quant.paper_live_watchlist watchlist
              ON watchlist.asset_id=book.asset_id AND watchlist.enabled=TRUE
            WHERE registry.market_state='LIVE'
              AND registry.execution_eligible=TRUE
              AND registry.active=TRUE
              AND registry.closed=FALSE
              AND registry.resolved=FALSE
              AND book.book_status='READY'
              AND book.coverage_grade IN ('A_PLUS','A')
              AND book.has_gap=FALSE
              AND book.transport_state='REDUNDANT'
              AND book.redundant_feed_match=TRUE
              AND book.observed_at >= statement_timestamp() - make_interval(secs => %s)
              AND book.best_bid > 0
              AND book.best_ask < 1
              AND book.best_bid < book.best_ask
            ORDER BY book.observed_at DESC, book.asset_id
            LIMIT %s
            """,
            (max(0.1, float(max_age_seconds)), max(1, int(limit))),
        )
        return [dict(row) for row in cur.fetchall()]


def load_maker_discovery_pool(
    *,
    limit: int = 1_000,
    connection_factory: Any = postgres_connection,
) -> list[dict[str, Any]]:
    """Load every live execution token before applying book-quality gates."""

    with connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                registry.asset_id,
                registry.market_id,
                registry.condition_id,
                registry.market_slug,
                registry.market_title,
                registry.outcome_name,
                registry.category,
                registry.end_date,
                COALESCE(registry.current_tick_size, 0.001) AS tick_size,
                COALESCE(registry.min_order_size, 1) AS min_order_size,
                book.best_bid,
                book.best_ask,
                book.bids,
                book.asks,
                book.observed_at,
                book.coverage_grade,
                book.transport_state,
                book.redundant_feed_match,
                COALESCE(watchlist.enabled, FALSE) AS on_watchlist,
                COALESCE(
                    book.book_status='READY'
                    AND book.coverage_grade IN ('A_PLUS','A')
                    AND book.has_gap=FALSE
                    AND book.transport_state='REDUNDANT'
                    AND book.redundant_feed_match=TRUE,
                    FALSE
                ) AS shadow_ready
            FROM quant.paper_execution_market_catalog registry
            LEFT JOIN quant.paper_live_current_books book
              ON book.asset_id=registry.asset_id
            LEFT JOIN quant.paper_live_watchlist watchlist
              ON watchlist.asset_id=registry.asset_id
            WHERE registry.market_state='LIVE'
              AND registry.execution_eligible=TRUE
              AND registry.active=TRUE
              AND registry.closed=FALSE
              AND registry.resolved=FALSE
              AND registry.archived=FALSE
              AND registry.deprecated=FALSE
            ORDER BY registry.end_date NULLS LAST, registry.asset_id
            LIMIT %s
            """,
            (max(1, int(limit)),),
        )
        return [dict(row) for row in cur.fetchall()]


def load_recent_maker_trade_pool(
    *,
    lookback_seconds: int = 900,
    limit: int = 200,
    connection_factory: Any = postgres_connection,
) -> list[dict[str, Any]]:
    """Load LIVE assets with current official WS trade evidence first."""

    with connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH recent AS (
                SELECT trades.asset_id,
                       count(*) AS observed_trade_count,
                       count(DISTINCT trades.worker_id) AS observed_worker_count,
                       sum(trades.size) AS observed_trade_volume,
                       max(trades.event_ts) AS observed_last_trade_at
                FROM quant.paper_live_maker_trade_events trades
                WHERE trades.received_at >= statement_timestamp()
                      - make_interval(secs => %s)
                GROUP BY trades.asset_id
            )
            SELECT registry.asset_id,
                   registry.market_id,
                   registry.condition_id,
                   registry.market_slug,
                   registry.market_title,
                   registry.outcome_name,
                   registry.category,
                   registry.end_date,
                   COALESCE(registry.current_tick_size, 0.001) AS tick_size,
                   COALESCE(registry.min_order_size, 1) AS min_order_size,
                   book.best_bid,
                   book.best_ask,
                   book.bids,
                   book.asks,
                   book.observed_at,
                   book.coverage_grade,
                   book.transport_state,
                   book.redundant_feed_match,
                   COALESCE(watchlist.enabled, FALSE) AS on_watchlist,
                   recent.observed_trade_count,
                   recent.observed_worker_count,
                   recent.observed_trade_volume,
                   recent.observed_last_trade_at
            FROM recent
            JOIN quant.paper_execution_market_catalog registry
              ON registry.asset_id=recent.asset_id
            LEFT JOIN quant.paper_live_current_books book
              ON book.asset_id=registry.asset_id
            LEFT JOIN quant.paper_live_watchlist watchlist
              ON watchlist.asset_id=registry.asset_id
            WHERE registry.market_state='LIVE'
              AND registry.execution_eligible=TRUE
              AND registry.active=TRUE
              AND registry.closed=FALSE
              AND registry.resolved=FALSE
              AND registry.archived=FALSE
              AND registry.deprecated=FALSE
            ORDER BY recent.observed_last_trade_at DESC,
                     recent.observed_trade_volume DESC,
                     registry.asset_id
            LIMIT %s
            """,
            (max(1, int(lookback_seconds)), max(1, int(limit))),
        )
        return [dict(row) for row in cur.fetchall()]


def preselect_maker_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    side: str,
    placement: str,
    order_size: Decimal,
    max_notional_usd: Decimal,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bound expensive trade-evidence queries using current official books."""

    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for source in candidates:
        candidate = dict(source)
        asset_id = str(candidate.get("asset_id") or "")
        try:
            if candidate.get("rest_reconciliation_error"):
                raise ValueError(str(candidate["rest_reconciliation_error"]))
            if candidate.get("gamma_activity_matched") and not candidate.get(
                "gamma_candidate_eligible", False
            ):
                raise ValueError("gamma_market_not_accepting_orders")
            tick = _positive(candidate.get("tick_size"), "tick_size")
            size = max(
                _positive(order_size, "order_size"),
                _positive(candidate.get("min_order_size"), "min_order_size"),
            )
            resolved = resolved_maker_placement(
                candidate,
                side=side,
                placement=placement,
                tick_size=tick,
            )
            price = maker_limit_price(
                candidate,
                side=side,
                placement=resolved,
                tick_size=tick,
            )
            gross = size * price
            if gross > _positive(max_notional_usd, "max_notional_usd"):
                raise ValueError("minimum_size_exceeds_notional_limit")
            queue = _displayed_size(candidate, side=side, price=price)
            compatible = _compatible_recent_public_trades(
                candidate.get("recent_public_trades") or (),
                maker_side=side,
                limit_price=price,
            )
            compatible_volume = sum(
                (Decimal(str(row["size"])) for row in compatible),
                Decimal(0),
            )
            spread_ticks = (
                Decimal(str(candidate["best_ask"]))
                - Decimal(str(candidate["best_bid"]))
            ) / tick
            candidate.update(
                {
                    "resolved_placement": resolved,
                    "preselection_limit_price": price,
                    "preselection_order_size": size,
                    "preselection_queue_ahead": queue,
                    "preselection_spread_ticks": spread_ticks,
                    "recent_public_compatible_trade_count": len(compatible),
                    "recent_public_compatible_trade_volume": compatible_volume,
                    "recent_public_compatible_last_at": (
                        compatible[0]["event_at"] if compatible else None
                    ),
                }
            )
            selected.append(candidate)
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "asset_id": asset_id,
                    "market_id": str(candidate.get("market_id") or ""),
                    "reason": f"{exc.__class__.__name__}:{exc}",
                }
            )
    selected.sort(
        key=lambda row: (
            0 if row.get("recent_public_compatible_trade_count") else 1,
            -int(row.get("recent_public_compatible_trade_count") or 0),
            -_nonnegative(row.get("recent_public_compatible_trade_volume") or 0),
            0 if Decimal(str(row["preselection_queue_ahead"])) == 0 else 1,
            0
            if row.get("resolved_placement")
            in {"ONE_TICK_INSIDE_SPREAD", "NEAR_OPPOSITE"}
            else 1,
            0 if _nonnegative(row.get("gamma_volume_24h") or 0) > 0 else 1,
            -_nonnegative(row.get("gamma_volume_24h") or 0),
            -_nonnegative(row.get("gamma_liquidity") or 0),
            _last_trade_distance_ticks(row),
            Decimal(str(row["preselection_queue_ahead"])),
            0 if row.get("on_watchlist") else 1,
            -Decimal(str(row["preselection_spread_ticks"])),
            str(row.get("asset_id") or ""),
        )
    )
    return selected[: max(1, int(limit))], rejected


def _compatible_recent_public_trades(
    rows: Iterable[Mapping[str, Any]],
    *,
    maker_side: str,
    limit_price: Decimal,
) -> list[dict[str, Any]]:
    side = str(maker_side).upper()
    expected_taker_side = "SELL" if side == "BUY" else "BUY"
    compatible: list[dict[str, Any]] = []
    for source in rows:
        try:
            price = Decimal(str(source.get("price") or 0))
            size = Decimal(str(source.get("size") or 0))
        except InvalidOperation:
            continue
        if str(source.get("side") or "").upper() != expected_taker_side:
            continue
        touches = price <= limit_price if side == "BUY" else price >= limit_price
        if touches and size > 0:
            compatible.append(dict(source))
    compatible.sort(key=lambda row: row.get("event_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return compatible


def rank_maker_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    evidence_client: Any,
    side: str = "BUY",
    placement: str = "AT_BEST",
    order_size: Decimal = Decimal(5),
    resting_seconds: Decimal = Decimal(60),
    lookback_seconds: int = 3600,
    max_notional_usd: Decimal = Decimal(1),
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Rank candidates without submitting an order or changing a watchlist."""

    normalized_side = str(side).upper()
    normalized_placement = str(placement).upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if normalized_placement not in {
        "AT_BEST",
        "ONE_TICK_BEHIND",
        "ONE_TICK_INSIDE_SPREAD",
        "NEAR_OPPOSITE",
        "ADAPTIVE_FRONT",
    }:
        raise ValueError("unsupported maker placement")
    requested_size = _positive(order_size, "order_size")
    horizon = _positive(resting_seconds, "resting_seconds")
    notional_limit = _positive(max_notional_usd, "max_notional_usd")
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

    ranked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for source in candidates:
        candidate = dict(source)
        asset_id = str(candidate.get("asset_id") or "")
        if not asset_id:
            rejected.append({"reason": "asset_id_missing"})
            continue
        if candidate.get("rest_reconciliation_error"):
            rejected.append(
                {
                    "asset_id": asset_id,
                    "market_id": str(candidate.get("market_id") or ""),
                    "reason": str(candidate["rest_reconciliation_error"]),
                }
            )
            continue
        try:
            tick_size = _positive(candidate.get("tick_size") or "0.001", "tick_size")
            minimum_size = _positive(
                candidate.get("min_order_size") or "1",
                "min_order_size",
            )
            size = max(requested_size, minimum_size)
            price = maker_limit_price(
                candidate,
                side=normalized_side,
                placement=normalized_placement,
                tick_size=tick_size,
            )
            gross_notional = size * price
            if gross_notional > notional_limit:
                rejected.append(
                    {
                        "asset_id": asset_id,
                        "market_id": str(candidate.get("market_id") or ""),
                        "reason": "minimum_size_exceeds_notional_limit",
                        "minimum_size": format(size, "f"),
                        "limit_price": format(price, "f"),
                        "gross_notional_usd": format(gross_notional, "f"),
                    }
                )
                continue
            forecast = maker_trade_forecast(
                evidence_client,
                asset_id=asset_id,
                side=normalized_side,
                price=price,
                horizon_seconds=horizon,
                observed_at=now,
                lookback_seconds=max(1, int(lookback_seconds)),
            )
            if forecast.get("status") != "READY":
                rejected.append(
                    {
                        "asset_id": asset_id,
                        "market_id": str(candidate.get("market_id") or ""),
                        "reason": "maker_trade_evidence_not_ready",
                        "forecast_source": forecast.get("source"),
                        "forecast_status": forecast.get("status"),
                        "coverage_reason": forecast.get("coverage_reason"),
                        "live_source_error": forecast.get("live_source_error"),
                    }
                )
                continue
            projected = max(
                Decimal(0),
                Decimal(str(forecast.get("forecast_trade_volume") or 0)),
            )
            predictions = maker_predictions(
                candidate,
                asset_id=asset_id,
                side=normalized_side,
                price=price,
                size=size,
                horizon_seconds=horizon,
                run_id="maker-candidate-plan",
                forecast_trade_volume=projected,
                aggressor_arrival_probability=Decimal(
                    str(forecast.get("aggressor_arrival_probability") or 0)
                ),
            )
            strict = predictions["STRICT_TRADE_EVIDENCE"]
            queue_ahead = Decimal(str(strict["queue_ahead_estimate"]))
            executable = max(Decimal(0), projected - queue_ahead)
            fill_fraction = min(Decimal(1), executable / size)
            target = _target_outcome(fill_fraction)
            resolved_placement = resolved_maker_placement(
                candidate,
                side=normalized_side,
                placement=normalized_placement,
                tick_size=tick_size,
            )
            ranked.append(
                {
                    "predicted_outcome": target,
                    "prediction_is_live_truth": False,
                    "prediction_basis": "pre_decision_trailing_trade_rate_and_l2_queue",
                    "priority_score": format(
                        _priority_score(
                            target,
                            fill_fraction=fill_fraction,
                            projected=projected,
                            queue_ahead=queue_ahead,
                            trade_count=int(forecast.get("trade_count") or 0),
                        ),
                        "f",
                    ),
                    "asset_id": asset_id,
                    "market_id": str(candidate.get("market_id") or ""),
                    "condition_id": str(candidate.get("condition_id") or ""),
                    "market_slug": candidate.get("market_slug"),
                    "market_title": candidate.get("market_title"),
                    "outcome_name": candidate.get("outcome_name"),
                    "category": candidate.get("category"),
                    "end_date": candidate.get("end_date"),
                    "side": normalized_side,
                    "placement": normalized_placement,
                    "resolved_placement": resolved_placement,
                    "order_size": format(size, "f"),
                    "limit_price": format(price, "f"),
                    "gross_notional_usd": format(gross_notional, "f"),
                    "queue_ahead": format(queue_ahead, "f"),
                    "forecast_trade_volume": format(projected, "f"),
                    "strict_executable_size": format(executable, "f"),
                    "strict_fill_fraction": format(fill_fraction, "f"),
                    "trade_count": int(forecast.get("trade_count") or 0),
                    "last_trade_at": forecast.get("last_trade_at"),
                    "observed_trade_count": int(
                        candidate.get("observed_trade_count") or 0
                    ),
                    "observed_worker_count": int(
                        candidate.get("observed_worker_count") or 0
                    ),
                    "observed_trade_volume": format(
                        Decimal(str(candidate.get("observed_trade_volume") or 0)),
                        "f",
                    ),
                    "observed_last_trade_at": candidate.get(
                        "observed_last_trade_at"
                    ),
                    "gamma_activity_matched": bool(
                        candidate.get("gamma_activity_matched")
                    ),
                    "gamma_volume_24h": format(
                        _nonnegative(candidate.get("gamma_volume_24h") or 0),
                        "f",
                    ),
                    "gamma_liquidity": format(
                        _nonnegative(candidate.get("gamma_liquidity") or 0),
                        "f",
                    ),
                    "forecast_status": forecast.get("status"),
                    "forecast_source": forecast.get("source"),
                    "forecast_source_ready": forecast.get("source_ready"),
                    "forecast_coverage_reason": forecast.get("coverage_reason"),
                    "forecast_coverage_watermark_at": forecast.get(
                        "coverage_watermark_at"
                    ),
                    "forecast_evidence_window_start": forecast.get(
                        "evidence_window_start"
                    ),
                    "forecast_evidence_window_end": forecast.get(
                        "evidence_window_end"
                    ),
                    "model_predictions": predictions,
                }
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "asset_id": asset_id,
                    "market_id": str(candidate.get("market_id") or ""),
                    "reason": f"{exc.__class__.__name__}:{exc}",
                }
            )

    ranked.sort(
        key=lambda row: (Decimal(str(row["priority_score"])), row["asset_id"]),
        reverse=True,
    )
    by_target = {
        outcome: [row for row in ranked if row["predicted_outcome"] == outcome]
        for outcome in OUTCOMES
    }
    return {
        "schema_version": "maker_holdout_candidate_plan_v3",
        "generated_at": now.isoformat(),
        "status": "FORECAST_READY" if ranked else "NO_CANDIDATES",
        "evidence_status": "FORECAST_ONLY_LIVE_ORDER_REQUIRED",
        "exchange_submit_called": False,
        "side": normalized_side,
        "placement": normalized_placement,
        "requested_order_size": format(requested_size, "f"),
        "resting_seconds": format(horizon, "f"),
        "max_notional_usd": format(notional_limit, "f"),
        "candidate_count": len(ranked),
        "rejected_count": len(rejected),
        "predicted_outcome_counts": {
            name: len(rows) for name, rows in by_target.items()
        },
        "recommended_predictions": {
            name: rows[0] if rows else None for name, rows in by_target.items()
        },
        "candidates": ranked,
        "rejected": rejected,
    }


def plan_stratified_holdout_campaign(
    candidates: Iterable[Mapping[str, Any]],
    *,
    evidence_client: Any,
    order_size: Decimal = Decimal(5),
    max_notional_usd: Decimal = Decimal(5),
    lookback_seconds: int = 3600,
    sides: Iterable[str] = CAMPAIGN_SIDES,
    placements: Iterable[str] = CAMPAIGN_PLACEMENTS,
    horizons_seconds: Iterable[int] = CAMPAIGN_HORIZONS_SECONDS,
    candidates_per_stratum: int = 25,
    recommendations_per_outcome: int = 3,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a balanced, no-submit live holdout campaign across Maker strata."""

    source_rows = [dict(row) for row in candidates]
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    normalized_sides = tuple(dict.fromkeys(str(value).upper() for value in sides))
    normalized_placements = tuple(
        dict.fromkeys(str(value).upper() for value in placements)
    )
    normalized_horizons = tuple(
        sorted({int(value) for value in horizons_seconds if int(value) > 0})
    )
    if not normalized_sides or any(
        side not in CAMPAIGN_SIDES for side in normalized_sides
    ):
        raise ValueError("campaign sides must be BUY and/or SELL")
    if not normalized_placements or any(
        placement not in CAMPAIGN_PLACEMENTS
        for placement in normalized_placements
    ):
        raise ValueError("unsupported campaign placement")
    if not normalized_horizons or any(value > 1_800 for value in normalized_horizons):
        raise ValueError("campaign horizons must be between 1 and 1800 seconds")

    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    stratum_reports: list[dict[str, Any]] = []
    for side in normalized_sides:
        for placement in normalized_placements:
            preselected, preselection_rejected = preselect_maker_candidates(
                source_rows,
                side=side,
                placement=placement,
                order_size=order_size,
                max_notional_usd=max_notional_usd,
                limit=max(1, int(candidates_per_stratum)),
            )
            rejected.extend(
                {
                    **row,
                    "side": side,
                    "placement": placement,
                    "stage": "PRESELECTION",
                }
                for row in preselection_rejected
            )
            for horizon in normalized_horizons:
                plan = rank_maker_candidates(
                    preselected,
                    evidence_client=evidence_client,
                    side=side,
                    placement=placement,
                    order_size=order_size,
                    resting_seconds=Decimal(horizon),
                    lookback_seconds=lookback_seconds,
                    max_notional_usd=max_notional_usd,
                    observed_at=now,
                )
                stratum_id = f"{side}|{placement}|{horizon}"
                stratum_reports.append(
                    {
                        "stratum_id": stratum_id,
                        "status": plan["status"],
                        "candidate_count": plan["candidate_count"],
                        "predicted_outcome_counts": plan[
                            "predicted_outcome_counts"
                        ],
                    }
                )
                rejected.extend(
                    {
                        **row,
                        "side": side,
                        "placement": placement,
                        "horizon_seconds": horizon,
                        "stage": "RANKING",
                    }
                    for row in plan["rejected"]
                )
                for candidate in plan["candidates"]:
                    enriched = dict(candidate)
                    size = Decimal(str(enriched["order_size"]))
                    queue = Decimal(str(enriched["queue_ahead"]))
                    probabilistic = enriched["model_predictions"].get(
                        "PROBABILISTIC_QUEUE", {}
                    )
                    enriched.update(
                        {
                            "stratum_id": stratum_id,
                            "category": str(enriched.get("category") or "unknown"),
                            "horizon_seconds": horizon,
                            "queue_bucket": _queue_ratio_bucket(queue, size),
                            "predicted_fill_probability": str(
                                probabilistic.get("fill_probability") or "0"
                            ),
                            "predicted_outcome_is_truth": False,
                            "authenticated_own_order_truth_required": True,
                        }
                    )
                    rows.append(enriched)

    recommendations: dict[str, list[dict[str, Any]]] = {
        outcome: [] for outcome in OUTCOMES
    }
    for outcome in OUTCOMES:
        used_assets: set[str] = set()
        used_events: set[str] = set()
        target_rows = sorted(
            (row for row in rows if row["predicted_outcome"] == outcome),
            key=lambda row: (
                Decimal(str(row["priority_score"])),
                Decimal(str(row["predicted_fill_probability"])),
            ),
            reverse=True,
        )
        for row in target_rows:
            asset_id = str(row["asset_id"])
            event_id = str(row.get("condition_id") or row.get("market_id") or "")
            if asset_id in used_assets or (event_id and event_id in used_events):
                continue
            recommendations[outcome].append(row)
            used_assets.add(asset_id)
            if event_id:
                used_events.add(event_id)
            if len(recommendations[outcome]) >= max(
                1, int(recommendations_per_outcome)
            ):
                break

    target_counts = {
        outcome: sum(row["predicted_outcome"] == outcome for row in rows)
        for outcome in OUTCOMES
    }
    checks = {
        "no_exchange_submission": True,
        "both_sides_planned": all(
            any(row["side"] == side for row in rows) for side in normalized_sides
        ),
        "all_placements_planned": all(
            any(row["placement"] == placement for row in rows)
            for placement in normalized_placements
        ),
        "all_horizons_planned": all(
            any(int(row["horizon_seconds"]) == horizon for row in rows)
            for horizon in normalized_horizons
        ),
        "partial_candidate_available": target_counts["PARTIAL"] > 0,
        "full_candidate_available": target_counts["FULL"] > 0,
    }
    return {
        "schema_version": "maker_stratified_holdout_campaign_v1",
        "generated_at": now.isoformat(),
        "status": "CAMPAIGN_READY" if all(checks.values()) else "TARGET_GAPS",
        "evidence_status": "FORECAST_ONLY_AUTHENTICATED_LIVE_OUTCOME_REQUIRED",
        "exchange_submit_called": False,
        "automatic_submission_allowed": False,
        "candidate_prediction_is_live_truth": False,
        "checks": checks,
        "matrix": {
            "sides": list(normalized_sides),
            "placements": list(normalized_placements),
            "horizons_seconds": list(normalized_horizons),
        },
        "candidate_count": len(rows),
        "predicted_outcome_counts": target_counts,
        "recommendations": recommendations,
        "strata": stratum_reports,
        "candidates": rows,
        "rejected_count": len(rejected),
        "rejected": rejected,
    }


def _target_outcome(fill_fraction: Decimal) -> str:
    if fill_fraction <= 0:
        return "NO_FILL"
    if fill_fraction >= 1:
        return "FULL"
    return "PARTIAL"


def _queue_ratio_bucket(queue_ahead: Decimal, order_size: Decimal) -> str:
    if queue_ahead <= 0:
        return "Q0_FRONT"
    ratio = queue_ahead / max(Decimal("0.000001"), order_size)
    if ratio <= 1:
        return "Q1_LE_1X"
    if ratio <= 5:
        return "Q2_1_TO_5X"
    if ratio <= 20:
        return "Q3_5_TO_20X"
    return "Q4_GT_20X"


def _priority_score(
    target: str,
    *,
    fill_fraction: Decimal,
    projected: Decimal,
    queue_ahead: Decimal,
    trade_count: int,
) -> Decimal:
    activity = min(Decimal(1), Decimal(max(0, trade_count)) / Decimal(10))
    if target == "PARTIAL":
        return Decimal(3) - abs(fill_fraction - Decimal("0.5")) + activity
    if target == "FULL":
        coverage = projected / max(Decimal("0.00000001"), queue_ahead)
        return Decimal(2) + min(Decimal(1), coverage / Decimal(2)) + activity
    if projected <= 0:
        return Decimal(0)
    proximity = projected / max(Decimal("0.00000001"), queue_ahead)
    return Decimal(1) + min(Decimal(1), proximity) + activity


def _displayed_size(
    candidate: Mapping[str, Any], *, side: str, price: Decimal
) -> Decimal:
    rows = (
        candidate.get("bids")
        if str(side).upper() == "BUY"
        else candidate.get("asks")
    )
    for level in rows if isinstance(rows, list) else ():
        if isinstance(level, Mapping):
            level_price, level_size = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            level_price, level_size = level[:2]
        else:
            continue
        if Decimal(str(level_price or 0)) == price:
            return max(Decimal(0), Decimal(str(level_size or 0)))
    return Decimal(0)


def _last_trade_distance_ticks(candidate: Mapping[str, Any]) -> Decimal:
    try:
        last_trade = Decimal(str(candidate.get("last_trade_price") or 0))
        price = Decimal(str(candidate["preselection_limit_price"]))
        tick = Decimal(str(candidate["tick_size"]))
        if last_trade <= 0 or last_trade >= 1 or tick <= 0:
            return Decimal(1000000000)
        return abs(last_trade - price) / tick
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return Decimal(1000000000)


def _positive(value: Any, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)
    if not parsed.is_finite() or parsed < 0:
        return Decimal(0)
    return parsed
