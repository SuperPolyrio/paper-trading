"""Validated offline calibration used by online Maker research forecasts."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .maker_queue import MakerFillPrediction, poisson_arrival_probability

SCHEMA_VERSION = "maker_probability_calibration_v1"
LOW_PROBABILITY_CLASSIFICATION = "RESEARCH_CALIBRATED_LOW_PROBABILITY_DOMAIN"
MIN_ACTIVITY_BUCKET_TRIALS = 20


@dataclass(frozen=True)
class MakerProbabilityCalibrationArtifact:
    """Train/calibration-only probability policy with a fail-closed domain."""

    schema_version: str
    model_version: str
    calibration_domain: str
    probability_min_inclusive: Decimal
    probability_max_exclusive: Decimal
    selected_config: Mapping[str, Any]
    train_prior: Mapping[str, Any]
    source_evidence_hash: str
    walk_forward_evidence: Mapping[str, Any]
    artifact_hash: str

    @classmethod
    def from_benchmark_report(
        cls,
        report: Mapping[str, Any],
    ) -> MakerProbabilityCalibrationArtifact:
        _require(
            report.get("status") == "PASS_OFFLINE", "benchmark is not PASS_OFFLINE"
        )
        _require(
            report.get("evidence_scope") == "OFFLINE_COUNTERFACTUAL_NO_LIVE_SUBMISSION",
            "benchmark evidence scope is not offline counterfactual",
        )
        _require(
            not bool(report.get("live_submission_performed")),
            "offline calibration unexpectedly submitted a live order",
        )
        split = _mapping(report.get("split_manifest"))
        _require(split.get("status") == "PASS", "benchmark split is not PASS")
        _require(
            int(split.get("event_leakage_count") or 0) == 0,
            "benchmark split contains event leakage",
        )
        classifications = _mapping(report.get("classifications"))
        classification = str(
            classifications.get("maker_probability_walk_forward") or ""
        )
        _require(
            classification == LOW_PROBABILITY_CLASSIFICATION,
            "walk-forward low-probability Maker domain is not calibrated",
        )
        maker = _mapping(report.get("maker"))
        walk_forward = _mapping(maker.get("walk_forward"))
        gate = _mapping(walk_forward.get("probability_research_gate"))
        _require(gate.get("status") == "PASS", "walk-forward Maker gate is not PASS")
        _require(
            gate.get("classification") == LOW_PROBABILITY_CLASSIFICATION,
            "walk-forward classification differs from report classification",
        )
        _require(
            str(gate.get("calibrated_probability_domain") or "") == "[0,0.5)",
            "only the explicit [0,0.5) calibration domain is supported",
        )
        high_domain = _mapping(gate.get("high_probability_domain"))
        _require(
            high_domain.get("status") == "DISABLED_INSUFFICIENT_EVIDENCE",
            "high-probability domain must remain disabled",
        )
        activity = _mapping(report.get("activity_calibration"))
        selected_config = dict(_mapping(activity.get("selected_config")))
        train_prior = dict(_mapping(activity.get("train_prior")))
        _validate_config(selected_config)
        _validate_prior(train_prior)
        _require(
            selected_config.get("prior_artifact_hash")
            == train_prior.get("artifact_hash"),
            "selected config does not reference the embedded train prior",
        )
        evidence = {
            "classification": classification,
            "evidence_scope": report.get("evidence_scope"),
            "split": split,
            "selected_config": selected_config,
            "train_prior": train_prior,
            "walk_forward_gate": gate,
            "fit_splits": activity.get("fit_splits"),
            "selection_splits": activity.get("selection_splits"),
            "holdout_rows_used_for_fit": activity.get("holdout_rows_used_for_fit"),
        }
        source_hash = _payload_hash(evidence)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "model_version": "maker-probability-eb-online-v1",
            "calibration_domain": (
                "OFFLINE_WALK_FORWARD_LOW_PROBABILITY:"
                f"{train_prior['artifact_hash'][:12]}:"
                f"{selected_config['decision_hash'][:12]}"
            ),
            "probability_min_inclusive": "0",
            "probability_max_exclusive": "0.5",
            "selected_config": selected_config,
            "train_prior": train_prior,
            "source_evidence_hash": source_hash,
            "walk_forward_evidence": {
                "classification": classification,
                "strict_confirmed_fill_count": int(
                    gate.get("strict_confirmed_fill_count") or 0
                ),
                "observed_tape_no_fill_count": int(
                    gate.get("observed_tape_no_fill_count") or 0
                ),
                "proxy_count": int(gate.get("proxy_count") or 0),
                "proxy_brier": gate.get("proxy_brier"),
                "proxy_ece": gate.get("proxy_ece"),
                "high_probability_domain": high_domain,
            },
        }
        artifact_hash = _payload_hash(payload)
        return cls(
            schema_version=SCHEMA_VERSION,
            model_version=str(payload["model_version"]),
            calibration_domain=str(payload["calibration_domain"]),
            probability_min_inclusive=Decimal(0),
            probability_max_exclusive=Decimal("0.5"),
            selected_config=selected_config,
            train_prior=train_prior,
            source_evidence_hash=source_hash,
            walk_forward_evidence=dict(payload["walk_forward_evidence"]),
            artifact_hash=artifact_hash,
        )

    @classmethod
    def load(cls, path: Path | str) -> MakerProbabilityCalibrationArtifact:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        _require(
            isinstance(payload, Mapping), "Maker calibration artifact must be an object"
        )
        expected = str(payload.get("artifact_hash") or "")
        body = dict(payload)
        body.pop("artifact_hash", None)
        _require(
            expected == _payload_hash(body), "Maker calibration artifact hash mismatch"
        )
        artifact = cls(
            schema_version=str(payload.get("schema_version") or ""),
            model_version=str(payload.get("model_version") or ""),
            calibration_domain=str(payload.get("calibration_domain") or ""),
            probability_min_inclusive=Decimal(
                str(payload.get("probability_min_inclusive") or 0)
            ),
            probability_max_exclusive=Decimal(
                str(payload.get("probability_max_exclusive") or 0)
            ),
            selected_config=dict(_mapping(payload.get("selected_config"))),
            train_prior=dict(_mapping(payload.get("train_prior"))),
            source_evidence_hash=str(payload.get("source_evidence_hash") or ""),
            walk_forward_evidence=dict(_mapping(payload.get("walk_forward_evidence"))),
            artifact_hash=expected,
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        _require(
            self.schema_version == SCHEMA_VERSION,
            "unsupported Maker calibration schema",
        )
        _require(
            self.probability_min_inclusive == 0
            and self.probability_max_exclusive == Decimal("0.5"),
            "Maker calibration artifact may enable only [0,0.5)",
        )
        _require(len(self.source_evidence_hash) == 64, "invalid source evidence hash")
        _validate_config(self.selected_config)
        _validate_prior(self.train_prior)
        body = self.as_dict()
        body.pop("artifact_hash", None)
        _require(
            self.artifact_hash == _payload_hash(body),
            "Maker artifact is not self-consistent",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "calibration_domain": self.calibration_domain,
            "probability_min_inclusive": str(self.probability_min_inclusive),
            "probability_max_exclusive": str(self.probability_max_exclusive),
            "selected_config": dict(self.selected_config),
            "train_prior": dict(self.train_prior),
            "source_evidence_hash": self.source_evidence_hash,
            "walk_forward_evidence": dict(self.walk_forward_evidence),
            "artifact_hash": self.artifact_hash,
        }

    def reference_dict(self) -> dict[str, Any]:
        """Return the compact immutable reference safe to freeze per order."""

        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "calibration_domain": self.calibration_domain,
            "probability_min_inclusive": str(self.probability_min_inclusive),
            "probability_max_exclusive": str(self.probability_max_exclusive),
            "artifact_hash": self.artifact_hash,
            "source_evidence_hash": self.source_evidence_hash,
            "config_decision_hash": self.selected_config.get("decision_hash"),
            "prior_artifact_hash": self.train_prior.get("artifact_hash"),
            "walk_forward_evidence": dict(self.walk_forward_evidence),
            "authoritative_pnl": False,
        }

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(
                json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def activity_forecast(
        self,
        *,
        category: str,
        side: str,
        quote_position: str,
        horizon_seconds: Decimal,
        observed_trade_count: int,
        observed_trade_volume: Decimal,
        lookback_seconds: Decimal,
    ) -> tuple[Decimal, Decimal, dict[str, Any]]:
        bucket_key, bucket = self._activity_bucket(
            category=category,
            side=side,
            quote_position=quote_position,
        )
        config = self.selected_config
        prior_rate = Decimal(str(bucket["posterior_arrival_rate_per_second"]))
        prior_size = Decimal(str(bucket["posterior_mean_trade_size"]))
        exposure = max(Decimal(0), Decimal(str(config["prior_exposure_seconds"])))
        lookback = max(Decimal("0.000001"), Decimal(str(lookback_seconds)))
        count = max(Decimal(0), Decimal(int(observed_trade_count)))
        volume = max(Decimal(0), Decimal(str(observed_trade_volume)))
        posterior_rate = (count + prior_rate * exposure) / (lookback + exposure)
        prior_arrivals = max(Decimal(1), prior_rate * exposure)
        posterior_size = (volume + prior_size * prior_arrivals) / (
            count + prior_arrivals
        )
        multiplier = Decimal(str(config["activity_multiplier"]))
        expected_arrivals = posterior_rate * horizon_seconds * multiplier
        arrival_probability = poisson_arrival_probability(
            observed_count=posterior_rate * lookback,
            lookback_seconds=lookback,
            horizon_seconds=horizon_seconds,
            rate_multiplier=multiplier,
        )
        conditional_arrivals = (
            expected_arrivals / arrival_probability
            if arrival_probability > 0
            else Decimal(0)
        )
        conditional_volume = conditional_arrivals * posterior_size
        return (
            conditional_volume,
            arrival_probability,
            {
                "artifact_hash": self.artifact_hash,
                "activity_bucket": bucket_key,
                "activity_bucket_trial_count": int(bucket.get("trial_count") or 0),
                "posterior_arrival_rate_per_second": str(posterior_rate),
                "posterior_mean_trade_size": str(posterior_size),
                "expected_arrivals": str(expected_arrivals),
                "arrival_probability": str(arrival_probability),
                "conditional_trade_volume": str(conditional_volume),
            },
        )

    def calibrate_prediction(
        self,
        prediction: MakerFillPrediction,
        *,
        order_size: Decimal,
        horizon_seconds: Decimal,
    ) -> MakerFillPrediction:
        raw_weight = Decimal(str(self.selected_config["raw_probability_weight"]))
        fill_prior = self._horizon_fill_probability(horizon_seconds)
        probability = prediction.fill_probability * raw_weight + fill_prior * (
            Decimal(1) - raw_weight
        )
        probability = _apply_odds_multiplier(
            probability,
            Decimal(str(self.selected_config["probability_odds_multiplier"])),
        )
        if not (
            self.probability_min_inclusive
            <= probability
            < self.probability_max_exclusive
        ):
            return replace(
                prediction,
                p_no_fill=Decimal(1),
                p_partial=Decimal(0),
                p_full=Decimal(0),
                expected_filled_size=Decimal(0),
                filled_size_p10=Decimal(0),
                filled_size_p50=Decimal(0),
                filled_size_p90=Decimal(0),
                expected_time_to_first_fill_seconds=None,
                expected_time_to_full_fill_seconds=None,
                confidence=Decimal(0),
                domain_status="RESEARCH_ABSTAIN_OUT_OF_CALIBRATION_DOMAIN",
                fill_probability=Decimal(0),
                expected_time_to_fill_seconds=None,
                time_to_fill_p90_seconds=None,
                model_confidence=Decimal(0),
                calibration_domain=self.calibration_domain,
            )
        raw = prediction.fill_probability
        if raw > 0:
            p_full = probability * prediction.p_full / raw
            p_partial = probability * prediction.p_partial / raw
        else:
            p_full = Decimal(0)
            p_partial = probability
        expected = order_size * (p_full + p_partial * Decimal("0.5"))
        first = prediction.expected_time_to_first_fill_seconds
        if first is None and probability > 0:
            first = horizon_seconds / Decimal(2)
        return replace(
            prediction,
            p_no_fill=Decimal(1) - probability,
            p_partial=p_partial,
            p_full=p_full,
            expected_filled_size=expected,
            filled_size_p50=order_size if p_full >= Decimal("0.5") else expected,
            filled_size_p90=min(order_size, expected * Decimal("1.5")),
            expected_time_to_first_fill_seconds=first,
            expected_time_to_full_fill_seconds=(
                horizon_seconds if p_full > 0 else None
            ),
            domain_status=LOW_PROBABILITY_CLASSIFICATION,
            fill_probability=probability,
            expected_time_to_fill_seconds=first,
            time_to_fill_p90_seconds=(horizon_seconds if probability > 0 else None),
            calibration_domain=self.calibration_domain,
        )

    def _activity_bucket(
        self,
        *,
        category: str,
        side: str,
        quote_position: str,
    ) -> tuple[str, Mapping[str, Any]]:
        buckets = _mapping(self.train_prior.get("activity_buckets"))
        normalized_category = str(category or "unknown").lower()
        normalized_side = str(side).upper()
        normalized_position = str(quote_position).upper()
        keys = (
            f"CATEGORY_POSITION:{normalized_category}|{normalized_side}|{normalized_position}",
            f"CATEGORY:{normalized_category}|{normalized_side}",
            f"POSITION:{normalized_side}|{normalized_position}",
            "GLOBAL",
        )
        for key in keys:
            bucket = _mapping(buckets.get(key))
            if not bucket:
                continue
            if key != "GLOBAL" and int(bucket.get("trial_count") or 0) < (
                MIN_ACTIVITY_BUCKET_TRIALS
            ):
                continue
            return key, bucket
        raise ValueError("Maker calibration artifact has no usable activity bucket")

    def _horizon_fill_probability(self, horizon_seconds: Decimal) -> Decimal:
        raw = _mapping(self.train_prior.get("horizon_fill_probabilities"))
        values = {int(key): Decimal(str(value)) for key, value in raw.items()}
        _require(bool(values), "Maker calibration artifact has no horizon priors")
        target = int(horizon_seconds)
        nearest = min(values, key=lambda value: (abs(value - target), value))
        return values[nearest]


def _validate_config(config: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "activity_multiplier",
        "prior_exposure_seconds",
        "raw_probability_weight",
        "probability_odds_multiplier",
        "prior_artifact_hash",
        "decision_hash",
    }
    _require(required <= set(config), "Maker calibration config is incomplete")
    payload = {
        "schema_version": str(config["schema_version"]),
        "activity_multiplier": str(config["activity_multiplier"]),
        "prior_exposure_seconds": str(config["prior_exposure_seconds"]),
        "raw_probability_weight": str(config["raw_probability_weight"]),
        "probability_odds_multiplier": str(config["probability_odds_multiplier"]),
        "prior_artifact_hash": str(config["prior_artifact_hash"]),
        "fit_split": "train",
        "selection_split": "calibration",
    }
    _require(
        str(config["decision_hash"]) == _payload_hash(payload),
        "Maker calibration config decision hash mismatch",
    )


def _validate_prior(prior: Mapping[str, Any]) -> None:
    expected = str(prior.get("artifact_hash") or "")
    body = dict(prior)
    body.pop("artifact_hash", None)
    _require(
        expected == _payload_hash(body), "Maker train prior artifact hash mismatch"
    )
    _require(prior.get("source_split") == "train", "Maker prior was not fit on train")


def _apply_odds_multiplier(probability: Decimal, multiplier: Decimal) -> Decimal:
    bounded = min(Decimal(1), max(Decimal(0), probability))
    if bounded in {Decimal(0), Decimal(1)}:
        return bounded
    _require(multiplier > 0, "Maker probability odds multiplier must be positive")
    odds = multiplier * bounded / (Decimal(1) - bounded)
    return odds / (Decimal(1) + odds)


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
