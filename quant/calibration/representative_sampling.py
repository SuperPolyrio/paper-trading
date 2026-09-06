"""Read-only representative market planning for live calibration probes."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from quant.core.db import postgres_connection

from .market_taxonomy import REPRESENTATIVE_DOMAINS, normalize_market_domain


DEFAULT_PER_DOMAIN = 3
DEFAULT_SHADOW_STATUS_MAX_AGE_SECONDS = 60.0

_CANDIDATE_SQL = """
WITH prior AS (
    SELECT market_id,
           count(*) FILTER (WHERE probe_state='CALIBRATABLE') AS calibratable_count,
           count(*) FILTER (WHERE exchange_submit_called) AS submitted_count
    FROM quant.paper_calibration_probes
    GROUP BY market_id
)
SELECT
    r.asset_id,
    r.market_id::text AS market_id,
    r.condition_id,
    r.market_slug,
    COALESCE(r.market_title, core_market.title, event_meta.event_title) AS market_title,
    r.outcome_name,
    COALESCE(r.current_tick_size, 0.001) AS tick_size,
    COALESCE(r.min_order_size, 1) AS min_order_size,
    COALESCE(live_book.best_bid, c.current_best_bid, r.best_bid) AS best_bid,
    COALESCE(live_book.best_ask, c.current_best_ask, r.best_ask) AS best_ask,
    COALESCE(live_book.coverage_grade, c.coverage_grade, 'DISCOVERY') AS coverage_grade,
    COALESCE(live_book.has_gap, c.has_gap, TRUE) AS has_gap,
    COALESCE(c.rest_reconciled, FALSE) AS rest_book_match,
    COALESCE(live_book.redundant_feed_match, c.redundant_feed_match) AS redundant_feed_match,
    COALESCE(live_book.observed_at, c.last_receive_ts, r.latest_book_at) AS last_receive_ts,
    COALESCE(live_book.observed_at, c.last_snapshot_ts, r.book_seen_last_at) AS last_snapshot_ts,
    COALESCE(live_book.source_connection_id, c.connection_id) AS connection_id,
    c.shard_id,
    COALESCE(
        live_book.book_status,
        CASE
            WHEN c.coverage_grade IN ('A_PLUS','A') AND c.has_gap=FALSE
                THEN 'READY'
            ELSE 'STALE'
        END
    ) AS book_status,
    COALESCE(
        live_book.transport_state,
        CASE WHEN c.redundant_feed_match IS TRUE THEN 'REDUNDANT' ELSE 'DEGRADED' END
    ) AS transport_state,
    COALESCE(w.enabled, FALSE) AS watchlisted,
    COALESCE(event_meta.event_category, core_market.category, r.raw_metadata->>'category') AS source_category,
    member.event_slug,
    event_meta.event_title,
    COALESCE(token_meta.end_date, core_market.end_date) AS end_date,
    COALESCE(event_meta.volume, member.volume, 0) AS event_volume,
    COALESCE(activity.event_count, 0) AS activity_event_count,
    COALESCE(activity.price_change_count, 0) AS activity_price_change_count,
    COALESCE(prior.calibratable_count, 0) AS prior_calibratable_count,
    COALESCE(prior.submitted_count, 0) AS prior_submitted_count,
    COALESCE(checkpoint.observed_at, live_book.observed_at, c.last_snapshot_ts) AS checkpoint_observed_at,
    COALESCE(checkpoint.bids, live_book.bids) AS checkpoint_bids,
    COALESCE(checkpoint.asks, live_book.asks) AS checkpoint_asks
