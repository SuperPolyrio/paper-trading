"""Conservative taker correction fit over CALIBRATABLE probes only."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from .holdout_split import grouped_split
from .latency_estimator import estimate_latency


def fit_taker_calibration(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    all_rows = [dict(row) for row in rows]
    samples = [row for row in all_rows if str(row.get("probe_state")) == "CALIBRATABLE"]
    training, validation, holdout = _group_split(samples)
    fit = _metrics(training)
    state_counts: dict[str, int] = {}
    for row in all_rows:
        state = str(row.get("probe_state") or "UNKNOWN")
        state_counts[state] = state_counts.get(state, 0) + 1
    complete_artifacts = sum(
        1 for row in samples if bool((row.get("artifact_bitmap") or {}).get("complete"))
    )
    pnl_rows = [_pnl(row) for row in samples]
    pnl_status_counts: dict[str, int] = {}
    for pnl in pnl_rows:
        status = str(pnl.get("status") or "MISSING")
        pnl_status_counts[status] = pnl_status_counts.get(status, 0) + 1
    pnl_errors = [
        _decimal((pnl.get("difference") or {}).get("total_pnl_error"))
        for pnl in pnl_rows
        if isinstance(pnl.get("difference"), Mapping)
        and (pnl.get("difference") or {}).get("total_pnl_error") is not None
    ]
    return {
        "schema_version": "taker_calibration_fit_v1",
        "total_probe_count": len(all_rows),
        "sample_count": len(samples),
        "excluded_probe_count": len(all_rows) - len(samples),
        "probe_state_counts": dict(sorted(state_counts.items())),
        "complete_artifact_count": complete_artifacts,
        "pnl_reconciled_count": sum(
            1 for pnl in pnl_rows if bool(pnl.get("pnl_reconciled"))
        ),
        "pnl_status_counts": dict(sorted(pnl_status_counts.items())),
        "pnl_total_error_max": max(pnl_errors, default=None),
        "training_count": len(training),
        "validation_count": len(validation),
        "holdout_count": len(holdout),
        "training_metrics": fit,
        "validation_metrics": _metrics(validation),
        "holdout_metrics": _metrics(holdout),
        "latency": estimate_latency(training),
        "depth_haircut_p10": _quantile(
            [_decimal(_sample(row).get("depth_survival_ratio")) for row in training],
            Decimal("0.10"),
        ),
        "split_rule": "event-or-condition plus UTC decision day hash groups; 70/15/15",
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"sample_count": 0}
    outcomes = [_sample(row) for row in rows]
    correct = sum(1 for item in outcomes if item.get("predicted_class") == item.get("actual_class"))
    fok = [item for item in outcomes if item.get("order_type") == "FOK"]
    false_full = sum(
        1
        for item in fok
        if item.get("predicted_class") == "FULL" and item.get("actual_class") != "FULL"
    )
    price_errors = [_decimal(item.get("price_error_ticks")) for item in outcomes if item.get("price_error_ticks") is not None]
    size_errors = [_decimal(item.get("filled_size_relative_error")) for item in outcomes if item.get("filled_size_relative_error") is not None]
    fee_errors = [_decimal(item.get("fee_error")) for item in outcomes if item.get("fee_error") is not None]
    return {
        "sample_count": len(rows),
        "fill_class_accuracy_pct": format(Decimal(correct) * 100 / Decimal(len(rows)), ".4f"),
        "fok_sample_count": len(fok),
        "fok_false_positive_full_fill_count": false_full,
        "vwap_error_median_ticks": _quantile(price_errors, Decimal("0.50")),
        "vwap_error_p95_ticks": _quantile(price_errors, Decimal("0.95")),
        "filled_size_relative_error_p95": _quantile(size_errors, Decimal("0.95")),
        "fee_error_max": max(fee_errors, default=None),
    }


def _group_split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    split = grouped_split(rows)
    return split["training"], split["validation"], split["holdout"]


def _sample(row: Mapping[str, Any]) -> Mapping[str, Any]:
    reconciliation = row.get("reconciliation")
    return reconciliation if isinstance(reconciliation, Mapping) else {}


def _pnl(row: Mapping[str, Any]) -> Mapping[str, Any]:
    sample = _sample(row)
    pnl = sample.get("pnl")
    return pnl if isinstance(pnl, Mapping) else {}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _quantile(values: list[Decimal], q: Decimal) -> str | None:
    valid = sorted(value for value in values if value.is_finite())
    if not valid:
        return None
    index = int((Decimal(len(valid) - 1) * q).to_integral_value(rounding="ROUND_HALF_UP"))
    return format(valid[index], "f")
