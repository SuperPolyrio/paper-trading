"""Acceptance checks and soak runner for the taker-only paper shadow."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.core.db import postgres_connection
from quant.paper.cash_reconciliation import TOTAL, load_account_cash_delta_breakdown
from quant.paper.route_health import has_independent_connected_routes
from quant.validation.slo import evaluate_slos

DEFAULT_OUTPUT_DIR = Path("runtime_outputs/paper_live_shadow/acceptance")
MAX_DISCONNECTED_SAMPLE_RATIO = 0.01
MIN_REDUNDANT_SAMPLE_RATIO = 0.95
MIN_READY_ASSET_RATIO = 0.98
MIN_READY_SAMPLE_PASS_RATIO = 0.99
AVAILABLE_TRANSPORT_STATES = frozenset({"REDUNDANT", "DEGRADED"})
SOAK_SLO_CHECKS = (
    "book_freshness_p99",
    "paper_execution_backlog_age_p99",
    "execution_timestamp_ordering",
    "unsafe_fill_zero",
    "unknown_terminal_zero",
    "accounting_mismatch_zero",
)


def build_health_gap_analysis(
    *,
    start_at: datetime,
    end_at: datetime,
    health_stale_seconds: float = 15.0,
    connection_factory: Any = postgres_connection,
) -> dict[str, Any]:
    """Classify health-observer gaps without treating them as venue outages."""
    start_at = _utc(start_at)
    end_at = _utc(end_at)
    if end_at <= start_at:
        raise ValueError("end_at must be after start_at")
    with connection_factory(readonly=True) as conn, conn.cursor() as cur:
        analysis = _query_health_gap_analysis(
            cur,
            start_at=start_at,
            end_at=end_at,
            limit_seconds=health_stale_seconds,
        )
    return _json_value(
        {
            "schema_version": "paper_health_gap_analysis_v1",
            "evidence_class": "POST_HOC_OBSERVER_VS_DATA_PLANE_CLASSIFICATION",
            "generated_at": datetime.now(timezone.utc),
            "start_at": start_at,
            "end_at": end_at,
            "health_stale_seconds": health_stale_seconds,
            "status": "PASS"
            if analysis["unexplained_data_plane_gap_count"] == 0
            else "FAIL",
            **analysis,
        }
    )


def build_acceptance_report(
    *,
    start_at: datetime,
    audit_start_at: datetime | None = None,
    minimum_hours: float = 4.0,
    min_intents: int = 4,
    health_stale_seconds: float = 15.0,
    connection_factory: Any = postgres_connection,
) -> dict[str, Any]:
    start_at = _utc(start_at)
    audit_start_at = _utc(audit_start_at or start_at)
    with connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp() AS now")
        now = _utc(cur.fetchone()["now"])
        cur.execute(
            "SELECT * FROM quant.paper_live_shadow_health ORDER BY updated_at DESC LIMIT 1"
        )
        health = dict(cur.fetchone() or {})
        cur.execute(
            """
            WITH ordered AS (
                SELECT sampled_at, websocket_messages, route_messages,
                       lag(sampled_at) OVER (ORDER BY sampled_at) AS previous_at,
                       lag(websocket_messages) OVER (ORDER BY sampled_at) AS previous_messages,
                       lag(route_messages) OVER (ORDER BY sampled_at) AS previous_route_messages
                FROM quant.paper_live_shadow_health_samples
                WHERE sampled_at >= %s
            )
            SELECT count(*) AS samples,
                   count(*) FILTER (
                       WHERE transport_state='REDUNDANT'
                         AND COALESCE(route_proxy_urls->>'primary', 'direct://')
                             <> COALESCE(route_proxy_urls->>'secondary', 'direct://')
                   ) AS redundant_samples,
                   count(*) FILTER (WHERE transport_state NOT IN ('REDUNDANT','DEGRADED')) AS disconnected_samples,
                   count(*) FILTER (WHERE last_error IS NOT NULL) AS error_samples,
                   COALESCE(min(
                       COALESCE(execution_ready_books, ready_books)::numeric
                       / NULLIF(
                           COALESCE(execution_watched_assets, watched_assets), 0
                       )
                   ), 0) AS min_ready_ratio,
                   count(*) FILTER (
                       WHERE COALESCE(execution_watched_assets, watched_assets) > 0
                   ) AS ready_ratio_samples,
                   count(*) FILTER (
                       WHERE COALESCE(execution_watched_assets, watched_assets) > 0
                         AND COALESCE(execution_ready_books, ready_books)::numeric
                             / COALESCE(execution_watched_assets, watched_assets) >= %s
                   ) AS ready_ratio_pass_samples,
                   COALESCE(max(feed_mismatch_assets), 0) AS max_feed_mismatch_assets,
                   COALESCE(sum(GREATEST(
                       h.websocket_messages - o.previous_messages, 0
                   )), 0) AS message_progress,
                   COALESCE(sum(GREATEST(
                       COALESCE((h.route_messages->>'primary')::bigint, 0)
                       - COALESCE((o.previous_route_messages->>'primary')::bigint, 0), 0
                   )), 0) AS primary_message_progress,
                   COALESCE(sum(GREATEST(
                       COALESCE((h.route_messages->>'secondary')::bigint, 0)
                       - COALESCE((o.previous_route_messages->>'secondary')::bigint, 0), 0
                   )), 0) AS secondary_message_progress,
                   COALESCE(max(EXTRACT(EPOCH FROM (h.sampled_at - o.previous_at))), 0) AS max_sample_gap_seconds,
                   min(h.sampled_at) AS first_sample_at,
                   max(h.sampled_at) AS last_sample_at
            FROM quant.paper_live_shadow_health_samples h
            LEFT JOIN ordered o USING (sampled_at)
            WHERE h.sampled_at >= %s
            """,
            (start_at, MIN_READY_ASSET_RATIO, start_at),
        )
        samples = dict(cur.fetchone() or {})
        gap_analysis = _query_health_gap_analysis(
            cur,
            start_at=start_at,
            end_at=now,
            limit_seconds=health_stale_seconds,
        )
        cur.execute(
            """
            SELECT count(*) AS intents,
                   count(*) FILTER (WHERE filled_size > 0) AS fill_results,
                   count(*) FILTER (WHERE filled_size = 0) AS no_fill_results,
                   count(*) FILTER (
                       WHERE filled_size > 0 AND (
                           status NOT IN ('FILLED','PARTIAL')
                           OR coverage_grade NOT IN ('A_PLUS','A','B')
                           OR arrival_checkpoint_id IS NULL
                           OR reason ILIKE ANY (ARRAY['%%stale%%','%%gap%%','%%data_not_ready%%'])
                       )
                   ) AS unsafe_fills,
                   count(*) FILTER (
                       WHERE filled_size > 0 AND decision_checkpoint_id IS NULL
                   ) AS missing_decision_checkpoint_fills
            FROM quant.paper_taker_order_audits
            WHERE arrival_ts >= %s
            """,
            (audit_start_at,),
        )
        audits = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT count(*) AS missing_applied_results
            FROM quant.paper_taker_order_audits a
            LEFT JOIN quant.paper_portfolio_applied_results p USING (audit_key)
            WHERE a.arrival_ts >= %s AND p.audit_key IS NULL
            """,
            (audit_start_at,),
        )
        applied = dict(cur.fetchone() or {})
        cur.execute(
            """
            WITH fill_totals AS (
                SELECT audit_key, COALESCE(sum(size), 0) AS fill_size
                FROM quant.paper_fills
                GROUP BY audit_key
            )
            SELECT count(*) AS fill_size_mismatches
            FROM quant.paper_taker_order_audits a
            LEFT JOIN fill_totals f USING (audit_key)
            WHERE a.arrival_ts >= %s
              AND abs(a.filled_size - COALESCE(f.fill_size, 0)) > 0.000000000001
            """,
            (audit_start_at,),
        )
        fill_consistency = dict(cur.fetchone() or {})
        cash_delta_breakdowns = load_account_cash_delta_breakdown(cur)
        cur.execute(
            """
            SELECT strategy_id,initial_cash,cash_balance
            FROM quant.paper_accounts
            ORDER BY strategy_id
            """
        )
        account_rows = [dict(row) for row in cur.fetchall()]
        cash_tolerance = Decimal("0.000000000001")
        cash_mismatches: list[dict[str, Any]] = []
        negative_cash_accounts = 0
        cash_source_totals: dict[str, Decimal] = {}
        for breakdown in cash_delta_breakdowns.values():
            for source, amount in breakdown.items():
                if source != TOTAL:
                    cash_source_totals[source] = cash_source_totals.get(
                        source, Decimal(0)
                    ) + amount
        for row in account_rows:
            strategy_id = str(row["strategy_id"])
            initial_cash = Decimal(row["initial_cash"])
            actual_cash = Decimal(row["cash_balance"])
            breakdown = cash_delta_breakdowns.get(strategy_id, {})
            expected_cash = initial_cash + breakdown.get(TOTAL, Decimal(0))
            if actual_cash < 0:
                negative_cash_accounts += 1
            if abs(actual_cash - expected_cash) > cash_tolerance:
                cash_mismatches.append(
                    {
                        "strategy_id": strategy_id,
                        "expected_cash": expected_cash,
                        "actual_cash": actual_cash,
                        "difference": actual_cash - expected_cash,
                        "cash_delta_sources": breakdown,
                    }
                )
        account_consistency = {
            "cash_reconciliation_mismatches": len(cash_mismatches),
            "negative_cash_accounts": negative_cash_accounts,
            "cash_source_totals": cash_source_totals,
            "mismatch_examples": cash_mismatches[:20],
        }
        cur.execute(
            """
            WITH share_ledger AS (
                SELECT strategy_id, asset_id, COALESCE(sum(shares_delta), 0) AS shares
                FROM quant.paper_ledger_entries GROUP BY strategy_id, asset_id
            )
            SELECT count(*) FILTER (
                       WHERE abs(p.quantity - COALESCE(l.shares, 0)) > 0.000000000001
                   ) AS position_reconciliation_mismatches,
                   count(*) FILTER (WHERE p.quantity < 0 OR p.cost_basis < 0) AS negative_positions
            FROM quant.paper_positions p
            LEFT JOIN share_ledger l USING (strategy_id, asset_id)
            """
        )
        position_consistency = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE claimed_at IS NOT NULL) AS admitted_orders,
                   percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (claimed_at - decision_ts)) * 1000
                   ) FILTER (WHERE claimed_at IS NOT NULL) AS order_acceptance_p95_ms,
                   percentile_cont(0.99) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (claimed_at - decision_ts)) * 1000
                   ) FILTER (WHERE claimed_at IS NOT NULL) AS order_acceptance_p99_ms,
                   percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (
                           submit_arrival_ts - submit_request_ts
                       )) * 1000
                   ) FILTER (
                       WHERE submit_arrival_ts IS NOT NULL
                         AND submit_request_ts IS NOT NULL
                   ) AS admission_to_arrival_p95_ms,
                   percentile_cont(0.99) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (
                           submit_arrival_ts - submit_request_ts
                       )) * 1000
                   ) FILTER (
                       WHERE submit_arrival_ts IS NOT NULL
                         AND submit_request_ts IS NOT NULL
                   ) AS admission_to_arrival_p99_ms,
                   percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (completed_at - submit_arrival_ts)) * 1000
                   ) FILTER (
                       WHERE completed_at IS NOT NULL
                         AND submit_arrival_ts IS NOT NULL
                   ) AS arrival_to_result_p95_ms,
                   percentile_cont(0.99) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (completed_at - submit_arrival_ts)) * 1000
                   ) FILTER (
                       WHERE completed_at IS NOT NULL
                         AND submit_arrival_ts IS NOT NULL
                   ) AS arrival_to_result_p99_ms,
                   count(*) FILTER (
                       WHERE completed_at IS NOT NULL
                         AND time_in_force IN ('FOK','FAK')
                   ) AS terminal_orders,
                   percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (completed_at - decision_ts)) * 1000
                   ) FILTER (
                       WHERE completed_at IS NOT NULL
                         AND time_in_force IN ('FOK','FAK')
                   ) AS terminal_result_p95_ms,
                   percentile_cont(0.99) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (completed_at - decision_ts)) * 1000
                   ) FILTER (
                       WHERE completed_at IS NOT NULL
                         AND time_in_force IN ('FOK','FAK')
                   ) AS terminal_result_p99_ms,
                   count(*) FILTER (
                       WHERE (
                           submit_request_ts IS NOT NULL
                           AND submit_arrival_ts IS NOT NULL
                           AND submit_arrival_ts < submit_request_ts
                       ) OR (
                           submit_arrival_ts IS NOT NULL
                           AND completed_at IS NOT NULL
                           AND completed_at < submit_arrival_ts
                       )
                   ) AS invalid_timestamp_order_count
            FROM quant.paper_live_order_intents
            WHERE decision_ts >= %s
            """,
            (audit_start_at,),
        )
        execution_latency = dict(cur.fetchone() or {})
        cur.execute(
            """
            WITH active_cash AS (
                SELECT strategy_id,COALESCE(sum(reserved_cash),0) AS reserved_cash
                FROM quant.paper_order_reservations
                WHERE status='ACTIVE'
                GROUP BY strategy_id
            ), active_shares AS (
                SELECT strategy_id,asset_id,
                       COALESCE(sum(reserved_shares),0) AS reserved_shares
                FROM quant.paper_order_reservations
                WHERE status='ACTIVE'
                GROUP BY strategy_id,asset_id
            )
            SELECT (
                       SELECT count(*)
                       FROM quant.paper_accounts account
                       LEFT JOIN active_cash cash USING (strategy_id)
                       WHERE abs(
                           account.cash_reserved - COALESCE(cash.reserved_cash,0)
                       ) > 0.000000000001
                   ) + (
                       SELECT count(*)
                       FROM quant.paper_positions position
                       FULL OUTER JOIN active_shares shares
                         USING (strategy_id,asset_id)
                       WHERE abs(
                           COALESCE(position.reserved_quantity,0)
                           - COALESCE(shares.reserved_shares,0)
                       ) > 0.000000000001
                   ) AS reservation_mismatch_count
            """
        )
        reservation_consistency = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT count(*) AS journal_hash_mismatch_count
            FROM (
                SELECT journal_id
                FROM quant.paper_journal_lines
                GROUP BY journal_id
                HAVING abs(sum(debit) - sum(credit)) > 0.000000000001
            ) unbalanced
            """
        )
        journal_consistency = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT count(*) AS eligible_books,
                   percentile_cont(0.99) WITHIN GROUP (
                       ORDER BY book_age_ms
                   ) AS book_freshness_p99_ms
            FROM quant.paper_taker_order_audits
            WHERE arrival_ts >= %s
              AND arrival_checkpoint_id IS NOT NULL
              AND book_age_ms IS NOT NULL
              AND coverage_grade IN ('A_PLUS','A','B')
              AND COALESCE(reason, '') NOT ILIKE ANY (
                  ARRAY[
                      '%%stale%%',
                      '%%gap%%',
                      '%%data_not_ready%%',
                      '%%data_unsafe%%'
                  ]
              )
            """,
            (audit_start_at,),
        )
        book_freshness = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT count(*) AS pending_execution_intent_count,
                   COALESCE(
                       percentile_cont(0.99) WITHIN GROUP (
                           ORDER BY GREATEST(
                               EXTRACT(EPOCH FROM (
                                   clock_timestamp() - COALESCE(claimed_at, created_at)
                               )) * 1000,
                               0
                           )
                       ),
                       0
                   ) AS paper_execution_backlog_age_p99_ms
            FROM quant.paper_live_order_intents
            WHERE status IN ('QUEUED', 'PROCESSING')
            """
        )
        execution_backlog = dict(cur.fetchone() or {})
        # The registry outbox drives the all-token LOB subscription control
        # plane. It is retained as operational evidence, but does not gate the
        # colocated paper executor, which reads its dedicated watchlist and
        # BookState directly.
        cur.execute(
            "SELECT to_regclass('quant.paper_registry_outbox')::text AS relation"
        )
        if cur.fetchone()["relation"] is not None:
            cur.execute(
                """
                SELECT count(*) AS pending_registry_outbox_count,
                       COALESCE(
                           percentile_cont(0.99) WITHIN GROUP (
                               ORDER BY GREATEST(
                                   EXTRACT(EPOCH FROM (
                                       clock_timestamp() - created_at
                                   )) * 1000,
                                   0
                               )
                           ),
                           0
                       ) AS registry_outbox_pending_age_p99_ms
                FROM quant.paper_registry_outbox
                WHERE status='pending'
                """
            )
            registry_outbox = dict(cur.fetchone() or {})
        else:
            registry_outbox = {
                "pending_registry_outbox_count": None,
                "registry_outbox_pending_age_p99_ms": None,
                "source": "not_present_in_execution_authority_db",
            }
        cur.execute(
            """
            SELECT count(*) FILTER (
                       WHERE exchange_submit_called=TRUE
                         AND probe_state IN (
                             'SUBMITTING', 'ACKED', 'MATCHED',
                             'SUBMIT_OUTCOME_UNKNOWN', 'MANUAL_INTERVENTION'
                         )
                         AND updated_at < clock_timestamp() - interval '3 minutes'
                   ) AS unknown_terminal_order_count,
                   count(*) FILTER (
                       WHERE probe_state='ACCOUNTING_MISMATCH'
                   ) AS accounting_mismatch_count
            FROM quant.paper_calibration_probes
            WHERE updated_at >= %s
            """,
            (audit_start_at,),
        )
        terminal = dict(cur.fetchone() or {})

    duration_hours = max(0.0, (now - start_at).total_seconds() / 3600)
    sample_count = int(samples.get("samples") or 0)
    redundant_ratio = (
        float(
            Decimal(str(samples.get("redundant_samples") or 0)) / Decimal(sample_count)
        )
        if sample_count
        else 0.0
    )
    ready_ratio_sample_count = int(samples.get("ready_ratio_samples") or 0)
    ready_ratio_pass_rate = (
        float(
            Decimal(str(samples.get("ready_ratio_pass_samples") or 0))
            / Decimal(ready_ratio_sample_count)
        )
        if ready_ratio_sample_count
        else 0.0
    )
    disconnected_ratio = (
        float(
            Decimal(str(samples.get("disconnected_samples") or 0))
            / Decimal(sample_count)
        )
        if sample_count
        else 1.0
    )
    route_states = health.get("route_states") or {}
    route_proxy_urls = health.get("route_proxy_urls") or {}
    route_progress = {
        "primary": int(samples.get("primary_message_progress") or 0),
        "secondary": int(samples.get("secondary_message_progress") or 0),
    }
    health_age_seconds = (
        max(0.0, (now - _utc(health["updated_at"])).total_seconds())
        if health.get("updated_at")
        else None
    )
    checks = [
        _check(
            "health_fresh",
            health_age_seconds is not None
            and health_age_seconds <= health_stale_seconds,
            {
                "health_age_seconds": health_age_seconds,
                "limit_seconds": health_stale_seconds,
            },
        ),
        _check(
            "dual_transport",
            _dual_transport_available(
                str(health.get("transport_state") or ""),
                route_states,
                route_progress,
                route_proxy_urls=route_proxy_urls,
            ),
            {
                "transport_state": health.get("transport_state"),
                "route_states": route_states,
                "route_proxy_urls": route_proxy_urls,
                "independent_connected_routes": has_independent_connected_routes(
                    route_states,
                    route_proxy_urls,
                ),
                "route_message_progress": route_progress,
            },
        ),
        _check("health_samples_present", sample_count >= 2, samples),
        _status_check(
            "health_sample_continuity",
            _health_observer_continuity_status(gap_analysis),
            {
                "max_sample_gap_seconds": float(
                    samples.get("max_sample_gap_seconds") or 0
                ),
                "limit_seconds": health_stale_seconds,
                "observer_gap_count": gap_analysis["observer_gap_count"],
                "classification": "MONITORING_WARNING"
                if gap_analysis["observer_gap_count"]
                and gap_analysis["unexplained_data_plane_gap_count"] == 0
                else "NO_OBSERVER_GAP"
                if gap_analysis["observer_gap_count"] == 0
                else "DATA_PLANE_CONTINUITY_UNPROVEN",
            },
        ),
        _check(
            "execution_data_continuity",
            gap_analysis["unexplained_data_plane_gap_count"] == 0,
            {
                "observer_gap_count": gap_analysis["observer_gap_count"],
                "explained_observer_gap_count": gap_analysis[
                    "explained_observer_gap_count"
                ],
                "unexplained_data_plane_gap_count": gap_analysis[
                    "unexplained_data_plane_gap_count"
                ],
                "max_unexplained_gap_seconds": gap_analysis[
                    "max_unexplained_gap_seconds"
                ],
            },
        ),
        _check(
            "message_progress",
            int(samples.get("message_progress") or 0) > 0,
            {
                "message_progress": int(samples.get("message_progress") or 0),
            },
        ),
        _check(
            "transport_sample_continuity",
            _transport_stability_passes(
                disconnected_ratio=disconnected_ratio,
            ),
            {
                "disconnected_sample_ratio": disconnected_ratio,
                "available_sample_ratio": 1.0 - disconnected_ratio,
                "minimum_available_ratio": 1.0 - MAX_DISCONNECTED_SAMPLE_RATIO,
                "redundant_sample_ratio": redundant_ratio,
                "target_redundant_ratio": MIN_REDUNDANT_SAMPLE_RATIO,
                "redundancy_target_met": redundant_ratio >= MIN_REDUNDANT_SAMPLE_RATIO,
                "redundancy_target_is_advisory": True,
            },
        ),
        _check(
            "execution_readiness_counters_present",
            health.get("execution_watched_assets") is not None
            and health.get("execution_ready_books") is not None
            and health.get("execution_fresh_books") is not None,
            {
                "execution_watched_assets": health.get(
                    "execution_watched_assets"
                ),
                "execution_ready_books": health.get("execution_ready_books"),
                "execution_fresh_books": health.get("execution_fresh_books"),
                "required_worker_schema": "execution-readiness-v1",
            },
        ),
        _check(
            "ready_book_ratio",
            ready_ratio_pass_rate >= MIN_READY_SAMPLE_PASS_RATIO,
            {
                "ready_ratio_pass_rate": ready_ratio_pass_rate,
                "min_ready_ratio": samples.get("min_ready_ratio"),
                "minimum_ready_asset_ratio": MIN_READY_ASSET_RATIO,
                "minimum_sample_pass_ratio": MIN_READY_SAMPLE_PASS_RATIO,
            },
        ),
        _check(
            "runtime_intents", int(audits.get("intents") or 0) >= min_intents, audits
        ),
        _check(
            "runtime_fill_and_reject_paths",
            int(audits.get("fill_results") or 0) >= 1
            and int(audits.get("no_fill_results") or 0) >= 1,
            audits,
        ),
        _check(
            "unsafe_fills_zero",
            int(audits.get("unsafe_fills") or 0) == 0
            and int(audits.get("missing_decision_checkpoint_fills") or 0) == 0,
            audits,
        ),
        _check(
            "all_audits_applied_once",
            int(applied.get("missing_applied_results") or 0) == 0,
            applied,
        ),
        _check(
            "fill_rows_reconcile",
            int(fill_consistency.get("fill_size_mismatches") or 0) == 0,
            fill_consistency,
        ),
        _check(
            "cash_reconciles",
            int(account_consistency.get("cash_reconciliation_mismatches") or 0) == 0
            and int(account_consistency.get("negative_cash_accounts") or 0) == 0,
            account_consistency,
        ),
        _check(
            "positions_reconcile",
            int(position_consistency.get("position_reconciliation_mismatches") or 0)
            == 0
            and int(position_consistency.get("negative_positions") or 0) == 0,
            position_consistency,
        ),
        _check(
            "reservations_reconcile",
            int(reservation_consistency.get("reservation_mismatch_count") or 0) == 0,
            reservation_consistency,
        ),
        _check(
            "journal_balances",
            int(journal_consistency.get("journal_hash_mismatch_count") or 0) == 0,
            journal_consistency,
        ),
        _check(
            "execution_timestamp_ordering",
            int(execution_latency.get("invalid_timestamp_order_count") or 0) == 0,
            execution_latency,
        ),
        _check(
            "paper_execution_backlog_age_p99",
            float(execution_backlog.get("paper_execution_backlog_age_p99_ms") or 0)
            < 500,
            execution_backlog,
        ),
        _check(
            "unknown_terminal_zero",
            int(terminal.get("unknown_terminal_order_count") or 0) == 0,
            terminal,
        ),
    ]
    hard_failures = [item for item in checks if item["status"] == "FAIL"]
    soak_complete = duration_hours >= minimum_hours
    if soak_complete:
        status = "FAIL" if hard_failures else "PASS"
    else:
        status = "DEGRADED" if hard_failures else "WARMING"
    return _json_value(
        {
            "status": status,
            "generated_at": now,
            "start_at": start_at,
            "audit_start_at": audit_start_at,
            "duration_hours": duration_hours,
            "minimum_hours": minimum_hours,
            "soak_complete": soak_complete,
            "redundant_sample_ratio": redundant_ratio,
            "checks": checks,
            "health": health,
            "metrics": {
                "samples": samples,
                "health_gap_analysis": gap_analysis,
                "audits": audits,
                "applied": applied,
                "fill_consistency": fill_consistency,
                "account_consistency": account_consistency,
                "position_consistency": position_consistency,
                "execution_latency": execution_latency,
                "reservation_consistency": reservation_consistency,
                "journal_consistency": journal_consistency,
                "slo_snapshot": {
                    "book_freshness_p99_ms": book_freshness.get(
                        "book_freshness_p99_ms"
                    ),
                    "eligible_book_count": int(
                        book_freshness.get("eligible_books") or 0
                    ),
                    "paper_execution_backlog_age_p99_ms": execution_backlog.get(
                        "paper_execution_backlog_age_p99_ms"
                    ),
                    "pending_execution_intent_count": int(
                        execution_backlog.get("pending_execution_intent_count") or 0
                    ),
                    "registry_outbox_pending_age_p99_ms": registry_outbox.get(
                        "registry_outbox_pending_age_p99_ms"
                    ),
                    "pending_registry_outbox_count": int(
                        registry_outbox.get("pending_registry_outbox_count") or 0
                    ),
                    "unknown_terminal_order_count": int(
                        terminal.get("unknown_terminal_order_count") or 0
                    ),
                    "accounting_mismatch_count": int(
                        terminal.get("accounting_mismatch_count") or 0
                    ),
                    "unsafe_fill_count": int(audits.get("unsafe_fills") or 0),
                    "order_acceptance_sample_count": int(
                        execution_latency.get("admitted_orders") or 0
                    ),
                    "order_acceptance_p95_ms": execution_latency.get(
                        "order_acceptance_p95_ms"
                    ),
                    "order_acceptance_p99_ms": execution_latency.get(
                        "order_acceptance_p99_ms"
                    ),
                    "decision_to_admission_p95_ms": execution_latency.get(
                        "order_acceptance_p95_ms"
                    ),
                    "decision_to_admission_p99_ms": execution_latency.get(
                        "order_acceptance_p99_ms"
                    ),
                    "admission_to_arrival_p95_ms": execution_latency.get(
                        "admission_to_arrival_p95_ms"
                    ),
                    "admission_to_arrival_p99_ms": execution_latency.get(
                        "admission_to_arrival_p99_ms"
                    ),
                    "arrival_to_result_p95_ms": execution_latency.get(
                        "arrival_to_result_p95_ms"
                    ),
                    "arrival_to_result_p99_ms": execution_latency.get(
                        "arrival_to_result_p99_ms"
                    ),
                    "invalid_timestamp_order_count": int(
                        execution_latency.get("invalid_timestamp_order_count") or 0
                    ),
                    "terminal_result_sample_count": int(
                        execution_latency.get("terminal_orders") or 0
                    ),
                    "terminal_result_p95_ms": execution_latency.get(
                        "terminal_result_p95_ms"
                    ),
                    "terminal_result_p99_ms": execution_latency.get(
                        "terminal_result_p99_ms"
                    ),
                    "reservation_mismatch_count": int(
                        reservation_consistency.get("reservation_mismatch_count") or 0
                    ),
                    "journal_hash_mismatch_count": int(
                        journal_consistency.get("journal_hash_mismatch_count") or 0
                    ),
                    "execution_gate_ms": 60_000,
                },
            },
        }
    )


