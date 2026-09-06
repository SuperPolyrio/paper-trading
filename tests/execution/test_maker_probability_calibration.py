from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from quant.execution.models.maker_model_domain import MakerModelDomainResolver
from quant.execution.models.maker_probability_calibration import (
    LOW_PROBABILITY_CLASSIFICATION,
    MakerProbabilityCalibrationArtifact,
)
from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
)
from quant.maker.live_probe_runner import maker_predictions


def _hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _report() -> dict:
    prior = {
        "schema_version": "maker_empirical_bayes_prior_v1",
        "source_split": "train",
        "source_trial_count": 80,
        "source_event_count": 20,
        "global_arrival_rate_per_second": "0.01",
        "global_mean_trade_size": "5",
        "activity_buckets": {
            "GLOBAL": {
                "trial_count": 80,
                "event_count": 20,
                "exposure_seconds": "24000",
                "trade_count": "240",
                "trade_volume": "1200",
                "posterior_arrival_rate_per_second": "0.01",
                "posterior_mean_trade_size": "5",
            }
        },
        "horizon_fill_probabilities": {"300": "0.1"},
        "horizon_fill_evidence": {
            "300": {
                "fill_count": 10,
                "no_fill_count": 90,
                "label_count": 100,
                "event_count": 20,
                "posterior_fill_probability": "0.1",
            }
        },
    }
    prior["artifact_hash"] = _hash(prior)
    config_payload = {
        "schema_version": "maker_empirical_bayes_config_v1",
        "activity_multiplier": "0.1",
        "prior_exposure_seconds": "600",
        "raw_probability_weight": "1",
        "probability_odds_multiplier": "0.75",
        "prior_artifact_hash": prior["artifact_hash"],
        "fit_split": "train",
        "selection_split": "calibration",
    }
    config = {
        key: value
        for key, value in config_payload.items()
        if key not in {"fit_split", "selection_split"}
    }
    config["decision_hash"] = _hash(config_payload)
    gate = {
        "status": "PASS",
        "classification": LOW_PROBABILITY_CLASSIFICATION,
        "calibrated_probability_domain": "[0,0.5)",
        "strict_confirmed_fill_count": 23,
        "observed_tape_no_fill_count": 308,
        "proxy_count": 331,
        "proxy_brier": "0.05",
        "proxy_ece": "0.03",
        "high_probability_domain": {
            "status": "DISABLED_INSUFFICIENT_EVIDENCE",
            "minimum_probability": "0.5",
        },
    }
    return {
        "status": "PASS_OFFLINE",
        "evidence_scope": "OFFLINE_COUNTERFACTUAL_NO_LIVE_SUBMISSION",
        "live_submission_performed": False,
        "classifications": {
            "maker_probability_walk_forward": LOW_PROBABILITY_CLASSIFICATION,
        },
        "split_manifest": {"status": "PASS", "event_leakage_count": 0},
        "activity_calibration": {
            "selected_config": config,
            "train_prior": prior,
            "fit_splits": ["train"],
            "selection_splits": ["calibration"],
            "holdout_rows_used_for_fit": 0,
        },
        "maker": {
            "walk_forward": {"probability_research_gate": gate},
        },
    }


def _state() -> MakerQueueState:
    return MakerQueueState(
        paper_order_id="paper-1",
        asset_id="asset-1",
        side="BUY",
        price_tick=Decimal("0.4"),
        queue_model_version="test",
        displayed_size_at_accept=Decimal("10"),
        own_orders_ahead=Decimal(0),
        estimated_external_queue_ahead=Decimal("10"),
        order_size=Decimal("5"),
    )


