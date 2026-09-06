"""Coverage and strategy-weighted probe distribution builders."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ProbeBucket:
    sampler: str
    side: str
    order_type: str
    price_bucket: str
    spread_ticks: int
    depth_ratio: Decimal
    pace: str
    weight: Decimal


def coverage_buckets() -> list[ProbeBucket]:
    result = []
    for side in ("BUY", "SELL"):
        for order_type in ("FAK", "FOK"):
            for spread in (1, 2, 3):
                for ratio in (Decimal("0.05"), Decimal("0.15"), Decimal("0.25")):
                    result.append(
                        ProbeBucket(
                            "coverage",
                            side,
                            order_type,
                            "0.05-0.95",
                            spread,
                            ratio,
                            "quiet-normal-fast",
                            Decimal("1"),
                        )
                    )
    return result


def strategy_weighted_buckets(
    historical_intents: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, ...], int] = {}
    for row in historical_intents:
        key = (
            str(row.get("category") or "UNKNOWN"),
            str(row.get("side") or "UNKNOWN"),
            str(row.get("order_type") or "UNKNOWN"),
            str(row.get("price_bucket") or "UNKNOWN"),
            str(row.get("spread_bucket") or "UNKNOWN"),
            str(row.get("depth_ratio_bucket") or "UNKNOWN"),
        )
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values()) or 1
    return [
        {
            "sampler": "strategy_weighted",
            "category": key[0],
            "side": key[1],
            "order_type": key[2],
            "price_bucket": key[3],
            "spread_bucket": key[4],
            "depth_ratio_bucket": key[5],
            "count": count,
            "weight": count / total,
        }
        for key, count in sorted(counts.items())
    ]
