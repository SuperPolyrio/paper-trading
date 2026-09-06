"""Evidence-safe offline labels and calibration reports for maker research.

This module is intentionally downstream of raw L2 reconstruction.  It does
not infer a maker fill from a depth decrease.  A fill label requires a
directionally compatible, ground-truth trade print at the resting price (or a
better price) after the estimated queue ahead.  Other observations are kept
as interval or unlabeled evidence and excluded from calibrated metrics.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from quant.calibration.calibration_metrics import wilson_interval
from quant.execution.models.maker_queue import MakerFillPrediction


class EvidenceQuality(str, Enum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    OBSERVED_TAPE = "OBSERVED_TAPE"
    WEAK_INTERVAL = "WEAK_INTERVAL"
    UNLABELED = "UNLABELED"


class MakerOutcome(str, Enum):
    NO_FILL = "NO_FILL"
    PARTIAL = "PARTIAL"
    FULL = "FULL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CounterfactualMakerOrder:
    shadow_order_id: str
    event_id: str
    asset_id: str
    side: str
    price: Decimal
    size: Decimal
    queue_ahead_estimate: Decimal
    horizon_seconds: Decimal
    quote_position: str = "AT_BEST"
    trial_id: str = ""


@dataclass(frozen=True)
class MakerEvidenceWindow:
    """Only ``compatible_trade_size`` can establish a high-confidence fill."""

    compatible_trade_size: Decimal = Decimal(0)
    incompatible_trade_size: Decimal = Decimal(0)
    book_level_removed_size: Decimal = Decimal(0)
    book_level_replenished_size: Decimal = Decimal(0)
    market_crossed_through_price: bool = False
    terminal_reason: str | None = None
    l2_complete: bool = True
    orderfilled_complete: bool = True
    queue_epoch_stable: bool = True
    observed_seconds: Decimal = Decimal(0)


@dataclass(frozen=True)
class MakerShadowLabel:
    outcome: MakerOutcome
    evidence_quality: EvidenceQuality
    filled_size_lower: Decimal
    filled_size_upper: Decimal
    observed_time_to_fill_seconds: Decimal | None
    reason: str

    @property
    def calibrated(self) -> bool:
        return self.evidence_quality == EvidenceQuality.HIGH_CONFIDENCE

    @property
    def label_class(self) -> str:
        if self.calibrated and self.outcome in {
            MakerOutcome.PARTIAL,
            MakerOutcome.FULL,
        }:
            return "STRICT_CONFIRMED_FILL"
        if (
            self.evidence_quality == EvidenceQuality.OBSERVED_TAPE
            and self.outcome == MakerOutcome.NO_FILL
        ):
            return "OBSERVED_TAPE_NO_FILL"
        return "AMBIGUOUS_ABSTAIN"


@dataclass(frozen=True)
class ShadowEvaluationRow:
    order: CounterfactualMakerOrder
    prediction: MakerFillPrediction
    label: MakerShadowLabel


def label_counterfactual(
    order: CounterfactualMakerOrder,
    evidence: MakerEvidenceWindow,
) -> MakerShadowLabel:
    """Produce an exact label only from complete, directional trade evidence."""

    if order.size <= 0:
        raise ValueError("counterfactual maker order size must be positive")
    if (
        not evidence.l2_complete
        or not evidence.orderfilled_complete
        or not evidence.queue_epoch_stable
    ):
        return MakerShadowLabel(
            MakerOutcome.UNKNOWN,
            EvidenceQuality.UNLABELED,
            Decimal(0),
            order.size,
            None,
            "incomplete_l2_orderfilled_or_queue_epoch_evidence",
        )
    tradable_after_queue = max(
        Decimal(0), evidence.compatible_trade_size - order.queue_ahead_estimate
    )
    confirmed_size = min(order.size, tradable_after_queue)
    if confirmed_size >= order.size:
        return MakerShadowLabel(
            MakerOutcome.FULL,
            EvidenceQuality.HIGH_CONFIDENCE,
            order.size,
            order.size,
            _observed_time(evidence),
            "compatible_orderfilled_trade_consumed_queue_and_order",
        )
    if confirmed_size > 0:
        return MakerShadowLabel(
            MakerOutcome.PARTIAL,
            EvidenceQuality.HIGH_CONFIDENCE,
            confirmed_size,
            confirmed_size,
            _observed_time(evidence),
            "compatible_orderfilled_trade_partially_consumed_queue",
        )
    if evidence.terminal_reason and not evidence.market_crossed_through_price:
        return MakerShadowLabel(
            MakerOutcome.NO_FILL,
            EvidenceQuality.OBSERVED_TAPE,
            Decimal(0),
            order.size,
            None,
            "complete_public_tape_window_ended_without_compatible_trade",
        )
    if evidence.book_level_removed_size > 0 or evidence.market_crossed_through_price:
        return MakerShadowLabel(
            MakerOutcome.UNKNOWN,
            EvidenceQuality.WEAK_INTERVAL,
            Decimal(0),
            min(order.size, evidence.book_level_removed_size),
            None,
            "book_change_or_cross_is_not_trade_evidence",
        )
    return MakerShadowLabel(
        MakerOutcome.UNKNOWN,
        EvidenceQuality.UNLABELED,
        Decimal(0),
        order.size,
        None,
        "window_has_no_terminal_or_trade_evidence",
    )


def evaluate_shadow_predictions(
    rows: Iterable[ShadowEvaluationRow],
    *,
    fill_threshold: Decimal = Decimal("0.5"),
) -> dict[str, object]:
    """Report calibration only where the label itself is defensible."""

    items = list(rows)
    strict_confirmed = [item for item in items if item.label.calibrated]
    tape_proxy = [
        item
        for item in items
        if item.label.label_class in {"STRICT_CONFIRMED_FILL", "OBSERVED_TAPE_NO_FILL"}
    ]
    brier_values: list[Decimal] = []
    proxy_positive_trials = 0
    proxy_false_positives = 0
    by_quality: dict[str, dict[str, object]] = {}
    for quality in EvidenceQuality:
        matching = [item for item in items if item.label.evidence_quality == quality]
        by_quality[quality.value] = {
            "count": len(matching),
            "outcomes": _outcome_counts(matching),
        }
    for item in tape_proxy:
        actual_fill = (
            Decimal(0) if item.label.outcome == MakerOutcome.NO_FILL else Decimal(1)
        )
        probability = _clamp_probability(item.prediction.fill_probability)
        brier_values.append((probability - actual_fill) ** 2)
        if probability >= fill_threshold:
            proxy_positive_trials += 1
            proxy_false_positives += int(actual_fill == 0)
    confidence_rows = [
        {
            "shadow_order_id": item.order.shadow_order_id,
            "trial_id": _trial_identifier(item.order),
            "event_id": item.order.event_id,
            "asset_id": item.order.asset_id,
            "side": item.order.side,
            "quote_position": item.order.quote_position,
            "horizon_seconds": str(item.order.horizon_seconds),
            "prediction_fill_probability": str(item.prediction.fill_probability),
            "model_confidence": str(item.prediction.model_confidence),
            "calibration_domain": item.prediction.calibration_domain,
            "label_class": item.label.label_class,
            "label": asdict(item.label),
        }
        for item in items
    ]
    proxy_interval = wilson_interval(proxy_false_positives, proxy_positive_trials)
    independent_rows = _terminal_trial_rows(items)
    independent_proxy = [
        item
        for item in independent_rows
        if item.label.label_class
        in {"STRICT_CONFIRMED_FILL", "OBSERVED_TAPE_NO_FILL"}
    ]
    independent_brier: list[Decimal] = []
    independent_positive_trials = 0
    independent_false_positives = 0
    for item in independent_proxy:
        actual_fill = (
            Decimal(0) if item.label.outcome == MakerOutcome.NO_FILL else Decimal(1)
        )
        probability = _clamp_probability(item.prediction.fill_probability)
        independent_brier.append((probability - actual_fill) ** 2)
        if probability >= fill_threshold:
            independent_positive_trials += 1
            independent_false_positives += int(actual_fill == 0)
    independent_interval = wilson_interval(
        independent_false_positives, independent_positive_trials
    )
    independent_outcomes = [
        Decimal(0) if item.label.outcome == MakerOutcome.NO_FILL else Decimal(1)
        for item in independent_proxy
    ]
    independent_base_rate = (
        sum(independent_outcomes, Decimal(0)) / Decimal(len(independent_outcomes))
        if independent_outcomes
        else None
    )
    independent_naive_brier = (
        sum(
            (independent_base_rate - outcome) ** 2
            for outcome in independent_outcomes
        )
        / Decimal(len(independent_outcomes))
        if independent_base_rate is not None
        else None
    )
    independent_model_brier = (
        sum(independent_brier, Decimal(0)) / Decimal(len(independent_brier))
        if independent_brier
        else None
    )
    independent_brier_skill = (
        Decimal(1) - independent_model_brier / independent_naive_brier
        if independent_model_brier is not None
        and independent_naive_brier is not None
        and independent_naive_brier > 0
        else None
    )
    return {
        "schema_version": "maker_offline_shadow_evaluation_v4",
        "status": "PASS" if strict_confirmed else "BLOCKED",
        "evidence_scope": "PUBLIC_L2_AND_ORDERFILLED_COUNTERFACTUAL",
        "live_submission_performed": False,
        "sample_count": len(items),
        "strict_confirmed_fill_count": len(strict_confirmed),
        "observed_tape_no_fill_count": sum(
            item.label.label_class == "OBSERVED_TAPE_NO_FILL" for item in items
        ),
        "ambiguous_abstain_count": sum(
            item.label.label_class == "AMBIGUOUS_ABSTAIN" for item in items
        ),
        "calibrated_count": len(strict_confirmed),
        "independent_trial_count": len(independent_rows),
        "independent_proxy_count": len(independent_proxy),
        "independent_strict_confirmed_fill_count": sum(
            item.label.label_class == "STRICT_CONFIRMED_FILL"
            for item in independent_rows
        ),
        "independent_observed_tape_no_fill_count": sum(
            item.label.label_class == "OBSERVED_TAPE_NO_FILL"
            for item in independent_rows
        ),
        "strict_confirmed_coverage_rate": _ratio(len(strict_confirmed), len(items)),
        "observed_tape_proxy_coverage_rate": _ratio(len(tape_proxy), len(items)),
        "coverage_rate": _ratio(len(strict_confirmed), len(items)),
        "unlabeled_rate": _ratio(
            sum(
                item.label.evidence_quality == EvidenceQuality.UNLABELED
                for item in items
            ),
            len(items),
        ),
        "metrics": {
            "brier_score_observed_tape_proxy": _mean(brier_values),
            "ece_observed_tape_proxy": _ece(tape_proxy),
            "false_positive_fill_proxy_upper_95": proxy_interval,
            "brier_score_independent_trial_proxy": _mean(independent_brier),
            "ece_independent_trial_proxy": _ece(independent_proxy),
            "base_fill_rate_independent_trial_proxy": (
                str(independent_base_rate)
                if independent_base_rate is not None
                else None
            ),
            "naive_base_rate_brier_independent_trial_proxy": (
                str(independent_naive_brier)
                if independent_naive_brier is not None
                else None
            ),
            "brier_skill_score_independent_trial_proxy": (
                str(independent_brier_skill)
                if independent_brier_skill is not None
                else None
            ),
            "reliability_bins_independent_trial_proxy": (
                _probability_reliability(independent_proxy)
            ),
            "false_positive_fill_independent_trial_upper_95": (
                independent_interval
            ),
            "true_false_positive_rate": None,
            "true_false_positive_rate_status": (
                "NOT_IDENTIFIABLE_WITH_PUBLIC_L2_WITHOUT_AUTHENTICATED_ORDER_OUTCOMES"
            ),
            "fill_threshold": str(fill_threshold),
        },
        "horizon_survival": _horizon_survival(items),
        "by_evidence_quality": by_quality,
        "rows": confidence_rows,
    }


def read_shadow_rows(path: Path) -> list[ShadowEvaluationRow]:
    """Read JSON/JSONL evidence exported by a replay; no DB or live API calls."""

    raw = path.read_text(encoding="utf-8")
    payload: Any = (
        json.loads(raw)
        if path.suffix == ".json"
        else [json.loads(line) for line in raw.splitlines() if line.strip()]
    )
    records = payload.get("rows", []) if isinstance(payload, dict) else payload
    return [_row_from_mapping(item) for item in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = evaluate_shadow_predictions(read_shadow_rows(args.evidence))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if report["status"] == "PASS" else 2


def _row_from_mapping(raw: Mapping[str, Any]) -> ShadowEvaluationRow:
    order_raw = raw["order"]
    prediction_raw = raw["prediction"]
    evidence_raw = raw["evidence"]
    order = CounterfactualMakerOrder(
        shadow_order_id=str(order_raw["shadow_order_id"]),
        event_id=str(order_raw["event_id"]),
        asset_id=str(order_raw["asset_id"]),
        side=str(order_raw["side"]),
        price=Decimal(str(order_raw["price"])),
        size=Decimal(str(order_raw["size"])),
        queue_ahead_estimate=Decimal(str(order_raw.get("queue_ahead_estimate") or 0)),
        horizon_seconds=Decimal(str(order_raw.get("horizon_seconds") or 0)),
        quote_position=str(order_raw.get("quote_position") or "AT_BEST"),
        trial_id=str(order_raw.get("trial_id") or ""),
    )
    prediction = MakerFillPrediction(
        **{
            key: _prediction_value(key, value)
            for key, value in prediction_raw.items()
            if key in MakerFillPrediction.__dataclass_fields__
        }
    )
    evidence = MakerEvidenceWindow(
        compatible_trade_size=Decimal(
            str(evidence_raw.get("compatible_trade_size") or 0)
        ),
        incompatible_trade_size=Decimal(
            str(evidence_raw.get("incompatible_trade_size") or 0)
        ),
        book_level_removed_size=Decimal(
            str(evidence_raw.get("book_level_removed_size") or 0)
        ),
        book_level_replenished_size=Decimal(
            str(evidence_raw.get("book_level_replenished_size") or 0)
        ),
        market_crossed_through_price=bool(
            evidence_raw.get("market_crossed_through_price")
        ),
        terminal_reason=(
            str(evidence_raw["terminal_reason"])
            if evidence_raw.get("terminal_reason")
            else None
        ),
        l2_complete=bool(evidence_raw.get("l2_complete", True)),
        orderfilled_complete=bool(evidence_raw.get("orderfilled_complete", True)),
        queue_epoch_stable=bool(evidence_raw.get("queue_epoch_stable", True)),
        observed_seconds=Decimal(str(evidence_raw.get("observed_seconds") or 0)),
    )
    return ShadowEvaluationRow(order, prediction, label_counterfactual(order, evidence))


def _prediction_value(name: str, value: Any) -> Any:
    if name.endswith("seconds") and value is None:
        return None
    if name in {"domain_status", "calibration_domain"}:
        return str(value)
    return Decimal(str(value))


def _observed_time(evidence: MakerEvidenceWindow) -> Decimal | None:
    return evidence.observed_seconds if evidence.observed_seconds > 0 else None


def _trial_identifier(order: CounterfactualMakerOrder) -> str:
    if order.trial_id:
        return order.trial_id
    base_order_id = re.sub(r"-\d+s$", "", order.shadow_order_id)
    return "|".join(
        (
            order.event_id,
            order.asset_id,
            order.side,
            order.quote_position,
            base_order_id,
        )
    )


def _terminal_trial_rows(
    items: Iterable[ShadowEvaluationRow],
) -> list[ShadowEvaluationRow]:
    selected: dict[str, ShadowEvaluationRow] = {}
    for item in items:
        key = _trial_identifier(item.order)
        existing = selected.get(key)
        if existing is None or item.order.horizon_seconds >= existing.order.horizon_seconds:
            selected[key] = item
    return [selected[key] for key in sorted(selected)]


def _horizon_survival(items: Iterable[ShadowEvaluationRow]) -> dict[str, object]:
    rows = list(items)
    by_horizon: dict[Decimal, list[ShadowEvaluationRow]] = {}
    by_trial: dict[str, list[ShadowEvaluationRow]] = {}
    for item in rows:
        by_horizon.setdefault(item.order.horizon_seconds, []).append(item)
        by_trial.setdefault(_trial_identifier(item.order), []).append(item)

    horizon_rows: list[dict[str, object]] = []
    for horizon, matching in sorted(by_horizon.items()):
        proxy = [
            item
            for item in matching
            if item.label.label_class
            in {"STRICT_CONFIRMED_FILL", "OBSERVED_TAPE_NO_FILL"}
        ]
        predicted = [
            _clamp_probability(item.prediction.fill_probability) for item in proxy
        ]
        actual = [
            Decimal(0) if item.label.outcome == MakerOutcome.NO_FILL else Decimal(1)
            for item in proxy
        ]
        brier = [
            (probability - outcome) ** 2
            for probability, outcome in zip(predicted, actual, strict=True)
        ]
        observed_fill_rate = (
            sum(actual, Decimal(0)) / Decimal(len(actual)) if actual else None
        )
        horizon_rows.append(
            {
                "horizon_seconds": str(horizon),
                "at_risk_count": len(matching),
                "proxy_label_count": len(proxy),
                "censored_count": len(matching) - len(proxy),
                "strict_fill_count": sum(value == 1 for value in actual),
                "observed_tape_no_fill_count": sum(value == 0 for value in actual),
                "mean_predicted_fill_probability": _mean(predicted),
                "observed_cumulative_fill_rate": (
                    str(observed_fill_rate)
                    if observed_fill_rate is not None
                    else None
                ),
                "observed_survival_rate": (
                    str(Decimal(1) - observed_fill_rate)
                    if observed_fill_rate is not None
                    else None
                ),
                "brier_score_proxy": _mean(brier),
            }
        )

    monotonic_violations: list[dict[str, str]] = []
    for trial_id, matching in sorted(by_trial.items()):
        ordered = sorted(matching, key=lambda item: item.order.horizon_seconds)
        previous: ShadowEvaluationRow | None = None
        for item in ordered:
            if (
                previous is not None
                and item.order.horizon_seconds > previous.order.horizon_seconds
                and item.prediction.fill_probability
                < previous.prediction.fill_probability
            ):
                monotonic_violations.append(
                    {
                        "trial_id": trial_id,
                        "previous_horizon_seconds": str(
                            previous.order.horizon_seconds
                        ),
                        "previous_probability": str(
                            previous.prediction.fill_probability
                        ),
                        "horizon_seconds": str(item.order.horizon_seconds),
                        "probability": str(item.prediction.fill_probability),
                    }
                )
            previous = item
    return {
        "schema_version": "maker_horizon_survival_v1",
        "trial_count": len(by_trial),
        "horizons": horizon_rows,
        "monotonic_probability_check": {
            "status": "PASS" if not monotonic_violations else "BLOCKED",
            "violation_count": len(monotonic_violations),
            "violations": monotonic_violations,
        },
        "censoring_rule": (
            "incomplete_l2_orderfilled_or_queue_epoch_rows_are_excluded_from_"
            "proxy_outcomes"
        ),
    }


def _clamp_probability(value: Decimal) -> Decimal:
    return min(Decimal(1), max(Decimal(0), value))


def _outcome_counts(items: Iterable[ShadowEvaluationRow]) -> dict[str, int]:
    return {
        outcome.value: sum(item.label.outcome == outcome for item in items)
        for outcome in MakerOutcome
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: Iterable[Decimal]) -> str | None:
    items = list(values)
    return str(sum(items, Decimal(0)) / len(items)) if items else None


def _ece(rows: Iterable[ShadowEvaluationRow], *, bins: int = 10) -> str | None:
    items = list(rows)
    if not items:
        return None
    total = Decimal(len(items))
    error = Decimal(0)
    for index in range(max(1, int(bins))):
        lower = Decimal(index) / Decimal(bins)
        upper = Decimal(index + 1) / Decimal(bins)
        bucket = [
            item
            for item in items
            if lower <= _clamp_probability(item.prediction.fill_probability)
            and (
                _clamp_probability(item.prediction.fill_probability) < upper
                or (index == bins - 1 and item.prediction.fill_probability == 1)
            )
        ]
        if not bucket:
            continue
        probabilities = [
            _clamp_probability(item.prediction.fill_probability) for item in bucket
        ]
        outcomes = [
            Decimal(0) if item.label.outcome == MakerOutcome.NO_FILL else Decimal(1)
            for item in bucket
        ]
        error += (
            Decimal(len(bucket))
            / total
            * abs(
                sum(probabilities, Decimal(0)) / Decimal(len(bucket))
                - sum(outcomes, Decimal(0)) / Decimal(len(bucket))
            )
        )
    return str(error)


def _probability_reliability(
    rows: Iterable[ShadowEvaluationRow],
    *,
    bins: int = 10,
) -> list[dict[str, object]]:
    items = list(rows)
    output: list[dict[str, object]] = []
    for index in range(max(1, int(bins))):
        lower = Decimal(index) / Decimal(bins)
        upper = Decimal(index + 1) / Decimal(bins)
        bucket = [
            item
            for item in items
            if lower <= _clamp_probability(item.prediction.fill_probability)
            and (
                _clamp_probability(item.prediction.fill_probability) < upper
                or (index == bins - 1 and item.prediction.fill_probability == 1)
            )
        ]
        if not bucket:
            continue
        probabilities = [
            _clamp_probability(item.prediction.fill_probability) for item in bucket
        ]
        outcomes = [
            Decimal(0) if item.label.outcome == MakerOutcome.NO_FILL else Decimal(1)
            for item in bucket
        ]
        fill_count = sum(outcome == 1 for outcome in outcomes)
        output.append(
            {
                "lower_inclusive": str(lower),
                "upper_exclusive": str(upper),
                "sample_count": len(bucket),
                "mean_predicted_probability": _mean(probabilities),
                "observed_fill_rate": _mean(outcomes),
                "observed_fill_count": fill_count,
                "observed_fill_wilson_95": wilson_interval(
                    fill_count, len(bucket)
                ),
            }
        )
    return output
