"""Idempotently pin one representative calibration cohort in the shadow watchlist."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from quant.core.db import postgres_connection

from .market_taxonomy import REPRESENTATIVE_DOMAINS

PLAN_SCHEMA = "representative_probe_sampling_plan_v1"
DEFAULT_STRATEGY_ID = "representative-calibration-v1"
DEFAULT_REASON = "representative_probe_candidate"


def validate_representative_plan(plan: Mapping[str, Any]) -> list[dict[str, str]]:
    if str(plan.get("schema_version") or "") != PLAN_SCHEMA:
        raise ValueError("unsupported representative sampling plan schema")
    if not bool(plan.get("read_only")) or bool(plan.get("exchange_order_submitted")):
        raise ValueError("representative plan safety markers are invalid")
    selected = plan.get("selected_candidates")
    if not isinstance(selected, list) or not selected:
        raise ValueError("representative plan has no selected candidates")
    rows: list[dict[str, str]] = []
    seen_assets: set[str] = set()
    seen_markets: set[str] = set()
    seen_events: set[str] = set()
    require_unique_events = bool(plan.get("event_uniqueness_required"))
    for item in selected:
        if not isinstance(item, Mapping):
            raise ValueError("representative candidate must be an object")
        asset_id = str(item.get("asset_id") or "").strip()
        market_id = str(item.get("market_id") or "").strip()
        domain = str(item.get("category_group") or "").strip().lower()
        if not asset_id or not market_id:
            raise ValueError("representative candidate identity is incomplete")
        if domain not in REPRESENTATIVE_DOMAINS:
            raise ValueError(f"unsupported representative domain: {domain or 'missing'}")
        if asset_id in seen_assets:
            raise ValueError(f"duplicate representative asset: {asset_id}")
        if market_id in seen_markets:
            raise ValueError(f"duplicate representative market: {market_id}")
        event_key = str(item.get("event_key") or "").strip()
        if require_unique_events and not event_key:
            raise ValueError(f"representative candidate event identity is incomplete: {market_id}")
        if require_unique_events and event_key in seen_events:
            raise ValueError(f"duplicate representative event: {event_key}")
        seen_assets.add(asset_id)
        seen_markets.add(market_id)
        if event_key:
            seen_events.add(event_key)
        rows.append({"asset_id": asset_id, "market_id": market_id, "domain": domain})
    return rows


def reconcile_representative_watchlist(
    plan: Mapping[str, Any],
    *,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    reason: str = DEFAULT_REASON,
    apply: bool = False,
    connection_factory: Any | None = None,
) -> dict[str, Any]:
    rows = validate_representative_plan(plan)
    asset_ids = [row["asset_id"] for row in rows]
    selected_factory = connection_factory or postgres_connection
    with selected_factory(readonly=not apply) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.asset_id, w.strategy_id, w.reason, w.enabled
            FROM quant.paper_live_watchlist w
            WHERE w.asset_id=ANY(%s::text[])
            ORDER BY w.asset_id
            """,
            (asset_ids,),
        )
        existing = {str(row["asset_id"]): dict(row) for row in cur.fetchall()}
        stale_rows: list[dict[str, Any]] = []
        cur.execute(
            """
            SELECT asset_id, strategy_id, reason, enabled
            FROM quant.paper_live_watchlist
            WHERE strategy_id=%s AND reason=%s
              AND NOT (asset_id=ANY(%s::text[]))
            ORDER BY asset_id
            """,
            (strategy_id, reason, asset_ids),
        )
        stale_rows = [dict(row) for row in cur.fetchall()]
        changed = 0
        disabled = 0
        if apply:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (strategy_id,))
            cur.execute(
                """
                INSERT INTO quant.paper_live_watchlist (
                    asset_id, strategy_id, enabled, reason
                )
                SELECT selected.asset_id, %s, TRUE, %s
                FROM unnest(%s::text[]) AS selected(asset_id)
                JOIN quant.paper_execution_market_catalog registry
                  ON registry.asset_id=selected.asset_id
                WHERE registry.market_state='LIVE'
                  AND registry.execution_eligible=TRUE
                  AND registry.active=TRUE
                  AND registry.closed=FALSE
                  AND registry.resolved=FALSE
                ON CONFLICT (asset_id) DO UPDATE SET
                    strategy_id=CASE
                        WHEN quant.paper_live_watchlist.strategy_id='paper-live-seed'
                          OR quant.paper_live_watchlist.reason IN (
                              'canary_execution_seed', 'coverage_execution_seed'
                          )
                        THEN EXCLUDED.strategy_id
                        ELSE quant.paper_live_watchlist.strategy_id
                    END,
                    enabled=TRUE,
                    reason=CASE
                        WHEN quant.paper_live_watchlist.strategy_id='paper-live-seed'
                          OR quant.paper_live_watchlist.reason IN (
                              'canary_execution_seed', 'coverage_execution_seed'
                          )
                        THEN EXCLUDED.reason
                        ELSE quant.paper_live_watchlist.reason
                    END,
                    updated_at=clock_timestamp()
                WHERE quant.paper_live_watchlist.enabled IS DISTINCT FROM TRUE
                   OR quant.paper_live_watchlist.strategy_id='paper-live-seed'
                   OR quant.paper_live_watchlist.reason IN (
                       'canary_execution_seed', 'coverage_execution_seed'
                   )
                """,
                (strategy_id, reason, asset_ids),
            )
            changed = int(cur.rowcount or 0)
            cur.execute(
                """
                UPDATE quant.paper_live_watchlist
                SET enabled=FALSE, updated_at=clock_timestamp()
                WHERE strategy_id=%s AND reason=%s AND enabled=TRUE
                  AND NOT (asset_id=ANY(%s::text[]))
                """,
                (strategy_id, reason, asset_ids),
            )
            disabled = int(cur.rowcount or 0)
            conn.commit()
    return {
        "schema_version": "representative_probe_watchlist_reconciliation_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "applied": bool(apply),
        "exchange_order_submitted": False,
        "strategy_id": strategy_id,
        "reason": reason,
        "selected_count": len(rows),
        "domain_counts": {
            domain: sum(row["domain"] == domain for row in rows)
            for domain in REPRESENTATIVE_DOMAINS
        },
        "already_present_count": len(existing),
        "ownership_preserved_count": sum(
            1
            for row in existing.values()
            if (
                row.get("strategy_id") != strategy_id
                and row.get("strategy_id") != "paper-live-seed"
                and row.get("reason") not in (
                    "canary_execution_seed", "coverage_execution_seed"
                )
            )
        ),
        "seed_ownership_leased_count": sum(
            1
            for row in existing.values()
            if row.get("strategy_id") == "paper-live-seed"
            or row.get("reason") in (
                "canary_execution_seed", "coverage_execution_seed"
            )
        ),
        "ownership_replaced_count": 0,
        "stale_candidate_count": len(stale_rows),
        "changed_count": changed,
        "disabled_count": disabled,
        "asset_ids": asset_ids,
    }
