from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.simulator.operations import (
    ConfirmedOperationLedger,
    NonceReservationBook,
    PositionOperationIntent,
    PositionOperationMachine,
    PositionOperationState,
    PositionOperationType,
    operation_reservation_requirements,
)

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _intent(event_id: str = "operation:one") -> PositionOperationIntent:
    return PositionOperationIntent(
        event_id=event_id,
        operation_type=PositionOperationType.SPLIT,
        account_id="account:one",
        strategy_id="strategy:one",
        condition_id="condition:one",
        amount=Decimal("1.12345678"),
        decision_ts=NOW,
        collateral_delta=Decimal("-1.12345678"),
        token_deltas={"yes": Decimal("1.12345678"), "no": Decimal("1.12345678")},
        token_decimals=6,
    )


def test_confirmed_operation_is_the_only_path_that_updates_balance_and_is_idempotent() -> None:
    ledger, nonces = ConfirmedOperationLedger(), NonceReservationBook()
    machine = PositionOperationMachine(_intent(), nonces=nonces, ledger=ledger)

    machine.allowance_checked(event_id="allowance", approved=True)
    machine.reserve_nonce(event_id="nonce", nonce=7)
    machine.submit(event_id="submit", transaction_hash="0xtx")
    machine.mined(event_id="mined")
    assert ledger.token_balance(account_id="account:one", asset_id="yes") == 0
    confirmed = machine.confirm(event_id="confirm")

    assert confirmed.state is PositionOperationState.CONFIRMED
    assert machine.confirm(event_id="confirm") == confirmed
    assert ledger.token_balance(account_id="account:one", asset_id="yes") == Decimal("1.123456")
    assert ledger.collateral_by_account["account:one"] == Decimal("-1.123456")


def test_nonce_contention_and_failed_operation_do_not_apply_token_changes() -> None:
    ledger, nonces = ConfirmedOperationLedger(), NonceReservationBook()
    first = PositionOperationMachine(_intent("operation:first"), nonces=nonces, ledger=ledger)
    second = PositionOperationMachine(_intent("operation:second"), nonces=nonces, ledger=ledger)
    first.allowance_checked(event_id="allowance:first", approved=True)
    first.reserve_nonce(event_id="nonce:first", nonce=9)
    second.allowance_checked(event_id="allowance:second", approved=True)
    with pytest.raises(ValueError, match="nonce already reserved"):
        second.reserve_nonce(event_id="nonce:second", nonce=9)
    first.fail(event_id="failed:first", reason="chain_revert")
    second.reserve_nonce(event_id="nonce:second-retry", nonce=9)

    assert ledger.token_balance(account_id="account:one", asset_id="yes") == 0


def test_split_reserves_collateral_and_merge_reserves_only_token_debits() -> None:
    split = operation_reservation_requirements(_intent())
    assert split.reserved_cash == Decimal("1.123456")
    assert split.reserved_tokens == {}

    merge_intent = PositionOperationIntent(
        event_id="operation:merge",
        operation_type=PositionOperationType.MERGE,
        account_id="account:one",
        strategy_id="strategy:one",
        condition_id="condition:one",
        amount=Decimal("2"),
        decision_ts=NOW,
        collateral_delta=Decimal("2"),
        token_deltas={"yes": Decimal("-2"), "no": Decimal("-2")},
    )
    merge = operation_reservation_requirements(merge_intent)
    assert merge.reserved_cash == 0
    assert merge.reserved_tokens == {"yes": Decimal("2"), "no": Decimal("2")}


def test_negative_risk_conversion_reserves_debits_not_credits() -> None:
    intent = PositionOperationIntent(
        event_id="operation:convert",
        operation_type=PositionOperationType.NEG_RISK_CONVERT,
        account_id="account:one",
        strategy_id="strategy:one",
        condition_id="condition:one",
        amount=Decimal("3"),
        decision_ts=NOW,
        collateral_delta=Decimal("0"),
        token_deltas={"no": Decimal("-3"), "alternate-yes": Decimal("3")},
    )

    reservation = operation_reservation_requirements(intent)

    assert reservation.reserved_cash == 0
    assert reservation.reserved_tokens == {"no": Decimal("3")}
