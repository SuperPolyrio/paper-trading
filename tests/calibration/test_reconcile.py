from quant.calibration.calibration_domain import LIVE_REQUIRED_ARTIFACTS, artifact_bitmap
from quant.calibration.live_probe_runner import (
    _is_expected_fok_depth_rejection,
    _prediction_matches_expected,
)
from quant.calibration.real_live_adapter import OrderSubmissionRejected
from quant.calibration.reconcile import reconcile_probe


def test_fak_immediate_fill_accepts_trade_ws_plus_rest_order(monkeypatch) -> None:
    monkeypatch.setattr(
        "quant.calibration.reconcile.reconcile_order_lifecycle",
        lambda **kwargs: {
            "errors": [],
            "execution_truth": "MATCHED",
            "actual_matched_size": "5",
            "actual_quote_amount": "0.125",
            "actual_avg_price": "0.025",
            "actual_fee": "0",
            "trade_observations": [],
            "rest_order_reconciled": True,
            "rest_trade_reconciled": True,
            "final_trade_status_present": True,
        },
    )
    probe = {
        "probe_id": "probe-fak",
        "order_type": "FAK",
        "side": "SELL",
        "amount": "5",
        "amount_unit": "SHARES",
        "asset_id": "asset-1",
        "prediction": {
            "status": "FILLED",
            "filled_size": "5",
            "avg_fill_price": "0.025",
            "total_fee": "0",
        },
        "market_snapshot": {"tick_size": "0.001"},
        "signed_order_audit": {
            "maker": "0xmaker",
            "signer": "0xsigner",
            "order_hash": "order-1",
            "maker_amount": "5000000",
        },
        "lifecycle": {
            "order_id": "order-1",
            "rest_order": {"id": "order-1"},
            "rest_trades": [{"id": "trade-1"}],
        },
        "artifact_bitmap": artifact_bitmap(
            LIVE_REQUIRED_ARTIFACTS - {"USER_ORDER_EVENT_PRESENT"},
            live=True,
        ),
        "errors": ["missing_artifact:USER_ORDER_EVENT_PRESENT"],
    }
    events = [
        {
            "source": "polymarket-user-ws",
            "event_type": "TRADE",
            "payload": {"id": "trade-1", "taker_order_id": "order-1"},
        }
    ]

    reconciled = reconcile_probe(probe, events)

    assert reconciled["artifact_bitmap"]["complete"] is True
    assert (
        reconciled["reconciliation"]["user_order_evidence_source"]
        == "user_ws_trade_plus_rest_order"
    )
    assert "missing_artifact:USER_ORDER_EVENT_PRESENT" not in reconciled["errors"]


def test_expected_fok_depth_rejection_requires_exact_venue_reason() -> None:
    base = {
        "order_type": "FOK",
        "prediction": {"status": "REJECTED"},
    }
    expected = OrderSubmissionRejected(
        "HTTP 400",
        response={
            "error": (
                "order couldn't be fully filled. "
                "FOK orders are fully filled or killed."
            )
        },
        status_code=400,
    )
    unrelated = OrderSubmissionRejected(
        "HTTP 400",
        response={"error": "not enough balance / allowance"},
        status_code=400,
    )

    assert _is_expected_fok_depth_rejection(
        base,
        expected,
        expected_outcome="REJECT",
    )
    assert not _is_expected_fok_depth_rejection(
        base,
        unrelated,
        expected_outcome="REJECT",
    )
    assert _prediction_matches_expected(
        {"status": "REJECTED"},
        "REJECT",
    )


def test_expected_rejection_bitmap_does_not_require_fill_artifacts() -> None:
    bitmap = artifact_bitmap(
        {
            "INTENT_PRESENT",
            "PREDICTION_PRESENT",
            "DECISION_BOOK_PRESENT",
            "ARRIVAL_BOOK_PRESENT",
            "MODEL_MANIFEST_PRESENT",
            "RISK_CONFIG_PRESENT",
            "MARKET_METADATA_PRESENT",
            "SIGNED_ORDER_PRESENT",
            "ORDER_HASH_PRESENT",
            "HTTP_REQUEST_PRESENT",
            "HTTP_RESPONSE_PRESENT",
            "ACCOUNTING_RECONCILED",
        },
        live=True,
        expected_rejection=True,
    )

    assert bitmap["complete"] is True
    assert "USER_TRADE_EVENT_PRESENT" not in bitmap["required"]
