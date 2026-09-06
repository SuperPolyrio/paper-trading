from decimal import Decimal

from quant.paper.position_economics import reconstruct_position_economics


def test_reconstructs_buy_partial_sell_fee_components() -> None:
    report = reconstruct_position_economics(
        [
            {
                "event_type": "BUY",
                "shares_delta": "10",
                "fee": "0.1",
                "position_after": "10",
                "cost_basis_after": "4.1",
            },
            {
                "event_type": "SELL",
                "shares_delta": "-4",
                "fee": "0.02",
                "position_after": "6",
                "cost_basis_after": "2.46",
            },
        ],
        current_quantity=Decimal("6"),
        current_gross_basis=Decimal("2.46"),
    )

    assert report["status"] == "EXACT"
    assert report["entry_fees_usdc"] == Decimal("0.06")
    assert report["fee_exclusive_basis"] == Decimal("2.40")
    assert report["avg_price_excluding_fee"] == Decimal("0.40")


def test_new_buy_after_partial_sell_changes_fee_ratio_without_losing_history() -> None:
    report = reconstruct_position_economics(
        [
            {
                "event_type": "BUY",
                "shares_delta": "10",
                "fee": "0.1",
                "position_after": "10",
                "cost_basis_after": "4.1",
            },
            {
                "event_type": "SELL",
                "shares_delta": "-5",
                "fee": "0.02",
                "position_after": "5",
                "cost_basis_after": "2.05",
            },
            {
                "event_type": "BUY",
                "shares_delta": "2",
                "fee": "0.03",
                "position_after": "7",
                "cost_basis_after": "3.08",
            },
        ],
        current_quantity=Decimal("7"),
        current_gross_basis=Decimal("3.08"),
    )

    assert report["status"] == "EXACT"
    assert report["entry_fees_usdc"] == Decimal("0.08")
    assert report["fee_exclusive_basis"] == Decimal("3.00")


def test_legacy_seed_is_disclosed_instead_of_inventing_fee_split() -> None:
    report = reconstruct_position_economics(
        [
            {
                "event_type": "POSITION_SEED",
                "shares_delta": "5",
                "fee": "0",
                "position_after": "5",
                "cost_basis_after": "2",
            }
        ],
        current_quantity=Decimal("5"),
        current_gross_basis=Decimal("2"),
    )

    assert report["status"] == "INCOMPLETE"
    assert report["reason_codes"] == ["LEGACY_SEED_FEE_UNATTRIBUTED"]
    assert report["entry_fees_usdc"] == 0


def test_missing_ledger_is_not_reported_as_exact() -> None:
    report = reconstruct_position_economics(
        [],
        current_quantity=Decimal("2"),
        current_gross_basis=Decimal("1"),
    )

    assert report["status"] == "INCOMPLETE"
    assert "LEDGER_GROSS_BASIS_MISMATCH" in report["reason_codes"]
