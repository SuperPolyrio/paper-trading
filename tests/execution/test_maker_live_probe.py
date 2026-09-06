from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.maker.live_probe_runner import (
    account_delta,
    actual_outcome,
    maker_finality_label,
    maker_account_delta_matches_truth,
    maker_holdout_row,
    maker_limit_price,
    maker_outcome_observation,
    maker_probe_artifact_complete,
    maker_predictions,
    maker_trade_evidence_admission,
    maker_trade_forecast,
    predicted_outcome_matches,
    rest_bbo_matches,
)

BOOK = {
    "best_bid": "0.40",
    "best_ask": "0.42",
    "bids": [{"price": "0.40", "size": "12"}],
    "asks": [{"price": "0.42", "size": "8"}],
}


def test_one_tick_behind_prices_remain_post_only() -> None:
    assert maker_limit_price(
        BOOK,
        side="BUY",
        placement="ONE_TICK_BEHIND",
        tick_size=Decimal("0.01"),
    ) == Decimal("0.39")
    assert maker_limit_price(
        BOOK,
        side="SELL",
        placement="ONE_TICK_BEHIND",
        tick_size=Decimal("0.01"),
    ) == Decimal("0.43")


def test_one_tick_inside_spread_prices_remain_post_only() -> None:
    assert maker_limit_price(
        BOOK,
        side="BUY",
        placement="ONE_TICK_INSIDE_SPREAD",
        tick_size=Decimal("0.01"),
    ) == Decimal("0.41")


def test_near_opposite_prices_maximize_priority_without_crossing() -> None:
    wide = {**BOOK, "best_bid": "0.29", "best_ask": "0.33"}
    assert maker_limit_price(
        wide,
        side="BUY",
        placement="NEAR_OPPOSITE",
        tick_size=Decimal("0.01"),
    ) == Decimal("0.32")
    assert maker_limit_price(
        wide,
        side="SELL",
        placement="NEAR_OPPOSITE",
        tick_size=Decimal("0.01"),
    ) == Decimal("0.30")
    assert maker_limit_price(
        BOOK,
        side="SELL",
        placement="ONE_TICK_INSIDE_SPREAD",
        tick_size=Decimal("0.01"),
    ) == Decimal("0.41")


def test_one_tick_inside_spread_rejects_one_tick_wide_book() -> None:
    with pytest.raises(ValueError, match="cross"):
        maker_limit_price(
            {"best_bid": "0.40", "best_ask": "0.41"},
            side="BUY",
            placement="ONE_TICK_INSIDE_SPREAD",
            tick_size=Decimal("0.01"),
        )


def test_crossing_post_only_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="cross"):
        maker_limit_price(
            {"best_bid": "0.42", "best_ask": "0.42"},
            side="BUY",
            placement="AT_BEST",
            tick_size=Decimal("0.01"),
        )


def test_zero_forecast_produces_conservative_no_fill_predictions() -> None:
    predictions = maker_predictions(
        BOOK,
        asset_id="asset",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal(5),
        horizon_seconds=Decimal(10),
        run_id="run",
    )
    assert set(predictions) == {
        "STRICT_TRADE_EVIDENCE",
        "RISK_AVERSE_QUEUE",
        "PROBABILISTIC_QUEUE",
    }
    assert all(Decimal(row["p_no_fill"]) == 1 for row in predictions.values())


def test_predecision_trade_rate_feeds_maker_predictions() -> None:
    class Evidence:
        def summarize_compatible_maker_volume(self, **kwargs):
            assert kwargs["end"] - kwargs["start"] == timedelta(seconds=900)
            return {
                "trade_count": 3,
                "compatible_trade_volume": "180",
                "last_trade_at": "2026-08-06T00:14:00Z",
            }

    observed = datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc)
    forecast = maker_trade_forecast(
        Evidence(),
        asset_id="asset",
        side="BUY",
        price=Decimal("0.40"),
        horizon_seconds=Decimal(60),
        observed_at=observed,
        lookback_seconds=900,
    )
    assert forecast["window_end"] == observed.isoformat()
    assert forecast["forecast_trade_volume"] == "12"
    assert Decimal(0) < Decimal(forecast["aggressor_arrival_probability"]) < Decimal(1)
    predictions = maker_predictions(
        {**BOOK, "bids": [{"price": "0.40", "size": "2"}]},
        asset_id="asset",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal(5),
        horizon_seconds=Decimal(60),
        run_id="run",
        forecast_trade_volume=Decimal(forecast["forecast_trade_volume"]),
        aggressor_arrival_probability=Decimal(
            forecast["aggressor_arrival_probability"]
        ),
    )
    assert Decimal(predictions["STRICT_TRADE_EVIDENCE"]["p_full"]) == 1
    assert Decimal(predictions["PROBABILISTIC_QUEUE"]["fill_probability"]) < Decimal(
        "0.95"
    )


