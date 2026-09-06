from quant.calibration.settlement_e2e_report import build_settlement_e2e_report


def test_verified_historical_redeem_renders_as_confirmed() -> None:
    report = build_settlement_e2e_report(
        {
            "settlement_key": "settlement-1",
            "account_id": "0xaccount",
            "asset_id": "asset-yes",
            "winning_asset_id": "asset-yes",
            "expected_real_payout": "5",
            "observed_real_payout": "5",
            "real_realized_pnl_delta": "2.4",
            "paper_realized_pnl_delta": "2.4",
            "cash_reconciliation_status": "PASS",
            "payload": {
                "paper_payout": "5",
                "redemption_evidence": {
                    "transaction_hash": "0xabc",
                    "receipt_status": 1,
                    "post_check_token_balance": "0",
                    "size": "5",
                },
            },
        }
    )

    assert report["status"] == "LIVE_REDEEM_CONFIRMED"
    assert report["submit_called"] is False
    assert all(report["checks"].values())


def test_pnl_formatting_does_not_turn_equal_decimal_values_into_a_failure() -> None:
    report = build_settlement_e2e_report(
        {
            "expected_real_payout": "5",
            "observed_real_payout": "5",
            "real_realized_pnl_delta": "2.40000",
            "paper_realized_pnl_delta": "2.4",
            "cash_reconciliation_status": "PASS",
            "payload": {
                "paper_payout": "5",
                "redemption_evidence": {
                    "transaction_hash": "0xabc",
                    "receipt_status": 1,
                    "post_check_token_balance": "0",
                },
            },
        }
    )

    assert report["status"] == "LIVE_REDEEM_CONFIRMED"
    assert report["checks"]["realized_pnl_matches_paper"] is True


def test_missing_receipt_never_claims_confirmed_redeem() -> None:
    report = build_settlement_e2e_report(
        {
            "expected_real_payout": "5",
            "observed_real_payout": "5",
            "cash_reconciliation_status": "PASS",
            "payload": {"paper_payout": "5", "redemption_evidence": {}},
        }
    )

    assert report["status"] == "EVIDENCE_INCOMPLETE"
    assert report["checks"]["polygon_receipt_success"] is False
