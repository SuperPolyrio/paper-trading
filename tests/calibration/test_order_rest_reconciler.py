from quant.calibration.order_rest_reconciler import reconcile_order_lifecycle


def test_reconciliation_proves_order_was_maker_from_official_trade_relation() -> None:
    result = reconcile_order_lifecycle(
        order_id="maker-order",
        user_ws_events=[],
        rest_order={"id": "maker-order", "status": "MATCHED"},
        rest_trades=[
            {
                "id": "trade-1",
                "status": "CONFIRMED",
                "size": "5000000",
                "price": "0.4",
                "maker_orders": [
                    {
                        "order_id": "maker-order",
                        "matched_amount": "5",
                        "price": "0.4",
                    }
                ],
                "taker_order_id": "other-order",
            }
        ],
    )

    assert result["liquidity_role_truth"] == "MAKER"
    assert result["maker_role_observations"] == 1
    assert result["taker_role_observations"] == 0


def test_reconciliation_does_not_mislabel_taker_fill_as_maker() -> None:
    result = reconcile_order_lifecycle(
        order_id="taker-order",
        user_ws_events=[],
        rest_order={"id": "taker-order", "status": "MATCHED"},
        rest_trades=[
            {
                "id": "trade-1",
                "status": "CONFIRMED",
                "size": "5000000",
                "price": "0.4",
                "maker_orders": [{"order_id": "other-order"}],
                "taker_order_id": "taker-order",
            }
        ],
    )

    assert result["liquidity_role_truth"] == "TAKER"
    assert result["maker_role_observations"] == 0
    assert result["taker_role_observations"] == 1


def test_reconciliation_counts_one_maker_leg_across_trade_lifecycle() -> None:
    maker_order_id = "0xf372"
    trade = {
        "event_type": "TRADE",
        "id": "trade-1",
        "asset_id": "opposite-asset",
        "price": "0.161",
        "size": "10",
        "side": "BUY",
        "timestamp": "1787893458",
        "maker_orders": [
            {
                "asset_id": "target-asset",
                "matched_amount": "5",
                "order_id": maker_order_id,
                "price": "0.839",
                "side": "BUY",
            },
            {
                "asset_id": "target-asset",
                "matched_amount": "5",
                "order_id": "other-maker",
                "price": "0.839",
                "side": "BUY",
            },
        ],
    }
    ws_events = [
        {**trade, "status": "MATCHED"},
        {**trade, "status": "MATCHED"},
        {**trade, "status": "MINED"},
        {**trade, "status": "CONFIRMED"},
        {
            "event_type": "ORDER",
            "id": maker_order_id,
            "status": "MATCHED",
            "type": "UPDATE",
            "price": "0.839",
            "original_size": "5",
            "size_matched": "5",
            "timestamp": "1787893458",
        },
    ]

    result = reconcile_order_lifecycle(
        order_id=maker_order_id,
        user_ws_events=ws_events,
        rest_order={"id": maker_order_id, "status": "MATCHED"},
        rest_trades=[],
    )

    assert result["matched_trade_count"] == 1
    assert result["actual_matched_size"] == "5"
    assert result["actual_quote_amount"] == "4.195"
    assert result["actual_avg_price"] == "0.839"
    assert result["liquidity_role_truth"] == "MAKER"
    assert result["maker_role_observations"] == 1
    assert result["final_trade_status"] == "CONFIRMED"
    assert result["trade_observations"] == [
        {
            "trade_key": "trade:trade-1",
            "size": "5",
            "price": "0.839",
            "fee_rate_bps": 0,
            "status": "CONFIRMED",
            "source": "user_ws",
            "liquidity_role": "MAKER",
        }
    ]