FROM quant.paper_market_registry_tokens r
LEFT JOIN quant.clob_l2_current_coverage c USING (asset_id)
LEFT JOIN quant.paper_live_current_books live_book USING (asset_id)
LEFT JOIN quant.paper_live_watchlist w USING (asset_id)
LEFT JOIN core.markets core_market ON core_market.id=r.market_id
LEFT JOIN quant.market_token_metadata token_meta ON token_meta.token_id=r.asset_id
LEFT JOIN LATERAL (
    SELECT member_row.event_slug, member_row.volume
    FROM quant.market_event_members member_row
    WHERE member_row.market_id=r.market_id
    ORDER BY member_row.updated_at DESC, member_row.event_slug
    LIMIT 1
) member ON TRUE
LEFT JOIN quant.market_event_metadata event_meta ON event_meta.event_slug=member.event_slug
LEFT JOIN LATERAL (
    SELECT coverage.event_count, coverage.price_change_count
    FROM quant.clob_l2_active_active_token_hour_coverage coverage
    WHERE coverage.asset_id=r.asset_id
    ORDER BY coverage.hour_start DESC
    LIMIT 1
) activity ON TRUE
LEFT JOIN LATERAL (
    SELECT book.observed_at, book.bids, book.asks
    FROM quant.paper_live_book_checkpoints book
    WHERE book.asset_id=r.asset_id
      AND book.coverage_grade IN ('A_PLUS','A')
      AND book.has_gap=FALSE
      AND book.book_status='READY'
    ORDER BY book.observed_at DESC
    LIMIT 1
) checkpoint ON TRUE
LEFT JOIN prior ON prior.market_id=r.market_id::text
WHERE r.market_state='LIVE'
  AND r.execution_eligible=TRUE
  AND r.market_id IS NOT NULL
  AND r.active=TRUE
  AND r.closed=FALSE
  AND r.resolved=FALSE
  AND COALESCE(live_book.best_bid, c.current_best_bid, r.best_bid) > 0
  AND COALESCE(live_book.best_ask, c.current_best_ask, r.best_ask) < 1
  AND COALESCE(live_book.best_bid, c.current_best_bid, r.best_bid)
      < COALESCE(live_book.best_ask, c.current_best_ask, r.best_ask)
  AND COALESCE(r.market_title, core_market.title, event_meta.event_title, '')
      !~* 'placeholder'
  AND COALESCE(token_meta.end_date, core_market.end_date) IS NOT NULL
  AND COALESCE(token_meta.end_date, core_market.end_date)
      > statement_timestamp() + make_interval(secs => %s)
ORDER BY COALESCE(activity.event_count, 0) DESC,
         COALESCE(live_book.observed_at, c.last_receive_ts, r.latest_book_at) DESC,
         r.asset_id
LIMIT %s
"""


def load_representative_candidate_pool(
    *,
    limit: int = 5000,
    min_seconds_to_close: int = 6 * 3600,
) -> list[dict[str, Any]]:
    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            _CANDIDATE_SQL,
            (max(0, int(min_seconds_to_close)), max(1, int(limit))),
        )
        return [decorate_sampling_candidate(row) for row in cur.fetchall()]


def load_representative_shadow_status(
    candidates: Sequence[Mapping[str, Any]],
    *,
    status_path: Path | None = None,
    max_age_seconds: float = DEFAULT_SHADOW_STATUS_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load a fresh worker projection without requiring a local WS process."""

    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path_payload: dict[str, Any] = {}
    if status_path is not None:
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            path_payload = dict(loaded) if isinstance(loaded, Mapping) else {}
        except (OSError, TypeError, ValueError):
            path_payload = {}
    if _status_is_fresh(path_payload, observed_now, max_age_seconds):
        return path_payload

    asset_ids = sorted(
        {
            str(row.get("asset_id") or "").strip()
            for row in candidates
            if str(row.get("asset_id") or "").strip()
        }
    )
    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT worker_id, transport_state, watched_assets, ready_books,
                   execution_watched_assets, execution_ready_books,
                   execution_fresh_books, fresh_books, stale_books,
                   route_states, route_messages, last_message_at,
                   last_error, updated_at
            FROM quant.paper_live_shadow_health
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        health = cur.fetchone() or {}
        heads: list[dict[str, Any]] = []
        if asset_ids:
            cur.execute(
                """
                SELECT asset_id, observed_at, generation, coverage_grade,
                       book_status, has_gap, best_bid, best_ask,
                       book_fingerprint AS checkpoint_id,
                       transport_state, redundant_feed_match
                FROM quant.paper_live_current_books
                WHERE asset_id=ANY(%s::text[])
                  AND observed_at >= statement_timestamp()
                      - make_interval(secs => %s)
                ORDER BY observed_at DESC, asset_id
                """,
                (asset_ids, max(1, int(max_age_seconds))),
            )
            heads = [dict(row) for row in cur.fetchall()]
    route_states = (
        dict(health.get("route_states"))
        if isinstance(health.get("route_states"), Mapping)
        else {}
    )
    last_message_at = health.get("last_message_at")
    return {
        **dict(health),
        "updated_at": _json_value(health.get("updated_at")),
        "last_message_at": _json_value(last_message_at),
        "route_states": route_states,
        "route_last_message_at": {
            route: _json_value(last_message_at)
            for route, state in route_states.items()
            if state == "CONNECTED"
        },
        "fresh_book_sample": [_json_value(row) for row in heads],
        "projection_source": "postgres",
        "stale_status_path_ignored": bool(path_payload),
    }


