"""Prediction quality metrics kept separate from trading PnL."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")
ONE = Decimal("1")
EPSILON = Decimal("0.000001")


@dataclass(frozen=True)
class PredictionObservation:
    event_id: str
    market_id: str
    category: str
    decision_ts: datetime
    resolution_ts: datetime
    probability: Decimal
    outcome: Decimal
    decision_market_price: Decimal
    final_pre_resolution_price: Decimal | None = None
    capital_at_risk: Decimal = ZERO
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        if not ZERO <= self.probability <= ONE:
            raise ValueError("probability must be within [0, 1]")
        if self.outcome not in {ZERO, Decimal("0.5"), ONE}:
            raise ValueError("outcome must be 0, 0.5 or 1")
        if self.capital_at_risk < 0:
            raise ValueError("capital_at_risk cannot be negative")
        if self.decision_ts.tzinfo is None or self.resolution_ts.tzinfo is None:
            raise ValueError("prediction timestamps must include timezones")
        if self.resolution_ts < self.decision_ts:
            raise ValueError("resolution_ts cannot precede decision_ts")


def build_prediction_quality_report(
    observations: Iterable[PredictionObservation],
    *,
    bucket_width: Decimal = Decimal("0.1"),
) -> dict[str, Any]:
    rows = list(observations)
    if not ZERO < bucket_width <= ONE:
        raise ValueError("bucket_width must be within (0, 1]")
    if not rows:
        return {
            "schema_version": "prediction_quality_v1",
            "status": "NO_DATA",
            "observation_count": 0,
            "independent_event_count": 0,
            "metrics": {},
            "calibration": [],
            "by_category": {},
        }

    metrics = _metrics(rows)
    by_category = {
        category: _metrics(items)
        for category, items in sorted(_group(rows, lambda row: row.category).items())
    }
    by_event = _group(rows, lambda row: row.event_id)
    return {
        "schema_version": "prediction_quality_v1",
        "status": "COMPLETE",
        "observation_count": len(rows),
        "independent_event_count": len(by_event),
        "metrics": metrics,
        "calibration": _calibration(rows, bucket_width),
        "by_category": by_category,
        "support": {
            "categories": sorted(by_category),
            "decision_start": min(row.decision_ts for row in rows).astimezone(timezone.utc),
            "decision_end": max(row.decision_ts for row in rows).astimezone(timezone.utc),
        },
    }


def _metrics(rows: list[PredictionObservation]) -> dict[str, Decimal | int | None]:
    count = Decimal(len(rows))
    brier = sum((row.probability - row.outcome) ** 2 for row in rows) / count
    log_loss = sum(_log_loss(row.probability, row.outcome) for row in rows) / count
    clv_rows = [
        row.final_pre_resolution_price - row.decision_market_price
        for row in rows
        if row.final_pre_resolution_price is not None
    ]
    capital_days = sum(
        (
            row.capital_at_risk
            * Decimal(str((row.resolution_ts - row.decision_ts).total_seconds()))
            / Decimal("86400")
        )
        for row in rows
    )
    return {
        "sample_count": len(rows),
        "independent_event_count": len({row.event_id for row in rows}),
        "brier": brier,
        "log_loss": log_loss,
        "mean_clv": sum(clv_rows, ZERO) / Decimal(len(clv_rows)) if clv_rows else None,
        "clv_sample_count": len(clv_rows),
        "capital_days": capital_days,
    }


def _log_loss(probability: Decimal, outcome: Decimal) -> Decimal:
    selected = min(ONE - EPSILON, max(EPSILON, probability))
    value = -(
        float(outcome) * math.log(float(selected))
        + float(ONE - outcome) * math.log(float(ONE - selected))
    )
    return Decimal(str(value))


def _calibration(
    rows: list[PredictionObservation], bucket_width: Decimal
) -> list[dict[str, Decimal | int]]:
    buckets: dict[int, list[PredictionObservation]] = defaultdict(list)
    bucket_count = int((ONE / bucket_width).to_integral_value(rounding="ROUND_CEILING"))
    for row in rows:
        index = min(bucket_count - 1, int(row.probability / bucket_width))
        buckets[index].append(row)
    result: list[dict[str, Decimal | int]] = []
    for index in range(bucket_count):
        items = buckets.get(index, [])
        if not items:
            continue
        count = Decimal(len(items))
        lower = Decimal(index) * bucket_width
        upper = min(ONE, lower + bucket_width)
        result.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(items),
                "independent_event_count": len({item.event_id for item in items}),
                "mean_probability": sum((item.probability for item in items), ZERO) / count,
                "observed_rate": sum((item.outcome for item in items), ZERO) / count,
            }
        )
    return result


def _group(rows: list[PredictionObservation], key: Any) -> dict[str, list[PredictionObservation]]:
    grouped: dict[str, list[PredictionObservation]] = defaultdict(list)
    for row in rows:
        grouped[str(key(row) or "unknown")].append(row)
    return grouped


__all__ = ["PredictionObservation", "build_prediction_quality_report"]
