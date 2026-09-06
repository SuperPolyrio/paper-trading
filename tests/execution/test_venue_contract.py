from quant.execution.venue_rules import (
    classify_venue_error,
    normalize_batch_order_response,
    normalize_order_response,
)


def test_trade_id_only_response_is_accepted_pending_reconciliation() -> None:
    row = normalize_order_response(
        {"success": True, "orderID": "order-1", "tradeIDs": ["trade-1"]}
    )
    assert row["accepted"] is True
    assert row["transaction_hashes"] == []
    assert row["finality_state"] == "TRADE_ID_ASSIGNED"
    assert row["requires_trade_reconciliation"] is True


def test_batch_preserves_per_order_failure() -> None:
    rows = normalize_batch_order_response(
        [
            {"success": True, "orderID": "order-1"},
            {"success": False, "error": "duplicate"},
        ]
    )
    assert [row["accepted"] for row in rows] == [True, False]


def test_submit_errors_never_request_blind_submit_retry() -> None:
    for status, message in (
        (400, "invalid tick"),
        (425, "restart"),
        (429, "rate limit"),
        (503, "post-only"),
        (None, "connection reset"),
    ):
        decision = classify_venue_error(status, {"error": message})
        assert decision.retry_submit is False
        assert decision.fail_closed is True
