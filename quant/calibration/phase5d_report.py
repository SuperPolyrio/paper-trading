"""Aggregate Phase 5D live probes and expose stratification gaps."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from quant.paper.depth_metrics import visible_executable_depth

from .calibration_report import build_calibration_report
from .market_taxonomy import REPRESENTATIVE_DOMAINS, normalize_market_domain
from .taker_calibration_fit import fit_taker_calibration


PRICE_BUCKETS = ("0.01-0.10", "0.10-0.30", "0.30-0.70", "0.70-0.90", "0.90-0.99")
SPREAD_BUCKETS = ("1_tick", "2_3_ticks", "gt_3_ticks")
ACTIVITY_BUCKETS = ("quiet", "normal", "fast")
DEPTH_RATIO_BUCKETS = (
    "lte_0.05",
    "0.05-0.25",
    "0.25-0.50",
    "0.50-0.90",
    "0.90-1.10",
    "gt_1.10",
)


def build_phase5d_report(
    probes: Iterable[Mapping[str, Any]],
    *,
    market_categories: Mapping[str, str] | None = None,
    target_min: int = 200,
    target_max: int = 300,
) -> dict[str, Any]:
    """Build a read-only campaign report over submitted live probes."""

    categories = {str(key): str(value) for key, value in (market_categories or {}).items()}
    submitted = _dedupe_submitted(probes)
    fit = fit_taker_calibration(submitted)
    promotion = build_calibration_report(fit)
    rows = [_stratification_row(probe, categories) for probe in submitted]
    dimensions = {
        "order_type": _dimension(rows, "order_type", ("FOK", "FAK")),
        "side": _dimension(rows, "side", ("BUY", "SELL")),
        "market_domain": _dimension(rows, "market_domain", REPRESENTATIVE_DOMAINS),
        "delay_cohort": _dimension(
            rows,
            "delay_cohort",
            ("BASELINE_NO_DELAY", "SPORTS_DELAY", "ITOD_250MS"),
        ),
        "price_region": _dimension(rows, "price_region", PRICE_BUCKETS),
        "spread": _dimension(rows, "spread", SPREAD_BUCKETS),
        "book_activity": _dimension(rows, "book_activity", ACTIVITY_BUCKETS),
        "depth_ratio": _dimension(rows, "depth_ratio", DEPTH_RATIO_BUCKETS),
        "fee": _dimension(rows, "fee", ("enabled", "disabled")),
    }
    missing_buckets = [
        f"{dimension}:{bucket}"
        for dimension, summary in dimensions.items()
        for bucket in summary["missing_buckets"]
    ]
    calibratable = int(fit.get("sample_count") or 0)
    status = (
        "PASS"
        if promotion.get("status") == "PASS"
        else "BLOCKED"
        if any(
            str(row.get("probe_state") or "") != "CALIBRATABLE"
            for row in submitted
        )
        else "COLLECTING"
    )
    return {
        "schema_version": "taker_phase5d_campaign_report_v1",
        "status": status,
        "read_only": True,
        "exchange_order_submitted": False,
        "target_sample_range": {"minimum": int(target_min), "maximum": int(target_max)},
        "submitted_probe_count": len(submitted),
        "calibratable_probe_count": calibratable,
        "remaining_to_minimum": max(0, int(target_min) - calibratable),
        "distinct_market_count": len(
            {str(row.get("market_id") or "") for row in submitted if row.get("market_id")}
        ),
        "distinct_condition_count": len(
            {
                str(row.get("condition_id") or "")
                for row in submitted
                if row.get("condition_id")
            }
        ),
        "stratification": dimensions,
        "missing_buckets": missing_buckets,
        "next_collection_priorities": _priorities(dimensions, calibratable, int(target_min)),
        "fit": fit,
        "promotion_gate": promotion,
        "split_policy": (
            "condition/event plus UTC decision day; deterministic 70/15/15 hash split"
        ),
    }


def _dedupe_submitted(probes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for source in probes:
        row = dict(source)
        if not bool(row.get("exchange_submit_called")):
            continue
        probe_id = str(row.get("probe_id") or "")
        if not probe_id:
            continue
        rows[probe_id] = row
    return sorted(
        rows.values(),
        key=lambda row: (str(row.get("decision_ts") or ""), str(row.get("probe_id") or "")),
    )


def _stratification_row(
    probe: Mapping[str, Any],
    market_categories: Mapping[str, str],
) -> dict[str, str]:
    snapshot = _mapping(probe.get("market_snapshot"))
    market_id = str(probe.get("market_id") or snapshot.get("market_id") or "")
    source_category = (
        market_categories.get(market_id)
        or snapshot.get("source_category")
        or snapshot.get("category")
        or ""
    )
    domain = normalize_market_domain(
        source_category,
        market_title=snapshot.get("market_title"),
        event_title=snapshot.get("event_title"),
        market_slug=snapshot.get("market_slug"),
    )
    side = str(probe.get("side") or "").upper()
    itode = bool(snapshot.get("itode"))
    delay = (
        "SPORTS_DELAY"
        if domain == "sports"
        else "ITOD_250MS"
        if itode
        else "BASELINE_NO_DELAY"
    )
    bid = _decimal(snapshot.get("best_bid"))
    ask = _decimal(snapshot.get("best_ask"))
    tick = _decimal(snapshot.get("tick_size"), Decimal("0.001"))
    midpoint = (bid + ask) / Decimal("2") if bid >= 0 and ask > 0 else Decimal("0")
    spread_ticks = (ask - bid) / tick if tick > 0 and ask >= bid else None
    fee_rate = _decimal(snapshot.get("fee_rate"))
    fee_bps = _decimal(snapshot.get("fee_rate_bps"))
    return {
        "order_type": str(probe.get("order_type") or "").upper(),
        "side": side,
        "market_domain": domain,
        "delay_cohort": delay,
        "price_region": _price_bucket(midpoint),
        "spread": _spread_bucket(spread_ticks),
        "book_activity": _activity_bucket(snapshot.get("activity_event_count")),
        "depth_ratio": _depth_ratio_bucket(_order_depth_ratio(probe, snapshot)),
        "fee": "enabled" if fee_rate > 0 or fee_bps > 0 else "disabled",
    }


def _dimension(
    rows: list[Mapping[str, str]],
    key: str,
    expected: Iterable[str],
) -> dict[str, Any]:
    counts = Counter(str(row.get(key) or "unknown") for row in rows)
    expected_values = tuple(str(value) for value in expected)
    missing = [value for value in expected_values if counts[value] == 0]
    return {
        "counts": dict(sorted(counts.items())),
        "expected_buckets": list(expected_values),
        "missing_buckets": missing,
        "status": "COVERED" if not missing else "GAPS",
    }


def _priorities(
    dimensions: Mapping[str, Mapping[str, Any]],
    sample_count: int,
    target_min: int,
) -> list[dict[str, Any]]:
    priorities: list[dict[str, Any]] = []
    for dimension in (
        "order_type",
        "side",
        "market_domain",
        "delay_cohort",
        "price_region",
        "spread",
        "book_activity",
        "depth_ratio",
        "fee",
    ):
        summary = dimensions[dimension]
        counts = summary.get("counts") if isinstance(summary.get("counts"), Mapping) else {}
        for bucket in summary.get("expected_buckets") or []:
            count = int(counts.get(bucket) or 0)
            priorities.append(
                {
                    "dimension": dimension,
                    "bucket": bucket,
                    "current_count": count,
                    "priority": "P0" if count == 0 else "P1" if count < 3 else "P2",
                }
            )
    priorities.sort(
        key=lambda row: (
            {"P0": 0, "P1": 1, "P2": 2}[str(row["priority"])],
            int(row["current_count"]),
            str(row["dimension"]),
            str(row["bucket"]),
        )
    )
    if sample_count < target_min:
        priorities.insert(
            0,
            {
                "dimension": "sample_count",
                "bucket": f"minimum_{target_min}",
                "current_count": sample_count,
                "priority": "P0",
            },
        )
    return priorities


def _order_depth_ratio(
    probe: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> Decimal | None:
    side = str(probe.get("side") or "").upper()
    prediction = _mapping(probe.get("prediction"))
    signed_order = _mapping(probe.get("signed_order_audit"))
    amount = _decimal(
        prediction.get("requested_amount")
        if prediction.get("requested_amount") not in (None, "")
        else probe.get("amount")
    )
    amount_unit = str(
        prediction.get("amount_unit") or probe.get("amount_unit") or ""
    ).upper()
    limit_price = _decimal(
        prediction.get("signed_worst_price")
        or signed_order.get("worst_price")
        or signed_order.get("implied_price")
    )
    levels = snapshot.get("asks") if side == "BUY" else snapshot.get("bids")
    if not isinstance(levels, list) or limit_price <= 0:
        return None
    depth = visible_executable_depth(
        levels,
        side=side,
        amount_unit=amount_unit,
        limit_price=limit_price,
    )
    return amount / depth if depth > 0 else None


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


def _depth_ratio_bucket(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value <= Decimal("0.05"):
        return "lte_0.05"
    if value <= Decimal("0.25"):
        return "0.05-0.25"
    if value <= Decimal("0.50"):
        return "0.25-0.50"
    if value <= Decimal("0.90"):
        return "0.50-0.90"
    if value <= Decimal("1.10"):
        return "0.90-1.10"
    return "gt_1.10"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
