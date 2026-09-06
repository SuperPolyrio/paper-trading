from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.simulator.economics import (
    AccountCashflowEvent,
    AccountCashflowOperation,
    AccountCashflowState,
    AccountCashflowType,
    PostgresAccountCashflowStore,
    build_account_return_report,
)

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def _operation(operation_type: AccountCashflowType) -> AccountCashflowOperation:
    return AccountCashflowOperation(
        operation_id=f"operation:{operation_type.value}",
        account_id="account",
        strategy_id="strategy",
        operation_type=operation_type,
        amount=Decimal("10"),
        currency="USDC",
        state=AccountCashflowState.CONFIRMED,
        effective_ts=NOW,
        source="OFFICIAL",
        source_event_id=f"source:{operation_type.value}",
        idempotency_key=f"key:{operation_type.value}",
    )


def test_capital_flows_change_cash_but_not_account_return() -> None:
    deposit = _operation(AccountCashflowType.DEPOSIT)
    withdrawal = _operation(AccountCashflowType.WITHDRAWAL)
    commitment = _operation(AccountCashflowType.SPONSOR_COMMITMENT)
    refund = _operation(AccountCashflowType.SPONSOR_REFUND)

    assert deposit.cash_delta == Decimal("10")
    assert withdrawal.cash_delta == Decimal("-10")
    assert commitment.cash_delta == Decimal("-10")
    assert refund.cash_delta == Decimal("10")
    assert all(
        operation.return_delta == 0
        for operation in (deposit, withdrawal, commitment, refund)
    )


def test_bridge_fee_sponsor_distribution_and_dispute_outcome_are_returns() -> None:
    bridge_fee = _operation(AccountCashflowType.BRIDGE_FEE)
    sponsor_distribution = _operation(AccountCashflowType.SPONSOR_DISTRIBUTION)
    bounty = _operation(AccountCashflowType.DISPUTE_BOUNTY)
    bond_loss = _operation(AccountCashflowType.DISPUTE_BOND_LOSS)

    assert bridge_fee.cash_delta == Decimal("-10")
    assert bridge_fee.return_delta == Decimal("-10")
    assert sponsor_distribution.cash_delta == 0
    assert sponsor_distribution.return_delta == Decimal("-10")
    assert bounty.cash_delta == Decimal("10")
    assert bounty.return_delta == Decimal("10")
    assert bond_loss.cash_delta == 0
    assert bond_loss.return_delta == Decimal("-10")


def test_account_return_separates_capital_flows_from_external_economics() -> None:
    report = build_account_return_report(
        strategy_id="strategy",
        account_id="account",
        as_of=NOW,
        cashflows=(
            AccountCashflowEvent("deposit", "DEPOSIT", Decimal("50"), "CONFIRMED"),
            AccountCashflowEvent(
                "withdraw", "WITHDRAWAL", Decimal("-20"), "CONFIRMED"
            ),
            AccountCashflowEvent(
                "bridge-fee", "BRIDGE_FEE", Decimal("-1.5"), "CONFIRMED"
            ),
            AccountCashflowEvent(
                "bounty", "DISPUTE_BOUNTY", Decimal("3"), "CONFIRMED"
            ),
        ),
    )

    assert report.capital_flows == {
        "DEPOSIT": Decimal("50"),
        "WITHDRAWAL": Decimal("-20"),
    }
    assert report.confirmed_external_cashflow_return == Decimal("1.5")
    assert report.confirmed_account_return == Decimal("1.5")
    assert report.unmodeled_cashflows == ()


def test_cashflow_state_machine_is_forward_only_and_terminal_is_immutable() -> None:
    validate = PostgresAccountCashflowStore._validate_state_transition

    assert validate(AccountCashflowState.CREATED, AccountCashflowState.PROCESSING)
    assert validate(AccountCashflowState.PROCESSING, AccountCashflowState.CONFIRMED)
    assert not validate(AccountCashflowState.CONFIRMED, AccountCashflowState.PROCESSING)
    assert not validate(AccountCashflowState.CONFIRMED, AccountCashflowState.CONFIRMED)
    with pytest.raises(ValueError, match="backwards"):
        validate(AccountCashflowState.PROCESSING, AccountCashflowState.CREATED)
    with pytest.raises(ValueError, match="terminal state"):
        validate(AccountCashflowState.CONFIRMED, AccountCashflowState.FAILED)
