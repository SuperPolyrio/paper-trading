from dataclasses import replace
from decimal import Decimal

from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
)
from quant.maker.offline_shadow import (
    CounterfactualMakerOrder,
    EvidenceQuality,
    MakerEvidenceWindow,
    MakerOutcome,
    ShadowEvaluationRow,
    evaluate_shadow_predictions,
    label_counterfactual,
)


def _order() -> CounterfactualMakerOrder:
    return CounterfactualMakerOrder(
        shadow_order_id="shadow-1",
        event_id="event-1",
        asset_id="asset-1",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal(2),
        queue_ahead_estimate=Decimal(3),
        horizon_seconds=Decimal(60),
    )


def _prediction():
    state = MakerQueueState(
        paper_order_id="p1",
        asset_id="asset-1",
        side="BUY",
        price_tick=Decimal("0.40"),
        queue_model_version="test",
        displayed_size_at_accept=Decimal(5),
        own_orders_ahead=Decimal(0),
        estimated_external_queue_ahead=Decimal(3),
        order_size=Decimal(2),
    )
    return MakerQueueEngine(QueueModel.STRICT_TRADE_EVIDENCE).predict(
        state, forecast_trade_volume=Decimal(5), horizon_seconds=Decimal(60)
    )


def test_book_decrease_is_interval_evidence_not_a_maker_fill() -> None:
    label = label_counterfactual(
        _order(),
        MakerEvidenceWindow(book_level_removed_size=Decimal(5)),
    )
    assert label.outcome == MakerOutcome.UNKNOWN
    assert label.evidence_quality == EvidenceQuality.WEAK_INTERVAL
    assert label.filled_size_lower == 0
    assert label.filled_size_upper == 2


def test_compatible_orderfilled_trade_must_first_consume_queue() -> None:
    partial = label_counterfactual(
        _order(),
        MakerEvidenceWindow(
            compatible_trade_size=Decimal(4), observed_seconds=Decimal(8)
        ),
    )
    assert partial.outcome == MakerOutcome.PARTIAL
    assert partial.filled_size_lower == 1
    full = label_counterfactual(
        _order(),
        MakerEvidenceWindow(
            compatible_trade_size=Decimal(5), observed_seconds=Decimal(12)
        ),
    )
    assert full.outcome == MakerOutcome.FULL
    assert full.filled_size_lower == 2


def test_incomplete_evidence_is_excluded_from_calibration_metrics() -> None:
    prediction = _prediction()
    high = ShadowEvaluationRow(
        _order(),
        prediction,
        label_counterfactual(
            _order(),
            MakerEvidenceWindow(compatible_trade_size=Decimal(5)),
        ),
    )
    unlabeled = ShadowEvaluationRow(
        _order(),
        prediction,
        label_counterfactual(_order(), MakerEvidenceWindow(l2_complete=False)),
    )
    report = evaluate_shadow_predictions([high, unlabeled])
    assert report["status"] == "PASS"
    assert report["calibrated_count"] == 1
    assert report["coverage_rate"] == 0.5
    assert report["unlabeled_rate"] == 0.5
    assert report["live_submission_performed"] is False


def test_new_book_generation_forces_abstention() -> None:
    label = label_counterfactual(
        _order(),
        MakerEvidenceWindow(
            compatible_trade_size=Decimal(5),
            queue_epoch_stable=False,
        ),
    )
    assert label.outcome == MakerOutcome.UNKNOWN
    assert label.label_class == "AMBIGUOUS_ABSTAIN"


def test_no_trade_public_window_is_proxy_no_fill_not_calibrated_truth() -> None:
    label = label_counterfactual(
        _order(),
        MakerEvidenceWindow(
            terminal_reason="horizon_elapsed",
            observed_seconds=Decimal(60),
        ),
    )
    assert label.outcome == MakerOutcome.NO_FILL
    assert label.evidence_quality == EvidenceQuality.OBSERVED_TAPE
    assert label.label_class == "OBSERVED_TAPE_NO_FILL"
    assert label.calibrated is False
    assert label.filled_size_upper == _order().size


