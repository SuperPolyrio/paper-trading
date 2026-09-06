"""Latency distribution estimates from fully timestamped live probes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping


LATENCY_PAIRS = {
    "decision_to_prediction_ms": ("strategy_decision_ts", "prediction_completed_ts"),
    "prediction_to_sign_ms": ("prediction_completed_ts", "sign_started_ts"),
    "sign_ms": ("sign_started_ts", "sign_completed_ts"),
    "sign_to_http_send_ms": ("sign_completed_ts", "http_send_started_ts"),
    "http_round_trip_ms": ("http_send_started_ts", "http_response_completed_ts"),
    "send_to_order_ack_ms": ("http_send_started_ts", "user_ws_order_event_receive_ts"),
    "send_to_matched_ms": ("http_send_started_ts", "user_ws_trade_matched_receive_ts"),
    "matched_to_mined_ms": ("user_ws_trade_matched_receive_ts", "trade_mined_ts"),
    "mined_to_confirmed_ms": ("trade_mined_ts", "trade_confirmed_ts"),
}


def estimate_latency(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, list[Decimal]] = {name: [] for name in LATENCY_PAIRS}
    for row in rows:
        timestamps = row.get("timestamps") if isinstance(row.get("timestamps"), Mapping) else row
        for name, (start_key, end_key) in LATENCY_PAIRS.items():
            start = _milliseconds(timestamps.get(start_key))
            end = _milliseconds(timestamps.get(end_key))
            if start is not None and end is not None and end >= start:
                metrics[name].append(end - start)
    return {
        name: {
            "count": len(values),
            "p50": _quantile(values, Decimal("0.50")),
            "p75": _quantile(values, Decimal("0.75")),
            "p90": _quantile(values, Decimal("0.90")),
            "p95": _quantile(values, Decimal("0.95")),
            "p99": _quantile(values, Decimal("0.99")),
        }
        for name, values in metrics.items()
    }


def _milliseconds(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    from datetime import datetime

    if isinstance(value, datetime):
        return Decimal(str(value.timestamp() * 1000))
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return Decimal(str(parsed.timestamp() * 1000))
    except ValueError:
        return None


def _quantile(values: list[Decimal], q: Decimal) -> str | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * q).to_integral_value(rounding="ROUND_HALF_UP"))
    return format(ordered[index], ".3f")