def test_maker_predictions_read_gcp_list_levels_as_queue_ahead() -> None:
    predictions = maker_predictions(
        {
            **BOOK,
            "bids": [["0.40", "12"]],
            "asks": [["0.42", "8"]],
        },
        asset_id="asset",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal(5),
        horizon_seconds=Decimal(60),
        run_id="run",
        forecast_trade_volume=Decimal(10),
    )

    assert all(
        Decimal(row["queue_ahead_estimate"]) == Decimal(12)
        for row in predictions.values()
    )
    assert all(Decimal(row["p_no_fill"]) == 1 for row in predictions.values())
    assert all(
        row["expected_time_to_first_fill_seconds"] is None
        for row in predictions.values()
    )


@pytest.mark.parametrize(
    ("actual", "requested", "expected"),
    [
        ("0", "5", "NO_FILL"),
        ("2", "5", "PARTIAL"),
        ("5", "5", "FULL"),
    ],
)
def test_actual_outcome(actual: str, requested: str, expected: str) -> None:
    assert actual_outcome(Decimal(actual), Decimal(requested)) == expected


def test_no_fill_is_a_horizon_censored_observation() -> None:
    observation = maker_outcome_observation(
        outcome="NO_FILL",
        matched_size=Decimal(0),
        requested_size=Decimal(5),
        resting_seconds=Decimal(120),
        capture={"cancel_trigger": "HORIZON_EXPIRED"},
    )

    assert observation["label"] == "CENSORED_AT_120S"
    assert observation["first_fill_right_censored"] is True
    assert observation["permanent_no_fill_claimed"] is False


def test_partial_cancel_and_chain_finality_remain_separate_labels() -> None:
    observation = maker_outcome_observation(
        outcome="PARTIAL",
        matched_size=Decimal(2),
        requested_size=Decimal(5),
        resting_seconds=Decimal(300),
        capture={"cancel_trigger": "FIRST_PARTIAL_FILL"},
    )
    provisional = maker_finality_label(
        matched_size=Decimal(2),
        orderfilled={"status": "PENDING_RECEIPT"},
    )
    confirmed = maker_finality_label(
        matched_size=Decimal(2),
        orderfilled={
            "status": "CONFIRMED_MATCH",
            "receipt_truth": {"status": "CONFIRMED_SUCCESS"},
        },
    )
    failed = maker_finality_label(
        matched_size=Decimal(2),
        orderfilled={
            "status": "RECEIPT_FAILED",
            "receipt_truth": {"status": "RECEIPT_FAILED"},
        },
    )

    assert observation["label"] == "PARTIAL_CANCEL_ON_FIRST_FILL"
    assert observation["full_fill_right_censored"] is True
    assert provisional["label"] == "MATCHED_PROVISIONAL"
    assert provisional["confirmed"] is False
    assert confirmed["label"] == "CONFIRMED"
    assert confirmed["confirmed"] is True
    assert failed["label"] == "FAILED"
    assert failed["confirmed"] is False


def test_fill_artifact_waits_for_venue_and_chain_finality() -> None:
    base = {
        "order_terminal_evidence": True,
        "order_still_open": False,
        "rest_order_reconciled": True,
        "order_not_found": False,
        "matched_size": Decimal("1.25"),
        "orderfilled_status": "CONFIRMED_MATCH",
        "account_delta_reconciled": True,
    }

    assert not maker_probe_artifact_complete(**base, ledger_truth="PENDING")
    assert maker_probe_artifact_complete(**base, ledger_truth="CONFIRMED")
    assert not maker_probe_artifact_complete(
        **{**base, "orderfilled_status": "PENDING_CHAIN_INDEX"},
        ledger_truth="CONFIRMED",
    )


def test_account_delta_must_match_exact_maker_order() -> None:
    assert maker_account_delta_matches_truth(
        side="BUY",
        matched_size=Decimal("1.25"),
        quote_amount=Decimal("0.5"),
        fee=Decimal("0"),
        delta={"collateral": "-0.5", "conditional": "1.25"},
    )
    assert not maker_account_delta_matches_truth(
        side="BUY",
        matched_size=Decimal("1.25"),
        quote_amount=Decimal("0.5"),
        fee=Decimal("0"),
        delta={"collateral": "-0.6", "conditional": "2.25"},
    )


def test_required_maker_prediction_matches_partial_or_full_only() -> None:
    assert predicted_outcome_matches("PARTIAL", "PARTIAL_OR_FULL")
    assert predicted_outcome_matches("FULL", "PARTIAL_OR_FULL")
    assert not predicted_outcome_matches("NO_FILL", "PARTIAL_OR_FULL")


