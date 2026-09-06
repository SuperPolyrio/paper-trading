"""Deterministic event/day grouped calibration splits."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Mapping


def group_key(row: Mapping[str, Any]) -> str:
    snapshot = row.get("market_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    identity = (
        snapshot.get("event_id")
        or snapshot.get("event_slug")
        or row.get("condition_id")
        or row.get("market_id")
        or row.get("probe_id")
    )
    decision = row.get("decision_ts")
    if isinstance(decision, datetime):
        day = decision.date().isoformat()
    else:
        day = str(decision or "")[:10] or "UNKNOWN_DAY"
    return f"{identity}|{day}"


def grouped_split(
    rows: Iterable[Mapping[str, Any]],
    *,
    train_pct: int = 70,
    validation_pct: int = 15,
) -> dict[str, list[dict[str, Any]]]:
    if train_pct <= 0 or validation_pct < 0 or train_pct + validation_pct >= 100:
        raise ValueError("split percentages must leave a non-empty holdout range")
    groups: dict[str, list[dict[str, Any]]] = {}
    for source in rows:
        row = dict(source)
        groups.setdefault(group_key(row), []).append(row)
    result: dict[str, list[dict[str, Any]]] = {
        "training": [],
        "validation": [],
        "holdout": [],
    }
    for key in sorted(groups):
        name = split_name(
            key,
            train_pct=train_pct,
            validation_pct=validation_pct,
        )
        result[name].extend(groups[key])
    return result


def split_name(
    key: str,
    *,
    train_pct: int = 70,
    validation_pct: int = 15,
) -> str:
    """Return the deterministic split for one event/day group."""

    if train_pct <= 0 or validation_pct < 0 or train_pct + validation_pct >= 100:
        raise ValueError("split percentages must leave a non-empty holdout range")
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < train_pct:
        return "training"
    if bucket < train_pct + validation_pct:
        return "validation"
    return "holdout"


def holdout_coverage(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    samples = [dict(row) for row in rows]
    groups = Counter(group_key(row) for row in samples)
    categories = {
        str((row.get("market_snapshot") or {}).get("category") or "UNKNOWN")
        for row in samples
        if isinstance(row.get("market_snapshot") or {}, Mapping)
    }
    days = {str(row.get("decision_ts") or "")[:10] for row in samples}
    max_group = max(groups.values(), default=0)
    return {
        "sample_count": len(samples),
        "independent_group_count": len(groups),
        "category_count": len(categories),
        "utc_day_count": len(days - {""}),
        "largest_group_count": max_group,
        "largest_group_ratio": (max_group / len(samples)) if samples else None,
        "group_keys": sorted(groups),
    }
