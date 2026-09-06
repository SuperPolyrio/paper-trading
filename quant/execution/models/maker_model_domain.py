"""Evidence-gated model-domain selection for maker execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .maker_probability_calibration import (
    LOW_PROBABILITY_CLASSIFICATION,
    MakerProbabilityCalibrationArtifact,
)
from .maker_queue import QueueModel


@dataclass(frozen=True)
class MakerModelDomainDecision:
    resolver_version: str
    authoritative_fill_model: QueueModel
    research_forecast_model: QueueModel
    domain_status: str
    calibration_domain: str
    promotion_allowed: bool
    evidence_sha256: str | None
    research_evidence_sha256: str | None
    research_calibration_artifact: Mapping[str, Any] | None
    reason_codes: tuple[str, ...]
    decision_hash: str = ""

    def __post_init__(self) -> None:
        if not self.decision_hash:
            object.__setattr__(self, "decision_hash", self._calculate_hash())

    @property
    def queue_model_version(self) -> str:
        return f"paper_maker_queue_strict_ws_trade_v3:{self.decision_hash[:16]}"

    @property
    def calibrated_in_domain(self) -> bool:
        return self.promotion_allowed and self.domain_status == "PROMOTED"

    @property
    def research_calibrated_in_domain(self) -> bool:
        return (
            self.domain_status == "RESEARCH_ONLY_CALIBRATED_LOW_PROBABILITY"
            and self.research_calibration_artifact is not None
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["authoritative_fill_model"] = self.authoritative_fill_model.value
        payload["research_forecast_model"] = self.research_forecast_model.value
        payload["reason_codes"] = list(self.reason_codes)
        payload["queue_model_version"] = self.queue_model_version
        payload["calibrated_in_domain"] = self.calibrated_in_domain
        payload["research_calibrated_in_domain"] = self.research_calibrated_in_domain
        return payload

    @property
    def hash_is_valid(self) -> bool:
        return self.decision_hash == self._calculate_hash()

    def _calculate_hash(self) -> str:
        payload = self.as_dict()
        payload["decision_hash"] = ""
        payload.pop("queue_model_version", None)
        payload.pop("calibrated_in_domain", None)
        payload.pop("research_calibrated_in_domain", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


class MakerModelDomainResolver:
    """Keep economic fills strict; promote only evidence-backed forecasts."""

    VERSION = "paper-maker-model-domain-resolver-v3"

    def __init__(
        self,
        evaluation: Mapping[str, Any] | None = None,
        *,
        evidence_sha256: str | None = None,
        offline_evaluation: Mapping[str, Any] | None = None,
        research_evidence_sha256: str | None = None,
        research_calibration_artifact: (
            MakerProbabilityCalibrationArtifact | None
        ) = None,
    ) -> None:
        self.evaluation = dict(evaluation or {})
        self.evidence_sha256 = evidence_sha256
        self.offline_evaluation = dict(offline_evaluation or {})
        self.research_evidence_sha256 = research_evidence_sha256
        self.research_calibration_artifact = research_calibration_artifact

    @classmethod
    def from_path(cls, path: Path | str | None) -> MakerModelDomainResolver:
        return cls.from_paths(path, None)

    @classmethod
    def from_paths(
        cls,
        authenticated_path: Path | str | None,
        offline_path: Path | str | None,
        research_calibration_path: Path | str | None = None,
    ) -> MakerModelDomainResolver:
        evaluation, evidence_hash = cls._load_evidence(authenticated_path)
        offline, research_hash = cls._load_evidence(offline_path)
        research_artifact = cls._load_research_artifact(
            offline_path=offline_path,
            offline_evaluation=offline,
            explicit_path=research_calibration_path,
        )
        return cls(
            evaluation,
            evidence_sha256=evidence_hash,
            offline_evaluation=offline,
            research_evidence_sha256=research_hash,
            research_calibration_artifact=research_artifact,
        )

    @staticmethod
    def _load_research_artifact(
        *,
        offline_path: Path | str | None,
        offline_evaluation: Mapping[str, Any] | None,
        explicit_path: Path | str | None,
    ) -> MakerProbabilityCalibrationArtifact | None:
        if explicit_path is not None:
            source = Path(explicit_path)
            if source.is_file():
                artifact = MakerProbabilityCalibrationArtifact.load(source)
                if offline_evaluation:
                    expected = (
                        MakerProbabilityCalibrationArtifact.from_benchmark_report(
                            offline_evaluation
                        )
                    )
                    if artifact.artifact_hash != expected.artifact_hash:
                        raise ValueError(
                            "Maker calibration artifact does not match benchmark"
                        )
                return artifact
        if not offline_evaluation:
            return None
        metadata = offline_evaluation.get("maker_probability_calibration_artifact")
        if isinstance(metadata, Mapping) and offline_path is not None:
            relative = str(metadata.get("path") or "")
            if relative:
                source = Path(offline_path).resolve().parent / relative
                if source.is_file():
                    raw = source.read_bytes()
                    expected = str(metadata.get("sha256") or "")
                    if expected and hashlib.sha256(raw).hexdigest() != expected:
                        raise ValueError("Maker calibration file SHA256 mismatch")
                    artifact = MakerProbabilityCalibrationArtifact.load(source)
                    if (
                        metadata.get("artifact_hash")
                        and metadata.get("artifact_hash") != artifact.artifact_hash
                    ):
                        raise ValueError("Maker calibration decision hash mismatch")
                    return artifact
        try:
            return MakerProbabilityCalibrationArtifact.from_benchmark_report(
                offline_evaluation
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _load_evidence(
        path: Path | str | None,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        if path is None:
            return None, None
        source = Path(path)
        if not source.is_file():
            return None, None
        raw = source.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            raise TypeError("maker calibration evidence must be a JSON object")
        return payload, hashlib.sha256(raw).hexdigest()

    def resolve(
        self,
        *,
        requested_forecast_model: QueueModel = QueueModel.PROBABILISTIC_QUEUE,
    ) -> MakerModelDomainDecision:
        promotion_allowed, reasons = self._promotion_evidence()
        if promotion_allowed:
            forecast = requested_forecast_model
            domain_status = "PROMOTED"
            calibration_domain = str(
                self.evaluation.get("calibration_domain")
                or self.evaluation.get("model_version")
                or "MAKER_HOLDOUT_PROMOTED"
            )
        elif self._offline_research_evidence():
            forecast = requested_forecast_model
            if self.research_calibration_artifact is not None:
                domain_status = "RESEARCH_ONLY_CALIBRATED_LOW_PROBABILITY"
                calibration_domain = (
                    self.research_calibration_artifact.calibration_domain
                )
                reasons = [
                    *reasons,
                    "offline_walk_forward_low_probability_calibration_only",
                    "high_probability_domain_disabled",
                    "authenticated_partial_full_required_for_live_promotion",
                ]
            else:
                domain_status = "RESEARCH_ONLY"
                calibration_domain = "OFFLINE_PUBLIC_L2_ORDERFILLED_COUNTERFACTUAL"
                reasons = [*reasons, "offline_research_calibration_only"]
        else:
            forecast = QueueModel.STRICT_TRADE_EVIDENCE
            domain_status = "STRICT_UNCALIBRATED"
            calibration_domain = "NO_REAL_PARTIAL_FULL_PROMOTION"
        return MakerModelDomainDecision(
            resolver_version=self.VERSION,
            authoritative_fill_model=QueueModel.STRICT_TRADE_EVIDENCE,
            research_forecast_model=forecast,
            domain_status=domain_status,
            calibration_domain=calibration_domain,
            promotion_allowed=promotion_allowed,
            evidence_sha256=self.evidence_sha256,
            research_evidence_sha256=self.research_evidence_sha256,
            research_calibration_artifact=(
                self.research_calibration_artifact.reference_dict()
                if self.research_calibration_artifact is not None
                else None
            ),
            reason_codes=tuple(reasons),
        )

    def _promotion_evidence(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        evaluation = self.evaluation
        if not evaluation:
            return False, ["holdout_evaluation_missing"]
        if str(evaluation.get("status") or "").upper() != "PASS":
            reasons.append("holdout_status_not_pass")
        if not bool(evaluation.get("promotion_allowed")):
            reasons.append("promotion_not_allowed")
        outcomes = evaluation.get("outcome_counts")
        counts = dict(outcomes) if isinstance(outcomes, Mapping) else {}
        if int(counts.get("PARTIAL") or 0) <= 0:
            reasons.append("real_partial_outcome_missing")
        if int(counts.get("FULL") or 0) <= 0:
            reasons.append("real_full_outcome_missing")
        sample_count = int(evaluation.get("evaluated_count") or 0)
        complete_count = int(evaluation.get("artifact_complete_count") or 0)
        if sample_count <= 0 or complete_count != sample_count:
            reasons.append("holdout_artifacts_incomplete")
        return not reasons, reasons or ["holdout_promotion_passed"]

    def _offline_research_evidence(self) -> bool:
        evaluation = self.offline_evaluation
        if str(evaluation.get("status") or "").upper() != "PASS_OFFLINE":
            return False
        if bool(evaluation.get("live_submission_performed")):
            return False
        if str(evaluation.get("evidence_scope") or "") != (
            "OFFLINE_COUNTERFACTUAL_NO_LIVE_SUBMISSION"
        ):
            return False
        classifications = evaluation.get("classifications")
        if not isinstance(classifications, Mapping):
            return False
        split = evaluation.get("split_manifest")
        if not isinstance(split, Mapping) or (
            split.get("status") != "PASS"
            or int(split.get("event_leakage_count") or 0) != 0
        ):
            return False
        if (
            classifications.get("maker_probability_walk_forward")
            == LOW_PROBABILITY_CLASSIFICATION
        ):
            maker = evaluation.get("maker")
            walk = maker.get("walk_forward") if isinstance(maker, Mapping) else None
            gate = (
                walk.get("probability_research_gate")
                if isinstance(walk, Mapping)
                else None
            )
            return bool(
                self.research_calibration_artifact is not None
                and isinstance(gate, Mapping)
                and gate.get("status") == "PASS"
                and gate.get("classification") == LOW_PROBABILITY_CLASSIFICATION
                and int(gate.get("strict_confirmed_fill_count") or 0) > 0
                and int(gate.get("observed_tape_no_fill_count") or 0) > 0
            )
        if classifications.get("maker_probability") != "RESEARCH_CALIBRATED":
            return False
        maker = evaluation.get("maker")
        holdout = maker.get("holdout") if isinstance(maker, Mapping) else None
        return bool(
            isinstance(holdout, Mapping)
            and int(holdout.get("strict_confirmed_fill_count") or 0) > 0
            and int(holdout.get("observed_tape_no_fill_count") or 0) > 0
        )
