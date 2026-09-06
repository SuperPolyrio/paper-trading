"""Build a review-only Phase 5C matrix from audited representative markets."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .market_taxonomy import REPRESENTATIVE_DOMAINS


def build_phase5c_matrix(
    audit: Mapping[str, Any],
    *,
    max_buy_amount_usd: Decimal = Decimal("1"),
) -> dict[str, Any]:
    if bool(audit.get("exchange_order_submitted")):
        raise ValueError("metadata audit must not contain submitted orders")
    rows = [
        dict(row)
        for row in audit.get("rows") or []
        if isinstance(row, Mapping)
        and str(row.get("status") or "") in {"PASS", "WARNING"}
        and not row.get("issues")
    ]
    selected_markets: list[dict[str, Any]] = []
    planned_orders: list[dict[str, Any]] = []
    domain_counts: Counter[str] = Counter()
    used_events: set[str] = set()
    for domain in REPRESENTATIVE_DOMAINS:
        domain_rows = sorted(
            (row for row in rows if str(row.get("category_group") or "") == domain),
            key=lambda row: (
                str(row.get("status") or "") != "PASS",
                str(row.get("market_id") or ""),
            ),
        )
        for row in domain_rows:
            event_key = _event_key(row)
            if event_key in used_events:
                continue
            used_events.add(event_key)
            selected_markets.append(dict(row))
            domain_counts[domain] += 1
            if domain_counts[domain] >= 3:
                break

    for market_index, row in enumerate(selected_markets):
        domain = str(row.get("category_group") or "other")
        cohort = _normalized_cohort(row.get("delay_cohort"), domain=domain)
        buy_order_type = "FOK" if market_index % 2 == 0 else "FAK"
        common = {
            "cohort": cohort,
            "category_group": domain,
            "source_category": row.get("source_category"),
            "event_key": _event_key(row),
            "event_slug": row.get("event_slug"),
            "event_title": row.get("event_title"),
            "market_id": str(row.get("market_id") or ""),
            "condition_id": str(row.get("condition_id") or ""),
            "asset_id": str(row.get("asset_id") or ""),
            "market_title": row.get("market_title"),
            "outcome_name": row.get("outcome_name"),
            "price_bucket": row.get("price_bucket"),
            "spread_bucket": row.get("spread_bucket"),
            "activity_bucket": row.get("activity_bucket"),
            "metadata_status": row.get("status"),
            "fee_rate_bps": row.get("fee_rate_bps"),
            "tick_size": row.get("tick_size"),
            "min_order_size": row.get("min_order_size"),
            "seconds_delay": row.get("seconds_delay"),
            "rest_best_bid": row.get("rest_best_bid"),
            "rest_best_ask": row.get("rest_best_ask"),
            "requires_fresh_preflight": True,
            "requires_specific_run_approval": False,
            "authorization_mode": "AUTO_WITHIN_CURRENT_RISK_LIMITS",
            "creates_live_run": False,
        }
        buy_sequence = len(planned_orders) + 1
        planned_orders.append(
            {
                **common,
                "sequence": buy_sequence,
                "execution_model": "TAKER_WALK_BOOK",
                "order_type": buy_order_type,
                "side": "BUY",
                "amount": str(max_buy_amount_usd),
                "amount_unit": "QUOTE",
                "maximum_spend_usd": str(max_buy_amount_usd),
                "expected_mechanical_outcome": "FULL_PARTIAL_OR_EXPLAINED_REJECT",
                "readiness": "READY_AFTER_PREFLIGHT",
            }
        )
        planned_orders.append(
            {
                **common,
                "sequence": len(planned_orders) + 1,
                "execution_model": "TAKER_WALK_BOOK",
                "order_type": "FAK" if buy_order_type == "FOK" else "FOK",
                "side": "SELL",
                "amount": None,
                "amount_unit": "SHARES",
                "size_policy": "MIN_CONFIRMED_POSITION_AND_USD_1_AT_CURRENT_BID",
                "maximum_spend_usd": "0",
                "expected_mechanical_outcome": "FULL_PARTIAL_OR_EXPLAINED_REJECT",
                "readiness": "WAITING_FOR_CONFIRMED_POSITION",
                "depends_on_sequence": buy_sequence,
            }
        )

    batches = []
    for cohort in ("BASELINE_NO_DELAY", "SPORTS_DELAY", "ITOD_250MS", "ITOD_OR_CONFIGURED_DELAY"):
        for side in ("BUY", "SELL"):
            orders = [
                row for row in planned_orders
                if row["cohort"] == cohort and row["side"] == side
            ]
            if not orders:
                continue
            batches.append(
                {
                    "batch_id": f"{cohort.lower().replace('_', '-')}-{side.lower()}",
                    "purpose": (
                        "Validate taker entry mechanics in this delay cohort."
                        if side == "BUY"
                        else "Validate confirmed-position reduction, cash return, and realized PnL."
                    ),
                    "planned_order_count": len(orders),
                    "maximum_spend_usd": str(
                        max_buy_amount_usd * len(orders) if side == "BUY" else Decimal("0")
                    ),
                    "orders": orders,
                }
            )
    missing_domains = [
        domain for domain in REPRESENTATIVE_DOMAINS if domain_counts[domain] < 3
    ]
    return {
        "schema_version": "representative_phase5c_matrix_v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "REVIEW_ONLY",
        "exchange_order_submitted": False,
        "live_run_created": False,
        "candidate_count": len(planned_orders),
        "selected_market_count": len(selected_markets),
        "selected_event_count": len(used_events),
        "domain_counts": dict(domain_counts),
        "missing_domains": missing_domains,
        "ready_for_run_preparation": len(selected_markets) >= 10 and not missing_domains,
        "maximum_total_buy_spend_usd": str(max_buy_amount_usd * len(selected_markets)),
        "batches": batches,
        "risk_policy": {
            "legacy_usd20_campaign_cap_applies": False,
            "per_order_buy_cap_usd": str(max_buy_amount_usd),
            "runtime_daily_fail_safe_from_probe_plan": True,
            "unbounded_live_execution_allowed": False,
            "unknown_submit_outcome_retry_allowed": False,
        },
        "stop_conditions": _stop_conditions(),
        "notes": [
            "Each order is prepared as a separate immutable run and may auto-submit only inside the current configured limits.",
            "A fresh redundant shadow head, REST match, user WS, account state, and market metadata are rechecked immediately before submission.",
            "Every selected event has a deferred SELL leg which becomes eligible only after a confirmed position exists.",
            "Sports results are not pooled with the no-delay baseline.",
        ],
    }


def _event_key(row: Mapping[str, Any]) -> str:
    existing = str(row.get("event_key") or "").strip()
    if existing:
        return existing
    event_slug = str(row.get("event_slug") or "").strip()
    if event_slug:
        return f"event:{event_slug}"
    condition_id = str(row.get("condition_id") or "").strip()
    if condition_id:
        return f"condition:{condition_id}"
    return f"market:{str(row.get('market_id') or '').strip()}"


def _normalized_cohort(value: Any, *, domain: str) -> str:
    cohort = str(value or "").strip().upper()
    if cohort in {"", "NO_DELAY", "BASELINE"}:
        return "SPORTS_DELAY" if domain == "sports" else "BASELINE_NO_DELAY"
    if cohort in {"SPORTS", "SPORTS_DELAYED"}:
        return "SPORTS_DELAY"
    return cohort


def _stop_conditions() -> Sequence[str]:
    return (
        "paper_full_actual_no_fill",
        "paper_reject_actual_fill",
        "actual_fill_exceeds_prediction_upper_bound",
        "unexplained_fee",
        "order_id_not_reconciled",
        "unknown_submit_outcome",
        "accounting_mismatch",
        "unattributed_open_order",
        "self_trade",
        "kill_switch_or_cancel_all_failure",
    )
