from decimal import Decimal

from quant.calibration.signed_order_prediction import (
    normalize_prediction_to_signed_order,
    signed_execution_result_payload,
    signed_order_amounts,
)


def test_buy_signed_amounts_map_maker_to_quote_and_taker_to_shares():
    assert signed_order_amounts(
        {
            "side": "BUY",
            "maker_amount": "1040000",
            "taker_amount": "40000000",
        }
    ) == {"quote": Decimal("1.04"), "shares": Decimal("40")}


def test_prediction_is_trimmed_to_exact_signed_buy_size_and_fee():
    prediction = {
        "status": "FILLED",
        "requested_amount": "1.1",
        "amount_unit": "QUOTE",
        "fills": [
            {
                "level_index": 0,
                "price": "0.026",
                "size": "42.30769230769230769230769231",
                "fee": "0.07500",
            }
        ],
    }
    normalized = normalize_prediction_to_signed_order(
        prediction,
        {
            "side": "BUY",
            "amount": "1.1",
            "amount_unit": "QUOTE",
            "maker_amount": "1040000",
            "taker_amount": "40000000",
        },
        {"fee_rate": "0.07", "fee_exponent": "1"},
    )
    assert normalized["approved_requested_amount"] == "1.1"
    assert normalized["requested_amount"] == "1.04"
    assert normalized["filled_size"] == "40"
    assert normalized["filled_notional"] == "1.040"
    assert normalized["avg_fill_price"] == "0.026"
    assert normalized["total_fee"] == "0.07090"


def test_buy_price_improvement_can_return_more_than_signed_taker_amount():
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "FILLED",
            "requested_amount": "1.1",
            "amount_unit": "QUOTE",
            "fills": [
                {"level_index": 0, "price": "0.012", "size": "79.97", "fee": "0.04741"},
                {
                    "level_index": 1,
                    "price": "0.027",
                    "size": "5.198518518518518518518518519",
                    "fee": "0.00683",
                },
            ],
        },
        {
            "side": "BUY",
            "amount": "1.1",
            "amount_unit": "QUOTE",
            "maker_amount": "1080000",
            "taker_amount": "40000000",
        },
        {"fee_rate": "0.05", "fee_exponent": "1"},
    )
    assert normalized["requested_amount"] == "1.08"
    assert normalized["signed_worst_price"] == "0.027"
    assert normalized["filled_size"] == "84.42777777777777777777777778"
    assert normalized["filled_notional"] == "1.080000000000000000000000000"
    assert normalized["avg_fill_price"] == ("0.01279199842074093571099559123")
    # Fees are rounded per consumed price level, matching per-fill ledger charges.
    assert normalized["total_fee"] == "0.05325"


def test_sell_prediction_keeps_exact_share_amount():
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "FILLED",
            "requested_amount": "5",
            "amount_unit": "SHARES",
            "fills": [{"price": "0.02", "size": "5", "fee": "0"}],
        },
        {
            "side": "SELL",
            "amount": "5",
            "amount_unit": "SHARES",
            "maker_amount": "5000000",
            "taker_amount": "100000",
        },
    )
    assert normalized["requested_amount"] == "5"
    assert normalized["filled_size"] == "5"


def test_fak_buy_preserves_partial_fill_and_quote_remainder():
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "PARTIAL",
            "requested_amount": "1",
            "amount_unit": "QUOTE",
            "fills": [{"price": "0.2", "size": "3", "fee": "0"}],
        },
        {
            "side": "BUY",
            "order_type": "FAK",
            "amount": "1",
            "amount_unit": "QUOTE",
            "maker_amount": "1000000",
            "taker_amount": "4000000",
        },
    )
    assert normalized["status"] == "PARTIAL"
    assert normalized["filled_size"] == "3"
    assert normalized["filled_notional"] == "0.6"
    assert normalized["remaining_amount"] == "0.4"
    assert normalized["remaining_size"] == "0"


def test_fak_sell_preserves_partial_fill_and_share_remainder():
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "PARTIAL",
            "requested_amount": "5",
            "amount_unit": "SHARES",
            "fills": [{"price": "0.2", "size": "3", "fee": "0"}],
        },
        {
            "side": "SELL",
            "order_type": "FAK",
            "amount": "5",
            "amount_unit": "SHARES",
            "maker_amount": "5000000",
            "taker_amount": "1000000",
        },
    )
    assert normalized["status"] == "PARTIAL"
    assert normalized["filled_size"] == "3"
    assert normalized["remaining_size"] == "2"
    assert normalized["remaining_amount"] == "2"


