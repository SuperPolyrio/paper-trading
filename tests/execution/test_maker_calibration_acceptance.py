from quant.maker.calibration_acceptance import evaluate_calibration_acquisition


def _authenticated(*, partial: int = 0, full: int = 0, status: str = "BLOCKED"):
    return {
        "status": status,
        "promotion_allowed": status == "PASS",
        "evaluated_count": 4 + partial + full,
        "outcome_counts": {"NO_FILL": 4, "PARTIAL": partial, "FULL": full},
        "evidence": {
            "authenticated_own_order_count": 4 + partial + full,
            "prediction_before_submission_count": 4 + partial + full,
            "censoring_metadata_count": 4 + partial + full,
            "observation_label_counts": {"CENSORED_AT_60S": 4},
        },
    }


def _historical():
    return {
        "status": "PASS_READ_ONLY",
        "exchange_submit_called": False,
        "exchange_cancel_called": False,
    }


def _collector():
    return {
        "status": "PASS",
        "exchange_submit_called": False,
        "exact_cancel_called": False,
        "resubmit_forbidden": True,
    }


def test_implementation_passes_without_faking_live_positive_evidence() -> None:
    report = evaluate_calibration_acquisition(
        authenticated_evaluation=_authenticated(),
        historical_recovery=_historical(),
        collector_status=_collector(),
        live_preflight={"status": "BLOCKED", "exchange_submit_called": False},
    )

    assert report["implementation_status"] == "PASS"
    assert report["live_evidence_status"] == "BLOCKED"
    assert report["status"] == "PASS_IMPLEMENTATION_LIVE_EVIDENCE_PENDING"
    assert report["claims"]["live_maker_calibrated"] is False


def test_live_promotion_requires_authenticated_partial_and_full() -> None:
    report = evaluate_calibration_acquisition(
        authenticated_evaluation=_authenticated(partial=3, full=3, status="PASS"),
        historical_recovery=_historical(),
        collector_status=_collector(),
    )

    assert report["implementation_status"] == "PASS"
    assert report["live_evidence_status"] == "PASS"
    assert report["status"] == "PASS_LIVE_MAKER_CALIBRATED"
