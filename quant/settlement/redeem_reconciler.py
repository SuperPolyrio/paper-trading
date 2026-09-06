"""Compare expected payout with token/collateral balances around redeem."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def reconcile_redeem(
    *,
    quantity: Decimal,
    payout_per_share: Decimal,
    collateral_before: Decimal,
    collateral_after: Decimal,
    token_before: Decimal,
    token_after: Decimal,
    fee_or_gas: Decimal = Decimal(0),
    tolerance: Decimal = Decimal("0.00001"),
) -> dict[str, Any]:
    expected = quantity * payout_per_share - fee_or_gas
    observed = collateral_after - collateral_before
    token_consumed = token_before - token_after
    difference = observed - expected
    passed = (
        abs(difference) <= tolerance and abs(token_consumed - quantity) <= tolerance
    )
    return {
        "schema_version": "redeem_reconciliation_v1",
        "status": "PASS" if passed else "FAIL",
        "expected_collateral_delta": format(expected, "f"),
        "observed_collateral_delta": format(observed, "f"),
        "collateral_difference": format(difference, "f"),
        "expected_token_consumed": format(quantity, "f"),
        "observed_token_consumed": format(token_consumed, "f"),
        "payout_per_share": format(payout_per_share, "f"),
        "fee_or_gas": format(fee_or_gas, "f"),
        "tolerance": format(tolerance, "f"),
    }
