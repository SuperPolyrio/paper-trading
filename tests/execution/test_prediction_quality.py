from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.paper.prediction_quality import (
    PredictionObservation,
    build_prediction_quality_report,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _observation(event_id: str, category: str, probability: str, outcome: str) -> PredictionObservation:
    return PredictionObservation(
        event_id=event_id,
        market_id=f"market-{event_id}",
        category=category,
        decision_ts=NOW,
        resolution_ts=NOW + timedelta(days=2),
        probability=Decimal(probability),
        outcome=Decimal(outcome),
        decision_market_price=Decimal("0.55"),
        final_pre_resolution_price=Decimal("0.70"),
        capital_at_risk=Decimal("10"),
    )


def test_prediction_report_separates_rows_from_independent_events() -> None:
    report = build_prediction_quality_report(
        [
            _observation("event-a", "politics", "0.8", "1"),
            _observation("event-a", "politics", "0.9", "1"),
            _observation("event-b", "weather", "0.2", "0"),
        ]
    )
    assert report["observation_count"] == 3
    assert report["independent_event_count"] == 2
    assert report["metrics"]["brier"] == Decimal("0.03")
    assert report["metrics"]["capital_days"] == Decimal("60")
    assert set(report["by_category"]) == {"politics", "weather"}


def test_prediction_report_handles_unknown_resolution_as_half() -> None:
    row = _observation("event-half", "other", "0.5", "0.5")
    report = build_prediction_quality_report([row])
    assert report["metrics"]["brier"] == 0
    assert report["calibration"][0]["observed_rate"] == Decimal("0.5")
