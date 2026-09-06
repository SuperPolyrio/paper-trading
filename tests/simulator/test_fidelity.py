from quant.simulator.fidelity import FidelityInputs, FidelityLevel, assess_fidelity


def _inputs(**overrides):
    values = {
        "data_quality_grade": "A",
        "data_safe": True,
        "venue_emulated": True,
        "capacity_safe": True,
        "taker_calibrated_in_domain": False,
        "maker_calibrated_in_domain": False,
        "impact_aware_scenario": False,
        "capacity_status": "IN_DOMAIN_CALIBRATED",
        "venue_regime_id": "venue-v1",
        "fill_model_version": "fill-v1",
        "latency_model_version": "latency-v1",
        "finality_model_version": "finality-v1",
        "valuation_model_version": "valuation-v1",
        "calibration_domain_status": "UNCALIBRATED",
    }
    values.update(overrides)
    return FidelityInputs(**values)


def test_fidelity_cannot_skip_missing_prerequisites() -> None:
    assessment = assess_fidelity(_inputs(data_safe=False, maker_calibrated_in_domain=True))
    assert assessment.fidelity_level is FidelityLevel.F0_FUNCTIONAL
    assert "data_not_checkpoint_gap_safe" in assessment.confidence_reasons


def test_fidelity_reaches_taker_only_with_all_lower_evidence() -> None:
    assessment = assess_fidelity(_inputs(taker_calibrated_in_domain=True))
    assert assessment.fidelity_level is FidelityLevel.F4_TAKER_CALIBRATED_IN_DOMAIN
    assert assessment.as_dict()["venue_regime_id"] == "venue-v1"


def test_fidelity_keeps_maker_research_below_calibrated_maker() -> None:
    assessment = assess_fidelity(_inputs())
    assert assessment.fidelity_level is FidelityLevel.F3_CAPACITY_SAFE
    assert "maker_probability_model_not_calibrated" in assessment.confidence_reasons
