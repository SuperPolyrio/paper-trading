from datetime import datetime, timezone

from quant.calibration.calibration_metrics import holdout_metrics, wilson_interval
from quant.calibration.drift_monitor import evaluate_drift, evaluate_drift_windows
from quant.calibration.holdout_split import group_key, grouped_split


def _row(index: int, event: str, day: int) -> dict:
    return {
        "probe_id": f"p{index}",
        "condition_id": f"c{index}",
        "decision_ts": datetime(2026, 7, day, tzinfo=timezone.utc),
        "market_snapshot": {"event_id": event, "category": "politics"},
    }


def test_grouped_split_never_leaks_group() -> None:
    rows = [_row(index, f"e{index // 3}", 1 + index % 5) for index in range(100)]
    split = grouped_split(rows)
    groups = {
        name: {group_key(row) for row in values}
        for name, values in split.items()
    }
    assert not groups["training"] & groups["validation"]
    assert not groups["training"] & groups["holdout"]
    assert not groups["validation"] & groups["holdout"]


def test_zero_observed_false_positive_does_not_mean_zero_risk() -> None:
    interval = wilson_interval(0, 10)
    assert interval["point"] == 0
    assert interval["upper"] > 0
    metrics = holdout_metrics(
        [
            {
                "predicted_class": "FULL",
                "actual_class": "FULL",
                "order_type": "FOK",
            }
            for _ in range(10)
        ]
    )
    assert metrics["false_positive_fill"]["upper"] > 0


def test_small_perfect_drift_window_is_insufficient_not_drifted() -> None:
    rows = [
        {
            "venue_regime_id": "regime-v2",
            "reconciliation": {
                "predicted_class": "FULL",
                "actual_class": "FULL",
                "order_type": "FOK",
            },
        }
        for _ in range(20)
    ]

    result = evaluate_drift(
        rows,
        window=50,
        minimum_samples=50,
        false_positive_threshold=0.02,
    )

    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["metrics"]["false_positive_fill"]["point"] == 0
    assert result["metrics"]["false_positive_fill"]["upper"] > 0.02


def test_observed_false_positive_rate_triggers_drift_after_minimum() -> None:
    rows = [
        {
            "venue_regime_id": "regime-v2",
            "reconciliation": {
                "predicted_class": "FULL",
                "actual_class": "NO_FILL" if index < 2 else "FULL",
                "order_type": "FOK",
            },
        }
        for index in range(50)
    ]

    result = evaluate_drift(
        rows,
        window=50,
        minimum_samples=50,
        false_positive_threshold=0.02,
    )

    assert result["status"] == "DRIFTED"
    assert result["metrics"]["false_positive_fill"]["point"] == 0.04


def test_drift_windows_identify_category_and_affected_regime() -> None:
    rows = [
        {
            "venue_regime_id": "regime-v2",
            "market_snapshot": {
                "category": "crypto" if index < 20 else "politics"
            },
            "reconciliation": {
                "predicted_class": "FULL",
                "actual_class": "NO_FILL" if index < 2 else "FULL",
                "order_type": "FOK",
            },
        }
        for index in range(60)
    ]

    result = evaluate_drift_windows(
        rows,
        window=50,
        minimum_samples=50,
        group_minimum_samples=20,
        false_positive_threshold=0.02,
    )

    assert result["status"] == "DRIFTED"
    assert result["by_category"]["crypto"]["status"] == "DRIFTED"
    assert "by_category.crypto" in result["drifted_scopes"]
    assert result["affected_regimes"] == ["regime-v2"]


def test_vwap_and_rest_mismatch_trigger_mature_drift() -> None:
    rows = [
        {
            "venue_regime_id": "regime-v2",
            "market_snapshot": {
                "category": "politics",
                "rest_book_match": index >= 2,
            },
            "reconciliation": {
                "predicted_class": "FULL",
                "actual_class": "FULL",
                "order_type": "FOK",
                "price_error_ticks": "1.5" if index >= 45 else "0",
            },
        }
        for index in range(50)
    ]

    result = evaluate_drift(
        rows,
        window=50,
        minimum_samples=50,
        false_positive_threshold=0.02,
        vwap_p95_threshold_ticks=1,
        rest_mismatch_threshold=0.02,
    )

    assert result["status"] == "DRIFTED"
    assert result["checks"]["vwap_p95_within_threshold"] is False
    assert result["checks"]["rest_mismatch_rate_within_threshold"] is False
    assert set(result["drift_reasons"]) == {
        "vwap_p95_within_threshold",
        "rest_mismatch_rate_within_threshold",
    }


def test_latency_p95_drift_uses_active_model_baseline() -> None:
    rows = [
        {
            "venue_regime_id": "regime-v2",
            "market_snapshot": {
                "category": "crypto",
                "rest_book_match": True,
            },
            "timestamps": {
                "http_send_started_ts": f"2026-07-29T00:00:{index:02d}.000+00:00",
                "http_response_completed_ts": f"2026-07-29T00:00:{index:02d}.250+00:00",
            },
            "reconciliation": {
                "predicted_class": "FULL",
                "actual_class": "FULL",
                "order_type": "FOK",
                "price_error_ticks": "0",
            },
        }
        for index in range(50)
    ]
    baseline = {
        "http_round_trip_ms": {
            "count": 50,
            "p95": "100",
        }
    }

    result = evaluate_drift(
        rows,
        window=50,
        minimum_samples=50,
        false_positive_threshold=0.02,
        latency_p95_ratio_threshold=2,
        latency_minimum_samples=10,
        latency_baseline=baseline,
    )

    assert result["status"] == "DRIFTED"
    assert result["checks"]["latency_p95_within_threshold"] is False
    assert (
        result["operational_metrics"]["latency_drift"]["comparisons"]
        ["http_round_trip_ms"]["ratio"]
        == 2.5
    )


def test_venue_regime_change_targets_only_old_active_regime() -> None:
    rows = [
        {
            "venue_regime_id": "regime-v3",
            "market_snapshot": {
                "category": "politics",
                "rest_book_match": True,
            },
            "reconciliation": {
                "predicted_class": "FULL",
                "actual_class": "FULL",
                "order_type": "FOK",
                "price_error_ticks": "0",
            },
        }
        for _ in range(50)
    ]

    result = evaluate_drift_windows(
        rows,
        window=50,
        minimum_samples=50,
        group_minimum_samples=20,
        false_positive_threshold=0.02,
        current_venue_regime_id="regime-v3",
        active_model_regimes={"regime-v2"},
    )

    assert result["status"] == "DRIFTED"
    assert result["affected_regimes"] == ["regime-v2"]
    assert result["drifted_scopes"][-1] == (
        "venue_regime_changed.regime-v2_to_regime-v3"
    )