def test_incomplete_trade_window_is_only_allowed_for_bounded_label_collection() -> None:
    forecast = {
        "status": "EVIDENCE_NOT_READY",
        "trade_count": 3,
        "coverage_reason": "asset_not_assigned_for_full_window",
    }

    allowed = maker_trade_evidence_admission(
        forecast,
        targeted_hot_preflight=True,
        probe_target="FULL",
        allow_incomplete=True,
    )
    strategy = maker_trade_evidence_admission(
        forecast,
        targeted_hot_preflight=False,
        probe_target="FULL",
        allow_incomplete=True,
    )

    assert allowed["allowed"] is True
    assert allowed["calibrated_prediction_claimed"] is False
    assert allowed["authoritative_outcome_source"].startswith("OWN_USER_WS")
    assert strategy["allowed"] is False


def test_current_ws_activity_allows_prospective_own_order_label_collection() -> None:
    result = maker_trade_evidence_admission(
        {
            "status": "EVIDENCE_NOT_READY",
            "trade_count": 0,
            "coverage_reason": "asset_not_assigned_for_full_window",
        },
        targeted_hot_preflight=True,
        probe_target="FULL",
        allow_incomplete=True,
        placement="NEAR_OPPOSITE",
        hot_preflight={
            "targeted_ws_book": {
                "activity_counts": {"price_change": 2},
            }
        },
        market_snapshot={"clob_market_info": {"ao": True}},
    )

    assert result["allowed"] is True
    assert result["mode"] == "PROSPECTIVE_OWN_ORDER_LABEL_COLLECTION_ONLY"
    assert result["calibrated_prediction_claimed"] is False
    assert result["activity_event_count"] == 2


def test_recent_public_trade_allows_prospective_label_collection_only() -> None:
    result = maker_trade_evidence_admission(
        {
            "status": "EVIDENCE_NOT_READY",
            "trade_count": 0,
            "coverage_reason": "asset_not_assigned_for_full_window",
        },
        targeted_hot_preflight=True,
        probe_target="FULL",
        allow_incomplete=True,
        placement="NEAR_OPPOSITE",
        hot_preflight={"targeted_ws_book": {"activity_counts": {}}},
        market_snapshot={"clob_market_info": {"ao": True}},
        recent_public_trade_activity={
            "status": "READY",
            "source_ready": True,
            "compatible_trade_count": 2,
            "payload_sha256": "public-trades-hash",
            "prediction_truth_claimed": False,
            "own_order_execution_truth_claimed": False,
        },
    )

    assert result["allowed"] is True
    assert result["mode"] == "PROSPECTIVE_OWN_ORDER_LABEL_COLLECTION_ONLY"
    assert result["calibrated_prediction_claimed"] is False
    assert result["activity_event_count"] == 0
    assert result["recent_public_compatible_trade_count"] == 2
    assert result["recent_public_trade_payload_sha256"] == "public-trades-hash"


def test_account_delta_is_reported_in_user_units() -> None:
    before = {
        "collateral": {"balance": "10"},
        "conditional": {"balance": "2"},
    }
    after = {
        "collateral": {"balance": "9.5"},
        "conditional": {"balance": "3"},
    }
    assert account_delta(before, after) == {
        "collateral": "-0.5",
        "conditional": "1",
    }


def test_rest_bbo_alignment_uses_the_resting_side_and_prevents_crossing() -> None:
    assert rest_bbo_matches(
        BOOK,
        {"rest_best_bid": "0.40", "rest_best_ask": "0.42"},
        side="BUY",
    )
    assert rest_bbo_matches(
        BOOK,
        {"rest_best_bid": "0.40", "rest_best_ask": "0.43"},
        side="BUY",
    )
    assert not rest_bbo_matches(
        BOOK,
        {"rest_best_bid": "0.39", "rest_best_ask": "0.43"},
        side="BUY",
    )


def test_maker_holdout_row_keeps_size_and_fill_timing_evidence() -> None:
    row = maker_holdout_row(
        {
            "run_id": "run-1",
            "started_at": "2026-08-07T00:00:00+00:00",
            "completed_at": "2026-08-07T00:00:20+00:00",
            "artifact_complete": True,
            "actual_outcome": "FULL",
            "intent": {"size": "4"},
            "candidate": {"condition_id": "condition-1"},
            "model_predictions": {
                "PROBABILISTIC_QUEUE": {
                    "p_no_fill": "0.1",
                    "p_partial": "0.2",
                    "p_full": "0.7",
                    "expected_filled_size": "3.6",
                    "expected_time_to_first_fill_seconds": "4",
                    "expected_time_to_full_fill_seconds": "10",
                }
            },
            "truth": {
                "actual_matched_size": "4",
                "first_match_at": "2026-08-07T00:00:05+00:00",
                "last_match_at": "2026-08-07T00:00:12+00:00",
            },
        }
    )

    assert row["order_size"] == "4"
    assert row["actual_time_to_first_fill_seconds"] == "5.0"
    assert row["actual_time_to_full_fill_seconds"] == "12.0"
    assert row["expected_time_to_first_fill_seconds"] == "4"
    assert row["expected_time_to_full_fill_seconds"] == "10"