def test_artifact_round_trip_and_low_probability_prediction(tmp_path) -> None:
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(_report())
    path = artifact.write(tmp_path / "artifact.json")
    loaded = MakerProbabilityCalibrationArtifact.load(path)
    forecast, arrival, context = loaded.activity_forecast(
        category="politics",
        side="BUY",
        quote_position="AT_BEST",
        horizon_seconds=Decimal(300),
        observed_trade_count=0,
        observed_trade_volume=Decimal(0),
        lookback_seconds=Decimal(300),
    )
    raw = MakerQueueEngine(QueueModel.PROBABILISTIC_QUEUE).predict(
        _state(),
        forecast_trade_volume=forecast,
        horizon_seconds=Decimal(300),
        aggressor_arrival_probability=arrival,
    )
    calibrated = loaded.calibrate_prediction(
        raw,
        order_size=Decimal(5),
        horizon_seconds=Decimal(300),
    )

    assert loaded.artifact_hash == artifact.artifact_hash
    assert context["activity_bucket"] == "GLOBAL"
    assert Decimal(0) <= calibrated.fill_probability < Decimal("0.5")
    assert calibrated.domain_status == LOW_PROBABILITY_CLASSIFICATION


def test_high_probability_prediction_abstains() -> None:
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(_report())
    raw = MakerQueueEngine(QueueModel.PROBABILISTIC_QUEUE).predict(
        _state(),
        forecast_trade_volume=Decimal(100),
        horizon_seconds=Decimal(300),
        aggressor_arrival_probability=Decimal(1),
    )
    calibrated = artifact.calibrate_prediction(
        raw,
        order_size=Decimal(5),
        horizon_seconds=Decimal(300),
    )

    assert calibrated.domain_status == "RESEARCH_ABSTAIN_OUT_OF_CALIBRATION_DOMAIN"
    assert calibrated.fill_probability == 0
    assert calibrated.expected_filled_size == 0


def test_model_domain_selects_artifact_for_research_only() -> None:
    report = _report()
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(report)
    decision = MakerModelDomainResolver(
        offline_evaluation=report,
        research_evidence_sha256="evidence",
        research_calibration_artifact=artifact,
    ).resolve()

    assert decision.authoritative_fill_model == QueueModel.STRICT_TRADE_EVIDENCE
    assert decision.research_forecast_model == QueueModel.PROBABILISTIC_QUEUE
    assert decision.domain_status == "RESEARCH_ONLY_CALIBRATED_LOW_PROBABILITY"
    assert decision.promotion_allowed is False
    assert decision.research_calibrated_in_domain is True
    assert decision.research_calibration_artifact["artifact_hash"] == (
        artifact.artifact_hash
    )


def test_online_probe_prediction_uses_frozen_artifact() -> None:
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(_report())
    predictions = maker_predictions(
        {
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        },
        asset_id="asset-1",
        side="BUY",
        price=Decimal("0.4"),
        size=Decimal(5),
        horizon_seconds=Decimal(300),
        run_id="run-1",
        category="politics",
        quote_position="AT_BEST",
        observed_trade_count=0,
        observed_trade_volume=Decimal(0),
        lookback_seconds=Decimal(300),
        probability_calibration=artifact,
    )

    assert predictions["STRICT_TRADE_EVIDENCE"]["domain_status"] == (
        "RESEARCH_UNCALIBRATED"
    )
    assert predictions["PROBABILISTIC_QUEUE"]["domain_status"] == (
        LOW_PROBABILITY_CLASSIFICATION
    )
    assert predictions["PROBABILISTIC_QUEUE"]["calibration_domain"] == (
        artifact.calibration_domain
    )


def test_online_probability_abstains_when_trade_evidence_is_not_ready() -> None:
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(_report())

    predictions = maker_predictions(
        {
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        },
        asset_id="asset-1",
        side="BUY",
        price=Decimal("0.4"),
        size=Decimal(5),
        horizon_seconds=Decimal(300),
        run_id="run-no-evidence",
        probability_calibration=artifact,
        trade_evidence_ready=False,
    )

    probability = predictions["PROBABILISTIC_QUEUE"]
    assert probability["domain_status"] == ("RESEARCH_ABSTAIN_TRADE_EVIDENCE_NOT_READY")
    assert Decimal(probability["fill_probability"]) == 0
    assert Decimal(probability["model_confidence"]) == 0
    assert predictions["STRICT_TRADE_EVIDENCE"]["domain_status"] == (
        "RESEARCH_UNCALIBRATED"
    )


def test_artifact_hash_tamper_fails_closed(tmp_path) -> None:
    artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(_report())
    payload = artifact.as_dict()
    payload["probability_max_exclusive"] = "0.9"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        MakerProbabilityCalibrationArtifact.load(path)