def apply_live_shadow_status(
    candidates: Sequence[Mapping[str, Any]],
    status: Mapping[str, Any],
    *,
    max_age_seconds: float = 60.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status_updated_at = _datetime(status.get("updated_at"))
    status_age_seconds = (
        (observed_now - status_updated_at).total_seconds()
        if status_updated_at is not None
        else None
    )
    status_fresh = bool(
        status_age_seconds is not None
        and 0 <= status_age_seconds <= max(0.1, float(max_age_seconds))
    )
    heads = {
        str(item.get("asset_id") or ""): item
        for item in status.get("fresh_book_sample") or []
        if isinstance(item, Mapping) and item.get("asset_id")
    }
    route_states = status.get("route_states")
    transport_ready = bool(
        status_fresh
        and status.get("transport_state") == "REDUNDANT"
        and isinstance(route_states, Mapping)
        and route_states
        and all(state == "CONNECTED" for state in route_states.values())
        and not status.get("last_error")
    )
    output: list[dict[str, Any]] = []
    for source in candidates:
        candidate = dict(source)
        head = heads.get(str(candidate.get("asset_id") or ""))
        head_observed_at = _datetime(head.get("observed_at")) if head else None
        age_seconds = (
            (observed_now - head_observed_at).total_seconds()
            if head_observed_at is not None
            else None
        )
        if head:
            candidate["best_bid"] = head.get("best_bid") or candidate.get("best_bid")
            candidate["best_ask"] = head.get("best_ask") or candidate.get("best_ask")
            candidate["checkpoint_observed_at"] = head.get("observed_at")
            candidate["shadow_checkpoint_id"] = head.get("checkpoint_id")
        trusted_current_book = bool(
            str(candidate.get("book_status") or "").upper() == "READY"
            and str(candidate.get("coverage_grade") or "") in {"A_PLUS", "A"}
            and not bool(candidate.get("has_gap"))
            and candidate.get("redundant_feed_match")
            and candidate.get("transport_state") == "REDUNDANT"
        )
        ready = bool(
            transport_ready
            and candidate.get("watchlisted")
            and candidate.get("redundant_feed_match")
            and (
                trusted_current_book
                or (
                    head
                    and str(head.get("book_status") or "").upper() == "READY"
                    and str(head.get("coverage_grade") or "") in {"A_PLUS", "A"}
                    and not bool(head.get("has_gap"))
                    and age_seconds is not None
                    and 0 <= age_seconds <= max(0.1, float(max_age_seconds))
                )
            )
        )
        candidate["runtime_ready"] = ready
        candidate["runtime_readiness_reason"] = (
            "READY"
            if ready
            else "SHADOW_STATUS_STALE"
            if not status_fresh
            else "SHADOW_TRANSPORT_NOT_REDUNDANT"
            if not transport_ready
            else "NOT_WATCHLISTED"
            if not candidate.get("watchlisted")
            else "REDUNDANT_CHECK_FAILED"
            if not candidate.get("redundant_feed_match")
            else "FRESH_TRUSTED_HEAD_MISSING"
        )
        output.append(candidate)
    return output


def decorate_sampling_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate = dict(row)
    candidate["event_key"] = _event_key(candidate)
    source_category = str(candidate.get("source_category") or "unknown").strip().lower()
    candidate["source_category"] = source_category
    candidate["category_group"] = normalize_market_domain(
        source_category,
        market_title=candidate.get("market_title"),
        event_title=candidate.get("event_title"),
        market_slug=candidate.get("market_slug"),
    )
    bid = _decimal(candidate.get("best_bid"))
    ask = _decimal(candidate.get("best_ask"))
    tick = _decimal(candidate.get("tick_size"), Decimal("0.001"))
    midpoint = (bid + ask) / Decimal("2")
    spread_ticks = (ask - bid) / tick if tick > 0 else None
    candidate["midpoint"] = midpoint
    candidate["price_bucket"] = _price_bucket(midpoint)
    candidate["spread_ticks"] = spread_ticks
    candidate["spread_bucket"] = _spread_bucket(spread_ticks)
    candidate["activity_bucket"] = _activity_bucket(candidate.get("activity_event_count"))
    candidate["bid_depth_shares"] = _level_depth(candidate.get("checkpoint_bids"))
    candidate["ask_depth_shares"] = _level_depth(candidate.get("checkpoint_asks"))
    candidate["runtime_ready"] = bool(
        candidate.get("watchlisted")
        and candidate.get("rest_book_match")
        and candidate.get("checkpoint_observed_at")
        and str(candidate.get("coverage_grade") or "") in {"A_PLUS", "A"}
        and not bool(candidate.get("has_gap"))
        and bool(candidate.get("redundant_feed_match"))
    )
    candidate["runtime_checks_required"] = [
        "fresh_dual_feed_book",
        "fresh_rest_book_match",
        "complete_fee_tick_min_size_metadata",
        "user_ws_connected",
        "no_open_self_order",
        "balance_and_allowance",
        "scheduled_close_distance",
    ]
    return candidate


def select_representative_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    domains: Iterable[str] = REPRESENTATIVE_DOMAINS,
    per_domain: int = DEFAULT_PER_DOMAIN,
    require_unique_events: bool = True,
) -> list[dict[str, Any]]:
    """Greedily cover independent events and distinct microstructure regimes."""

    selected: list[dict[str, Any]] = []
    used_markets: set[str] = set()
    used_events: set[str] = set()
    target_domains = [str(domain).strip().lower() for domain in domains]
    for domain in target_domains:
        pool = [
            dict(row)
            for row in candidates
            if str(row.get("category_group") or "") == domain
            and str(row.get("market_id") or "") not in used_markets
        ]
        covered_prices: set[str] = set()
        covered_spreads: set[str] = set()
        covered_activities: set[str] = set()
        covered_outcomes: set[str] = set()
        for _ in range(max(0, int(per_domain))):
            available = [
                row for row in pool
                if str(row.get("market_id") or "") not in used_markets
                and (
                    not require_unique_events
                    or str(row.get("event_key") or _event_key(row)) not in used_events
                )
            ]
            if not available:
                break
            available.sort(
                key=lambda row: _selection_key(
                    row,
                    covered_prices=covered_prices,
                    covered_spreads=covered_spreads,
                    covered_activities=covered_activities,
                    covered_outcomes=covered_outcomes,
                )
            )
            chosen = available[0]
            selected.append(chosen)
            used_markets.add(str(chosen.get("market_id") or ""))
            used_events.add(str(chosen.get("event_key") or _event_key(chosen)))
            covered_prices.add(str(chosen.get("price_bucket") or "unknown"))
            covered_spreads.add(str(chosen.get("spread_bucket") or "unknown"))
            covered_activities.add(str(chosen.get("activity_bucket") or "unknown"))
            covered_outcomes.add(str(chosen.get("outcome_name") or "unknown").upper())
    return selected


