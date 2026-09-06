from quant.maker.evaluate_holdout import evaluate


def _config(minimum: int = 1):
    return {
        "model_version": "maker-test",
        "holdout_gates": {
            "minimum_samples": minimum,
            "independent_events": 1,
            "utc_days": 1,
            "max_ece": 0.2,
            "max_false_positive_fill_upper_95": 1.0,
            "require_brier_better_than_naive": True,
        },
    }


def test_perfect_maker_holdout_passes_small_test_gate() -> None:
    rows = [
        {
            "event_id": "event-1",
            "utc_day": "2026-07-28",
            "artifact_complete": True,
            "p_no_fill": 0,
            "p_partial": 0,
            "p_full": 1,
            "actual_outcome": "FULL",
            "expected_filled_size": 1,
            "actual_filled_size": 1,
        }
    ]
    report = evaluate(rows, config=_config())
    assert report["status"] == "PASS"
    assert report["metrics"]["brier_score"] == 0
    assert report["live_submission_performed"] is False


def test_empty_maker_holdout_is_blocked() -> None:
    report = evaluate([], config=_config(minimum=60))
    assert report["status"] == "BLOCKED"
    assert report["promotion_allowed"] is False


def test_maker_promotion_requires_partial_and_full_timing_evidence() -> None:
    config = _config(minimum=2)
    config["holdout_gates"].update(
        {
            "minimum_partial_outcomes": 1,
            "minimum_full_outcomes": 1,
            "minimum_fill_size_observations": 2,
            "minimum_first_fill_time_observations": 2,
            "minimum_full_fill_time_observations": 1,
            "max_fill_size_relative_mae": 0.1,
            "max_time_to_first_fill_relative_mae": 0.1,
            "max_time_to_full_fill_relative_mae": 0.1,
        }
    )
    rows = [
        {
            "event_id": "event-partial",
            "utc_day": "2026-08-07",
            "artifact_complete": True,
            "p_no_fill": 0,
            "p_partial": 1,
            "p_full": 0,
            "actual_outcome": "PARTIAL",
            "order_size": 4,
            "expected_filled_size": 2,
            "actual_filled_size": 2,
            "expected_time_to_first_fill_seconds": 5,
            "actual_time_to_first_fill_seconds": 5,
        },
        {
            "event_id": "event-full",
            "utc_day": "2026-08-08",
            "artifact_complete": True,
            "p_no_fill": 0,
            "p_partial": 0,
            "p_full": 1,
            "actual_outcome": "FULL",
            "order_size": 4,
            "expected_filled_size": 4,
            "actual_filled_size": 4,
            "expected_time_to_first_fill_seconds": 3,
            "actual_time_to_first_fill_seconds": 3,
            "expected_time_to_full_fill_seconds": 8,
            "actual_time_to_full_fill_seconds": 8,
        },
    ]

    report = evaluate(rows, config=config)

    assert report["status"] == "PASS"
    assert report["checks"]["minimum_partial_outcomes"] is True
    assert report["checks"]["minimum_full_outcomes"] is True
    assert report["metrics"]["fill_size_observation_count"] == 2
    assert report["metrics"]["time_to_first_fill_observation_count"] == 2
    assert report["metrics"]["time_to_full_fill_observation_count"] == 1


def test_nofill_only_evidence_cannot_promote_maker_model() -> None:
    config = _config(minimum=1)
    config["holdout_gates"].update(
        {
            "minimum_partial_outcomes": 1,
            "minimum_full_outcomes": 1,
        }
    )
    rows = [
        {
            "event_id": "event-1",
            "utc_day": "2026-08-07",
            "artifact_complete": True,
            "p_no_fill": 1,
            "p_partial": 0,
            "p_full": 0,
            "actual_outcome": "NO_FILL",
        }
    ]

    report = evaluate(rows, config=config)

    assert report["status"] == "BLOCKED"
    assert report["checks"]["minimum_partial_outcomes"] is False
    assert report["checks"]["minimum_full_outcomes"] is False


def test_right_censored_observations_are_not_timing_errors() -> None:
    config = _config(minimum=2)
    config["holdout_gates"]["require_censoring_metadata"] = True
    rows = [
        {
            "event_id": "event-no-fill",
            "utc_day": "2026-08-27",
            "artifact_complete": True,
            "p_no_fill": 1,
            "actual_outcome": "NO_FILL",
            "resting_seconds": 120,
            "expected_time_to_first_fill_seconds": 20,
            "actual_time_to_first_fill_seconds": 999,
            "outcome_observation": {
                "label": "CENSORED_AT_120S",
                "first_fill_right_censored": True,
                "full_fill_right_censored": True,
            },
        },
        {
            "event_id": "event-partial",
            "utc_day": "2026-08-28",
            "artifact_complete": True,
            "p_partial": 1,
            "actual_outcome": "PARTIAL",
            "order_size": 5,
            "expected_filled_size": 2,
            "actual_filled_size": 2,
            "expected_time_to_first_fill_seconds": 5,
            "actual_time_to_first_fill_seconds": 5,
            "expected_time_to_full_fill_seconds": 10,
            "actual_time_to_full_fill_seconds": 999,
            "outcome_observation": {
                "label": "PARTIAL_CANCEL_ON_FIRST_FILL",
                "first_fill_right_censored": False,
                "full_fill_right_censored": True,
            },
        },
    ]

    report = evaluate(rows, config=config)

    assert report["checks"]["censoring_metadata_100pct"] is True
    assert report["metrics"]["first_fill_right_censored_count"] == 1
    assert report["metrics"]["full_fill_right_censored_count"] == 2
    assert report["metrics"]["time_to_first_fill_observation_count"] == 1
    assert report["metrics"]["time_to_full_fill_observation_count"] == 0
    assert report["evidence"]["observation_label_counts"] == {
        "CENSORED_AT_120S": 1,
        "PARTIAL_CANCEL_ON_FIRST_FILL": 1,
    }