def run_soak(args: argparse.Namespace) -> int:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.resume_state
    start_at, resumed = _resolve_soak_start(
        state_path=state_path,
        latest_path=output_dir / "latest.json",
        duration_seconds=args.duration_seconds,
    )
    audit_start_at = _load_soak_audit_start(
        state_path=state_path,
        latest_path=output_dir / "latest.json",
        default=start_at,
    )
    if args.run_canary and not resumed:
        from .canary import run_canary

        canary_path = output_dir / "canary.json"
        audit_start_at = start_at
        try:
            canary = run_canary(
                output_path=canary_path,
                wait_seconds=float(getattr(args, "canary_wait_seconds", 30.0)),
            )
        except Exception as exc:  # noqa: BLE001 - publish structured gate evidence
            canary = {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "exception_class": type(exc).__name__,
            }
        if canary.get("status") != "PASS":
            failed_at = datetime.now(timezone.utc)
            failure = {
                "start_at": start_at,
                "audit_start_at": audit_start_at,
                "duration_seconds": args.duration_seconds,
                "completed_at": failed_at,
                "status": "CANARY_FAILED",
                "canary_status": canary.get("status"),
                "canary_error": canary.get("error"),
                "canary_wait_seconds": float(
                    getattr(args, "canary_wait_seconds", 30.0)
                ),
                "canary_path": str(canary_path),
                "strategy_id": canary.get("strategy_id"),
            }
            if state_path is not None:
                _write_json(state_path, failure)
            write_acceptance_artifacts(output_dir, failure)
            print(json.dumps(_json_value(failure), indent=2))
            return _completed_failure_exit_code(args)
        start_at = datetime.now(timezone.utc)
        if state_path is not None:
            _write_json(
                state_path,
                {
                    "start_at": start_at,
                    "audit_start_at": audit_start_at,
                    "duration_seconds": args.duration_seconds,
                    "status": "RUNNING",
                    "canary_status": "PASS",
                    "canary_path": str(canary_path),
                    "strategy_id": canary.get("strategy_id"),
                },
            )
    elapsed_seconds = max(0.0, (datetime.now(timezone.utc) - start_at).total_seconds())
    deadline = time.monotonic() + max(0.0, args.duration_seconds - elapsed_seconds)
    samples = _load_soak_samples(output_dir, start_at=start_at)
    sequence = len(samples)
    latest: dict[str, Any] = {}
    while True:
        latest = build_acceptance_report(
            start_at=start_at,
            audit_start_at=audit_start_at,
            minimum_hours=args.duration_seconds / 3600,
            min_intents=args.min_intents,
            health_stale_seconds=args.health_stale_seconds,
        )
        samples.append(latest)
        _attach_slo_window(latest, samples)
        write_acceptance_artifacts(output_dir, latest)
        _write_json(output_dir / f"sample-{sequence:05d}.json", latest)
        sequence += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(args.interval_seconds, remaining))
    latest = build_acceptance_report(
        start_at=start_at,
        audit_start_at=audit_start_at,
        minimum_hours=args.duration_seconds / 3600,
        min_intents=args.min_intents,
        health_stale_seconds=args.health_stale_seconds,
    )
    samples.append(latest)
    _attach_slo_window(latest, samples)
    write_acceptance_artifacts(output_dir, latest)
    if state_path is not None:
        _write_json(
            state_path,
            {
                "start_at": start_at,
                "audit_start_at": audit_start_at,
                "duration_seconds": args.duration_seconds,
                "completed_at": datetime.now(timezone.utc),
                "status": latest["status"],
            },
        )
    print(json.dumps(latest, indent=2))
    return 0 if latest["status"] == "PASS" else _completed_failure_exit_code(args)


