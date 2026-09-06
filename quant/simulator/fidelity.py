"""Evidence-based simulator fidelity labels.

Fidelity is not a marketing badge.  It is the highest level whose prerequisites
are all present on an individual order, with every missing prerequisite kept as
an audit reason.  The level is therefore safe to compare across replay and
live-shadow output without treating uncalibrated PnL as trusted PnL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Mapping


class FidelityLevel(str, Enum):
    F0_FUNCTIONAL = "F0_FUNCTIONAL"
    F1_DATA_SAFE = "F1_DATA_SAFE"
    F2_VENUE_EMULATED = "F2_VENUE_EMULATED"
    F3_CAPACITY_SAFE = "F3_CAPACITY_SAFE"
    F4_TAKER_CALIBRATED_IN_DOMAIN = "F4_TAKER_CALIBRATED_IN_DOMAIN"
    F5_MAKER_CALIBRATED_IN_DOMAIN = "F5_MAKER_CALIBRATED_IN_DOMAIN"
    F6_IMPACT_AWARE_SCENARIO = "F6_IMPACT_AWARE_SCENARIO"


@dataclass(frozen=True)
class FidelityInputs:
    data_quality_grade: str | None
    data_safe: bool
    venue_emulated: bool
    capacity_safe: bool
    taker_calibrated_in_domain: bool
    maker_calibrated_in_domain: bool
    impact_aware_scenario: bool
    capacity_status: str
    venue_regime_id: str
    fill_model_version: str
    latency_model_version: str
    finality_model_version: str
    valuation_model_version: str
    calibration_domain_status: str
    confidence_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FidelityAssessment:
    fidelity_level: FidelityLevel
    data_quality_grade: str | None
    capacity_status: str
    venue_regime_id: str
    fill_model_version: str
    latency_model_version: str
    finality_model_version: str
    valuation_model_version: str
    calibration_domain_status: str
    confidence_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["fidelity_level"] = self.fidelity_level.value
        payload["confidence_reasons"] = list(self.confidence_reasons)
        return payload


def assess_fidelity(inputs: FidelityInputs) -> FidelityAssessment:
    """Return the strict highest admitted level and all unmet prerequisites."""

    level = FidelityLevel.F0_FUNCTIONAL
    reasons = list(inputs.confidence_reasons)
    if inputs.data_safe:
        level = FidelityLevel.F1_DATA_SAFE
    else:
        reasons.append("data_not_checkpoint_gap_safe")
    if inputs.data_safe and inputs.venue_emulated:
        level = FidelityLevel.F2_VENUE_EMULATED
    elif inputs.data_safe:
        reasons.append("venue_gateway_not_bound_to_order")
    if inputs.data_safe and inputs.venue_emulated and inputs.capacity_safe:
        level = FidelityLevel.F3_CAPACITY_SAFE
    elif inputs.data_safe and inputs.venue_emulated:
        reasons.append("global_capacity_overlay_not_verified")
    if level == FidelityLevel.F3_CAPACITY_SAFE and inputs.taker_calibrated_in_domain:
        level = FidelityLevel.F4_TAKER_CALIBRATED_IN_DOMAIN
    elif level == FidelityLevel.F3_CAPACITY_SAFE:
        reasons.append("taker_holdout_not_in_calibration_domain")
    if level in {FidelityLevel.F3_CAPACITY_SAFE, FidelityLevel.F4_TAKER_CALIBRATED_IN_DOMAIN} and inputs.maker_calibrated_in_domain:
        level = FidelityLevel.F5_MAKER_CALIBRATED_IN_DOMAIN
    elif level in {FidelityLevel.F3_CAPACITY_SAFE, FidelityLevel.F4_TAKER_CALIBRATED_IN_DOMAIN}:
        reasons.append("maker_probability_model_not_calibrated")
    if level == FidelityLevel.F5_MAKER_CALIBRATED_IN_DOMAIN and inputs.impact_aware_scenario:
        level = FidelityLevel.F6_IMPACT_AWARE_SCENARIO
    elif level == FidelityLevel.F5_MAKER_CALIBRATED_IN_DOMAIN:
        reasons.append("impact_aware_scenario_not_enabled")
    return FidelityAssessment(
        fidelity_level=level,
        data_quality_grade=inputs.data_quality_grade,
        capacity_status=inputs.capacity_status,
        venue_regime_id=inputs.venue_regime_id,
        fill_model_version=inputs.fill_model_version,
        latency_model_version=inputs.latency_model_version,
        finality_model_version=inputs.finality_model_version,
        valuation_model_version=inputs.valuation_model_version,
        calibration_domain_status=inputs.calibration_domain_status,
        confidence_reasons=tuple(dict.fromkeys(reasons)),
    )