def build_representative_sampling_report(
    candidates: Sequence[Mapping[str, Any]],
    *,
    domains: Iterable[str] = REPRESENTATIVE_DOMAINS,
    per_domain: int = DEFAULT_PER_DOMAIN,
    require_unique_events: bool = True,
) -> dict[str, Any]:
    target_domains = tuple(str(domain).strip().lower() for domain in domains)
    selected = select_representative_candidates(
        candidates,
        domains=target_domains,
        per_domain=per_domain,
        require_unique_events=require_unique_events,
    )
    by_domain: dict[str, dict[str, Any]] = {}
    for domain in target_domains:
        pool = [row for row in candidates if row.get("category_group") == domain]
        picks = [row for row in selected if row.get("category_group") == domain]
        by_domain[domain] = {
            "target_market_count": max(0, int(per_domain)),
            "eligible_market_count": len({str(row.get("market_id")) for row in pool}),
            "eligible_event_count": len(
                {str(row.get("event_key") or _event_key(row)) for row in pool}
            ),
            "eligible_token_count": len(pool),
            "runtime_ready_token_count": sum(bool(row.get("runtime_ready")) for row in pool),
            "selected_market_count": len(picks),
            "selected_event_count": len(
                {str(row.get("event_key") or _event_key(row)) for row in picks}
            ),
            "quota_status": "MET" if len(picks) >= max(0, int(per_domain)) else "SHORTFALL",
            "price_buckets": dict(Counter(str(row.get("price_bucket") or "unknown") for row in picks)),
            "spread_buckets": dict(Counter(str(row.get("spread_bucket") or "unknown") for row in picks)),
            "activity_buckets": dict(
                Counter(str(row.get("activity_bucket") or "unknown") for row in picks)
            ),
            "outcomes": dict(
                Counter(str(row.get("outcome_name") or "unknown").upper() for row in picks)
            ),
            "source_categories": dict(Counter(str(row.get("source_category") or "unknown") for row in pool)),
        }
    selected_payload = [_json_candidate(row) for row in selected]
    return {
        "schema_version": "representative_probe_sampling_plan_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "exchange_order_submitted": False,
        "watchlist_modified": False,
        "target_domains": list(target_domains),
        "target_markets_per_domain": max(0, int(per_domain)),
        "eligible_token_count": len(candidates),
        "selected_market_count": len(selected),
        "selected_event_count": len(
            {str(row.get("event_key") or _event_key(row)) for row in selected}
        ),
        "event_uniqueness_required": bool(require_unique_events),
        "domain_summary": by_domain,
        "selected_candidates": selected_payload,
        "execution_policy": {
            "phase_5c": "mechanism coverage with separate delayed-market cohorts",
            "phase_5d": "100-300 stratified paired probes after phase 5c passes",
            "sports_and_delayed_crypto": "separate calibration cohort; do not mix with no-delay baseline",
            "independent_event_first": bool(require_unique_events),
            "selection_is_not_authorization": True,
            "runtime_preflight_required": True,
        },
    }


