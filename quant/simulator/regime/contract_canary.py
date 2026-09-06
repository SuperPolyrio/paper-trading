"""Pure schema/value diff used by a scheduled external contract canary."""

from __future__ import annotations

from typing import Any, Mapping

from .model import VenueRegimeSnapshot, _json_value


def diff_regime(
    snapshot: VenueRegimeSnapshot, observed: Mapping[str, Any]
) -> dict[str, object]:
    baseline = _json_value(snapshot.__dict__)
    observed_normalized = _json_value(dict(observed))
    changed = {
        key: {"expected": baseline.get(key), "observed": observed_normalized.get(key)}
        for key in sorted(set(baseline) | set(observed_normalized))
        if baseline.get(key) != observed_normalized.get(key)
    }
    return {
        "status": "PASS" if not changed else "CHANGED",
        "regime_id": snapshot.regime_id,
        "source_hash": snapshot.source_hash,
        "changed": changed,
        "live_submission_performed": False,
    }
