from quant.validation.slo import evaluate_slos


def test_missing_slo_metrics_are_not_false_failures() -> None:
    result = evaluate_slos(
        {
            "prediction_p99_ms": 1,
            "prediction_budget_ms": 10,
            "unsafe_fill_count": 0,
        }
    )
    assert result["status"] == "NOT_ENOUGH_DATA"
    assert result["checks"]["unknown_terminal_zero"] is None


def test_observed_slo_failure_still_fails() -> None:
    result = evaluate_slos(
        {
            "book_freshness_p99_ms": 11,
            "execution_gate_ms": 10,
        }
    )
    assert result["status"] == "FAIL"
    assert result["checks"]["book_freshness_p99"] is False