def test_proxy_metrics_do_not_claim_true_false_positive_rate() -> None:
    prediction = _prediction()
    positive = ShadowEvaluationRow(
        _order(),
        prediction,
        label_counterfactual(
            _order(), MakerEvidenceWindow(compatible_trade_size=Decimal(5))
        ),
    )
    proxy_no_fill = ShadowEvaluationRow(
        _order(),
        prediction,
        label_counterfactual(
            _order(), MakerEvidenceWindow(terminal_reason="horizon_elapsed")
        ),
    )
    report = evaluate_shadow_predictions([positive, proxy_no_fill])
    assert report["strict_confirmed_fill_count"] == 1
    assert report["observed_tape_no_fill_count"] == 1
    assert report["metrics"]["brier_score_observed_tape_proxy"] is not None
    assert report["metrics"]["true_false_positive_rate"] is None


def test_independent_reliability_reports_skill_against_base_rate() -> None:
    rows = []
    for trial_id, probability, evidence in (
        (
            "fill",
            Decimal("0.8"),
            MakerEvidenceWindow(compatible_trade_size=Decimal(5)),
        ),
        (
            "no-fill",
            Decimal("0.2"),
            MakerEvidenceWindow(terminal_reason="horizon_elapsed"),
        ),
    ):
        order = replace(_order(), trial_id=trial_id)
        prediction = replace(
            _prediction(),
            p_no_fill=Decimal(1) - probability,
            p_partial=probability,
            p_full=Decimal(0),
            fill_probability=probability,
        )
        rows.append(
            ShadowEvaluationRow(
                order,
                prediction,
                label_counterfactual(order, evidence),
            )
        )

    metrics = evaluate_shadow_predictions(rows)["metrics"]

    assert Decimal(metrics["brier_skill_score_independent_trial_proxy"]) > 0
    assert metrics["base_fill_rate_independent_trial_proxy"] == "0.5"
    assert sum(
        row["sample_count"]
        for row in metrics["reliability_bins_independent_trial_proxy"]
    ) == 2


def test_prediction_exposes_full_distribution_and_domain_fields() -> None:
    prediction = _prediction()
    assert prediction.fill_probability == prediction.p_partial + prediction.p_full
    assert prediction.queue_ahead_estimate == 3
    assert prediction.calibration_domain == "OFFLINE_L2_TRADE_EVIDENCE_REQUIRED"


def test_repeated_horizons_count_as_one_independent_trial() -> None:
    rows = []
    for horizon, probability in ((30, "0.2"), (120, "0.4"), (300, "0.8")):
        order = replace(
            _order(),
            shadow_order_id=f"shadow-trial-{horizon}s",
            trial_id="trial-1",
            horizon_seconds=Decimal(horizon),
        )
        prediction = replace(
            _prediction(),
            p_no_fill=Decimal(1) - Decimal(probability),
            p_partial=Decimal(probability),
            p_full=Decimal(0),
            fill_probability=Decimal(probability),
        )
        rows.append(
            ShadowEvaluationRow(
                order,
                prediction,
                label_counterfactual(
                    order,
                    MakerEvidenceWindow(
                        terminal_reason="horizon_elapsed",
                        observed_seconds=Decimal(horizon),
                    ),
                ),
            )
        )

    report = evaluate_shadow_predictions(rows)

    assert report["metrics"]["false_positive_fill_proxy_upper_95"]["total"] == 1
    assert (
        report["metrics"]["false_positive_fill_independent_trial_upper_95"][
            "total"
        ]
        == 1
    )
    assert report["independent_trial_count"] == 1
    assert report["horizon_survival"]["trial_count"] == 1
    assert (
        report["horizon_survival"]["monotonic_probability_check"]["status"]
        == "PASS"
    )
    assert len(report["horizon_survival"]["horizons"]) == 3


def test_horizon_survival_reports_probability_reversal() -> None:
    rows = []
    for horizon, probability in ((30, "0.8"), (120, "0.4")):
        order = replace(
            _order(),
            shadow_order_id=f"shadow-reversal-{horizon}s",
            trial_id="trial-reversal",
            horizon_seconds=Decimal(horizon),
        )
        prediction = replace(
            _prediction(),
            p_no_fill=Decimal(1) - Decimal(probability),
            p_partial=Decimal(probability),
            p_full=Decimal(0),
            fill_probability=Decimal(probability),
        )
        rows.append(
            ShadowEvaluationRow(
                order,
                prediction,
                label_counterfactual(
                    order,
                    MakerEvidenceWindow(terminal_reason="horizon_elapsed"),
                ),
            )
        )

    monotonicity = evaluate_shadow_predictions(rows)["horizon_survival"][
        "monotonic_probability_check"
    ]

    assert monotonicity["status"] == "BLOCKED"
    assert monotonicity["violation_count"] == 1