def _selection_key(
    row: Mapping[str, Any],
    *,
    covered_prices: set[str],
    covered_spreads: set[str],
    covered_activities: set[str],
    covered_outcomes: set[str],
) -> tuple[Any, ...]:
    price_bucket = str(row.get("price_bucket") or "unknown")
    spread_bucket = str(row.get("spread_bucket") or "unknown")
    activity_bucket = str(row.get("activity_bucket") or "unknown")
    outcome_name = str(row.get("outcome_name") or "unknown").upper()
    return (
        0 if row.get("runtime_ready") else 1,
        0 if int(row.get("prior_calibratable_count") or 0) == 0 else 1,
        0 if price_bucket not in covered_prices else 1,
        0 if spread_bucket not in covered_spreads else 1,
        0 if activity_bucket not in covered_activities else 1,
        0 if outcome_name not in covered_outcomes else 1,
        int(row.get("prior_submitted_count") or 0),
        -int(row.get("activity_event_count") or 0),
        _decimal(row.get("spread_ticks"), Decimal("999999")),
        str(row.get("market_id") or ""),
        str(row.get("asset_id") or ""),
    )


def _json_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "category_group",
        "source_category",
        "event_key",
        "event_slug",
        "event_title",
        "market_id",
        "condition_id",
        "market_slug",
        "market_title",
        "asset_id",
        "outcome_name",
        "best_bid",
        "best_ask",
        "midpoint",
        "tick_size",
        "min_order_size",
        "price_bucket",
        "spread_ticks",
        "spread_bucket",
        "activity_bucket",
        "activity_event_count",
        "bid_depth_shares",
        "ask_depth_shares",
        "coverage_grade",
        "rest_book_match",
        "redundant_feed_match",
        "watchlisted",
        "runtime_ready",
        "runtime_readiness_reason",
        "shadow_checkpoint_id",
        "checkpoint_observed_at",
        "last_receive_ts",
        "end_date",
        "prior_calibratable_count",
        "prior_submitted_count",
        "runtime_checks_required",
    )
    return {key: _json_value(row.get(key)) for key in keys}


def _price_bucket(value: Decimal) -> str:
    if value < Decimal("0.10"):
        return "0.01-0.10"
    if value < Decimal("0.30"):
        return "0.10-0.30"
    if value <= Decimal("0.70"):
        return "0.30-0.70"
    if value <= Decimal("0.90"):
        return "0.70-0.90"
    return "0.90-0.99"


def _spread_bucket(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value <= 1:
        return "1_tick"
    if value <= 3:
        return "2_3_ticks"
    return "gt_3_ticks"


def _activity_bucket(value: Any) -> str:
    count = int(value or 0)
    if count <= 5:
        return "quiet"
    if count <= 100:
        return "normal"
    return "fast"


def _event_key(row: Mapping[str, Any]) -> str:
    event_slug = str(row.get("event_slug") or "").strip()
    if event_slug:
        return f"event:{event_slug}"
    condition_id = str(row.get("condition_id") or "").strip()
    if condition_id:
        return f"condition:{condition_id}"
    return f"market:{str(row.get('market_id') or '').strip()}"


def _level_depth(levels: Any) -> Decimal | None:
    if not isinstance(levels, list):
        return None
    total = Decimal("0")
    for level in levels:
        if isinstance(level, Mapping):
            size = level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            size = level[1]
        else:
            continue
        total += max(Decimal("0"), _decimal(size))
    return total


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status_is_fresh(
    payload: Mapping[str, Any],
    now: datetime,
    max_age_seconds: float,
) -> bool:
    updated_at = _datetime(payload.get("updated_at"))
    if updated_at is None:
        return False
    age_seconds = (now - updated_at).total_seconds()
    return -1 <= age_seconds <= max(0.1, float(max_age_seconds))
