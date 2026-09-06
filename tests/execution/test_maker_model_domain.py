from quant.execution.models.maker_model_domain import MakerModelDomainResolver
from quant.execution.models.maker_queue import QueueModel


def test_missing_holdout_fails_closed_to_strict_model() -> None:
    decision = MakerModelDomainResolver().resolve()

    assert decision.authoritative_fill_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.research_forecast_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.domain_status == "STRICT_UNCALIBRATED"
    assert decision.promotion_allowed is False
    assert "holdout_evaluation_missing" in decision.reason_codes
    assert decision.hash_is_valid


def test_no_fill_only_holdout_cannot_promote_probabilistic_model() -> None:
    decision = MakerModelDomainResolver(
        {
            "status": "PASS",
            "promotion_allowed": True,
            "evaluated_count": 60,
            "artifact_complete_count": 60,
            "outcome_counts": {"NO_FILL": 60, "PARTIAL": 0, "FULL": 0},
        },
        evidence_sha256="evidence",
    ).resolve(requested_forecast_model=QueueModel.PROBABILISTIC_QUEUE)

    assert decision.authoritative_fill_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.research_forecast_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.promotion_allowed is False
    assert set(decision.reason_codes) == {
        "real_partial_outcome_missing",
        "real_full_outcome_missing",
    }


def test_complete_holdout_promotes_forecast_but_never_economic_fill_model() -> None:
    decision = MakerModelDomainResolver(
        {
            "status": "PASS",
            "promotion_allowed": True,
            "model_version": "maker-holdout-v2",
            "evaluated_count": 60,
            "artifact_complete_count": 60,
            "outcome_counts": {"NO_FILL": 40, "PARTIAL": 10, "FULL": 10},
        },
        evidence_sha256="evidence",
    ).resolve(requested_forecast_model=QueueModel.PROBABILISTIC_QUEUE)

    assert decision.authoritative_fill_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.research_forecast_model == QueueModel.PROBABILISTIC_QUEUE
    assert decision.domain_status == "PROMOTED"
    assert decision.calibrated_in_domain is True
    assert decision.hash_is_valid


def test_offline_l2_orderfilled_only_promotes_research_forecast() -> None:
    decision = MakerModelDomainResolver(
        offline_evaluation={
            "status": "PASS_OFFLINE",
            "evidence_scope": "OFFLINE_COUNTERFACTUAL_NO_LIVE_SUBMISSION",
            "live_submission_performed": False,
            "classifications": {"maker_probability": "RESEARCH_CALIBRATED"},
            "split_manifest": {"status": "PASS", "event_leakage_count": 0},
            "maker": {
                "holdout": {
                    "strict_confirmed_fill_count": 1,
                    "observed_tape_no_fill_count": 6,
                }
            },
        },
        research_evidence_sha256="offline-evidence",
    ).resolve(requested_forecast_model=QueueModel.PROBABILISTIC_QUEUE)

    assert decision.authoritative_fill_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.research_forecast_model == QueueModel.PROBABILISTIC_QUEUE
    assert decision.domain_status == "RESEARCH_ONLY"
    assert decision.calibration_domain == (
        "OFFLINE_PUBLIC_L2_ORDERFILLED_COUNTERFACTUAL"
    )
    assert decision.promotion_allowed is False
    assert decision.calibrated_in_domain is False
    assert decision.research_evidence_sha256 == "offline-evidence"
    assert "offline_research_calibration_only" in decision.reason_codes
    assert decision.hash_is_valid


def test_offline_evidence_with_event_leakage_cannot_select_probability() -> None:
    decision = MakerModelDomainResolver(
        offline_evaluation={
            "status": "PASS_OFFLINE",
            "evidence_scope": "OFFLINE_COUNTERFACTUAL_NO_LIVE_SUBMISSION",
            "live_submission_performed": False,
            "classifications": {"maker_probability": "RESEARCH_CALIBRATED"},
            "split_manifest": {"status": "PASS", "event_leakage_count": 1},
            "maker": {
                "holdout": {
                    "strict_confirmed_fill_count": 1,
                    "observed_tape_no_fill_count": 6,
                }
            },
        }
    ).resolve(requested_forecast_model=QueueModel.PROBABILISTIC_QUEUE)

    assert decision.research_forecast_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.domain_status == "STRICT_UNCALIBRATED"