def test_fok_insufficient_depth_remains_atomic_rejection():
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "REJECTED",
            "requested_amount": "5",
            "amount_unit": "SHARES",
            "fills": [],
        },
        {
            "side": "SELL",
            "order_type": "FOK",
            "amount": "5",
            "amount_unit": "SHARES",
            "maker_amount": "5000000",
            "taker_amount": "1000000",
        },
    )
    assert normalized["status"] == "REJECTED"
    assert normalized["fills"] == []
    assert normalized["filled_size"] == "0"
    assert normalized["remaining_size"] == "5"


def test_normalized_fee_components_remain_ledger_consistent():
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "FILLED",
            "requested_amount": "1.90",
            "amount_unit": "QUOTE",
            "fills": [
                {
                    "price": "0.37",
                    "size": "5.135135135135135135135135135",
                    "fee": "0.05985",
                    "platform_fee": "0.05985",
                    "builder_fee": "0",
                    "platform_fee_rate": "0.05",
                    "platform_fee_exponent": "1",
                    "builder_fee_rate_bps": 0,
                    "fee_charge_id": "fee-charge:raw",
                }
            ],
        },
        {
            "side": "BUY",
            "order_type": "FOK",
            "amount_unit": "QUOTE",
            "maker_amount": "1850000",
            "taker_amount": "5000000",
            "order_hash": "0xorder",
        },
        {"fee_rate": "0.05", "fee_exponent": "1"},
    )

    fill = normalized["fills"][0]
    assert normalized["filled_size"] == "5"
    assert normalized["filled_notional"] == "1.85"
    assert normalized["total_fee"] == "0.05827"
    assert Decimal(fill["fee"]) == (
        Decimal(fill["platform_fee"]) + Decimal(fill["builder_fee"])
    )


def test_signed_execution_payload_preserves_raw_audit_and_freezes_exact_amounts():
    staged = {
        "audit_key": "raw-audit",
        "status": "FILLED",
        "reason": "raw-book-walk",
        "intent": {
            "strategy_id": "stage-1",
            "market_id": "market-1",
            "condition_id": "condition-1",
            "asset_id": "asset-1",
            "side": "BUY",
            "order_type": "FOK",
            "limit_price": "0.37",
            "size": "1.90",
            "amount_unit": "QUOTE",
            "post_only": False,
            "decision_ts": "2026-09-03T00:00:00+00:00",
            "client_order_id": "paper-1",
        },
        "arrival_ts": "2026-09-03T00:00:01+00:00",
        "fills": [],
        "requested_amount": "1.90",
        "amount_unit": "QUOTE",
        "filled_size": "0",
        "remaining_size": "0",
        "filled_notional": "0",
        "remaining_amount": "1.90",
        "avg_fill_price": None,
        "total_fee": "0",
        "slippage": None,
        "source_manifest_ids": [],
        "source_files": [],
        "model_version": "model-1",
        "config_hash": "config-1",
        "fidelity": {},
    }
    audit = {
        "side": "BUY",
        "order_type": "FOK",
        "amount_unit": "QUOTE",
        "token_id": "asset-1",
        "maker_amount": "1850000",
        "taker_amount": "5000000",
        "worst_price": "0.37",
        "order_hash": "0xorder",
        "signed_order_fingerprint": "signed-fingerprint",
    }
    normalized = normalize_prediction_to_signed_order(
        {
            "status": "FILLED",
            "requested_amount": "1.90",
            "amount_unit": "QUOTE",
            "fills": [{"price": "0.37", "size": "5.135135135", "fee": "0"}],
        },
        audit,
    )

    frozen = signed_execution_result_payload(staged, normalized, audit)

    assert frozen["audit_key"] != "raw-audit"
    assert frozen["intent"]["size"] == "1.85"
    assert frozen["filled_size"] == "5"
    assert frozen["filled_notional"] == "1.85"
    assert frozen["fidelity"]["staged_paper_audit_key"] == "raw-audit"
    assert frozen["fidelity"]["signed_order_hash"] == "0xorder"
