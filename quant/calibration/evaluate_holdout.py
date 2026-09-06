"""Evaluate grouped holdout evidence and emit all promotion artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .calibration_metrics import holdout_metrics
from .calibration_domain import canonical_json
from .depth_survival_fit import fit_depth_survival
from .holdout_split import group_key, grouped_split, holdout_coverage, split_name
from .latency_estimator import estimate_latency
from .model_card import render_model_card
from .store import DEFAULT_VENUE_REGIME_ID, CalibrationStore

DEFAULT_CONFIG = Path("configs/calibration/taker_core_v1.yaml")


def evaluate_run(
    run_id: str,
    *,
    store: CalibrationStore | None = None,
    config_path: Path = DEFAULT_CONFIG,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    source = store or CalibrationStore()
    probes = source.load_probes(run_id)
    run = source.load_run(run_id) or {}
    return evaluate_probes(
        probes,
        evaluation_id=run_id,
        model_version=str(run.get("model_version") or "UNKNOWN"),
        venue_regime_id=str(run.get("venue_regime_id") or DEFAULT_VENUE_REGIME_ID),
        source={"kind": "single_run", "source_run_ids": [run_id]},
        config_path=config_path,
    )


def evaluate_probes(
    probes: Iterable[Mapping[str, Any]],
    *,
    evaluation_id: str,
    model_version: str,
    venue_regime_id: str,
    source: Mapping[str, Any] | None = None,
    config_path: Path = DEFAULT_CONFIG,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Evaluate one immutable collection of live probes.

    `evaluate_run` remains the compatibility entry point. Campaign callers pass
    every probe in their frozen collection here so the emitted manifest, fit,
    and eventual promotion gate all use exactly the same event/day split.
    """

    rows = [dict(row) for row in probes]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, Mapping):
        raise ValueError(f"calibration config must be an object: {config_path}")
    calibratable = [
        row for row in rows if str(row.get("probe_state")) == "CALIBRATABLE"
    ]
    split = grouped_split(calibratable)
    samples = {name: [_sample(row) for row in rows] for name, rows in split.items()}
    holdout = samples["holdout"]
    metrics = holdout_metrics(holdout)
    coverage = holdout_coverage(split["holdout"])
    state_counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("probe_state") or "UNKNOWN")
        state_counts[state] = state_counts.get(state, 0) + 1
    complete = sum(
        bool((row.get("artifact_bitmap") or {}).get("complete")) for row in calibratable
    )
    gates = config["sample_gates"]
    thresholds = config["promotion_gates"]
    false_positive = metrics["false_positive_fill"]
    checks = {
        "artifact_completeness_100pct": complete == len(calibratable)
        and bool(calibratable),
        "core_calibratable_minimum": len(calibratable)
        >= int(gates["core_calibratable"]),
        "holdout_minimum": len(holdout) >= int(gates["holdout"]),
        "holdout_independent_events": coverage["independent_group_count"]
        >= int(gates["independent_events"]),
        "holdout_categories": coverage["category_count"] >= int(gates["categories"]),
        "holdout_utc_days": coverage["utc_day_count"] >= int(gates["utc_days"]),
        "single_group_ratio": (
            coverage["largest_group_ratio"] is not None
            and coverage["largest_group_ratio"]
            <= float(gates["max_single_group_ratio"])
        ),
        "unknown_submit_outcome_zero": state_counts.get("SUBMIT_OUTCOME_UNKNOWN", 0)
        == 0,
        "accounting_mismatch_zero": state_counts.get("ACCOUNTING_MISMATCH", 0) == 0,
        "self_trade_contaminated_zero": state_counts.get("SELF_TRADE_CONTAMINATED", 0)
        == 0,
        "fok_false_positive_full_zero": (
            metrics["fok_false_positive_full"]["successes"] == 0
        ),
        "false_positive_fill_upper": (
            false_positive["upper"] is not None
            and false_positive["upper"]
            <= float(thresholds["false_positive_fill_upper_95"])
        ),
        "vwap_median": _le(
            metrics["vwap_error_median_ticks"], thresholds["vwap_error_median_ticks"]
        ),
        "vwap_p95": _le(
            metrics["vwap_error_p95_ticks"], thresholds["vwap_error_p95_ticks"]
        ),
        "size_overprediction_p95": _le(
            metrics["filled_size_overprediction_p95"],
            thresholds["filled_size_overprediction_p95"],
        ),
        "fee_error": _le(metrics["fee_error_max"], thresholds["fee_rounding_unit"]),
    }
    passed = all(checks.values())
    dataset_manifest = _dataset_manifest(
        rows,
        split=split,
        evaluation_id=evaluation_id,
        model_version=model_version,
        venue_regime_id=venue_regime_id,
        source=source or {},
    )
    payload = {
        "schema_version": "taker_holdout_evaluation_v3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": evaluation_id,
        "evaluation_id": evaluation_id,
        "model_version": model_version,
        "venue_regime_id": venue_regime_id,
        "validated_domain": {
            key: value
            for key, value in config.items()
            if key not in {"sample_gates", "promotion_gates", "samplers"}
        },
        "probe_count": len(rows),
        "calibratable_count": len(calibratable),
        "state_counts": state_counts,
        "artifact_complete_count": complete,
        "holdout_coverage": coverage,
        "holdout_metrics": metrics,
        "latency_quantiles": estimate_latency(split["training"]),
        "depth_survival": fit_depth_survival(samples["training"]),
        "dataset_manifest": dataset_manifest,
        "promotion_decision": {
            "status": "PASS" if passed else "BLOCKED",
            "promotion_allowed": passed,
            "checks": checks,
            "reason": None
            if passed
            else "real grouped holdout evidence does not satisfy every gate",
        },
        # Compatibility keys consumed by the immutable registry gate.
        "status": "PASS" if passed else "BLOCKED",
        "promotion_allowed": passed,
        "checks": checks,
        "fit": {
            "sample_count": len(calibratable),
            "holdout_count": len(holdout),
        },
    }
    return payload, samples


