"""Conditional conservative depth-survival estimates."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .calibration_metrics import quantile

FEATURES = ("venue_regime_id", "side", "order_type", "category", "spread_ticks")


def fit_depth_survival(
    rows: Iterable[Mapping[str, Any]],
    *,
    conservative_quantile: float = 0.10,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[Decimal]] = defaultdict(list)
    for row in rows:
        try:
            ratio = Decimal(str(row.get("depth_survival_ratio")))
        except Exception:
            continue
        if ratio < 0 or not ratio.is_finite():
            continue
        key = tuple(str(row.get(feature) or "ALL") for feature in FEATURES)
        buckets[key].append(min(Decimal("1"), ratio))
    result = []
    for key, values in sorted(buckets.items()):
        estimate = quantile(values, conservative_quantile)
        result.append(
            {
                **dict(zip(FEATURES, key)),
                "sample_count": len(values),
                "survival_quantile": conservative_quantile,
                "depth_survival": format(estimate or Decimal("0"), "f"),
            }
        )
    return result
