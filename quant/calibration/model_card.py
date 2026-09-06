"""Markdown model-card rendering."""

from __future__ import annotations

from typing import Any, Mapping


def render_model_card(payload: Mapping[str, Any]) -> str:
    decision = payload.get("promotion_decision") or {}
    metrics = payload.get("holdout_metrics") or {}
    coverage = payload.get("holdout_coverage") or {}
    lines = [
        "# Taker Execution Model Card",
        "",
        f"- Model version: `{payload.get('model_version', 'UNKNOWN')}`",
        f"- Run ID: `{payload.get('run_id', 'UNKNOWN')}`",
        f"- Venue regime: `{payload.get('venue_regime_id', 'UNKNOWN')}`",
        f"- Status: **{decision.get('status', 'BLOCKED')}**",
        "",
        "## Validated Domain",
        "",
        "```json",
        _json(payload.get("validated_domain") or {}),
        "```",
        "",
        "## Holdout",
        "",
        f"- Samples: `{coverage.get('sample_count', 0)}`",
        f"- Independent groups: `{coverage.get('independent_group_count', 0)}`",
        f"- Categories: `{coverage.get('category_count', 0)}`",
        f"- UTC days: `{coverage.get('utc_day_count', 0)}`",
        f"- False-positive fill 95% upper: `{(metrics.get('false_positive_fill') or {}).get('upper')}`",
        f"- VWAP p95 ticks: `{metrics.get('vwap_error_p95_ticks')}`",
        "",
        "## Promotion Decision",
        "",
        "```json",
        _json(decision),
        "```",
        "",
        "This card does not treat test success or positive paper PnL as live-fill accuracy.",
    ]
    return "\n".join(lines) + "\n"


def _json(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True, default=str)