def write_artifacts(
    output: Path,
    payload: Mapping[str, Any],
    samples: Mapping[str, list[dict[str, Any]]],
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _json(output / "manifest.json", payload)
    _json(output / "sample_manifest.json", payload.get("dataset_manifest") or {})
    _csv(
        output / "probe_completeness.csv",
        [
            {
                "split": name,
                "sample_count": len(rows),
                "complete": bool(rows),
            }
            for name, rows in samples.items()
        ],
    )
    _csv(
        output / "holdout_groups.csv",
        [
            {
                "probe_id": row.get("probe_id"),
                "group_key": group_key(row),
                "split": "holdout",
            }
            for row in samples["holdout"]
        ],
    )
    _csv(
        output / "confusion_matrix.csv", payload["holdout_metrics"]["confusion_matrix"]
    )
    _csv(
        output / "latency_quantiles.csv",
        [
            {"metric": metric, **values}
            for metric, values in payload["latency_quantiles"].items()
        ],
    )
    _csv(output / "depth_survival.csv", payload["depth_survival"])
    _csv(
        output / "price_quantity_fee_errors.csv",
        [
            {
                key: row.get(key)
                for key in (
                    "probe_id",
                    "price_error_ticks",
                    "filled_size_absolute_error",
                    "filled_size_relative_error",
                    "fee_error",
                )
            }
            for row in samples["holdout"]
        ],
    )
    _json(
        output / "confidence_intervals.json",
        {
            "false_positive_fill": payload["holdout_metrics"]["false_positive_fill"],
            "fok_false_positive_full": payload["holdout_metrics"][
                "fok_false_positive_full"
            ],
        },
    )
    _json(output / "promotion_decision.json", payload["promotion_decision"])
    (output / "model_card.md").write_text(render_model_card(payload), encoding="utf-8")


def _sample(row: Mapping[str, Any]) -> dict[str, Any]:
    reconciliation = row.get("reconciliation")
    sample = dict(reconciliation) if isinstance(reconciliation, Mapping) else {}
    snapshot = row.get("market_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    sample.update(
        {
            "probe_id": row.get("probe_id"),
            "condition_id": row.get("condition_id"),
            "market_id": row.get("market_id"),
            "decision_ts": row.get("decision_ts"),
            "market_snapshot": dict(snapshot),
            "order_type": row.get("order_type"),
            "side": row.get("side"),
            "venue_regime_id": row.get("venue_regime_id"),
            "category": snapshot.get("category"),
            "spread_ticks": snapshot.get("spread_ticks"),
            "timestamps": row.get("timestamps") or {},
            "depth_survival_ratio": sample.get("depth_survival_ratio"),
        }
    )
    return sample


def _dataset_manifest(
    rows: Iterable[Mapping[str, Any]],
    *,
    split: Mapping[str, Iterable[Mapping[str, Any]]],
    evaluation_id: str,
    model_version: str,
    venue_regime_id: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    split_by_probe_id = {
        str(row.get("probe_id") or ""): name
        for name, group_rows in split.items()
        for row in group_rows
    }
    entries = []
    for row in sorted(rows, key=lambda item: str(item.get("probe_id") or "")):
        probe_id = str(row.get("probe_id") or "")
        evidence = {
            "probe_id": probe_id,
            "run_id": str(row.get("run_id") or ""),
            "model_version": str(row.get("model_version") or model_version),
            "manifest_id": str(row.get("manifest_id") or ""),
            "venue_regime_id": str(row.get("venue_regime_id") or venue_regime_id),
            "probe_state": str(row.get("probe_state") or "UNKNOWN"),
            "exchange_submit_called": bool(row.get("exchange_submit_called")),
            "condition_id": str(row.get("condition_id") or ""),
            "market_id": str(row.get("market_id") or ""),
            "decision_ts": str(row.get("decision_ts") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "group_key": group_key(row),
            "split": split_by_probe_id.get(probe_id, "excluded"),
            "artifact_complete": bool(
                (row.get("artifact_bitmap") or {}).get("complete")
            ),
        }
        fingerprint_input = {
            "evidence": evidence,
            "reconciliation": row.get("reconciliation") or {},
        }
        evidence["evidence_sha256"] = hashlib.sha256(
            canonical_json(fingerprint_input).encode("utf-8")
        ).hexdigest()
        entries.append(evidence)
    source_run_ids = sorted(
        {str(value) for value in (source.get("source_run_ids") or []) if str(value)}
        | {entry["run_id"] for entry in entries if entry["run_id"]}
    )
    immutable = {
        "schema_version": "taker_calibration_dataset_manifest_v1",
        "evaluation_id": str(evaluation_id),
        "model_version": str(model_version),
        "venue_regime_id": str(venue_regime_id),
        "split_policy": "condition/event plus UTC decision day; deterministic 70/15/15 hash split",
        "source": {
            key: value
            for key, value in source.items()
            if key not in {"generated_at", "content_sha256"}
        },
        "source_run_ids": source_run_ids,
        "entries": entries,
    }
    return {
        **immutable,
        "content_sha256": hashlib.sha256(
            canonical_json(immutable).encode("utf-8")
        ).hexdigest(),
    }


def _le(value: Any, maximum: Any) -> bool:
    try:
        return value is not None and float(value) <= float(maximum)
    except (TypeError, ValueError):
        return False


def _json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    values = list(rows)
    fields = sorted({key for row in values for key in row}) or ["status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        if values:
            writer.writerows(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload, samples = evaluate_run(args.run_id, config_path=args.config)
    output = args.output or Path("reports/calibration") / args.run_id
    write_artifacts(output, payload, samples)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