def write_acceptance_artifacts(
    output_dir: Path,
    report: dict[str, Any],
) -> None:
    """Publish the current soak status as JSON, metrics, and Markdown."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "latest.json", report)
    _write_json(
        output_dir / "metrics.json",
        {
            "generated_at": report.get("generated_at"),
            "status": report.get("status"),
            "soak_complete": report.get("soak_complete"),
            "duration_hours": report.get("duration_hours"),
            "minimum_hours": report.get("minimum_hours"),
            "slo_snapshot": (report.get("metrics") or {}).get("slo_snapshot"),
            "slo_window": report.get("slo_window"),
        },
    )
    (output_dir / "report.md").write_text(
        render_acceptance_markdown(report),
        encoding="utf-8",
    )


def render_acceptance_markdown(report: dict[str, Any]) -> str:
    checks = list(report.get("checks") or [])
    slo_evaluation = (report.get("slo_window") or {}).get("evaluation") or {}
    rows = [
        "# Paper Simulator Soak Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Complete: `{bool(report.get('soak_complete'))}`",
        f"- Duration hours: `{report.get('duration_hours')}`",
        f"- Required hours: `{report.get('minimum_hours')}`",
        f"- Longitudinal SLO: `{slo_evaluation.get('status', 'NOT_ENOUGH_DATA')}`",
        "",
        "## Acceptance Checks",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    if checks:
        rows.extend(
            f"| `{row.get('name')}` | `{row.get('status')}` |" for row in checks
        )
    else:
        rows.append("| `canary` | `" + str(report.get("status")) + "` |")
    rows.extend(["", "## Longitudinal SLO", "", "| Check | Result |", "|---|---|"])
    slo_checks = slo_evaluation.get("checks") or {}
    if slo_checks:
        rows.extend(
            f"| `{name}` | `{value if value is not None else 'NOT_ENOUGH_DATA'}` |"
            for name, value in slo_checks.items()
        )
    else:
        rows.append("| `evidence` | `NOT_ENOUGH_DATA` |")
    return "\n".join(rows) + "\n"


def soak_promotion_eligible(report: dict[str, Any]) -> bool:
    slo = (report.get("slo_window") or {}).get("evaluation") or {}
    return bool(
        report.get("status") == "PASS"
        and report.get("soak_complete") is True
        and slo.get("status") == "PASS"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--start-at", required=True)
    check.add_argument("--minimum-hours", type=float, default=4.0)
    check.add_argument("--min-intents", type=int, default=4)
    check.add_argument("--health-stale-seconds", type=float, default=15.0)
    check.add_argument(
        "--json-out", type=Path, default=DEFAULT_OUTPUT_DIR / "latest.json"
    )
    soak = sub.add_parser("soak")
    soak.add_argument("--duration-seconds", type=float, default=14_400)
    soak.add_argument("--interval-seconds", type=float, default=60)
    soak.add_argument("--min-intents", type=int, default=4)
    soak.add_argument("--health-stale-seconds", type=float, default=15.0)
    soak.add_argument("--canary-wait-seconds", type=float, default=30.0)
    soak.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    soak.add_argument("--run-canary", action="store_true")
    soak.add_argument("--resume-state", type=Path)
    soak.add_argument(
        "--completed-failure-exit-code",
        type=int,
        default=1,
        help=(
            "Exit code for a completed FAIL/CANARY_FAILED result. A dedicated "
            "code lets a supervisor restart unexpected crashes without "
            "rerunning a finished failed acceptance window."
        ),
    )
    gaps = sub.add_parser("health-gaps")
    gaps.add_argument("--start-at", required=True)
    gaps.add_argument("--end-at", required=True)
    gaps.add_argument("--health-stale-seconds", type=float, default=15.0)
    gaps.add_argument(
        "--json-out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "health-gap-analysis.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "soak":
        return run_soak(args)
    if args.command == "health-gaps":
        report = build_health_gap_analysis(
            start_at=_parse_datetime(args.start_at),
            end_at=_parse_datetime(args.end_at),
            health_stale_seconds=args.health_stale_seconds,
        )
        _write_json(args.json_out, report)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "PASS" else 1
    report = build_acceptance_report(
        start_at=_parse_datetime(args.start_at),
        minimum_hours=args.minimum_hours,
        min_intents=args.min_intents,
        health_stale_seconds=args.health_stale_seconds,
    )
    _write_json(args.json_out, report)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


def _check(name: str, passed: bool, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def _status_check(
    name: str, status: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    normalized = str(status).upper()
    if normalized not in {"PASS", "WARN", "FAIL"}:
        raise ValueError(f"unsupported check status: {status}")
    return {"name": name, "status": normalized, "evidence": evidence}


def _query_health_gap_analysis(
    cur: Any,
    *,
    start_at: datetime,
    end_at: datetime,
    limit_seconds: float,
) -> dict[str, Any]:
    cur.execute(
        """
        WITH ordered AS (
            SELECT sample_id, worker_id, sampled_at, websocket_messages,
                   last_message_at, transport_state, route_states, route_messages,
                   ready_books, watched_assets,
                   execution_ready_books, execution_watched_assets,
                   lag(worker_id) OVER timeline AS previous_worker_id,
                   lag(sampled_at) OVER timeline AS previous_at,
                   lag(websocket_messages) OVER timeline AS previous_messages,
                   lag(last_message_at) OVER timeline AS previous_last_message_at,
                   lag(transport_state) OVER timeline AS previous_transport_state,
                   lag(route_states) OVER timeline AS previous_route_states,
                   lag(route_messages) OVER timeline AS previous_route_messages,
                   lag(ready_books) OVER timeline AS previous_ready_books,
                   lag(watched_assets) OVER timeline AS previous_watched_assets,
                   lag(execution_ready_books) OVER timeline
                       AS previous_execution_ready_books,
                   lag(execution_watched_assets) OVER timeline
                       AS previous_execution_watched_assets
            FROM quant.paper_live_shadow_health_samples
            WHERE sampled_at >= %s AND sampled_at <= %s
            WINDOW timeline AS (ORDER BY sampled_at, sample_id)
        )
        SELECT *
        FROM ordered
        WHERE previous_at IS NOT NULL
          AND EXTRACT(EPOCH FROM (sampled_at - previous_at)) > %s
        ORDER BY sampled_at - previous_at DESC
        """,
        (start_at, end_at, limit_seconds),
    )
    gaps = [
        _classify_sample_gap(dict(row), limit_seconds=limit_seconds)
        for row in cur.fetchall()
    ]
    unexplained = [
        row for row in gaps if row["classification"] == "DATA_PLANE_CONTINUITY_UNPROVEN"
    ]
    return {
        "observer_gap_count": len(gaps),
        "explained_observer_gap_count": len(gaps) - len(unexplained),
        "unexplained_data_plane_gap_count": len(unexplained),
        "max_observer_gap_seconds": max(
            (float(row["gap_seconds"]) for row in gaps), default=0.0
        ),
        "max_unexplained_gap_seconds": max(
            (float(row["gap_seconds"]) for row in unexplained), default=0.0
        ),
        "gaps": gaps,
    }


def _classify_sample_gap(
    row: dict[str, Any], *, limit_seconds: float
) -> dict[str, Any]:
    sampled_at = _utc(row["sampled_at"])
    previous_at = _utc(row["previous_at"])
    gap_seconds = max(0.0, (sampled_at - previous_at).total_seconds())
    current_routes = row.get("route_messages") or {}
    previous_routes = row.get("previous_route_messages") or {}
    route_progress = {
        route: int(current_routes.get(route, 0)) - int(previous_routes.get(route, 0))
        for route in set(current_routes) | set(previous_routes)
    }
    progressed_routes = sorted(
        route for route, progress in route_progress.items() if progress > 0
    )
    last_message_at = row.get("last_message_at")
    message_age_seconds = (
        max(0.0, (sampled_at - _utc(last_message_at)).total_seconds())
        if last_message_at is not None
        else None
    )
    watched_assets = int(
        row.get("execution_watched_assets")
        if row.get("execution_watched_assets") is not None
        else row.get("watched_assets") or 0
    )
    previous_watched_assets = int(
        row.get("previous_execution_watched_assets")
        if row.get("previous_execution_watched_assets") is not None
        else row.get("previous_watched_assets") or 0
    )
    ready_books = (
        row.get("execution_ready_books")
        if row.get("execution_ready_books") is not None
        else row.get("ready_books")
    )
    previous_ready_books = (
        row.get("previous_execution_ready_books")
        if row.get("previous_execution_ready_books") is not None
        else row.get("previous_ready_books")
    )
    ready_ratio = (
        float(Decimal(str(ready_books or 0)) / Decimal(watched_assets))
        if watched_assets
        else 0.0
    )
    previous_ready_ratio = (
        float(
            Decimal(str(previous_ready_books or 0))
            / Decimal(previous_watched_assets)
        )
        if previous_watched_assets
        else 0.0
    )
    reasons = []
    if row.get("worker_id") != row.get("previous_worker_id"):
        reasons.append("worker_changed")
    if str(row.get("transport_state") or "").upper() not in AVAILABLE_TRANSPORT_STATES:
        reasons.append("current_transport_unavailable")
    if (
        str(row.get("previous_transport_state") or "").upper()
        not in AVAILABLE_TRANSPORT_STATES
    ):
        reasons.append("previous_transport_unavailable")
    message_progress = int(row.get("websocket_messages") or 0) - int(
        row.get("previous_messages") or 0
    )
    if message_progress <= 0:
        reasons.append("no_websocket_message_progress")
    if not progressed_routes:
        reasons.append("no_route_message_progress")
    if message_age_seconds is None or message_age_seconds > limit_seconds:
        reasons.append("message_not_fresh_after_gap")
    if min(ready_ratio, previous_ready_ratio) < MIN_READY_ASSET_RATIO:
        reasons.append("ready_ratio_below_threshold")
    classification = (
        "OBSERVER_GAP_WITH_DATA_PROGRESS"
        if not reasons
        else "DATA_PLANE_CONTINUITY_UNPROVEN"
    )
    return _json_value(
        {
            "classification": classification,
            "reasons": reasons,
            "gap_seconds": gap_seconds,
            "previous_at": previous_at,
            "sampled_at": sampled_at,
            "previous_worker_id": row.get("previous_worker_id"),
            "worker_id": row.get("worker_id"),
            "previous_transport_state": row.get("previous_transport_state"),
            "transport_state": row.get("transport_state"),
            "websocket_message_progress": message_progress,
            "route_message_progress": route_progress,
            "progressed_routes": progressed_routes,
            "message_age_after_gap_seconds": message_age_seconds,
            "previous_ready_ratio": previous_ready_ratio,
            "ready_ratio": ready_ratio,
        }
    )


def _health_observer_continuity_status(analysis: dict[str, Any]) -> str:
    if int(analysis.get("unexplained_data_plane_gap_count") or 0) > 0:
        return "FAIL"
    if int(analysis.get("observer_gap_count") or 0) > 0:
        return "WARN"
    return "PASS"


def _dual_transport_available(
    transport_state: str,
    route_states: dict[str, Any],
    route_progress: dict[str, int],
    route_proxy_urls: dict[str, Any] | None = None,
) -> bool:
    connected = sum(
        str(state).upper() == "CONNECTED" for state in route_states.values()
    )
    route_urls = route_proxy_urls or {
        source: f"legacy-route://{source}" for source in route_states
    }
    progressed_routes = {
        source: "CONNECTED"
        for source, messages in route_progress.items()
        if int(messages) > 0
    }
    return (
        transport_state in {"REDUNDANT", "DEGRADED"}
        and connected >= 1
        and has_independent_connected_routes(progressed_routes, route_urls)
        and route_progress.get("primary", 0) > 0
        and route_progress.get("secondary", 0) > 0
    )


def _transport_stability_passes(*, disconnected_ratio: float) -> bool:
    # A single-feed interval is degraded but remains usable. Only simultaneous
    # route loss breaks continuity; redundant overlap is retained as an
    # operational target in the report rather than invalidating the full soak.
    return disconnected_ratio <= MAX_DISCONNECTED_SAMPLE_RATIO


def _sample_gap_passes(*, max_gap_seconds: float, limit_seconds: float) -> bool:
    return 0 <= max_gap_seconds <= limit_seconds


def _resolve_soak_start(
    *,
    state_path: Path | None,
    latest_path: Path,
    duration_seconds: float,
) -> tuple[datetime, bool]:
    now = datetime.now(timezone.utc)
    candidates = [
        path
        for path in (state_path, latest_path)
        if path is not None and path.is_file()
    ]
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            start_at = _parse_datetime(payload["start_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
        expected_seconds = float(
            payload.get("duration_seconds")
            or float(payload.get("minimum_hours") or 0) * 3600
        )
        deadline = start_at.timestamp() + duration_seconds
        if (
            not payload.get("completed_at")
            and abs(expected_seconds - duration_seconds) <= 1
            and now.timestamp() < deadline
        ):
            if state_path is not None:
                resumed_state = {
                    "start_at": start_at,
                    "duration_seconds": duration_seconds,
                    "status": "RUNNING",
                }
                if payload.get("audit_start_at"):
                    resumed_state["audit_start_at"] = payload["audit_start_at"]
                _write_json(state_path, resumed_state)
            return start_at, True
    start_at = now
    if state_path is not None:
        _write_json(
            state_path,
            {
                "start_at": start_at,
                "duration_seconds": duration_seconds,
                "status": "RUNNING",
            },
        )
    return start_at, False


def _completed_failure_exit_code(args: argparse.Namespace) -> int:
    code = int(getattr(args, "completed_failure_exit_code", 1))
    if code <= 0 or code > 255:
        raise ValueError("completed_failure_exit_code must be between 1 and 255")
    return code


def _load_soak_audit_start(
    *,
    state_path: Path | None,
    latest_path: Path,
    default: datetime,
) -> datetime:
    for path in (state_path, latest_path):
        if path is None or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("audit_start_at")
            if value:
                return _parse_datetime(value)
        except (TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
    return _utc(default)


def _load_soak_samples(
    output_dir: Path,
    *,
    start_at: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("sample-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            generated_at = _parse_datetime(payload["generated_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            continue
        if generated_at >= start_at:
            rows.append(payload)
    return rows


def _summarize_slo_window(samples: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = [
        (row.get("metrics") or {}).get("slo_snapshot") or {} for row in samples
    ]
    book_values = [
        float(row["book_freshness_p99_ms"])
        for row in snapshots
        if row.get("book_freshness_p99_ms") is not None
    ]
    execution_backlog_values = [
        float(row["paper_execution_backlog_age_p99_ms"])
        for row in snapshots
        if row.get("paper_execution_backlog_age_p99_ms") is not None
    ]
    registry_outbox_values = [
        float(row["registry_outbox_pending_age_p99_ms"])
        for row in snapshots
        if row.get("registry_outbox_pending_age_p99_ms") is not None
    ]
    latest_snapshot = snapshots[-1] if snapshots else {}
    cumulative_execution_metrics = {
        key: (
            float(latest_snapshot[key])
            if latest_snapshot.get(key) is not None
            else None
        )
        for key in (
            "order_acceptance_p95_ms",
            "order_acceptance_p99_ms",
            "decision_to_admission_p95_ms",
            "decision_to_admission_p99_ms",
            "admission_to_arrival_p95_ms",
            "admission_to_arrival_p99_ms",
            "arrival_to_result_p95_ms",
            "arrival_to_result_p99_ms",
            "terminal_result_p95_ms",
            "terminal_result_p99_ms",
        )
    }
    metrics = {
        "sample_count": len(snapshots),
        "book_freshness_p99_ms": _percentile(book_values, 0.99),
        "paper_execution_backlog_age_p99_ms": _percentile(
            execution_backlog_values, 0.99
        ),
        "registry_outbox_pending_age_p99_ms": _percentile(registry_outbox_values, 0.99),
        "unknown_terminal_order_count": max(
            (int(row.get("unknown_terminal_order_count") or 0) for row in snapshots),
            default=0,
        ),
        "accounting_mismatch_count": max(
            (int(row.get("accounting_mismatch_count") or 0) for row in snapshots),
            default=0,
        ),
        "unsafe_fill_count": max(
            (int(row.get("unsafe_fill_count") or 0) for row in snapshots),
            default=0,
        ),
        "reservation_mismatch_count": max(
            (int(row.get("reservation_mismatch_count") or 0) for row in snapshots),
            default=0,
        ),
        "journal_hash_mismatch_count": max(
            (int(row.get("journal_hash_mismatch_count") or 0) for row in snapshots),
            default=0,
        ),
        "invalid_timestamp_order_count": max(
            (
                int(row.get("invalid_timestamp_order_count") or 0)
                for row in snapshots
            ),
            default=0,
        ),
        "execution_gate_ms": 60_000,
        **cumulative_execution_metrics,
    }
    full_evaluation = evaluate_slos(metrics)
    full_checks = dict(full_evaluation.get("checks") or {})
    full_checks["execution_timestamp_ordering"] = (
        metrics["invalid_timestamp_order_count"] == 0
    )
    checks = {
        name: full_checks.get(name)
        for name in SOAK_SLO_CHECKS
    }
    available = [value for value in checks.values() if value is not None]
    evaluation = {
        "scope": "LONGITUDINAL_LIVE_SOAK",
        "status": (
            "PASS"
            if len(available) == len(checks) and all(available)
            else "FAIL"
            if any(value is False for value in available)
            else "NOT_ENOUGH_DATA"
        ),
        "checks": checks,
        "readiness_external_checks": {
            name: value
            for name, value in full_checks.items()
            if name not in checks
        },
    }
    return {"metrics": metrics, "evaluation": evaluation}


def _attach_slo_window(
    report: dict[str, Any],
    samples: list[dict[str, Any]],
) -> None:
    window = _summarize_slo_window(samples)
    report["slo_window"] = window
    if (window.get("evaluation") or {}).get("status") != "FAIL":
        return
    report["status"] = "FAIL" if report.get("soak_complete") else "DEGRADED"


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _parse_datetime(value: str) -> datetime:
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
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(payload), indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
