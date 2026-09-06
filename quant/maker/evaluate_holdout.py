"""Evaluate post-only maker evidence without submitting any order."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from quant.calibration.calibration_metrics import wilson_interval

OUTCOMES = ("NO_FILL", "PARTIAL", "FULL")


def evaluate(
    rows: Iterable[Mapping[str, Any]],
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    samples = [dict(row) for row in rows]
    gates = config["holdout_gates"]
    complete = [row for row in samples if bool(row.get("artifact_complete"))]
    authenticated = [
        row
        for row in samples
        if row.get("evidence_authority") == "AUTHENTICATED_OWN_ORDER"
        and bool(row.get("evidence_validated"))
    ]
    events = {str(row.get("event_id") or "") for row in samples if row.get("event_id")}
    days = {str(row.get("utc_day") or "") for row in samples if row.get("utc_day")}
    order_ids = [str(row.get("order_id") or "") for row in samples]
    prediction_ordered = [
        row for row in samples if bool(row.get("prediction_precedes_submission"))
    ]
    prediction_input_ready = [
        row for row in samples if row.get("prediction_input_ready") is not False
    ]
    actual_counts = {outcome: 0 for outcome in OUTCOMES}
    brier_values: list[float] = []
    log_losses: list[float] = []
    confidence_rows: list[tuple[float, float]] = []
    false_positive_trials = 0
    false_positives = 0
    size_errors: list[float] = []
    size_relative_errors: list[float] = []
    first_fill_errors: list[float] = []
    first_fill_relative_errors: list[float] = []
    full_fill_errors: list[float] = []
    full_fill_relative_errors: list[float] = []
    censoring_metadata_count = 0
    first_fill_right_censored_count = 0
    full_fill_right_censored_count = 0
    observation_label_counts: dict[str, int] = {}
    for row in samples:
        actual = str(row.get("actual_outcome") or "").upper()
        if actual not in OUTCOMES:
            continue
        observation = _outcome_observation(row, actual=actual)
        if observation["explicit"]:
            censoring_metadata_count += 1
        first_fill_right_censored_count += int(observation["first_fill_right_censored"])
        full_fill_right_censored_count += int(observation["full_fill_right_censored"])
        label = str(observation["label"])
        observation_label_counts[label] = observation_label_counts.get(label, 0) + 1
        actual_counts[actual] += 1
        probabilities = _probabilities(row)
        brier_values.append(
            sum(
                (probabilities[outcome] - (1.0 if actual == outcome else 0.0)) ** 2
                for outcome in OUTCOMES
            )
        )
        log_losses.append(-math.log(max(1e-12, probabilities[actual])))
        predicted = max(OUTCOMES, key=lambda outcome: probabilities[outcome])
        confidence_rows.append(
            (probabilities[predicted], 1.0 if predicted == actual else 0.0)
        )
        predicted_fill = probabilities["PARTIAL"] + probabilities["FULL"] >= 0.5
        if predicted_fill:
            false_positive_trials += 1
            false_positives += int(actual == "NO_FILL")
        _append_error(size_errors, row, "expected_filled_size", "actual_filled_size")
        if actual in {"PARTIAL", "FULL"}:
            _append_relative_error(
                size_relative_errors,
                row,
                "expected_filled_size",
                "actual_filled_size",
                scale_key="order_size",
            )
        if not observation["first_fill_right_censored"]:
            _append_error(
                first_fill_errors,
                row,
                "expected_time_to_first_fill_seconds",
                "actual_time_to_first_fill_seconds",
            )
            _append_relative_error(
                first_fill_relative_errors,
                row,
                "expected_time_to_first_fill_seconds",
                "actual_time_to_first_fill_seconds",
            )
        if not observation["full_fill_right_censored"]:
            _append_error(
                full_fill_errors,
                row,
                "expected_time_to_full_fill_seconds",
                "actual_time_to_full_fill_seconds",
            )
            _append_relative_error(
                full_fill_relative_errors,
                row,
                "expected_time_to_full_fill_seconds",
                "actual_time_to_full_fill_seconds",
            )
    evaluated = sum(actual_counts.values())
    configured_naive = config.get("naive_probabilities") or {
        outcome: 1 / len(OUTCOMES) for outcome in OUTCOMES
    }
    naive_probabilities = _probabilities(
        {
            "p_no_fill": configured_naive.get("NO_FILL"),
            "p_partial": configured_naive.get("PARTIAL"),
            "p_full": configured_naive.get("FULL"),
        }
    )
    naive = (
        sum(
            actual_counts[actual]
            * sum(
                (naive_probabilities[outcome] - (1.0 if actual == outcome else 0.0))
                ** 2
                for outcome in OUTCOMES
            )
            for actual in OUTCOMES
        )
        / evaluated
        if evaluated
        else None
    )
    brier = _mean(brier_values)
    fp_interval = wilson_interval(false_positives, false_positive_trials)
    ece = _ece(confidence_rows)
    size_relative_mae = _mean(size_relative_errors)
    first_fill_relative_mae = _mean(first_fill_relative_errors)
    full_fill_relative_mae = _mean(full_fill_relative_errors)
    checks = {
        "artifact_completeness_100pct": len(complete) == len(samples) and bool(samples),
        "minimum_samples": evaluated >= int(gates["minimum_samples"]),
        "independent_events": len(events) >= int(gates["independent_events"]),
        "utc_days": len(days) >= int(gates["utc_days"]),
        "brier_better_than_naive": (
            brier is not None
            and naive is not None
            and (
                brier < naive
                or not bool(gates.get("require_brier_better_than_naive", True))
            )
        ),
        "ece": ece is not None and ece <= float(gates["max_ece"]),
        "false_positive_fill_upper": (
            fp_interval["upper"] is not None
            and fp_interval["upper"] <= float(gates["max_false_positive_fill_upper_95"])
        ),
    }
    placement_counts = _value_counts(samples, "placement")
    horizon_counts = _value_counts(samples, "resting_seconds")
    queue_bucket_counts = _value_counts(samples, "queue_bucket")
    required_placements = {
        str(value) for value in gates.get("required_placements") or ()
    }
    required_horizons = {
        str(value) for value in gates.get("required_resting_seconds") or ()
    }
    if required_placements:
        checks["required_placement_coverage"] = required_placements <= {
            key for key, count in placement_counts.items() if count > 0
        }
    if required_horizons:
        checks["required_resting_horizon_coverage"] = required_horizons <= {
            key for key, count in horizon_counts.items() if count > 0
        }
    if gates.get("minimum_observed_queue_buckets") is not None:
        checks["minimum_observed_queue_buckets"] = sum(
            count > 0 for count in queue_bucket_counts.values()
        ) >= int(gates["minimum_observed_queue_buckets"])
    if bool(gates.get("require_authenticated_evidence")):
        checks["authenticated_own_order_evidence_100pct"] = len(authenticated) == len(
            samples
        ) and bool(samples)
    if bool(gates.get("require_unique_order_ids")):
        checks["unique_official_order_ids"] = (
            bool(order_ids) and all(order_ids) and len(set(order_ids)) == len(order_ids)
        )
    if bool(gates.get("require_prediction_before_submission")):
        checks["prediction_frozen_before_submission_100pct"] = len(
            prediction_ordered
        ) == len(samples) and bool(samples)
    checks["prediction_input_ready_100pct"] = len(prediction_input_ready) == len(
        samples
    ) and bool(samples)
    if bool(gates.get("require_censoring_metadata")):
        checks["censoring_metadata_100pct"] = (
            censoring_metadata_count == evaluated and bool(evaluated)
        )
    _add_minimum_check(
        checks,
        gates,
        "minimum_no_fill_outcomes",
        actual_counts["NO_FILL"],
    )
    _add_minimum_check(
        checks,
        gates,
        "minimum_partial_outcomes",
        actual_counts["PARTIAL"],
    )
    _add_minimum_check(
        checks,
        gates,
        "minimum_full_outcomes",
        actual_counts["FULL"],
    )
    _add_minimum_check(
        checks,
        gates,
        "minimum_fill_size_observations",
        len(size_relative_errors),
    )
    _add_minimum_check(
        checks,
        gates,
        "minimum_first_fill_time_observations",
        len(first_fill_relative_errors),
    )
    _add_minimum_check(
        checks,
        gates,
        "minimum_full_fill_time_observations",
        len(full_fill_relative_errors),
    )
    _add_maximum_check(
        checks,
        gates,
        "max_fill_size_relative_mae",
        size_relative_mae,
    )
    _add_maximum_check(
        checks,
        gates,
        "max_time_to_first_fill_relative_mae",
        first_fill_relative_mae,
    )
    _add_maximum_check(
        checks,
        gates,
        "max_time_to_full_fill_relative_mae",
        full_fill_relative_mae,
    )
    status = "PASS" if all(checks.values()) else "BLOCKED"
    outcome_diversity = sum(count > 0 for count in actual_counts.values())
    return {
        "schema_version": "maker_holdout_evaluation_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": config["model_version"],
        "status": status,
        "live_submission_performed": False,
        "evidence_contains_live_orders": bool(authenticated),
        "sample_count": len(samples),
        "evaluated_count": evaluated,
        "artifact_complete_count": len(complete),
        "independent_event_count": len(events),
        "utc_day_count": len(days),
        "outcome_counts": actual_counts,
        "outcome_diversity": outcome_diversity,
        "evidence": {
            "authenticated_own_order_count": len(authenticated),
            "prediction_before_submission_count": len(prediction_ordered),
            "prediction_input_ready_count": len(prediction_input_ready),
            "prediction_input_not_ready_count": len(samples)
            - len(prediction_input_ready),
            "unique_official_order_count": len({item for item in order_ids if item}),
            "category_counts": _value_counts(samples, "category"),
            "placement_counts": placement_counts,
            "horizon_counts": horizon_counts,
            "queue_bucket_counts": queue_bucket_counts,
            "observation_label_counts": dict(sorted(observation_label_counts.items())),
            "censoring_metadata_count": censoring_metadata_count,
        },
        "metrics": {
            "brier_score": brier,
            "naive_base_rate_brier": naive,
            "log_loss": _mean(log_losses),
            "expected_calibration_error": ece,
            "false_positive_fill": fp_interval,
            "fill_size_mae": _mean(size_errors),
            "fill_size_observation_count": len(size_relative_errors),
            "fill_size_relative_mae": size_relative_mae,
            "time_to_first_fill_mae_seconds": _mean(first_fill_errors),
            "time_to_first_fill_observation_count": len(first_fill_relative_errors),
            "time_to_first_fill_relative_mae": first_fill_relative_mae,
            "time_to_full_fill_mae_seconds": _mean(full_fill_errors),
            "time_to_full_fill_observation_count": len(full_fill_relative_errors),
            "time_to_full_fill_relative_mae": full_fill_relative_mae,
            "first_fill_right_censored_count": first_fill_right_censored_count,
            "full_fill_right_censored_count": full_fill_right_censored_count,
            "first_fill_uncensored_count": evaluated - first_fill_right_censored_count,
            "full_fill_uncensored_count": evaluated - full_fill_right_censored_count,
        },
        "checks": checks,
        "promotion_allowed": status == "PASS",
        "metric_interpretation": (
            "PROMOTION_ELIGIBLE"
            if status == "PASS"
            else "DESCRIPTIVE_ONLY_NOT_CALIBRATED"
        ),
    }


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [
            dict(row)
            for row in (
                payload if isinstance(payload, list) else payload.get("rows", [])
            )
        ]
    return [
        dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_report(output: Path, report: Mapping[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Maker Holdout Evaluation",
        "",
        f"- Model: `{report['model_version']}`",
        f"- Status: **{report['status']}**",
        f"- Samples: `{report['evaluated_count']}`",
        f"- Independent events: `{report['independent_event_count']}`",
        f"- UTC days: `{report['utc_day_count']}`",
        f"- Brier: `{report['metrics']['brier_score']}`",
        f"- ECE: `{report['metrics']['expected_calibration_error']}`",
        (
            "- False-positive fill 95% upper: "
            f"`{report['metrics']['false_positive_fill']['upper']}`"
        ),
        f"- Outcome counts: `{report['outcome_counts']}`",
        f"- Metric interpretation: `{report['metric_interpretation']}`",
        (
            "- Authenticated own-order evidence: "
            f"`{report['evidence']['authenticated_own_order_count']}`"
        ),
        (
            "- Filled-size observations: "
            f"`{report['metrics']['fill_size_observation_count']}`"
        ),
        (
            "- First-fill timing observations: "
            f"`{report['metrics']['time_to_first_fill_observation_count']}`"
        ),
        (
            "- Full-fill timing observations: "
            f"`{report['metrics']['time_to_full_fill_observation_count']}`"
        ),
        (
            "- First-fill right-censored observations: "
            f"`{report['metrics']['first_fill_right_censored_count']}`"
        ),
        (
            "- Full-fill right-censored observations: "
            f"`{report['metrics']['full_fill_right_censored_count']}`"
        ),
        "",
        "## Promotion Checks",
        "",
        *[
            f"- {name}: `{'PASS' if passed else 'FAIL'}`"
            for name, passed in report["checks"].items()
        ],
        "",
        "No live order was submitted by this evaluator.",
        (
            "Existing LIVE evidence is evaluated only when its official order "
            "truth is authenticated."
        ),
    ]
    (output / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _probabilities(row: Mapping[str, Any]) -> dict[str, float]:
    values = {
        "NO_FILL": float(row.get("p_no_fill") or 0),
        "PARTIAL": float(row.get("p_partial") or 0),
        "FULL": float(row.get("p_full") or 0),
    }
    total = sum(max(0.0, value) for value in values.values())
    if total <= 0:
        return {outcome: 1 / 3 for outcome in OUTCOMES}
    return {outcome: max(0.0, values[outcome]) / total for outcome in OUTCOMES}


def _outcome_observation(
    row: Mapping[str, Any], *, actual: str
) -> dict[str, bool | str]:
    raw = row.get("outcome_observation")
    observation = raw if isinstance(raw, Mapping) else {}
    explicit = (
        observation.get("first_fill_right_censored") is not None
        and observation.get("full_fill_right_censored") is not None
        and bool(observation.get("label"))
    )
    if explicit:
        return {
            "explicit": True,
            "label": str(observation["label"]),
            "first_fill_right_censored": bool(observation["first_fill_right_censored"]),
            "full_fill_right_censored": bool(observation["full_fill_right_censored"]),
        }

    horizon = str(row.get("resting_seconds") or "UNKNOWN")
    if actual == "NO_FILL":
        return {
            "explicit": False,
            "label": f"LEGACY_CENSORED_AT_{horizon}S",
            "first_fill_right_censored": True,
            "full_fill_right_censored": True,
        }
    if actual == "PARTIAL":
        return {
            "explicit": False,
            "label": "LEGACY_PARTIAL_REMAINDER_CENSORED",
            "first_fill_right_censored": False,
            "full_fill_right_censored": True,
        }
    return {
        "explicit": False,
        "label": "LEGACY_FULLY_OBSERVED",
        "first_fill_right_censored": False,
        "full_fill_right_censored": False,
    }


def _ece(rows: list[tuple[float, float]], bins: int = 10) -> float | None:
    if not rows:
        return None
    total = len(rows)
    value = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [
            row
            for row in rows
            if low <= row[0] <= high and (index == bins - 1 or row[0] < high)
        ]
        if bucket:
            value += (
                len(bucket)
                / total
                * abs(
                    sum(row[0] for row in bucket) / len(bucket)
                    - sum(row[1] for row in bucket) / len(bucket)
                )
            )
    return value


def _append_error(
    target: list[float],
    row: Mapping[str, Any],
    predicted_key: str,
    actual_key: str,
) -> None:
    if row.get(predicted_key) not in (None, "") and row.get(actual_key) not in (
        None,
        "",
    ):
        target.append(abs(float(row[predicted_key]) - float(row[actual_key])))


def _append_relative_error(
    target: list[float],
    row: Mapping[str, Any],
    predicted_key: str,
    actual_key: str,
    *,
    scale_key: str | None = None,
) -> None:
    if row.get(predicted_key) in (None, "") or row.get(actual_key) in (None, ""):
        return
    predicted = abs(float(row[predicted_key]))
    actual = abs(float(row[actual_key]))
    scale_value = row.get(scale_key) if scale_key else actual
    if scale_value in (None, ""):
        return
    scale = abs(float(scale_value))
    if scale <= 0:
        return
    target.append(abs(predicted - actual) / scale)


def _add_minimum_check(
    checks: dict[str, bool],
    gates: Mapping[str, Any],
    name: str,
    observed: int,
) -> None:
    if name in gates:
        checks[name] = observed >= int(gates[name])


def _add_maximum_check(
    checks: dict[str, bool],
    gates: Mapping[str, Any],
    name: str,
    observed: float | None,
) -> None:
    if name in gates:
        checks[name] = observed is not None and observed <= float(gates[name])


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _value_counts(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/calibration/maker_core_v1.yaml")
    )
    parser.add_argument("--output", type=Path, default=Path("reports/maker/holdout"))
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    report = evaluate(read_rows(args.evidence), config=config)
    write_report(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
