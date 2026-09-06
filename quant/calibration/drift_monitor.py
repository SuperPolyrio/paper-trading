"""Rolling calibrated-model drift monitor with fail-closed demotion."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant.core.db import postgres_connection

from .calibration_metrics import holdout_metrics, wilson_interval
from .latency_estimator import estimate_latency
from .store import DEFAULT_VENUE_REGIME_ID, CalibrationStore


def check_drift(
    *,
    window: int = 50,
    minimum_samples: int = 50,
    group_minimum_samples: int = 20,
    false_positive_threshold: float = 0.02,
    vwap_p95_threshold_ticks: float = 1.0,
    rest_mismatch_threshold: float = 0.02,
    latency_p95_ratio_threshold: float = 2.0,
    latency_minimum_samples: int = 10,
) -> dict[str, object]:
    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT venue_regime_id, market_snapshot, reconciliation, timestamps
            FROM quant.paper_calibration_probes
            WHERE probe_state='CALIBRATABLE'
              AND decision_ts >= %s
            ORDER BY decision_ts DESC
            LIMIT 10000
            """,
            (datetime.now(timezone.utc) - timedelta(days=7),),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT model_version, venue_regime_id, metrics_json
            FROM quant.execution_model_versions
            WHERE model_family='TAKER_L2' AND status='CALIBRATED_ACTIVE'
            ORDER BY promoted_at DESC NULLS LAST, created_at DESC
            """
        )
        active_models = [dict(row) for row in cur.fetchall()]
    latency_baselines = {
        str(row["venue_regime_id"]): _latency_baseline(row.get("metrics_json"))
        for row in active_models
    }
    payload = evaluate_drift_windows(
        rows,
        window=window,
        minimum_samples=minimum_samples,
        group_minimum_samples=group_minimum_samples,
        false_positive_threshold=false_positive_threshold,
        vwap_p95_threshold_ticks=vwap_p95_threshold_ticks,
        rest_mismatch_threshold=rest_mismatch_threshold,
        latency_p95_ratio_threshold=latency_p95_ratio_threshold,
        latency_minimum_samples=latency_minimum_samples,
        latency_baselines=latency_baselines,
        current_venue_regime_id=DEFAULT_VENUE_REGIME_ID,
        active_model_regimes={
            str(row["venue_regime_id"]) for row in active_models
        },
    )
    drifted = payload["status"] == "DRIFTED"
    regimes = payload["affected_regimes"]
    demoted = 0
    if drifted:
        store = CalibrationStore()
        for regime in regimes:
            demoted += store.mark_models_stale(
                venue_regime_id=regime,
                model_family="TAKER_L2",
                reason=(
                    "drift_scopes="
                    + ",".join(payload["drifted_scopes"])
                ),
            )
    return {
        **payload,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models_demoted": demoted,
    }


def evaluate_drift_windows(
    rows: Iterable[dict[str, Any]],
    *,
    window: int,
    minimum_samples: int,
    group_minimum_samples: int,
    false_positive_threshold: float,
    vwap_p95_threshold_ticks: float = 1.0,
    rest_mismatch_threshold: float = 0.02,
    latency_p95_ratio_threshold: float = 2.0,
    latency_minimum_samples: int = 10,
    latency_baselines: dict[str, dict[str, Any]] | None = None,
    current_venue_regime_id: str | None = None,
    active_model_regimes: set[str] | None = None,
) -> dict[str, Any]:
    samples = list(rows)
    short_window = max(1, int(window))
    long_window = max(100, short_window * 2)
    minimum = max(1, int(minimum_samples))
    group_minimum = max(1, int(group_minimum_samples))
    baselines = latency_baselines or {}

    def evaluate(
        values: list[dict[str, Any]],
        *,
        size: int,
        required: int,
    ) -> dict[str, Any]:
        return evaluate_drift(
            values,
            window=size,
            minimum_samples=required,
            false_positive_threshold=false_positive_threshold,
            vwap_p95_threshold_ticks=vwap_p95_threshold_ticks,
            rest_mismatch_threshold=rest_mismatch_threshold,
            latency_p95_ratio_threshold=latency_p95_ratio_threshold,
            latency_minimum_samples=latency_minimum_samples,
            latency_baseline=_baseline_for_rows(values, baselines),
        )

    windows = {
        f"last_{short_window}": evaluate(
            samples[:short_window],
            size=short_window,
            required=minimum,
        ),
        f"last_{long_window}": evaluate(
            samples[:long_window],
            size=long_window,
            required=max(minimum, long_window),
        ),
        "last_7_days": evaluate(
            samples,
            size=len(samples),
            required=minimum,
        ),
    }
    by_regime = _grouped_drift(
        samples,
        key=lambda row: str(row.get("venue_regime_id") or "UNKNOWN"),
        window=short_window,
        minimum_samples=group_minimum,
        false_positive_threshold=false_positive_threshold,
        vwap_p95_threshold_ticks=vwap_p95_threshold_ticks,
        rest_mismatch_threshold=rest_mismatch_threshold,
        latency_p95_ratio_threshold=latency_p95_ratio_threshold,
        latency_minimum_samples=latency_minimum_samples,
        latency_baselines=baselines,
    )
    by_category = _grouped_drift(
        samples,
        key=lambda row: str(
            (row.get("market_snapshot") or {}).get("category") or "UNKNOWN"
        ),
        window=short_window,
        minimum_samples=group_minimum,
        false_positive_threshold=false_positive_threshold,
        vwap_p95_threshold_ticks=vwap_p95_threshold_ticks,
        rest_mismatch_threshold=rest_mismatch_threshold,
        latency_p95_ratio_threshold=latency_p95_ratio_threshold,
        latency_minimum_samples=latency_minimum_samples,
        latency_baselines=baselines,
    )
    drifted_scopes = [
        *[
            f"windows.{name}"
            for name, result in windows.items()
            if result["status"] == "DRIFTED"
        ],
        *[
            f"by_venue_regime.{name}"
            for name, result in by_regime.items()
            if result["status"] == "DRIFTED"
        ],
        *[
            f"by_category.{name}"
            for name, result in by_category.items()
            if result["status"] == "DRIFTED"
        ],
    ]
    changed_regimes = sorted(
        regime
        for regime in (active_model_regimes or set())
        if current_venue_regime_id and regime != current_venue_regime_id
    )
    drifted_scopes.extend(
        f"venue_regime_changed.{regime}_to_{current_venue_regime_id}"
        for regime in changed_regimes
    )
    mature = any(
        result["status"] in {"PASS", "DRIFTED"}
        for result in [
            *windows.values(),
            *by_regime.values(),
            *by_category.values(),
        ]
    )
    venue_regimes = sorted(
        {
            str(row["venue_regime_id"])
            for row in samples
            if row.get("venue_regime_id")
        }
    )
    affected_regimes = sorted(
        {
            regime
            for regime, result in by_regime.items()
            if regime != "UNKNOWN" and result["status"] == "DRIFTED"
        }
    )
    affected_regimes = sorted(set(affected_regimes) | set(changed_regimes))
    if drifted_scopes and not affected_regimes:
        affected_regimes = venue_regimes
    primary = windows[f"last_{short_window}"]
    return {
        **primary,
        "status": (
            "NO_DATA"
            if not samples
            else "DRIFTED"
            if drifted_scopes
            else "PASS"
            if mature
            else "INSUFFICIENT_DATA"
        ),
        "sample_count": len(samples),
        "minimum_samples": minimum,
        "group_minimum_samples": group_minimum,
        "venue_regimes": venue_regimes,
        "windows": windows,
        "by_venue_regime": by_regime,
        "by_category": by_category,
        "drifted_scopes": drifted_scopes,
        "affected_regimes": affected_regimes,
        "current_venue_regime_id": current_venue_regime_id,
        "active_model_regimes": sorted(active_model_regimes or set()),
    }


def _grouped_drift(
    rows: Iterable[dict[str, Any]],
    *,
    key: Any,
    window: int,
    minimum_samples: int,
    false_positive_threshold: float,
    vwap_p95_threshold_ticks: float,
    rest_mismatch_threshold: float,
    latency_p95_ratio_threshold: float,
    latency_minimum_samples: int,
    latency_baselines: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return {
        name: evaluate_drift(
            values[:window],
            window=window,
            minimum_samples=minimum_samples,
            false_positive_threshold=false_positive_threshold,
            vwap_p95_threshold_ticks=vwap_p95_threshold_ticks,
            rest_mismatch_threshold=rest_mismatch_threshold,
            latency_p95_ratio_threshold=latency_p95_ratio_threshold,
            latency_minimum_samples=latency_minimum_samples,
            latency_baseline=_baseline_for_rows(values, latency_baselines),
        )
        for name, values in sorted(grouped.items())
    }


def evaluate_drift(
    rows: Iterable[dict[str, Any]],
    *,
    window: int,
    minimum_samples: int,
    false_positive_threshold: float,
    vwap_p95_threshold_ticks: float = 1.0,
    rest_mismatch_threshold: float = 0.02,
    latency_p95_ratio_threshold: float = 2.0,
    latency_minimum_samples: int = 10,
    latency_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    samples = list(rows)
    metrics = holdout_metrics(
        row["reconciliation"] if isinstance(row.get("reconciliation"), dict) else {}
        for row in samples
    )
    false_positive = metrics.get("false_positive_fill") or {}
    point = false_positive.get("point")
    enough_data = len(samples) >= max(1, int(minimum_samples))
    rest_values = [
        bool((row.get("market_snapshot") or {}).get("rest_book_match"))
        for row in samples
        if (row.get("market_snapshot") or {}).get("rest_book_match") is not None
    ]
    rest_mismatch = wilson_interval(
        sum(not value for value in rest_values),
        len(rest_values),
    )
    latency = estimate_latency(samples)
    latency_drift = _latency_drift(
        latency,
        latency_baseline or {},
        ratio_threshold=latency_p95_ratio_threshold,
        minimum_samples=latency_minimum_samples,
    )
    vwap_p95 = _float(metrics.get("vwap_error_p95_ticks"))
    checks = {
        "minimum_samples_met": enough_data,
        "false_positive_rate_within_threshold": (
            None
            if point is None
            else float(point) <= float(false_positive_threshold)
        ),
        "vwap_p95_within_threshold": (
            None
            if vwap_p95 is None
            else vwap_p95 <= float(vwap_p95_threshold_ticks)
        ),
        "rest_mismatch_rate_within_threshold": (
            None
            if rest_mismatch["point"] is None
            else float(rest_mismatch["point"]) <= float(rest_mismatch_threshold)
        ),
        "latency_p95_within_threshold": latency_drift["within_threshold"],
    }
    drift_reasons = [
        name
        for name, passed in checks.items()
        if name != "minimum_samples_met" and passed is False
    ]
    drifted = enough_data and bool(drift_reasons)
    return {
        "status": (
            "NO_DATA"
            if not samples
            else "INSUFFICIENT_DATA"
            if not enough_data
            else "DRIFTED"
            if drifted
            else "PASS"
        ),
        "window": window,
        "minimum_samples": max(1, int(minimum_samples)),
        "sample_count": len(samples),
        "false_positive_threshold": false_positive_threshold,
        "vwap_p95_threshold_ticks": vwap_p95_threshold_ticks,
        "rest_mismatch_threshold": rest_mismatch_threshold,
        "latency_p95_ratio_threshold": latency_p95_ratio_threshold,
        "venue_regimes": sorted(
            {
                str(row["venue_regime_id"])
                for row in samples
                if row.get("venue_regime_id")
            }
        ),
        "metrics": metrics,
        "operational_metrics": {
            "rest_book_mismatch": rest_mismatch,
            "latency_quantiles": latency,
            "latency_drift": latency_drift,
        },
        "checks": checks,
        "drift_reasons": drift_reasons,
    }


def _latency_baseline(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    direct = metrics.get("latency_quantiles")
    if isinstance(direct, dict):
        return direct
    fit = metrics.get("fit")
    if isinstance(fit, dict) and isinstance(fit.get("latency_quantiles"), dict):
        return fit["latency_quantiles"]
    return {}


def _baseline_for_rows(
    rows: Iterable[dict[str, Any]],
    baselines: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    regimes = {
        str(row["venue_regime_id"])
        for row in rows
        if row.get("venue_regime_id")
    }
    if len(regimes) != 1:
        return {}
    return baselines.get(next(iter(regimes)), {})


def _latency_drift(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    ratio_threshold: float,
    minimum_samples: int,
) -> dict[str, Any]:
    comparisons: dict[str, dict[str, Any]] = {}
    drifted_metrics: list[str] = []
    for name, values in current.items():
        current_count = int(values.get("count") or 0)
        current_p95 = _float(values.get("p95"))
        baseline_values = baseline.get(name)
        if not isinstance(baseline_values, dict):
            continue
        baseline_p95 = _float(baseline_values.get("p95"))
        baseline_count = int(baseline_values.get("count") or 0)
        if (
            current_count < max(1, int(minimum_samples))
            or baseline_count < max(1, int(minimum_samples))
            or current_p95 is None
            or baseline_p95 is None
            or baseline_p95 <= 0
        ):
            continue
        ratio = current_p95 / baseline_p95
        drifted = ratio > float(ratio_threshold)
        comparisons[name] = {
            "current_count": current_count,
            "current_p95_ms": current_p95,
            "baseline_count": baseline_count,
            "baseline_p95_ms": baseline_p95,
            "ratio": ratio,
            "drifted": drifted,
        }
        if drifted:
            drifted_metrics.append(name)
    return {
        "status": (
            "NO_BASELINE"
            if not comparisons
            else "DRIFTED"
            if drifted_metrics
            else "PASS"
        ),
        "ratio_threshold": ratio_threshold,
        "minimum_samples": max(1, int(minimum_samples)),
        "within_threshold": (
            None if not comparisons else not bool(drifted_metrics)
        ),
        "drifted_metrics": drifted_metrics,
        "comparisons": comparisons,
    }


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(parsed) if parsed.is_finite() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--window", type=int, default=50)
    parser.add_argument("--minimum-samples", type=int, default=50)
    parser.add_argument("--group-minimum-samples", type=int, default=20)
    parser.add_argument("--false-positive-threshold", type=float, default=0.02)
    parser.add_argument("--vwap-p95-threshold-ticks", type=float, default=1.0)
    parser.add_argument("--rest-mismatch-threshold", type=float, default=0.02)
    parser.add_argument("--latency-p95-ratio-threshold", type=float, default=2.0)
    parser.add_argument("--latency-minimum-samples", type=int, default=10)
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("runtime_outputs/taker_calibration/drift-latest.json"),
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=Path("runtime_outputs/taker_calibration/drift-latest.md"),
    )
    args = parser.parse_args(argv)
    payload = check_drift(
        window=max(1, args.window),
        minimum_samples=max(1, args.minimum_samples),
        group_minimum_samples=max(1, args.group_minimum_samples),
        false_positive_threshold=max(0.0, args.false_positive_threshold),
        vwap_p95_threshold_ticks=max(0.0, args.vwap_p95_threshold_ticks),
        rest_mismatch_threshold=max(0.0, args.rest_mismatch_threshold),
        latency_p95_ratio_threshold=max(
            1.0, args.latency_p95_ratio_threshold
        ),
        latency_minimum_samples=max(1, args.latency_minimum_samples),
    )
    _write_report(payload, args.json_out, args.markdown_out)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 2 if payload["status"] == "DRIFTED" else 0


def _write_report(
    payload: dict[str, Any],
    json_path: Path,
    markdown_path: Path,
) -> None:
    _atomic_write(
        json_path,
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
    )
    false_positive = payload.get("metrics", {}).get("false_positive_fill") or {}
    lines = [
        "# Taker Calibration Drift Monitor",
        "",
        f"- Status: **{payload.get('status')}**",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Samples: `{payload.get('sample_count')}/{payload.get('minimum_samples')}`",
        f"- Window: `{payload.get('window')}`",
        f"- False-positive threshold: `{payload.get('false_positive_threshold')}`",
        f"- VWAP p95 threshold: `{payload.get('vwap_p95_threshold_ticks')}` ticks",
        f"- REST mismatch threshold: `{payload.get('rest_mismatch_threshold')}`",
        (
            "- Latency p95 ratio threshold: "
            f"`{payload.get('latency_p95_ratio_threshold')}`"
        ),
        f"- False-positive point estimate: `{false_positive.get('point')}`",
        f"- False-positive 95% upper bound: `{false_positive.get('upper')}`",
        f"- Models demoted: `{payload.get('models_demoted')}`",
        f"- Drifted scopes: `{payload.get('drifted_scopes')}`",
        f"- Affected regimes: `{payload.get('affected_regimes')}`",
        "",
        "## Window Status",
        "",
    ]
    lines.extend(
        f"- {name}: `{result.get('status')}` "
        f"({result.get('sample_count')}/{result.get('minimum_samples')})"
        for name, result in (payload.get("windows") or {}).items()
    )
    lines.extend(
        [
            "",
            "## Category Status",
            "",
        ]
    )
    lines.extend(
        f"- {name}: `{result.get('status')}` "
        f"({result.get('sample_count')}/{result.get('minimum_samples')})"
        for name, result in (payload.get("by_category") or {}).items()
    )
    lines.extend(
        [
            "",
            (
                "Promotion still uses the configured holdout confidence-interval gate. "
                "This rolling monitor uses the observed rate after the minimum sample "
                "count so a small perfect window does not automatically demote a model."
            ),
        ]
    )
    _atomic_write(markdown_path, "\n".join(lines) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
